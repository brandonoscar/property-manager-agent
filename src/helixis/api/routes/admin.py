"""Admin / diagnostic routes.

TEMPORARY — PR #77 diagnostic surface + PR #82 backfill endpoint.
These routes exist to let Brandon see / repair things the receiver
should have done automatically. Tagged for removal once we don't
need them.

Auth: every route here requires ``get_current_user`` so an
unauthenticated caller cannot probe a connected Buildium account
through the diagnostic.

ID redaction is intentionally relaxed in this file — Brandon
needs to see the raw Buildium response to diagnose; the HARD
PRESENTATION RULES that gate chat output do not gate the
admin/diag JSON surface he reads himself.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.buildium.api import BuildiumApi
from helixis.buildium.client import BuildiumClient
from helixis.buildium.credentials import load_buildium_settings_for_company
from helixis.buildium.errors import BuildiumApiError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


_NOT_CONNECTED = "Buildium is not connected for this company."


async def _open_for_company(
    user: AuthenticatedUser,
) -> tuple[BuildiumClient | None, BuildiumApi | None, str | None]:
    if not user.company_ids:
        return None, None, "No company associated with this account."
    settings = await load_buildium_settings_for_company(user.company_ids[0])
    if settings is None:
        return None, None, _NOT_CONNECTED
    client = BuildiumClient(settings)
    return client, BuildiumApi(client), None


def _row_count_if_list(value: Any) -> int | None:
    if isinstance(value, list):
        return len(value)
    return None


def _truncate(value: Any, max_chars: int = 4000) -> Any:
    """Cap a JSON-serializable payload so the diagnostic response stays
    under reasonable size. We dump → truncate → re-parse so the
    structure on the way out is still JSON, but values are clamped."""
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        return str(value)[:max_chars]
    if len(text) <= max_chars:
        return value
    return text[:max_chars] + "... [truncated]"


@router.get("/diag/outstanding_balances")
async def diag_outstanding_balances(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """TEMPORARY — PR #77 diagnostic; remove after debugging.

    Hits ``/leases/outstandingbalances`` with three call shapes and
    samples per-lease transactions for three active leases. Returns
    the raw row count + a truncated body preview for each shape so
    Brandon can see whether the endpoint is empty-by-design, needs
    a different param, or is returning data we're parsing wrong.
    """
    client, api, err = await _open_for_company(current_user)
    if client is None or api is None:
        return {"error": err or _NOT_CONNECTED}

    shapes: list[dict[str, Any]] = [
        {"name": "active_only", "params": {"leasestatuses": ["Active"]}},
        {
            "name": "active_total_balance",
            "params": {
                "leasestatuses": ["Active"],
                "balanceduration": "TotalBalance",
            },
        },
        {"name": "bare", "params": {}},
    ]

    results: list[dict[str, Any]] = []
    async with client:
        for shape in shapes:
            entry: dict[str, Any] = {
                "shape": shape["name"],
                "params": shape["params"],
            }
            try:
                # ``request`` without a ``schema=`` returns the raw
                # parsed JSON; we can inspect length + a body preview
                # without forcing the response through Pydantic. The
                # underlying client also raises ``BuildiumApiError``
                # on 4xx/5xx so we capture the status when that fires.
                raw = await client.request(
                    "GET",
                    "/leases/outstandingbalances",
                    query=shape["params"] or None,
                )
                entry["status"] = 200
                entry["row_count_if_list"] = _row_count_if_list(raw)
                entry["body_preview"] = _truncate(raw)
            except BuildiumApiError as exc:
                entry["status"] = getattr(exc, "status", None)
                entry["error"] = str(exc)[:4000]
            except Exception as exc:  # noqa: BLE001 — diagnostic only
                entry["error"] = repr(exc)[:4000]
            results.append(entry)

        # Per-lease transactions sample — useful when the bulk
        # endpoint is empty but per-lease ledger access still works
        # (the "compute delinquency from transactions" fallback path).
        sample_entry: dict[str, Any] = {"shape": "per_lease_transactions_sample"}
        try:
            active = await api.leases.list(leasestatuses=["Active"])
            sample: list[dict[str, Any]] = []
            for lease in active[:3]:
                lease_entry: dict[str, Any] = {"lease_id": lease.id}
                try:
                    txns = await api.leases.list_transactions(lease.id)
                    lease_entry["tx_count"] = len(txns)
                except BuildiumApiError as exc:
                    lease_entry["error"] = str(exc)[:1000]
                except Exception as exc:  # noqa: BLE001
                    lease_entry["error"] = repr(exc)[:1000]
                sample.append(lease_entry)
            sample_entry["active_lease_count"] = len(active)
            sample_entry["sample"] = sample
        except BuildiumApiError as exc:
            sample_entry["error"] = str(exc)[:4000]
        except Exception as exc:  # noqa: BLE001
            sample_entry["error"] = repr(exc)[:4000]
        results.append(sample_entry)

    return {"results": results}


# ─────────────────────────────────────────────────────────────────────
# PR #82 — one-shot historical backfill of ``action_surface``
#
# Brandon's live audit on the ``team`` company turned up 2 recent
# ``Task.Created`` rows with ``action_surface = NULL`` (the LLM
# planner has been silently 0/2 since 2026-05-16). PR #82's
# deterministic surface fixes new events going forward; this endpoint
# repairs the historical rows so Inbox shows them too.
#
# Parked for removal once the backfill is done — same lifecycle as
# the outstanding-balances diag above.
# ─────────────────────────────────────────────────────────────────────


from pydantic import AwareDatetime, BaseModel  # noqa: E402

from helixis.db.repositories.buildium_staff import load_staff_cache  # noqa: E402
from helixis.webhooks.handlers.enrichment_registry import (  # noqa: E402
    get_enrichment_handler,
)
from helixis.webhooks.queue import (  # noqa: E402
    mark_enrichment_failed,
    store_routing_decision,
)
from helixis.webhooks.routing import route_event  # noqa: E402
from helixis.webhooks.surface_builder import (  # noqa: E402
    build_action_surface_from_routing,
)


class _BackfillRequest(BaseModel):
    """Optional filters for the backfill — defaults patch the
    caller's company since the requester's ``company_users`` row
    is the only company they can reach via the auth gate anyway."""

    company_id: str | None = None
    #: ⚠ TYPED, and AWARE — this is bound to ``$2::timestamptz`` in
    #: ``_fetch_null_surface_rows``. A ``str`` there is a DataError from
    #: asyncpg (the live defect in ``activity.py``, Sentry PYTHON-FASTAPI-3G),
    #: and a naive value would be read in the container's local zone by
    #: asyncpg's encoder, silently shifting the backfill window by hours.
    since: AwareDatetime | None = None


class _BackfillResponse(BaseModel):
    company_id: str
    patched: int
    skipped: int
    errors: int


@router.post("/diag/backfill_action_surface", response_model=_BackfillResponse)
async def backfill_action_surface(
    body: _BackfillRequest | None = None,
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> _BackfillResponse:
    """Re-run ``route_event`` + ``build_action_surface_from_routing``
    on historical ``buildium_events`` rows that have
    ``action_surface IS NULL``.

    Scoped to the caller's company by default (or the explicit
    ``company_id`` in the body, which must match a company the
    requester belongs to). ``since`` is an optional ISO timestamp
    that narrows to events newer than the bound.

    PR #82 — parked for removal once Brandon confirms the live
    rows are patched. Same lifecycle as ``/diag/outstanding_balances``.
    """
    if not current_user.company_ids:
        # Mirrors the auth pattern from /properties — every other
        # route depends on a company_users membership.
        from fastapi import HTTPException
        from fastapi import status as http_status

        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="No company associated with this account.",
        )

    company_id = (body.company_id if body else None) or current_user.company_ids[0]
    if company_id not in current_user.company_ids:
        from fastapi import HTTPException
        from fastapi import status as http_status

        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="Cannot backfill events for a company you don't belong to.",
        )

    since = body.since if body else None

    pool = _get_pool_for_admin()
    if pool is None:
        return _BackfillResponse(company_id=company_id, patched=0, skipped=0, errors=0)

    rows = await _fetch_null_surface_rows(pool, company_id=company_id, since=since)
    staff_cache = await load_staff_cache(pool, company_id)

    patched = 0
    skipped = 0
    errors = 0
    for row in rows:
        if row.get("action_surface") is not None:
            skipped += 1
            continue
        envelope = row.get("payload") or {}
        if isinstance(envelope, str):
            try:
                envelope = json.loads(envelope)
            except json.JSONDecodeError:
                errors += 1
                continue
        if not isinstance(envelope, dict):
            errors += 1
            continue
        event_name = row.get("event_name") or envelope.get("EventName") or ""

        # PR #83 — run enrichment first so the deterministic floor
        # can render rich summaries. The brief's specific bug was
        # that PR #82's backfill skipped this step, leaving
        # ``enrichment IS NULL`` AND the floor reading only
        # envelope-shaped fields (which real Task.Created payloads
        # don't carry — TaskId + EventName only). Enrichment is
        # best-effort: a failure on this row records last_error +
        # falls through with no EnrichedEvent so we still get the
        # bare floor (PR #82 behavior).
        enriched_event: Any = None
        enrichment_dict: dict[str, Any] | None = None
        had_enrichment_failure = False
        enrichment_handler = get_enrichment_handler(event_name)
        if enrichment_handler is not None:
            try:
                enriched_event = await enrichment_handler.enricher.enrich(envelope, company_id)
                enrichment_dict = enriched_event.model_dump(mode="json")
            except Exception as exc:  # noqa: BLE001 — log + continue
                had_enrichment_failure = True
                logger.warning(
                    "PR #83 backfill enrichment failed for event id=%s: %s",
                    row.get("id"),
                    exc,
                )
                try:
                    await mark_enrichment_failed(pool, row["id"], error=repr(exc))
                except Exception:  # noqa: BLE001 — best-effort
                    logger.exception(
                        "Could not persist enrichment failure on backfill row %s",
                        row.get("id"),
                    )

        try:
            decision = await route_event(
                pool,
                event_name=event_name,
                envelope=envelope,
                company_id=company_id,
            )
            surface = build_action_surface_from_routing(
                envelope=envelope,
                event_name=event_name,
                decision=decision,
                staff_cache=staff_cache,
                enriched=enriched_event,
            )
            await store_routing_decision(
                pool,
                row["id"],
                decision={
                    "action_type": decision.action_type,
                    "assigned_buildium_user_id": decision.assigned_buildium_user_id,
                    "assigned_helixis_user_id": decision.assigned_helixis_user_id,
                    "surface": decision.surface,
                    "reason": decision.reason,
                },
                deterministic_surface=surface,
                enrichment=enrichment_dict,
            )
            # An enrichment failure on this row counts toward
            # ``errors`` even when the bare-floor write succeeds —
            # the response surfaces partial success so Brandon can
            # see which rows degraded.
            if had_enrichment_failure:
                errors += 1
            else:
                patched += 1
        except Exception as exc:  # noqa: BLE001 — diagnostic; log + continue
            errors += 1
            logger.warning("PR #82 backfill failed for event id=%s: %s", row.get("id"), exc)

    return _BackfillResponse(
        company_id=company_id,
        patched=patched,
        skipped=skipped,
        errors=errors,
    )


def _get_pool_for_admin() -> Any:
    """Indirection over ``get_pool`` so tests can monkeypatch the
    pool without intercepting every caller in the module."""
    from helixis.db.engine import get_pool

    return get_pool()


async def _fetch_null_surface_rows(
    pool: Any,
    *,
    company_id: str,
    since: datetime | None,
) -> list[dict[str, Any]]:
    """Pull the ``buildium_events`` rows that need a backfill.

    Uses a small explicit projection (id / event_name / payload /
    action_surface) so we don't drag the full JSONB blob into Python
    memory more than once.
    """
    if since is None:
        rows = await pool.fetch(
            """
            SELECT id, event_name, payload, action_surface
              FROM public.buildium_events
             WHERE company_id = $1::uuid
               AND action_surface IS NULL
            """,
            company_id,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, event_name, payload, action_surface
              FROM public.buildium_events
             WHERE company_id = $1::uuid
               AND action_surface IS NULL
               AND created_at >= $2::timestamptz
            """,
            company_id,
            since,
        )
    return [dict(r) for r in rows]
