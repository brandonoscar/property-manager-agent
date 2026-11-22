"""Unit tests for ``BuildiumTaskEnricher``.

Strategy: inject a fake ``BuildiumApi`` via the enricher's
``api_factory`` parameter so we never touch credentials, network, or
asyncpg. The fake supports the four endpoints the enricher hits:
``GET /tasks/{id}``, ``GET /rentals/units/{id}``, ``GET /leases``,
``GET /tasks`` (history).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from helixis.agent.enrichment import BuildiumTaskEnricher

COMPANY_ID = "company-A"


# ── Fake Buildium API client + factory ────────────────────────────


class _FakeClient:
    def __init__(self, responses: dict[tuple[str, str], Any]) -> None:
        # responses keyed by (method, path) — ``request()`` ignores the
        # query dict for matching since none of our paths reuse the
        # same (method, path) with different responses in one test.
        self._responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: Any | None = None,
        schema: Any | None = None,
    ) -> Any:
        self.calls.append((method, path, query or {}))
        key = (method, path)
        if key not in self._responses:
            raise AssertionError(f"unexpected Buildium call: {method} {path}")
        response = self._responses[key]
        if isinstance(response, Exception):
            raise response
        return response


class _FakeApi:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client


def _make_factory(responses: dict[tuple[str, str], Any]):
    client = _FakeClient(responses)

    @asynccontextmanager
    async def factory(_company_id: str):
        yield _FakeApi(client)

    factory.client = client  # type: ignore[attr-defined]
    return factory


# ── Sample Buildium responses (verified shape against /api docs) ──


_TASK_HAPPY = {
    "Id": 4001,
    "Title": "Kitchen sink leaking",
    "Description": "Tenant reports drip under the sink, please investigate.",
    "Status": "New",
    "Category": {"Id": 7, "Name": "Plumbing"},
    "Priority": "Normal",
    "AssignedToUserId": 88,
    "RequestedByUserEntity": {"Id": 9001, "Type": "Tenant", "FirstName": "Mei", "LastName": "Chen"},
    "DueDate": "2026-05-20T00:00:00",
    "PropertyId": 31,
    "UnitId": 412,
}

_PROPERTY_HAPPY = {
    "Id": 31,
    "Name": "Maple Court",
    "RentalSubType": "MultiFamily",
    "Address": {
        "AddressLine1": "1208 Oak St",
        "City": "Boston",
        "State": "MA",
        "PostalCode": "02101",
    },
}

_UNIT_HAPPY = {
    "Id": 412,
    "PropertyId": 31,
    "UnitNumber": "3B",
    "Address": {"AddressLine1": "1208 Oak St"},
    "MarketRent": 2200.0,
}

_LEASE_HAPPY = [
    {
        "Id": 7771,
        "LeaseStatus": "Active",
        "LeaseFromDate": "2025-06-01T00:00:00",
        "LeaseToDate": "2026-05-31T00:00:00",
        "Rent": 2200.0,
        "UnitId": 412,
        "CurrentTenants": [
            {"Id": 9001, "FirstName": "Mei", "LastName": "Chen", "Email": "mei@example.com"},
        ],
    }
]

# History rows carry the LIVE list-row scoping keys (verified against
# production 2026-08-20): a flat ``UnitId`` and a nested ``Property.Id``.
# These matter now — Buildium ignores the ?unitids/?propertyids filters
# on /tasks (gotcha 24), so the enricher scopes CLIENT-SIDE on exactly
# these keys; a fixture without them is invisible to the history.
_HISTORY_UNIT_HAPPY = [
    {
        "Id": 4001,
        "Title": "Kitchen sink leaking",
        "Status": "New",
        "Category": "Plumbing",
        "UnitId": 412,
        "Property": {"Id": 31, "Type": "Rental"},
    },
    # Older plumbing tasks at the same unit — pattern-detect material.
    {
        "Id": 3980,
        "Title": "Bathroom sink slow drain",
        "Category": "Plumbing",
        "Status": "Completed",
        "UnitId": 412,
        "Property": {"Id": 31, "Type": "Rental"},
    },
    {
        "Id": 3950,
        "Title": "Toilet running",
        "Category": "Plumbing",
        "Status": "Completed",
        "UnitId": 412,
        "Property": {"Id": 31, "Type": "Rental"},
    },
]

_HISTORY_PROPERTY_HAPPY = [
    {
        "Id": 3990,
        "Title": "Common area light out",
        "Category": "Electrical",
        "Status": "Completed",
        "UnitId": None,  # property-level task
        "Property": {"Id": 31, "Type": "Rental"},
    },
    {
        "Id": 3950,
        "Title": "Toilet running",
        "Category": "Plumbing",
        "Status": "Completed",
        "UnitId": 412,
        "Property": {"Id": 31, "Type": "Rental"},
    },
]

# Rows belonging to OTHER entities on the same account — what the live
# /tasks list actually returns alongside ours, since the server-side
# filter is a no-op. The enricher must drop every one of these.
_HISTORY_FOREIGN = [
    {
        "Id": 8801,
        "Title": "HVAC not cooling at the other building",
        "Category": "HVAC",
        "Status": "New",
        "UnitId": 777,
        "Property": {"Id": 999, "Type": "Rental"},
    },
    {
        "Id": 8802,
        "Title": "Kitchen sink leaking",  # same TITLE, different property
        "Category": "Plumbing",
        "Status": "New",
        "UnitId": 778,
        "Property": {"Id": 999, "Type": "Rental"},
    },
]


# ── Tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_populates_every_field() -> None:
    """Full enrichment with all 4 fan-out calls succeeding."""
    factory = _make_factory(
        {
            ("GET", "/tasks/4001"): _TASK_HAPPY,
            ("GET", "/rentals/units/412"): _UNIT_HAPPY,
            ("GET", "/rentals/31"): _PROPERTY_HAPPY,
            ("GET", "/leases"): _LEASE_HAPPY,
            ("GET", "/tasks"): _HISTORY_UNIT_HAPPY,  # both history queries hit /tasks
        }
    )
    enricher = BuildiumTaskEnricher(api_factory=factory)

    result = await enricher.enrich(
        {"EventName": "Task.Created", "TaskId": 4001},
        COMPANY_ID,
    )

    assert result.event_name == "Task.Created"
    assert result.company_id == COMPANY_ID
    assert result.task.id == 4001
    assert result.task.title == "Kitchen sink leaking"
    assert result.task.category == "Plumbing"
    assert result.task.requested_by_tenant_id == 9001
    assert result.unit is not None
    assert result.unit.unit_number == "3B"
    assert result.active_lease is not None
    assert result.active_lease.id == 7771
    assert len(result.active_lease.tenants) == 1
    assert result.active_lease.tenants[0].first_name == "Mei"
    # The current task (4001) is filtered out of the history — and the
    # history is genuinely POPULATED (an empty list would also satisfy
    # an all() assertion; that hid the scoping bug once).
    assert {h.id for h in result.history_unit} == {3980, 3950}
    assert len(result.errors) == 0


@pytest.mark.asyncio
async def test_no_active_lease_returns_none_lease() -> None:
    """A vacant unit produces ``active_lease=None``, never crashes."""
    factory = _make_factory(
        {
            ("GET", "/tasks/4001"): _TASK_HAPPY,
            ("GET", "/rentals/units/412"): _UNIT_HAPPY,
            ("GET", "/rentals/31"): _PROPERTY_HAPPY,
            ("GET", "/leases"): [],  # No active lease
            ("GET", "/tasks"): [],
        }
    )
    enricher = BuildiumTaskEnricher(api_factory=factory)

    result = await enricher.enrich(
        {"EventName": "Task.Created", "TaskId": 4001},
        COMPANY_ID,
    )

    assert result.active_lease is None
    # Unit + task still populated.
    assert result.unit is not None
    assert result.task.id == 4001
    # No error appended for an empty lease list — that's a valid state.
    assert all(e.field != "active_lease" for e in result.errors)


@pytest.mark.asyncio
async def test_unit_fetch_failure_does_not_kill_enrichment() -> None:
    """A failed sub-fetch records an EnrichmentError; other slots populate."""
    factory = _make_factory(
        {
            ("GET", "/tasks/4001"): _TASK_HAPPY,
            ("GET", "/rentals/units/412"): RuntimeError("502 from Buildium"),
            ("GET", "/leases"): _LEASE_HAPPY,
            ("GET", "/tasks"): _HISTORY_UNIT_HAPPY,
        }
    )
    enricher = BuildiumTaskEnricher(api_factory=factory)

    result = await enricher.enrich(
        {"EventName": "Task.Created", "TaskId": 4001},
        COMPANY_ID,
    )

    assert result.unit is None
    # The other parallel slots still populate.
    assert result.task.id == 4001
    assert result.active_lease is not None
    # The error is recorded against the right field.
    unit_errors = [e for e in result.errors if e.field == "unit"]
    assert len(unit_errors) == 1
    assert "502" in unit_errors[0].message


@pytest.mark.asyncio
async def test_missing_task_id_returns_error_stub() -> None:
    """A webhook envelope with no TaskId produces an error-stubbed
    EnrichedEvent rather than raising."""
    enricher = BuildiumTaskEnricher(api_factory=_make_factory({}))

    result = await enricher.enrich(
        {"EventName": "Task.Unknown"},
        COMPANY_ID,
    )

    assert result.task.id == 0
    assert len(result.errors) == 1
    assert result.errors[0].field == "task"
    assert "TaskId" in result.errors[0].message


@pytest.mark.asyncio
async def test_history_excludes_current_task_and_dedupes_property() -> None:
    """The current task is filtered out of unit history; rows that
    appear in unit history are dropped from property history so the
    planner doesn't see duplicates."""

    # Both history endpoints hit the same path; the enricher issues
    # two calls. The fake only returns the unit-history shape for the
    # first call and an overlapping property-history list on a second.
    # We approximate by serving the same payload to both calls — the
    # dedupe path drops overlaps regardless of order.
    factory = _make_factory(
        {
            ("GET", "/tasks/4001"): _TASK_HAPPY,
            ("GET", "/rentals/units/412"): _UNIT_HAPPY,
            ("GET", "/rentals/31"): _PROPERTY_HAPPY,
            ("GET", "/leases"): _LEASE_HAPPY,
            ("GET", "/tasks"): _HISTORY_UNIT_HAPPY + _HISTORY_PROPERTY_HAPPY,
        }
    )
    enricher = BuildiumTaskEnricher(api_factory=factory)

    result = await enricher.enrich(
        {"EventName": "Task.Created", "TaskId": 4001},
        COMPANY_ID,
    )

    # Current task is gone from unit history.
    assert all(h.id != 4001 for h in result.history_unit)
    # Property history does not duplicate unit history rows.
    unit_ids = {h.id for h in result.history_unit}
    assert unit_ids  # populated, not vacuously true
    assert all(h.id not in unit_ids for h in result.history_property)


