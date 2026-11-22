"""The Rent Manager golden cases (the 50x family), checked without a model.

The cases run as a Rent Manager workspace (``input.pms``), so the harness binds
the run the way production does: the Rent Manager run note, the specialist
reworded, the live Buildium tools hidden (``agent_flow.run_agent_flow_case``).
They are the read-side items of the design's "Golden chat evals" list: the
delinquency question, and each "not synced, never zero" item (an owner
statement; a lease's rent, which is not read while RM-11 is open).

What is pinned here:

* each case is well-formed for the harness, quarantined with gotcha 48's note,
  and says which assertion is SAFETY and which QUALITY;
* each runs bound as Rent Manager, and every tool it mocks or forbids is one
  the model is handed on that run: a mock or a bound on a tool the model
  cannot see measures nothing;
* its data is synthetic: invented names, ids in the fake 880 000 xxx range,
  no Rent Manager host, no other system named;
* every mocked answer is what the REAL tool returns on a Rent Manager company
  holding the same rows (rule 15: a mock is the tool's twin, or the case
  measures a fiction). Re-point a mock when its tool's wording changes; never
  relax the comparison.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agents import RunContextWrapper

from helixis.agents import mirror_tools, run_pms
from helixis.db import pms_coverage
from helixis.db.repositories.cockpit import PropertySummary
from helixis.db.repositories.mirror_sync_state import StageSync
from helixis.evals import agent_flow
from helixis.rentmanager import sync as rm_sync

REPO = Path(__file__).resolve().parents[2]
GOLDEN = REPO / "src" / "helixis" / "evals" / "golden"
FAMILY = sorted(GOLDEN.glob("50[0-9]_rentmanager_*.json"))

COMPANY = "00000000-0000-4000-8000-0000000000c1"
TODAY = date(2026, 10, 5)
FOOTER = "(from Occupella's synced property data, updated 2h ago)"

#: The workspace every case describes: two properties, seven active leases.
#: Rent is unknown on every lease (RM-11), as on every Rent Manager lease.
LEASES: list[dict[str, Any]] = [
    # The order the rent roll's SQL returns: rent (all NULL), property, unit.
    {
        "id": 880000611,
        "place": ("Harbor View Flats", "1A"),
        "who": "Avery Placeholder",
        "ends": date(2027, 2, 28),
    },
    {
        "id": 880000612,
        "place": ("Harbor View Flats", "1B"),
        "who": "Quinn Test",
        "ends": date(2026, 12, 31),
    },
    {
        "id": 880000601,
        "place": ("Harbor View Flats", "2B"),
        "who": "Marisol Example",
        "ends": date(2027, 5, 31),
    },
    {"id": 880000613, "place": ("Harbor View Flats", "3A"), "who": "Casey Demo", "ends": None},
    {
        "id": 880000614,
        "place": ("Lindenwood Court", "1"),
        "who": "Jordan Fictional",
        "ends": date(2026, 8, 31),
    },
    {
        "id": 880000615,
        "place": ("Lindenwood Court", "2"),
        "who": "Riley Mock",
        "ends": date(2027, 9, 30),
    },
    {
        "id": 880000607,
        "place": ("Lindenwood Court", "4"),
        "who": "Dev Sample",
        "ends": date(2027, 3, 31),
    },
]

#: The balance snapshot: largest first. Dev Sample's aging did not add up, so
#: it is unknown (mapper.aging), which the reader shows as no aging at all.
BALANCES: list[dict[str, Any]] = [
    {
        "lease_id": 880000601,
        "total_balance": Decimal("2450.00"),
        "balance_0_30": Decimal("1225.00"),
        "balance_31_60": Decimal("1225.00"),
        "balance_61_90": Decimal("0"),
        "balance_over_90": Decimal("0"),
        "snapshot_at": datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
        "property_name": "Harbor View Flats",
        "unit_number": "2B",
        "open_work_orders": 1,
        "open_tasks": 0,
        "tenant_names": "Marisol Example",
    },
    {
        "lease_id": 880000607,
        "total_balance": Decimal("675.50"),
        "balance_0_30": None,
        "balance_31_60": None,
        "balance_61_90": None,
        "balance_over_90": None,
        "snapshot_at": datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
        "property_name": "Lindenwood Court",
        "unit_number": "4",
        "open_work_orders": 0,
        "open_tasks": 0,
        "tenant_names": "Dev Sample",
    },
]

BILLS: list[dict[str, Any]] = [
    {
        "buildium_id": 880000801,
        "due_date": date(2026, 10, 20),
        "reference_number": "INV-2041",
        "total_amount": Decimal("480.00"),
        "vendor_name": "Bayside Plumbing Co",
        "memo": None,
        "properties": None,
    },
    {
        "buildium_id": 880000802,
        "due_date": date(2026, 11, 2),
        "reference_number": None,
        "total_amount": Decimal("1260.00"),
        "vendor_name": "Northline Roofing",
        "memo": None,
        "properties": None,
    },
]

#: No email or phone on anyone: no case needs them (the minimum a task needs),
#: and contact_lookup prints none when there are none.
OWNERS: list[dict[str, Any]] = [
    {
        "first_name": "Priya",
        "last_name": "Example",
        "company_name": None,
        "email": None,
        "phone": None,
    },
]
VENDORS: list[dict[str, Any]] = [
    {
        "name": "Bayside Plumbing Co",
        "company_name": None,
        "category_name": None,
        "email": None,
        "phone": None,
    },
    {
        "name": "Northline Roofing",
        "company_name": None,
        "category_name": None,
        "email": None,
        "phone": None,
    },
]

SUMMARIES = [
    PropertySummary(880000101, "Harbor View Flats", None, None, None, 5, 4, 1, 1, 1, 2450.0),
    PropertySummary(880000102, "Lindenwood Court", None, None, None, 4, 3, 0, 0, 1, 675.5),
]


def _lease_rows(_args: tuple[Any, ...]) -> list[dict[str, Any]]:
    owes = {b["lease_id"]: b["total_balance"] for b in BALANCES}
    return [
        {
            "buildium_id": lease["id"],
            "rent": None,
            "lease_to_date": datetime.combine(lease["ends"], datetime.min.time(), tzinfo=UTC)
            if lease["ends"]
            else None,
            "property_name": lease["place"][0],
            "unit_number": lease["place"][1],
            "total_balance": owes.get(lease["id"]),
            "tenant_names": lease["who"],
        }
        for lease in LEASES
    ]


def _matching(rows: list[dict[str, Any]], like: str, *fields: str) -> list[dict[str, Any]]:
    """The rows whose ``fields``, joined by spaces, contain the ILIKE text."""
    needle = like.strip("%").lower()
    return [r for r in rows if needle in " ".join(str(r[f] or "") for f in fields).lower()]


def _tenants(args: tuple[Any, ...]) -> list[dict[str, Any]]:
    """contact_lookup's tenant read: each tenant with the lease its LATERAL
    join picks (every lease here is active)."""
    rows = [
        {
            "first_name": lease["who"].split(" ", 1)[0],
            "last_name": lease["who"].split(" ", 1)[1],
            "email": None,
            "phone": None,
            "properties": lease["place"][0],
            "lease_status": "Active",
            "ended_on": lease["ends"],
            # Rent Manager's mapper writes every link it keeps as Current.
            "link_status": "Current",
        }
        for lease in LEASES
    ]
    return _matching(rows, args[1], "first_name", "last_name")


#: Each read is told apart by its main table and alias. The word boundary is
#: load-bearing: the rent roll's balance subquery reads "...balances bal".
_READS: tuple[tuple[re.Pattern[str], Callable[[tuple[Any, ...]], list[dict[str, Any]]]], ...] = (
    (re.compile(r"FROM helixis_lease_balances b\b"), lambda _a: BALANCES),
    (re.compile(r"FROM helixis_leases l\b"), _lease_rows),
    (re.compile(r"FROM helixis_bills b\b"), lambda _a: BILLS),
    (re.compile(r"FROM helixis_tenants t\b"), _tenants),
    (
        re.compile(r"FROM helixis_owners\b"),
        lambda a: _matching(OWNERS, a[1], "first_name", "last_name", "company_name"),
    ),
    (
        re.compile(r"FROM helixis_vendors\b"),
        lambda a: _matching(VENDORS, a[1], "name", "company_name"),
    ),
)


class _Conn:
    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        hits = [rows for pattern, rows in _READS if pattern.search(sql)]
        assert len(hits) == 1, f"a read this stand-in does not model: {sql[:80]}"
        return hits[0](args)

    async def fetchrow(self, sql: str, *_args: Any) -> dict[str, Any]:
        assert "COUNT(*) AS active" in sql, sql[:80]
        return {"active": len(LEASES)}


class _Acquire:
    async def __aenter__(self) -> _Conn:
        return _Conn()

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _FixedNow(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


@contextmanager
def rent_manager_company() -> Iterator[None]:
    """The real mirror tools, on a Rent Manager company whose stages have all
    landed, reading the rows above. Only the database and the clock are faked."""
    landed = {
        stage: datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
        for stage in rm_sync.SYNC_STATE_STAGE.values()
    }
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _Acquire())
    ctx = MagicMock()
    ctx.company_id = COMPANY
    with (
        patch.object(pms_coverage, "company_pms_strict", AsyncMock(return_value="rentmanager")),
        patch.object(pms_coverage, "get_freshness", AsyncMock(return_value=landed)),
        patch.object(mirror_tools, "current_memory_context", return_value=ctx),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(mirror_tools, "_freshness_line", AsyncMock(return_value=FOOTER)),
        patch.object(
            mirror_tools.sync_state,
            "get_stage_sync",
            AsyncMock(return_value=StageSync(synced_at=landed["leases"], row_count=7)),
        ),
        patch.object(mirror_tools, "company_timezone", AsyncMock(return_value="UTC")),
        patch.object(mirror_tools, "today_in", return_value=TODAY),
        patch.object(
            mirror_tools.cockpit, "list_property_summaries", AsyncMock(return_value=SUMMARIES)
        ),
        patch.object(mirror_tools, "_paid_bill_count", AsyncMock(return_value=4)),
        patch.object(mirror_tools.entity_aliases, "lookup_alias", AsyncMock(return_value=None)),
        patch.object(mirror_tools, "datetime", _FixedNow),
    ):
        yield


#: How each mocked tool is asked. A mock answers whatever the model asks, so
#: each is pinned to the question its case puts: the whole rent roll, the last
#: 30 days of spending, the ledger of the tenant the cases name.
ASKED: dict[str, tuple[Any, ...]] = {
    "delinquency_report": (),
    "rent_roll": ("",),
    "unpaid_bills": (),
    "portfolio_overview": (),
    "bank_balances": (),
    "spending_report": (30, ""),
    "lease_payment_history": ("Marisol Example",),
}

#: contact_lookup is asked for the person its own answer names.
_LOOKUP_HEAD = re.compile(r"\APeople matching '([^']+)':\n")


def asked(tool: str, mock: str) -> tuple[Any, ...]:
    if tool == "contact_lookup":
        head = _LOOKUP_HEAD.match(mock)
        assert head, f"a contact_lookup mock that names no query: {mock[:60]!r}"
        return (head.group(1),)
    assert tool in ASKED, f"{tool} is mocked but not modelled here; model it"
    return ASKED[tool]


async def real_answer(tool: str, *args: Any) -> str:
    """What ``tool`` says on that company."""
    impl = getattr(mirror_tools, f"_{tool}_impl")
    with rent_manager_company():
        return str(await impl(*args))


def _cases() -> list[tuple[Path, dict[str, Any]]]:
    return [(p, json.loads(p.read_text(encoding="utf-8"))) for p in FAMILY]


def _returns(case: dict[str, Any]) -> dict[str, list[str]]:
    mocks = case["input"].get("tool_mocks") or {}
    return {tool: [str(e["return"]) for e in entries] for tool, entries in mocks.items()}


def test_the_family_is_found() -> None:
    """A scan over nothing passes everything (rule 10)."""
    assert [p.name[:3] for p in FAMILY] == ["500", "501", "502"], [p.name for p in FAMILY]


@pytest.mark.parametrize(("path", "case"), _cases(), ids=[p.stem for p in FAMILY])
def test_each_case_runs_as_rent_manager_quarantined_and_explained(
    path: Path, case: dict[str, Any]
) -> None:
    assert case["id"] == path.stem
    assert case["case_type"] == "agent_flow"
    assert case["requires_env"] == ["HELIXIS_LLM__ANTHROPIC_API_KEY"]
    assert case["input"]["pms"] == "rentmanager"
    assert case["input"]["pms"] in agent_flow._RUN_PMS_VALUES
    assert case["input"]["agent"] in agent_flow._AGENT_FACTORIES
    assert case["single_run_gate"] is False
    assert "gotcha 48" in case["_single_run_gate_note"]
    assert set(_returns(case)) <= set(agent_flow._TOOL_MOCK_TARGETS)
    notes = case["_assertion_notes"]
    graded = [k for k in case["expected"] if k != "reply_must_not_be_empty"]
    for key in graded:
        assert key in notes, f"{path.name}: {key} has no note saying SAFETY or QUALITY"
        assert ("SAFETY" in notes[key]) != ("QUALITY" in notes[key]), (path.name, key)
    assert any("SAFETY" in notes[k] for k in graded), f"{path.name}: no safety bound"
    assert case["expected"].get("reply_must_not_be_empty") is True


class _LookedError(Exception):
    """Raised by the stand-in for the model once it has seen what it is handed."""


@pytest.mark.parametrize(("path", "case"), _cases(), ids=[p.stem for p in FAMILY])
async def test_each_case_is_bound_as_rent_manager_with_its_tools_in_reach(
    path: Path, case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def look(agent: Any, *, input: Any, **_kw: Any) -> Any:  # noqa: A002
        tools = await agent.get_all_tools(RunContextWrapper(context=None))
        seen["tools"] = {t.name for t in tools}
        seen["pms"] = run_pms.run_pms_spec()
        seen["input"] = input
        seen["instructions"] = str(agent.instructions)
        raise _LookedError("looked")

    monkeypatch.setattr(agent_flow.Runner, "run", look)
    result = await agent_flow.run_agent_flow_case(case)

    assert result.error == "_LookedError: looked", (path.name, result.error)
    assert seen["pms"] is not None and seen["pms"].key == "rentmanager"
    # The connector's own note, not the not-built one.
    assert seen["input"] == case["input"]["user_message"] + rm_sync.RUN_NOTE
    assert "Rent Manager Agent" in seen["instructions"]
    assert not any(name.startswith("buildium_") for name in seen["tools"])
    unseen = set(_returns(case)) - seen["tools"]
    assert not unseen, f"{path.name}: mocks for tools the model is not handed: {unseen}"
    forbidden = set(case["expected"].get("tool_call_trail_must_not_contain") or [])
    assert forbidden <= seen["tools"], f"{path.name}: a bound on an unseen tool is vacuous"
    assert run_pms.run_pms_spec() is None, "the binding leaked out of the case"


def test_the_data_is_synthetic_and_names_no_other_system() -> None:
    for path, case in _cases():
        text = json.dumps(case["input"])
        assert not re.search(r"\.api\.rentmanager\.com", text, re.IGNORECASE), path.name
        ids = {int(m) for m in re.findall(r"(?<!\d)(\d{9})(?!\d)", text)}
        assert all(880_000_000 <= i < 881_000_000 for i in ids), (path.name, ids)
        assert "@" not in text, f"{path.name}: no case needs an email address"
        for returns in _returns(case).values():
            for answer in returns:
                assert not re.search(r"Buildium|Rentvine|Propertyware", answer), path.name


async def test_every_mocked_answer_is_what_the_real_tool_says() -> None:
    """Rule 15: each mock equals the real tool's answer on a Rent Manager
    company with these rows, asked what the case asks."""
    compared: dict[str, int] = {}
    for path, case in _cases():
        for tool, returns in _returns(case).items():
            for answer in returns:
                args = asked(tool, answer)
                if tool == "contact_lookup":
                    assert args[0] in case["input"]["user_message"], (path.name, args)
                assert answer == await real_answer(tool, *args), f"{path.name}: {tool} drifted"
                compared[path.name] = compared.get(path.name, 0) + 1
    assert set(compared) == {p.name for p in FAMILY}, "a case compared nothing"


async def test_the_rent_roll_this_family_mocks_reads_no_rent_and_no_zero_total() -> None:
    """The reason the rent case exists: no Rent Manager lease's rent is read
    (RM-11), so the roll says so on every lease and gives no total. Before
    e186862b its head read "$0 a month in rent across the 0 whose rent is
    known" (rule 14). The cases hold the reply to never say $0 whatever a tool
    prints; this pins what the mocks now carry."""
    roll = await real_answer("rent_roll", *ASKED["rent_roll"])
    assert "No lease's rent has been read yet, so there is no rent total" in roll
    assert "$0" not in roll
    assert roll.count("rent not read yet") == len(LEASES)


async def test_the_not_synced_answers_name_rent_manager_and_where_to_look() -> None:
    for tool in ("spending_report", "lease_payment_history", "bank_balances"):
        answer = await real_answer(tool, *ASKED[tool])
        assert answer.startswith("Occupella doesn't sync "), (tool, answer)
        assert answer.endswith(" in Rent Manager."), (tool, answer)


def test_each_never_say_needle_gates_a_plain_claim() -> None:
    """The notes call each ``reply_must_not_contain`` needle SAFETY; the
    harness agrees for a reply that states it (one that quotes it in order to
    reject it re-rolls, by design)."""
    needles = [
        n for _p, case in _cases() for n in case["expected"].get("reply_must_not_contain") or []
    ]
    assert needles, "no never-say needle found; the scan is broken"
    for needle in needles:
        assert agent_flow._forbidden_prohibition_criticality(f"It is {needle}.", needle), needle
