"""Google Drive → static index (#67 PR-2).

When a user connects Google Drive we do NOT want the agent hitting the Drive API
on every question. Instead the daily company-knowledge cycle enumerates the
connected Drive ONCE via Composio and caches a lightweight index (title + folder
+ mime + modified) in ``drive_index``. The knowledge source then reads that
static index — zero API calls at query time. Fetching a specific file's
*contents* still goes live (the Drive specialist agent, ``agents/drive_agent.py``).

Refresh is full-refresh + prune per (company, user): each cycle re-enumerates and
``replace_for_user`` swaps the whole listing, so a file deleted from Drive leaves
the index (gotcha 15). Everything is best-effort (ADR 0006 §4 / gotcha 7) — a
missing key, an unavailable Composio client, or an SDK drift degrades to "no
Drive facts this cycle", never a raised turn.

⚠ Live seam (same class as the Gmail slugs in gotcha 12 and ``documents.py``'s
extractor): the exact Composio Drive list slug + response shape can't be dry-run
without a live connection. ``_DRIVE_LIST_SLUG`` + :func:`parse_drive_listing`
are written defensively over the shapes Composio is known to return and MUST be
confirmed on the first dogfood (does ``execute_tool`` return files?). The parser
is pure + unit-tested; only the one ``execute_tool`` call is the unverified seam.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any

import asyncpg

from helixis.db.repositories import drive_index as repo
from helixis.db.repositories.drive_index import DriveFile
from helixis.settings import Settings

logger = logging.getLogger(__name__)

# Composio toolkit slug for Google Drive (lowercase, as list_connections reports).
_DRIVE_TOOLKIT = "googledrive"
# The manual-execution slug for "list my Drive files". The exact slug is the
# dogfood seam noted in the module docstring; kept a constant so a bump is a
# one-line change.
_DRIVE_LIST_SLUG = "GOOGLEDRIVE_LIST_FILES"


def _parse_modified(value: Any) -> datetime.datetime | None:
    """Parse a Drive ``modifiedTime`` (RFC-3339, e.g. ``2026-07-01T12:00:00Z``)
    to an aware datetime. Returns ``None`` on anything unparseable — a missing
    modified time must never drop the file from the index."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        return None


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    """First present, non-empty value among ``keys`` (tolerates the casing/naming
    drift between Composio's normalized output and raw Google fields)."""
    for key in keys:
        val = mapping.get(key)
        if val not in (None, "", [], {}):
            return val
    return None