@pytest.mark.asyncio
async def test_history_drops_other_properties_rows() -> None:
    """Gotcha 24 regression: Buildium silently ignores ?unitids /
    ?propertyids on GET /tasks, so the raw list contains the WHOLE
    account. Before the client-side scoping fix, foreign rows (other
    properties' tasks — even same-titled ones) polluted the history the
    planner pattern-matches over ("3 similar tickets at this unit"
    citing another building). Every foreign row must be dropped."""
    factory = _make_factory(
        {
            ("GET", "/tasks/4001"): _TASK_HAPPY,
            ("GET", "/rentals/units/412"): _UNIT_HAPPY,
            ("GET", "/rentals/31"): _PROPERTY_HAPPY,
            ("GET", "/leases"): _LEASE_HAPPY,
            # What the live endpoint really returns: ours mixed with
            # everyone else's, filter param ignored.
            ("GET", "/tasks"): _HISTORY_FOREIGN + _HISTORY_UNIT_HAPPY + _HISTORY_FOREIGN,
        }
    )
    enricher = BuildiumTaskEnricher(api_factory=factory)

    result = await enricher.enrich(
        {"EventName": "Task.Created", "TaskId": 4001},
        COMPANY_ID,
    )

    foreign_ids = {r["Id"] for r in _HISTORY_FOREIGN}
    assert {h.id for h in result.history_unit} == {3980, 3950}
    assert all(h.id not in foreign_ids for h in result.history_unit)
    assert all(h.id not in foreign_ids for h in result.history_property)


