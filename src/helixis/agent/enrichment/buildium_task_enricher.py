"""Enricher for Task.* Buildium webhooks.

Given a ``Task.Created`` envelope (or any Task.* event), this fans out
to Buildium's read API in parallel:

1. ``GET /tasks/{task_id}``               — the full task body
2. ``GET /rentals/{property_id}``         — the building, resolved to a NAME
   (both downstream prompts render "property name, unit N"; without this the
   enrichment carries only an id the model is forbidden to print)
3. ``GET /rentals/units/{unit_id}``       — unit details
4. ``GET /leases?unitids=<unit_id>&leasestatuses=Active``
                                          — active lease(s) on the unit
5. ``GET /tasks?unitids=<unit_id>&lastupdatedfrom=<-90d>&limit=50``
                                          — 90-day task history at unit
6. ``GET /tasks?propertyids=<property_id>&lastupdatedfrom=<-90d>&limit=50``
                                          — 90-day task history at property

(2-6) run via ``asyncio.gather(..., return_exceptions=True)`` so a
single failed sub-fetch never sinks the whole enrichment — the error
becomes an entry on ``EnrichedEvent.errors`` and the field stays
``None``.

Tenant detail comes from the lease body (Buildium embeds
``CurrentTenants`` on every lease record), so we save a round-trip per
tenant. If the lease doesn't carry the embedded list we issue an extra
``GET /leases/tenants/{id}`` per tenant — bounded to the tenants on the
active lease.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from helixis.agent.enrichment.base import EventEnricher
from helixis.agent.enrichment.models import (
    EnrichedEvent,
    EnrichedHistoricalTask,
    EnrichedLease,
    EnrichedProperty,
    EnrichedTask,
    EnrichedTenant,
    EnrichedUnit,
    EnrichmentError,
)
from helixis.buildium.api import BuildiumApi
from helixis.buildium.client import BuildiumClient
from helixis.buildium.credentials import load_buildium_settings_for_company
from helixis.buildium.linkage import task_property_id

logger = logging.getLogger(__name__)


HISTORY_LIMIT = 50
HISTORY_WINDOW_DAYS = 90


def _to_int(value: Any) -> int | None:
    """Best-effort int conversion — Buildium envelopes carry ids as
    either int or numeric string depending on the source. Returns
    ``None`` when the value can't be parsed."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_dt(value: Any) -> datetime | None:
    """Accept a Buildium-shaped ISO string or already-parsed datetime."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _task_from_raw(raw: dict[str, Any]) -> EnrichedTask:
    """Project the Buildium task body onto our typed fields plus keep
    the full raw payload so the planner can quote unmodelled fields."""
    requested_by_raw = raw.get("RequestedByUserEntity") or {}
    requested_by: dict[str, Any] = requested_by_raw if isinstance(requested_by_raw, dict) else {}

    category_raw = raw.get("Category")
    if isinstance(category_raw, dict):
        category = category_raw.get("Name")
    else:
        category = category_raw

    return EnrichedTask(
        id=int(raw.get("Id", 0)),
        title=raw.get("Title"),
        description=raw.get("Description"),
        status=raw.get("Status") or raw.get("TaskStatus"),
        category=category,
        priority=raw.get("Priority"),
        assigned_to_user_id=_to_int(raw.get("AssignedToUserId")),
        requested_by_tenant_id=_to_int(requested_by.get("Id")),
        requested_by_first_name=requested_by.get("FirstName"),
        requested_by_last_name=requested_by.get("LastName"),
        # ``Type`` discriminator (Tenant / RentalOwner / Contact / …) — lets
        # the Inbox card label an owner or contact request as such instead of
        # rendering it like a resident maintenance request. Best-effort: absent
        # on staff todorequests → None → generic Task card.
        requested_by_type=requested_by.get("Type"),
        # PR #108 — preserve Buildium's ``CreatedByUserId`` so the
        # inbox route can resolve a staff-name fallback for the
        # Reporter column when ``RequestedByUserEntity`` is absent
        # (the ``todorequests`` subtype, per Buildium's task docs).
        created_by_user_id=_to_int(raw.get("CreatedByUserId")),
        due_date=_coerce_dt(raw.get("DueDate")),
        # Nested ``Property.Id``; production sends no flat key (buildium.linkage).
        property_id=task_property_id(raw),
        unit_id=_to_int(raw.get("UnitId")),
        raw=raw,
    )


def _property_from_raw(raw: dict[str, Any]) -> EnrichedProperty:
    """Project a ``GET /rentals/{id}`` body onto the naming fields.

    Address is nested under ``Address`` (same shape as a unit's), so a flat
    read would give the name and nothing else — enough for most cards but not
    for a vendor dispatch, which needs the street.
    """
    address = raw.get("Address")
    address = address if isinstance(address, dict) else {}
    return EnrichedProperty(
        id=int(raw.get("Id", 0)),
        name=raw.get("Name"),
        address_line1=address.get("AddressLine1"),
        city=address.get("City"),
        state=address.get("State"),
        postal_code=address.get("PostalCode"),
        rental_sub_type=raw.get("RentalSubType"),
    )


def _unit_from_raw(raw: dict[str, Any]) -> EnrichedUnit:
    address = raw.get("Address") or {}
    return EnrichedUnit(
        id=int(raw.get("Id", 0)),
        property_id=_to_int(raw.get("PropertyId")),
        unit_number=raw.get("UnitNumber"),
        address_line1=address.get("AddressLine1") if isinstance(address, dict) else None,
        market_rent=raw.get("MarketRent"),
        raw=raw,
    )


def _tenant_from_raw(raw: dict[str, Any]) -> EnrichedTenant:
    phones = raw.get("PhoneNumbers") or []
    phone = None
    if phones and isinstance(phones, list) and isinstance(phones[0], dict):
        phone = phones[0].get("Number")
    return EnrichedTenant(
        id=int(raw.get("Id", 0)),
        first_name=raw.get("FirstName"),
        last_name=raw.get("LastName"),
        email=raw.get("Email"),
        phone=phone,
    )


def _lease_from_raw(raw: dict[str, Any], tenants: list[EnrichedTenant]) -> EnrichedLease:
    return EnrichedLease(
        id=int(raw.get("Id", 0)),
        # PR #124 — capture the lease's UnitId for the post-fetch
        # validation in ``_fetch_active_lease`` below. Buildium's
        # lease body carries this directly on every lease.
        unit_id=_to_int(raw.get("UnitId")),
        status=raw.get("LeaseStatus") or raw.get("Status"),
        lease_from_date=_coerce_dt(raw.get("LeaseFromDate")),
        lease_to_date=_coerce_dt(raw.get("LeaseToDate")),
        rent=raw.get("Rent"),
        tenants=tenants,
    )


def _history_from_rows(rows: list[dict[str, Any]], exclude_id: int) -> list[EnrichedHistoricalTask]:
    """Project a Buildium ``/tasks`` list response onto our slim model
    and drop the row referencing the same task the webhook is about."""
    out: list[EnrichedHistoricalTask] = []
    for row in rows:
        rid = _to_int(row.get("Id"))
        if rid is None or rid == exclude_id:
            continue
        category = row.get("Category")
        if isinstance(category, dict):
            category = category.get("Name")
        out.append(
            EnrichedHistoricalTask(
                id=rid,
                title=row.get("Title"),
                category=category,
                priority=row.get("Priority"),
                status=row.get("Status") or row.get("TaskStatus"),
                created_date=_coerce_dt(row.get("CreatedDate") or row.get("CreatedDateTime")),
                last_updated=_coerce_dt(row.get("LastUpdatedDateTime") or row.get("LastUpdated")),
            )
        )
    return out[:HISTORY_LIMIT]


class BuildiumTaskEnricher(EventEnricher):
    """First-class enricher for the ``Task.Created`` webhook."""

    def __init__(
        self,
        *,
        api_factory: Any | None = None,
    ) -> None:
        """``api_factory`` lets tests inject a fake BuildiumApi without
        touching the credentials/decryption layer. When ``None`` the
        enricher uses the production path:
        ``load_buildium_settings_for_company`` → ``BuildiumClient`` →
        ``BuildiumApi``.
        """
        self._api_factory = api_factory

    async def enrich(self, event: dict[str, Any], company_id: str) -> EnrichedEvent:
        task_id = _to_int(event.get("TaskId"))
        if task_id is None:
            # No TaskId on the envelope → degenerate; return an empty
            # enrichment with an error stub so the planner can decide
            # whether to bail or render a "context unavailable" surface.
            return EnrichedEvent(
                event_name=str(event.get("EventName") or "Task.Unknown"),
                company_id=company_id,
                task=EnrichedTask(id=0),
                errors=[
                    EnrichmentError(
                        field="task",
                        message="Webhook envelope had no TaskId.",
                    )
                ],
            )

        errors: list[EnrichmentError] = []

        if self._api_factory is not None:
            api_cm = self._api_factory(company_id)
        else:
            api_cm = _open_api_for_company(company_id)

        async with api_cm as api:
            if api is None:
                # No Buildium credentials for this company. Still return
                # a serialisable enrichment so the row write doesn't blow
                # up — the planner can surface a "configure Buildium"
                # prompt.
                return EnrichedEvent(
                    event_name=str(event.get("EventName") or "Task.Created"),
                    company_id=company_id,
                    task=EnrichedTask(id=task_id),
                    errors=[
                        EnrichmentError(
                            field="credentials",
                            message=(
                                "No Buildium credentials configured for this "
                                "company — cannot enrich."
                            ),
                        )
                    ],
                )

            t0 = time.monotonic()
            task = await self._fetch_task(api, task_id, errors)
            if task is None:
                return EnrichedEvent(
                    event_name=str(event.get("EventName") or "Task.Created"),
                    company_id=company_id,
                    task=EnrichedTask(id=task_id),
                    errors=errors,
                )

            unit_id = task.unit_id
            property_id = task.property_id
            since_iso = _iso_since_days(HISTORY_WINDOW_DAYS)

            # Fan out the remaining reads in parallel — they're
            # independent of each other and the bulk of the latency.
            prop_coro = self._fetch_property(api, property_id) if property_id else _none_coro()
            unit_coro = self._fetch_unit(api, unit_id) if unit_id else _none_coro()
            lease_coro = self._fetch_active_lease(api, unit_id) if unit_id else _none_coro()
            hist_unit_coro = (
                self._fetch_history(api, since_iso, unit_id=unit_id)
                if unit_id
                else _empty_list_coro()
            )
            hist_prop_coro = (
                self._fetch_history(api, since_iso, property_id=property_id)
                if property_id
                else _empty_list_coro()
            )

            prop_res, unit_res, lease_res, hist_unit_res, hist_prop_res = await asyncio.gather(
                prop_coro,
                unit_coro,
                lease_coro,
                hist_unit_coro,
                hist_prop_coro,
                return_exceptions=True,
            )

            prop = _unwrap("property", prop_res, errors)
            unit = _unwrap("unit", unit_res, errors)
            lease = _unwrap("active_lease", lease_res, errors)
            history_unit_rows = _unwrap("history_unit", hist_unit_res, errors) or []
            history_property_rows = _unwrap("history_property", hist_prop_res, errors) or []

            history_unit = _history_from_rows(history_unit_rows, exclude_id=task.id)
            # Drop unit-history rows from the property cut so the
            # planner doesn't see duplicates.
            unit_history_ids = {h.id for h in history_unit}
            history_property = [
                h
                for h in _history_from_rows(history_property_rows, exclude_id=task.id)
                if h.id not in unit_history_ids
            ]

            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.info(
                "buildium task enrichment complete",
                extra={
                    "event_name": event.get("EventName"),
                    "company_id": company_id,
                    "task_id": task_id,
                    "unit_id": unit_id,
                    "property_id": property_id,
                    "history_unit_count": len(history_unit),
                    "history_property_count": len(history_property),
                    "errors": [e.field for e in errors],
                    "elapsed_ms": elapsed_ms,
                    "trace_name": f"enrich.{event.get('EventName')}",
                },
            )

            return EnrichedEvent(
                event_name=str(event.get("EventName") or "Task.Created"),
                company_id=company_id,
                task=task,
                property=prop,
                unit=unit,
                active_lease=lease,
                history_unit=history_unit,
                history_property=history_property,
                errors=errors,
            )

    # ── individual fetchers ───────────────────────────────────────

    async def _fetch_task(
        self,
        api: BuildiumApi,
        task_id: int,
        errors: list[EnrichmentError],
    ) -> EnrichedTask | None:
        """Fetch the task body as a raw dict so we can keep every
        unmodelled field on ``EnrichedTask.raw`` for the planner."""
        try:
            raw = await api.client.request("GET", f"/tasks/{task_id}")
        except Exception as exc:  # noqa: BLE001 — surface to errors, not raise
            errors.append(
                EnrichmentError(
                    field="task",
                    message=f"GET /tasks/{task_id} failed: {exc}",
                )
            )
            return None
        if not isinstance(raw, dict):
            errors.append(
                EnrichmentError(
                    field="task",
                    message=f"GET /tasks/{task_id} returned non-dict {type(raw).__name__}",
                )
            )
            return None
        return _task_from_raw(raw)

    async def _fetch_property(
        self,
        api: BuildiumApi,
        property_id: int,
    ) -> EnrichedProperty:
        """Resolve the building to a name the narrative can actually use.

        One extra read, and only when the task has a property. It is not an
        N+1: enrichment runs once per event, so this is +1 call on a fan-out
        that already makes four.

        Kept on the raw dict rather than ``api.rentals.get_property`` for the
        same reason ``_fetch_unit`` is — the typed model would drop anything
        Buildium adds later, and ``raw`` is what the planner quotes from.
        """
        raw = await api.client.request("GET", f"/rentals/{property_id}")
        if not isinstance(raw, dict):
            raise RuntimeError(f"GET /rentals/{property_id} returned non-dict {type(raw).__name__}")
        return _property_from_raw(raw)

    async def _fetch_unit(
        self,
        api: BuildiumApi,
        unit_id: int,
    ) -> EnrichedUnit:
        raw = await api.client.request("GET", f"/rentals/units/{unit_id}")
        if not isinstance(raw, dict):
            raise RuntimeError(
                f"GET /rentals/units/{unit_id} returned non-dict {type(raw).__name__}"
            )
        return _unit_from_raw(raw)

    async def _fetch_active_lease(
        self,
        api: BuildiumApi,
        unit_id: int,
    ) -> EnrichedLease | None:
        """Return the most-recently-started Active lease on the unit,
        with tenants populated from the embedded ``CurrentTenants`` /
        ``Tenants`` list when present, or via per-tenant fetch
        otherwise. ``None`` when the unit has no active lease.

        PR #124 — defensively validate that the returned lease's own
        ``UnitId`` matches the unit we queried. Production bug we're
        fixing: Buildium's sandbox returned the same lease (24188)
        for queries against 3 different units (30663 / 30687 /
        30698), which surfaced cross-property tenant hallucinations
        ("Clifton Munoz" appeared on the HVAC ticket at 33 Garden
        Row even though he lives at 100 Main Ave). Either Buildium's
        ``unitids`` filter silently ignores invalid combos, or the
        sandbox's only active lease is returned as a fallback when
        the queried unit has none. We reject the response when
        UnitId doesn't match and treat the unit as having no
        active lease — better an empty lease than wrong tenants."""
        raw = await api.client.request(
            "GET",
            "/leases",
            query={"unitids": [unit_id], "leasestatuses": ["Active"], "limit": 1},
        )
        if not isinstance(raw, list) or not raw:
            return None
        lease_raw = raw[0]
        if not isinstance(lease_raw, dict):
            return None

        # PR #124 — unit-id cross-check. ``UnitId`` is always present
        # on a Buildium lease body (Residential / Commercial). When
        # absent we still bail to avoid attributing an unknown
        # lease's tenants to a unit. When present and mismatched we
        # log + bail; the planner sees ``active_lease: None`` and
        # falls back to non-tenant-specific actions.
        lease_unit_id = _to_int(lease_raw.get("UnitId"))
        if lease_unit_id is None or lease_unit_id != unit_id:
            logger.warning(
                "active-lease unit mismatch: queried unit %s but Buildium "
                "returned lease %s (UnitId=%s). Dropping the lease to avoid "
                "cross-unit tenant attribution.",
                unit_id,
                lease_raw.get("Id"),
                lease_unit_id,
            )
            return None

        # Buildium embeds tenants on the lease record. Prefer the
        # richer ``CurrentTenants`` list (has names + emails); fall
        # back to ``Tenants`` (just ids + status) and fan out per-
        # tenant fetches.
        tenants: list[EnrichedTenant] = []
        embedded = lease_raw.get("CurrentTenants")
        if isinstance(embedded, list) and embedded:
            for t in embedded:
                if isinstance(t, dict):
                    tenants.append(_tenant_from_raw(t))
        else:
            tenant_stubs = lease_raw.get("Tenants") or []
            tenant_ids = [
                _to_int((stub or {}).get("Id")) for stub in tenant_stubs if isinstance(stub, dict)
            ]
            tenant_ids_clean: list[int] = [tid for tid in tenant_ids if tid is not None]
            if tenant_ids_clean:
                fetched = await asyncio.gather(
                    *(self._fetch_tenant(api, tid) for tid in tenant_ids_clean),
                    return_exceptions=True,
                )
                for f in fetched:
                    if isinstance(f, EnrichedTenant):
                        tenants.append(f)

        return _lease_from_raw(lease_raw, tenants)

    async def _fetch_tenant(self, api: BuildiumApi, tenant_id: int) -> EnrichedTenant:
        raw = await api.client.request("GET", f"/leases/tenants/{tenant_id}")
        if not isinstance(raw, dict):
            raise RuntimeError(
                f"GET /leases/tenants/{tenant_id} returned non-dict {type(raw).__name__}"
            )
        return _tenant_from_raw(raw)

    async def _fetch_history(
        self,
        api: BuildiumApi,
        since_iso: str,
        *,
        unit_id: int | None = None,
        property_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch up to ``HISTORY_LIMIT`` recent task rows for either a
        unit or a property.

        ⚠ Buildium SILENTLY IGNORES ``propertyids`` (and, assumed by the
        same mechanism, ``unitids``) on ``GET /tasks`` — live-verified
        against production 2026-08-20 (every param form returned the
        identical whole-account list; gotcha 24). Before this fix the
        "history at this unit/property" the planner reasons over was
        actually the WHOLE ACCOUNT's recent tasks, so pattern signals
        ("3 similar tickets at this unit") could cite unrelated
        properties. The params stay on the request (harmless, and
        correct if Buildium ever fixes the filter) but the REAL scoping
        is the client-side filter below, keyed on the live wire shape:
        a task row carries ``Property.Id`` (nested) and a flat
        ``UnitId``. Rows that don't match are someone else's history.

        Because the server ignores the filter, ``HISTORY_LIMIT`` bounds
        the ACCOUNT-wide window, not the entity's — on a busy account a
        quiet property's older history can fall outside it. Acceptable:
        signals degrade toward "no pattern," never toward a wrong one.
        """
        query: dict[str, Any] = {
            "lastupdatedfrom": since_iso,
            "limit": HISTORY_LIMIT,
            "orderby": "LastUpdatedDateTime desc",
        }
        if unit_id is not None:
            query["unitids"] = [unit_id]
        if property_id is not None:
            query["propertyids"] = [property_id]

        rows = await api.client.request("GET", "/tasks", query=query)
        if not isinstance(rows, list):
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            if unit_id is not None and r.get("UnitId") != unit_id:
                continue
            if property_id is not None:
                row_property = r.get("Property")
                if not isinstance(row_property, dict) or row_property.get("Id") != property_id:
                    continue
            out.append(r)
        return out


# ── helpers ───────────────────────────────────────────────────────


async def _none_coro() -> None:
    return None


async def _empty_list_coro() -> list[Any]:
    return []


def _unwrap(field: str, result: Any, errors: list[EnrichmentError]) -> Any:
    """Pull a value out of an ``asyncio.gather(return_exceptions=True)``
    result. If the slot is an exception, append to ``errors`` and
    return ``None`` (or an empty list for list-typed slots)."""
    if isinstance(result, BaseException):
        errors.append(EnrichmentError(field=field, message=f"{type(result).__name__}: {result}"))
        return None
    return result


def _iso_since_days(days: int) -> str:
    """ISO-8601 UTC timestamp `days` ago — what Buildium accepts in
    ``lastupdatedfrom``."""
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


# Production path: open a BuildiumApi for a company. Mirrors
# ``webhooks.handlers._helpers.open_api_for_company`` but kept local
# so the enricher has no module-level dependency on the handlers
# package (the dispatch handlers still own that helper).
@asynccontextmanager
async def _open_api_for_company(company_id: str):  # type: ignore[no-untyped-def]
    settings = await load_buildium_settings_for_company(company_id)
    if settings is None:
        logger.warning("buildium enrichment: no credentials for company %s", company_id)
        yield None
        return
    async with BuildiumClient(settings) as client:
        yield BuildiumApi(client)
