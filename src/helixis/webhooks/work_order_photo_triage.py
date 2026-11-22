"""Work-order photo triage (#533 entry point 2).

A work order in Buildium is a Task, and a tenant's / staffer's photos ride on
the task's HISTORY entries (grounded in the LeadSimple/buildium reference:
``TaskHistoryMessage.FileIds`` + ``GET /tasks/{id}/history`` +
``POST /tasks/{taskId}/history/{historyId}/files/{fileId}/downloadrequest`` →
a 5-min presigned ``DownloadUrl``). This module walks that chain when a
work-order webhook lands, runs each photo through the vision assessor
(:func:`helixis.agents.image_tools.assess_image_impl`), and records a DRAFT
work order for the PM — the same "assess → draft, never file" flow as the SMS
entry point (#533 entry point 1), on the Buildium side.

Runs in the webhook worker (which has the LLM but not memory — assess only
needs the model). Cost-bounded: gated to work-order CREATION and capped at
:data:`_MAX_PHOTOS` images per work order. Best-effort throughout — a fetch
failure, an unassessable image, or a missing key degrades to "nothing added"
and never breaks webhook processing.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import asyncpg

from helixis.agents.image_tools import ImageAssessment, assess_image_impl
from helixis.buildium.api import BuildiumApi
from helixis.buildium.schemas import WorkOrder
from helixis.db.repositories.activity import ActivityEntry, write_activity
from helixis.settings import Settings
from helixis.webhooks.sms_photo_triage import draft_work_order_summary

logger = logging.getLogger(__name__)

_IMAGE_MIME_PREFIX = "image/"
_MAX_MEDIA_BYTES = 8 * 1024 * 1024
_FETCH_TIMEOUT_S = 15.0
# Cap vision calls per work order — bounds cost + latency; a WO rarely carries
# more than a couple of photos worth assessing. (Per-company cost-guard via the
# token ledger is the follow-up the #533 issue calls for.)
_MAX_PHOTOS = 3


def _history_file_refs(history: list[dict[str, Any]], *, limit: int) -> list[tuple[int, int]]:
    """(history_id, file_id) pairs from a task's history, newest-first, capped
    at ``limit``. Reads the documented ``Id`` + ``FileIds`` keys — no guessing
    (see the reference grounding in the module docstring)."""
    refs: list[tuple[int, int]] = []
    for entry in history or []:
        hid = entry.get("Id")
        if hid is None:
            continue
        for fid in entry.get("FileIds") or []:
            refs.append((int(hid), int(fid)))
            if len(refs) >= limit:
                return refs
    return refs


async def _fetch_image(url: str) -> tuple[bytes, str] | None:
    """Download a presigned Buildium file URL (no auth — the URL carries its own
    token). Returns ``(bytes, mime)`` only for an image within the size cap;
    ``None`` otherwise. Never raises."""
    if not url:
        return None
    try:
        import httpx

        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception:  # noqa: BLE001 — best-effort; degrade, never propagate
        logger.warning("work-order photo fetch failed", exc_info=True)
        return None
    data = resp.content
    if len(data) > _MAX_MEDIA_BYTES:
        logger.warning("work-order photo too large (%d bytes) — skipping", len(data))
        return None
    mime = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not mime.startswith(_IMAGE_MIME_PREFIX):
        return None
    return data, mime


async def assess_work_order_photos(
    wo: WorkOrder,
    api: BuildiumApi,
    *,
    settings: Settings,
    max_photos: int = _MAX_PHOTOS,
) -> list[ImageAssessment]:
    """Assess up to ``max_photos`` images attached to a work order's task
    history. Returns the assessments (may be empty). Never raises — every
    Buildium call / fetch / assess is best-effort."""
    task_id = wo.task.id if wo.task else None
    if task_id is None:
        return []
    try:
        history = await api.tasks.list_history(task_id)
    except Exception:  # noqa: BLE001 — no history / API hiccup → nothing to assess
        logger.warning("work-order %s: list_history failed", wo.id, exc_info=True)
        return []

    out: list[ImageAssessment] = []
    for history_id, file_id in _history_file_refs(history, limit=max_photos):
        try:
            dl = await api.tasks.download_history_file_url(task_id, history_id, file_id)
            fetched = await _fetch_image(str(dl.get("DownloadUrl") or ""))
            if fetched is None:
                continue
            data, mime = fetched
            assessment = await assess_image_impl(
                base64_content=base64.b64encode(data).decode("ascii"),
                mime_type=mime,
                hint="This photo is attached to a maintenance work order — assess the issue.",
                settings=settings,
            )
            out.append(assessment)
        except Exception:  # noqa: BLE001 — one bad file must not sink the rest
            logger.warning("work-order %s: photo file %s failed", wo.id, file_id, exc_info=True)
            continue
    return out


async def triage_work_order_photos(
    wo: WorkOrder,
    api: BuildiumApi,
    pool: asyncpg.Pool,
    *,
    settings: Settings,
    company_id: str,
    account_id: int | None,
) -> int:
    """Assess a new work order's attached photos and record a DRAFT work order
    per actionable maintenance image. Returns the number of drafts recorded.
    Never raises — a maintenance draft is a bonus on top of the WO sync, never
    allowed to break it."""
    try:
        assessments = await assess_work_order_photos(wo, api, settings=settings)
    except Exception:  # noqa: BLE001 — defensive; assess_* is already best-effort
        logger.warning("work-order %s photo triage failed", wo.id, exc_info=True)
        return 0

    recorded = 0
    for a in assessments:
        summary = draft_work_order_summary(a, source="a work-order photo")
        if summary is None:
            continue
        entry = ActivityEntry(
            account_id=account_id,
            company_id=company_id,
            source="webhook",
            source_label="Buildium work-order photo",
            action_type="maintenance_photo_drafted",
            description=summary[:500],
            entity_type="work_order",
            entity_id=wo.id,
            raw={
                "work_order_id": wo.id,
                "image_kind": a.image_kind,
                "likely_trade": a.likely_trade,
                "severity": a.severity,
                "draft_work_order": a.draft_work_order.model_dump() if a.draft_work_order else None,
                "warnings": list(a.warnings),
            },
        )
        try:
            await write_activity(pool, entry)
            recorded += 1
        except Exception:  # noqa: BLE001 — best-effort; the assessment still happened
            logger.warning("work-order %s: failed to record photo draft", wo.id, exc_info=True)
    return recorded


__all__ = [
    "assess_work_order_photos",
    "triage_work_order_photos",
]