# ── The subject task's property lives under nested ``Property.Id`` ──


#: The EXACT shape production sends. Captured 2026-08-24 from the live
#: enrichment of task 39517 (account 695763) via the replayed webhook: a
#: nested ``Property`` object and NO flat ``PropertyId`` key whatsoever.
#: ``_TASK_HAPPY`` above uses the flat key, which is why every existing test
#: passed against the broken extractor — the fixture was the bug's alibi.
_TASK_PRODUCTION_SHAPE = {
    "Id": 39517,
    "Title": "Front porch light out - possible wiring issue",
    "Description": "",
    "UnitId": None,  # property-level task: no unit to fall back on
    "DueDate": None,
    "Category": {"Id": 1, "Name": "Uncategorized", "SubCategory": None},
    "Priority": "Normal",
    "Property": {
        "Id": 38038,
        "Href": "https://api.buildium.com/v1/rentals/38038",
        "Type": "Rental",
    },
    "TaskType": "Todo",
    "TaskStatus": "New",
    "AssignedToUserId": 229661,
    "RequestedByUserEntity": None,
}

_HISTORY_AT_38038 = [
    {
        "Id": 39400,
        "Title": "Porch light flickering",
        "Category": "Electrical",
        "Status": "Completed",
        "UnitId": None,
        "Property": {"Id": 38038, "Type": "Rental"},
    },
]