def _unwrap_files(raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Dig the file list out of a Composio ``execute_tool`` result.

    Composio wraps tool output under ``data`` (sometimes ``response_data``), and
    the Drive list itself surfaces as ``files`` / ``items`` / ``results``. We
    probe the known shapes and return the first list of dicts we find, or ``[]``.
    """
    if not isinstance(raw, dict):
        return []
    # Unwrap one or two envelope layers.
    candidates: list[Any] = [raw]
    for envelope in ("data", "response_data", "result"):
        inner = raw.get(envelope)
        if isinstance(inner, dict):
            candidates.append(inner)
    for scope in candidates:
        if not isinstance(scope, dict):
            continue
        for key in ("files", "items", "results", "file_list"):
            val = scope.get(key)
            if isinstance(val, list):
                return [f for f in val if isinstance(f, dict)]
    return []


def parse_drive_listing(raw: dict[str, Any] | None, *, limit: int = 200) -> list[DriveFile]:
    """Normalize a Composio Drive-list result into ``DriveFile`` rows (pure).

    Defensive over field names: id from ``id``/``fileId``/``file_id``; title from
    ``name``/``title``; mime from ``mimeType``/``mime_type``; folder from
    ``path``/``parent``/``folder``; modified from ``modifiedTime`` variants.
    Files with no id or no title are skipped. Capped at ``limit``. Deterministic.
    """
    files: list[DriveFile] = []
    for item in _unwrap_files(raw):
        file_id = _first(item, "id", "fileId", "file_id")
        title = _first(item, "name", "title", "fileName")
        if not file_id or not title:
            continue
        mime = _first(item, "mimeType", "mime_type", "mimetype")
        path = _first(item, "path", "parent", "parentName", "folder")
        modified = _parse_modified(_first(item, "modifiedTime", "modified_time", "modifiedDate"))
        files.append(
            DriveFile(
                file_id=str(file_id),
                title=str(title),
                mime_type=str(mime) if mime is not None else None,
                path=str(path) if path is not None else None,
                modified_at=modified,
            )
        )
        if len(files) >= limit:
            break
    return files


async def enumerate_drive_files(
    composio: Any, *, composio_user_id: str, limit: int = 200
) -> list[DriveFile] | None:
    """List one user's Drive files via Composio, normalized.

    Returns ``None`` when the API call itself failed (so the caller does NOT
    prune a good prior index), and ``[]`` for a genuinely empty Drive. The
    blocking Composio SDK call is offloaded to a thread so it never stalls the
    async cycle's event loop.
    """
    if composio is None or not getattr(composio, "available", False):
        return None
    try:
        result = await asyncio.to_thread(
            composio.execute_tool,
            user_id=composio_user_id,
            slug=_DRIVE_LIST_SLUG,
            arguments={"page_size": limit},
        )
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — never load-bearing
        logger.warning("drive enumerate failed (user=%s): %s", composio_user_id, exc)
        return None
    if result is None:
        return None
    return parse_drive_listing(result, limit=limit)


async def _ensure_composio(settings: Settings, composio: Any | None) -> Any | None:
    """Return a usable Composio client — the injected one, or a fresh one built
    from ``settings``. ``None`` when it can't be made available (best-effort)."""
    if composio is None:
        try:
            from helixis.connectors.composio_client import ComposioClient

            composio = ComposioClient(settings)
            await composio.initialize()
        except (Exception, SystemExit) as exc:  # noqa: BLE001
            logger.warning("drive index: composio client unavailable: %s", exc)
            return None
    return composio if getattr(composio, "available", False) else None


async def _index_one_user(
    pool: asyncpg.Pool, composio: Any, *, company_id: str, composio_user_id: str, limit: int
) -> int:
    """Index one Drive-connected user: check the grant, enumerate, replace.

    Returns files written — 0 when the user isn't connected or the enumeration
    failed (in which case the existing index is left intact, never pruned)."""
    try:
        connected = await asyncio.to_thread(composio.is_connected, composio_user_id, _DRIVE_TOOLKIT)
    except (Exception, SystemExit):  # noqa: BLE001
        logger.debug(
            "drive index: connection check failed for user=%s", composio_user_id, exc_info=True
        )
        return 0
    if not connected:
        return 0
    files = await enumerate_drive_files(composio, composio_user_id=composio_user_id, limit=limit)
    if files is None:
        # Enumeration failed — leave the existing index untouched (no prune).
        return 0
    return await repo.replace_for_user(
        pool, company_id=company_id, composio_user_id=composio_user_id, files=files
    )


async def refresh_drive_index_for_user(
    pool: asyncpg.Pool,
    settings: Settings,
    *,
    company_id: str,
    composio_user_id: str,
    composio: Any | None = None,
) -> int:
    """Index ONE user's Drive now — the instant-on-connect path.

    Called fire-and-forget when ``GET /connectors`` first observes an active
    Google Drive grant, so the profile learns the Drive within seconds of the
    OAuth popup closing instead of waiting for the next daily cycle. Never
    raises. Returns files written (0 when disabled / not connected / no client).
    """
    if not settings.company_knowledge.drive_source:
        return 0
    composio = await _ensure_composio(settings, composio)
    if composio is None:
        return 0
    return await _index_one_user(
        pool,
        composio,
        company_id=company_id,
        composio_user_id=composio_user_id,
        limit=settings.company_knowledge.drive_max_files,
    )


async def refresh_drive_index(
    pool: asyncpg.Pool,
    settings: Settings,
    *,
    company_id: str,
    composio: Any | None = None,
) -> int:
    """(Re)build the Drive index for every Drive-connected user of one company.

    For each user in ``company_users`` with an ACTIVE Google Drive connection,
    enumerate their Drive and ``replace_for_user``. Returns the total files
    indexed this cycle. Never raises — a source that can't run contributes
    nothing (the whole feature is an enhancement, gotcha 7).

    ``composio`` may be injected (tests / a caller that already holds a client);
    when omitted, a client is built from ``settings`` on demand.
    """
    if not settings.company_knowledge.drive_source:
        return 0
    composio = await _ensure_composio(settings, composio)
    if composio is None:
        return 0

    try:
        rows = await pool.fetch(
            "SELECT user_id::text AS user_id FROM public.company_users WHERE company_id = $1::uuid",
            company_id,
        )
    except Exception as exc:  # noqa: BLE001 — table may not exist / transient
        logger.warning(
            "drive index: could not list company users (company=%s): %s", company_id, exc
        )
        return 0

    limit = settings.company_knowledge.drive_max_files
    total = 0
    for row in rows:
        total += await _index_one_user(
            pool,
            composio,
            company_id=company_id,
            composio_user_id=row["user_id"],
            limit=limit,
        )
    if total:
        logger.info("drive index: refreshed %d file(s) for company=%s", total, company_id)
    return total


__all__ = [
    "DriveFile",
    "parse_drive_listing",
    "enumerate_drive_files",
    "refresh_drive_index",
    "refresh_drive_index_for_user",
]
