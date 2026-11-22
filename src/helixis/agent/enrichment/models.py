"""Pydantic models for the enriched event surface.

The shape mirrors the structured-output schema the planner LLM sees,
and is also what gets persisted into ``buildium_events.enrichment``
(jsonb). Every nested record is independently nullable so a partial
failure during fan-out reads (one Buildium call 404s or 502s) still
produces a serialisable enrichment row — the missing fields are
``None`` and ``errors`` records what could not be fetched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EnrichmentError(BaseModel):
    """One sub-fetch failure, captured for observability.

    The enricher never re-raises a single sub-fetch failure — instead it
    appends an entry here and lets the rest of the fan-out continue. The
    planner can then either include the partial context in its prompt
    or skip the action that would have needed the missing entity.
    """

    field: str
    """Which top-level enrichment slot failed (e.g. ``"unit"``, ``"history_unit"``)."""

    message: str
    """One-line human-readable reason. Never contains plaintext credentials."""


class EnrichedTask(BaseModel):
    """The full Buildium task (the entity the webhook is announcing).

    Only fields the planner needs are typed; the raw response is kept on
    ``raw`` so the planner prompt can quote unmodelled fields verbatim
    when a tenant-supplied description carries important detail.
    """

    model_config = ConfigDict(extra="allow")

    id: int
    title: str | None = None
    description: str | None = None
    status: str | None = None
    category: str | None = None
    priority: str | None = None
    assigned_to_user_id: int | None = None
    requested_by_tenant_id: int | None = None
    requested_by_first_name: str | None = None
    requested_by_last_name: str | None = None
    # Buildium's ``RequestedByUserEntity.Type`` discriminator — the field
    # that tells a resident request (``Tenant``) apart from a rental-owner
    # request (``RentalOwner``) or a prospect/contact request (``Contact``).
    # All three ride the SAME ``Task.Created`` webhook + ``GET /tasks/{id}``
    # super-set read, so this Type is the only thing distinguishing them
    # once the row is enriched (the envelope carries just TaskId). Optional
    # + best-effort: absent on staff ``todorequests`` and on pre-existing
    # rows, so it degrades to None (→ a generic "Task" card) rather than
    # forcing a shape.
    requested_by_type: str | None = None
    # PR #108 — ``CreatedByUserId`` from Buildium. Per the Tasks API
    # docs, ``RequestedByUserEntity`` is populated only on resident /
    # rental-owner / contact requests; staff-created ``todorequests``
    # have no requester field. For those tasks the inbox falls back
    # to resolving the creator through ``buildium_staff`` so the
    # Reporter column shows the staff member who entered the task
    # rather than '—'. Stays optional on the model so v1/v2-era rows
    # in prod still deserialize cleanly.
    created_by_user_id: int | None = None
    due_date: datetime | None = None
    property_id: int | None = None
    unit_id: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


# Buildium ``RequestedByUserEntity.Type`` value → the human request-kind label
# the Inbox card shows. Case-insensitive; unknown / absent types return None so
# a generic task (staff todorequest, or a pre-Type row) stays an unlabelled
# "Task" card rather than getting a wrong badge. Mirrored in the frontend
# (services/inbox.ts requestKindLabel) — keep the two in sync.
_REQUEST_KIND_LABELS: dict[str, str] = {
    "tenant": "Resident request",
    "rentalowner": "Owner request",
    "owner": "Owner request",
    "contact": "Contact request",
    "association": "Association request",
}


def request_kind_label(requested_by_type: str | None) -> str | None:
    """Map a ``RequestedByUserEntity.Type`` to the Inbox request-kind label,
    or None when the type is absent/unrecognised (→ generic Task card)."""
    if not requested_by_type:
        return None
    return _REQUEST_KIND_LABELS.get(requested_by_type.strip().lower())


class EnrichedProperty(BaseModel):
    """The building a task belongs to, resolved to a NAME.

    Both downstream prompts are written to use this and neither could: the
    summarizer's rule is "name places as 'property name, unit N' or just
    'property name'", and its enrichment section is literally headed
    "resolved names + property + unit" — but the enricher fanned out to unit,
    lease and history only, so a property-level task (every task on the live
    account, which carries no UnitId) reached the model with a bare integer
    and nothing sayable.

    The model is also forbidden from surfacing raw identifiers in owner-facing
    prose (gotcha 10), and correctly so. Given an id it cannot use and no name,
    the honest thing it could write was "no property details came through" —
    which it did, on cards for correctly-linked tasks.
    """

    model_config = ConfigDict(extra="allow")

    id: int
    name: str | None = None
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    rental_sub_type: str | None = None


class EnrichedUnit(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    property_id: int | None = None
    unit_number: str | None = None
    address_line1: str | None = None
    market_rent: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class EnrichedTenant(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None


class EnrichedLease(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int
    # PR #124 — capture the lease's own UnitId so we can defensively
    # validate that the lease we got back from Buildium actually
    # belongs to the unit we queried. Production bug we're fixing:
    # the sandbox returned the same lease (24188) for queries against
    # 3 different units (30663 / 30687 / 30698), which produced
    # cross-property tenant hallucinations on the inbox (Clifton
    # Munoz showed up on the HVAC ticket at 33 Garden Row even though
    # he lives at 100 Main Ave). Either Buildium's ``unitids`` filter
    # silently ignores invalid combos, or the sandbox's single
    # active lease gets returned as a fallback. Either way, capturing
    # this field lets us reject mismatched leases at the enricher
    # before they reach the planner.
    unit_id: int | None = None
    status: str | None = None
    lease_from_date: datetime | None = None
    lease_to_date: datetime | None = None
    rent: float | None = None
    tenants: list[EnrichedTenant] = Field(default_factory=list)


class EnrichedHistoricalTask(BaseModel):
    """One row of the 90-day task history at unit or property level.

    Trimmed shape — the planner only needs enough to spot patterns
    (recurring plumbing, ignored requests, escalations).
    """

    model_config = ConfigDict(extra="allow")

    id: int
    title: str | None = None
    category: str | None = None
    priority: str | None = None
    status: str | None = None
    created_date: datetime | None = None
    last_updated: datetime | None = None


class EnrichedEvent(BaseModel):
    """Top-level container the planner receives + Postgres stores.

    Versioned because the JSON shape is consumed directly by Phase A.2's
    frontend — if we change the contract in a backwards-incompatible
    way, bump ``schema_version`` and the frontend can degrade
    gracefully against older rows.
    """

    schema_version: int = 1

    event_name: str
    """The Buildium event name that triggered enrichment (e.g. ``Task.Created``)."""

    company_id: str
    """Helixis company UUID this enrichment is scoped to."""

    task: EnrichedTask
    """The task the webhook referenced. Always present (without it, enrichment
    bails out before reaching the planner)."""

    property: EnrichedProperty | None = None
    """Building the task belongs to, resolved to a name. ``None`` only when the
    task genuinely has no property or the lookup failed (the failure lands on
    ``errors``). Property-level tasks — every task on the live account, which
    carries no UnitId — have nothing else to anchor the narrative to."""

    unit: EnrichedUnit | None = None
    """Unit the task is attached to. ``None`` if the task has no UnitId
    (some Buildium tasks live at property level only)."""

    active_lease: EnrichedLease | None = None
    """The currently-active lease on the unit. ``None`` if the unit is
    vacant or between leases."""

    history_unit: list[EnrichedHistoricalTask] = Field(default_factory=list)
    """Up to 50 most-recent tasks on the same unit in the last 90 days,
    excluding the current task."""

    history_property: list[EnrichedHistoricalTask] = Field(default_factory=list)
    """Up to 50 most-recent tasks on the same property in the last 90
    days, excluding tasks on the current unit (those are already in
    ``history_unit``)."""

    errors: list[EnrichmentError] = Field(default_factory=list)
    """Sub-fetch failures collected during enrichment. Empty when every
    fan-out call succeeded."""

    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """Wall-clock time the enrichment completed. Used by the planner
    for `as of` phrasing in its summary.

    ⚠ UTC-AWARE, not naive. `datetime.now()` records the container's wall
    clock with no zone attached, so what the stored value MEANS depends on
    where the process ran and cannot be recovered afterwards — the same
    defect migration 0098 fixes for dates, made permanent by being stored.
    An "as of" line is the planner quoting a time back to a person, so the
    zone is the part that makes it true."""