@pytest.mark.asyncio
async def test_property_is_read_from_the_nested_object_production_sends() -> None:
    """gotcha 26 applied to the enricher — the layer the original fix missed.

    A ``/v1/tasks`` row carries its property under nested ``Property.Id`` and
    has no flat ``PropertyId``; reading the flat key yielded ``None`` for every
    task ever enriched. This asserts the field itself; the test below asserts
    the consequence, which is the part that actually reached users.
    """
    factory = _make_factory(
        {
            ("GET", "/tasks/39517"): _TASK_PRODUCTION_SHAPE,
            ("GET", "/tasks"): _HISTORY_AT_38038,
        }
    )
    enricher = BuildiumTaskEnricher(api_factory=factory)

    result = await enricher.enrich(
        {"EventName": "Task.Created", "TaskId": 39517},
        COMPANY_ID,
    )

    assert result.task is not None
    assert result.task.property_id == 38038, (
        "the subject task's property must come from the nested Property.Id; "
        "production sends no flat PropertyId key at all"
    )


@pytest.mark.asyncio
async def test_a_unitless_task_still_gets_its_property_history() -> None:
    """The consequence, and the reason this mattered.

    With ``property_id`` None the enricher skipped the property-history fetch
    entirely, so a task with no unit reached the planner with NO history
    signal — and the planner, seeing a null property, wrote it up as a
    finding. Live cards on correctly-linked tasks read "no property or unit
    details came through" and offered a "flag missing property link" action.
    A false claim about the record is worse than a thin card.
    """
    factory = _make_factory(
        {
            ("GET", "/tasks/39517"): _TASK_PRODUCTION_SHAPE,
            ("GET", "/tasks"): _HISTORY_AT_38038,
        }
    )
    enricher = BuildiumTaskEnricher(api_factory=factory)

    result = await enricher.enrich(
        {"EventName": "Task.Created", "TaskId": 39517},
        COMPANY_ID,
    )

    assert [h.id for h in result.history_property] == [39400], (
        "property history was not fetched for a unitless task -- the planner "
        "sees no history at all and reports the property as missing"
    )


@pytest.mark.asyncio
async def test_the_flat_key_still_works_as_a_fallback() -> None:
    """The sandbox and our own write bodies use the flat key.

    ``endpoints/tasks.py`` converts nested -> flat for the read-merge-write
    PUT, so a caller handing us that shape is not wrong. Nested wins; flat is
    accepted. Without this the fix would trade one silent None for another.
    """
    factory = _make_factory(
        {
            ("GET", "/tasks/4001"): _TASK_HAPPY,  # flat PropertyId: 31
            ("GET", "/rentals/units/412"): _UNIT_HAPPY,
            ("GET", "/rentals/31"): _PROPERTY_HAPPY,
            ("GET", "/leases"): _LEASE_HAPPY,
            ("GET", "/tasks"): _HISTORY_UNIT_HAPPY,
        }
    )
    enricher = BuildiumTaskEnricher(api_factory=factory)

    result = await enricher.enrich(
        {"EventName": "Task.Created", "TaskId": 4001},
        COMPANY_ID,
    )

    assert result.task is not None
    assert result.task.property_id == 31


# ── The building has to be resolved to a NAME ───────────────────────────────


@pytest.mark.asyncio
async def test_property_is_resolved_to_a_name() -> None:
    """Both downstream prompts are written to use a property NAME.

    The summarizer's rule is "name places as 'property name, unit N' or just
    'property name'", and it renders its enrichment block under the heading
    "resolved names + property + unit". But the fan-out went to unit, lease and
    history only, so the enrichment carried a bare integer. The model is also
    (correctly) barred from surfacing raw identifiers in owner-facing prose, so
    the honest thing left to write was "no property details came through" --
    which is what live cards said about correctly-linked tasks.
    """
    factory = _make_factory(
        {
            ("GET", "/tasks/39517"): _TASK_PRODUCTION_SHAPE,
            ("GET", "/rentals/38038"): {
                "Id": 38038,
                "Name": "NH - 107 N Main St",
                "Address": {"AddressLine1": "107 N Main St", "City": "Concord", "State": "NH"},
            },
            ("GET", "/tasks"): _HISTORY_AT_38038,
        }
    )
    enricher = BuildiumTaskEnricher(api_factory=factory)

    result = await enricher.enrich(
        {"EventName": "Task.Created", "TaskId": 39517},
        COMPANY_ID,
    )

    assert result.property is not None, (
        "the enrichment carries no property object, so the narrative can only "
        "refer to the building by an id it is forbidden to print"
    )
    assert result.property.name == "NH - 107 N Main St"
    assert result.property.address_line1 == "107 N Main St"
    assert not result.errors


@pytest.mark.asyncio
async def test_a_failed_property_lookup_costs_the_name_not_the_enrichment() -> None:
    """Every other sub-fetch degrades to an ``errors`` entry; so does this one.

    A property read that 500s must still yield a usable card built from the
    task itself -- enrichment is best-effort by design, and one dead lookup
    must never cost the whole event.
    """
    factory = _make_factory(
        {
            ("GET", "/tasks/39517"): _TASK_PRODUCTION_SHAPE,
            ("GET", "/rentals/38038"): RuntimeError("Buildium 500"),
            ("GET", "/tasks"): _HISTORY_AT_38038,
        }
    )
    enricher = BuildiumTaskEnricher(api_factory=factory)

    result = await enricher.enrich(
        {"EventName": "Task.Created", "TaskId": 39517},
        COMPANY_ID,
    )

    assert result.property is None
    assert result.task is not None and result.task.id == 39517
    assert [e.field for e in result.errors] == ["property"]
    # The rest of the fan-out is unaffected -- a failure is isolated, not fatal.
    assert [h.id for h in result.history_property] == [39400]


@pytest.mark.asyncio
async def test_no_property_means_no_lookup_is_attempted() -> None:
    """A task with no property must not fire a GET /rentals/None.

    The fake client raises on any unregistered call, so an attempted fetch
    fails this test rather than silently landing in ``errors``.
    """
    task_without_property = {k: v for k, v in _TASK_PRODUCTION_SHAPE.items() if k != "Property"}
    factory = _make_factory(
        {
            ("GET", "/tasks/39517"): task_without_property,
            ("GET", "/tasks"): [],
        }
    )
    enricher = BuildiumTaskEnricher(api_factory=factory)

    result = await enricher.enrich(
        {"EventName": "Task.Created", "TaskId": 39517},
        COMPANY_ID,
    )

    assert result.property is None
    assert not result.errors
    paths = [path for _method, path, _q in factory.client.calls]  # type: ignore[attr-defined]
    assert not [p for p in paths if p.startswith("/rentals/")]


@pytest.mark.asyncio
async def test_the_property_reaches_the_serialized_enrichment() -> None:
    """Both consumers read the enrichment by dumping it whole.

    The planner does ``enriched.model_dump(exclude={"task": {"raw"}})`` and the
    summarizer ``json.dumps(enrichment)``, so a field that does not survive
    serialisation reaches neither -- and this is exactly the producer/consumer
    seam gotcha 28 is about.
    """
    factory = _make_factory(
        {
            ("GET", "/tasks/39517"): _TASK_PRODUCTION_SHAPE,
            ("GET", "/rentals/38038"): {"Id": 38038, "Name": "NH - 107 N Main St"},
            ("GET", "/tasks"): _HISTORY_AT_38038,
        }
    )
    enricher = BuildiumTaskEnricher(api_factory=factory)

    result = await enricher.enrich(
        {"EventName": "Task.Created", "TaskId": 39517},
        COMPANY_ID,
    )

    dumped = result.model_dump(mode="json", exclude={"task": {"raw"}})
    assert dumped["property"]["name"] == "NH - 107 N Main St"
