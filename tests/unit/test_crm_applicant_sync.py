"""Unit tests for the CRM ↔ Buildium applicant sync (webhooks/crm_applicant_sync.py).

Pins the auto-move contract: the pure stage mapping (forward-only, never a
demotion, Rejected/Cancelled never move, AddedToLease → leased), the
match order (sticky link → email → phone), the lost-lead guard, and the
never-raises promise (the sync is an enrichment on the mirror write).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.buildium.schemas import Applicant
from helixis.db.repositories.leads import LeadRow
from helixis.webhooks import crm_applicant_sync as sync
from helixis.webhooks.crm_applicant_sync import (
    create_stage_for_applicant_status,
    stage_for_applicant_status,
    sync_lead_from_applicant,
)

# ── stage mapping (pure) ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "current", "expected"),
    [
        # An applicant exists → the lead applied (from any earlier stage).
        ("Undecided", "new", "applied"),
        ("New", "contacted", "applied"),
        ("Approved", "scheduled", "applied"),
        (None, "new", "applied"),
        # A draft lease is NOT leased yet.
        ("AddedToDraftLease", "new", "applied"),
        # On a lease → leased, from anywhere earlier.
        ("AddedToLease", "applied", "leased"),
        ("AddedToLease", "new", "leased"),
        # Forward-only: no self-move, never a demotion.
        ("Undecided", "applied", None),
        ("Undecided", "leased", None),
        ("AddedToLease", "leased", None),
        # Out of the running → link only, no move (the PM decides).
        ("Rejected", "new", None),
        ("Cancelled", "scheduled", None),
        # Unknown current stage never moves.
        ("Undecided", "weird", None),
    ],
)
def test_stage_for_applicant_status(status: str | None, current: str, expected: str | None) -> None:
    assert stage_for_applicant_status(status, current) == expected


# ── sync (mocked repos) ──────────────────────────────────────────────


def _applicant(**over: object) -> Applicant:
    payload: dict[str, object] = {
        "Id": 900,
        "FirstName": "Priya",
        "LastName": "Natarajan",
        "Email": "priya@example.com",
        "Status": "Undecided",
        "PhoneNumbers": [{"Number": "+15551234567", "Type": "Cell"}],
    }
    payload.update(over)
    return Applicant.model_validate(payload)


def _lead(**over: object) -> LeadRow:
    base: dict[str, object] = dict(
        id="lead-1",
        company_id="co-1",
        name="Priya Natarajan",
        kind="renter",
        stage="contacted",
        source="email",
        status="active",
        email="priya@example.com",
    )
    base.update(over)
    return LeadRow(**base)  # type: ignore[arg-type]


def _repo_mocks(
    *,
    by_applicant: LeadRow | None = None,
    by_email: LeadRow | None = None,
    by_phone: LeadRow | None = None,
) -> dict[str, AsyncMock]:
    return {
        "find_lead_by_applicant": AsyncMock(return_value=by_applicant),
        "find_lead_by_email": AsyncMock(return_value=by_email),
        "find_lead_by_phone": AsyncMock(return_value=by_phone),
        "link_lead_applicant": AsyncMock(),
        "update_stage": AsyncMock(),
        "insert_lead": AsyncMock(return_value="new-lead-9"),
    }


async def _run(
    applicant: Applicant,
    mocks: dict[str, AsyncMock],
    activity: AsyncMock | None = None,
    *,
    allow_create: bool = False,
) -> str | None:
    with (
        patch.multiple(sync.leads_repo, **mocks),
        patch.object(sync, "write_activity", activity or AsyncMock()),
    ):
        return await sync_lead_from_applicant(
            MagicMock(),
            applicant,
            company_id="co-1",
            account_id=7,
            allow_create=allow_create,
        )


@pytest.mark.asyncio
async def test_email_match_links_moves_and_logs() -> None:
    mocks = _repo_mocks(by_email=_lead())
    activity = AsyncMock()
    result = await _run(_applicant(), mocks, activity)
    assert result is not None and "Applied" in result
    mocks["link_lead_applicant"].assert_awaited_once()
    link_kwargs = mocks["link_lead_applicant"].await_args.kwargs
    assert link_kwargs["lead_id"] == "lead-1"
    assert link_kwargs["buildium_applicant_id"] == 900
    assert mocks["update_stage"].await_args.kwargs["stage"] == "applied"
    activity.assert_awaited_once()
    entry = activity.await_args.args[1]
    assert entry.action_type == "crm_stage_moved"
    assert entry.raw["to_stage"] == "applied"


@pytest.mark.asyncio
async def test_sticky_link_wins_over_email() -> None:
    mocks = _repo_mocks(by_applicant=_lead(buildium_applicant_id=900))
    result = await _run(_applicant(), mocks)
    assert result is not None
    mocks["find_lead_by_email"].assert_not_awaited()
    # Already linked — no relink write.
    mocks["link_lead_applicant"].assert_not_awaited()


@pytest.mark.asyncio
async def test_phone_fallback_when_no_email_match() -> None:
    mocks = _repo_mocks(by_phone=_lead(email=None, phone="+15551234567"))
    result = await _run(_applicant(), mocks)
    assert result is not None
    mocks["find_lead_by_phone"].assert_awaited_once()
    assert mocks["find_lead_by_phone"].await_args.kwargs["phone"] == "+15551234567"


@pytest.mark.asyncio
async def test_added_to_lease_moves_to_leased() -> None:
    mocks = _repo_mocks(by_email=_lead(stage="applied"))
    result = await _run(_applicant(Status="AddedToLease"), mocks)
    assert result is not None and "Leased" in result
    assert mocks["update_stage"].await_args.kwargs["stage"] == "leased"


@pytest.mark.asyncio
async def test_rejected_links_but_never_moves() -> None:
    mocks = _repo_mocks(by_email=_lead())
    activity = AsyncMock()
    result = await _run(_applicant(Status="Rejected"), mocks, activity)
    assert result is None
    mocks["link_lead_applicant"].assert_awaited_once()
    mocks["update_stage"].assert_not_awaited()
    activity.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_lead_links_but_never_moves() -> None:
    mocks = _repo_mocks(by_email=_lead(status="lost"))
    result = await _run(_applicant(), mocks)
    assert result is None
    mocks["link_lead_applicant"].assert_awaited_once()
    mocks["update_stage"].assert_not_awaited()


@pytest.mark.asyncio
async def test_no_match_is_a_quiet_noop() -> None:
    # Default (backfill) path: no match + no allow_create → link/move-only,
    # never creates a lead (so a historical import can't mass-create).
    mocks = _repo_mocks()
    result = await _run(_applicant(), mocks)
    assert result is None
    mocks["update_stage"].assert_not_awaited()
    mocks["insert_lead"].assert_not_awaited()


# ── create-or-move (webhook allow_create path) ───────────────────────


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("Undecided", "applied"),
        ("New", "applied"),
        (None, "applied"),
        ("Rejected", "applied"),  # they DID apply; PM marks lost from the board
        ("Cancelled", "applied"),
        ("AddedToDraftLease", "applied"),
        ("AddedToLease", "leased"),
    ],
)
def test_create_stage_for_applicant_status(status: str | None, expected: str) -> None:
    assert create_stage_for_applicant_status(status) == expected


@pytest.mark.asyncio
async def test_no_match_creates_lead_when_allow_create() -> None:
    # Webhook path: an applicant who applied directly in Buildium with no
    # prior lead gets a Leasing lead created, tagged source='buildium'.
    mocks = _repo_mocks()
    activity = AsyncMock()
    result = await _run(_applicant(), mocks, activity, allow_create=True)
    assert result is not None and "Added" in result and "Applied" in result

    mocks["insert_lead"].assert_awaited_once()
    new_lead = mocks["insert_lead"].await_args.args[1]
    assert new_lead.source == "buildium"
    assert new_lead.kind == "renter"
    assert new_lead.stage == "applied"
    assert new_lead.name == "Priya Natarajan"
    assert new_lead.email == "priya@example.com"
    assert new_lead.phone == "+15551234567"

    # Sticky link so the next webhook moves (not re-creates) this lead.
    link_kwargs = mocks["link_lead_applicant"].await_args.kwargs
    assert link_kwargs["lead_id"] == "new-lead-9"
    assert link_kwargs["buildium_applicant_id"] == 900

    entry = activity.await_args.args[1]
    assert entry.action_type == "crm_lead_created"
    assert entry.raw["to_stage"] == "applied"
    # A move must NOT happen on the create path.
    mocks["update_stage"].assert_not_awaited()


@pytest.mark.asyncio
async def test_create_added_to_lease_starts_leased() -> None:
    mocks = _repo_mocks()
    result = await _run(_applicant(Status="AddedToLease"), mocks, allow_create=True)
    assert result is not None and "Leased" in result
    assert mocks["insert_lead"].await_args.args[1].stage == "leased"


@pytest.mark.asyncio
async def test_create_falls_back_to_id_name_when_unnamed() -> None:
    mocks = _repo_mocks()
    unnamed = _applicant(FirstName=None, LastName=None, Email=None, PhoneNumbers=None)
    result = await _run(unnamed, mocks, allow_create=True)
    assert result is not None
    assert mocks["insert_lead"].await_args.args[1].name == "Applicant #900"


@pytest.mark.asyncio
async def test_create_skipped_when_insert_fails() -> None:
    # insert_lead is best-effort (returns None on failure); no link/activity
    # should follow a failed insert.
    mocks = _repo_mocks()
    mocks["insert_lead"] = AsyncMock(return_value=None)
    activity = AsyncMock()
    result = await _run(_applicant(), mocks, activity, allow_create=True)
    assert result is None
    mocks["link_lead_applicant"].assert_not_awaited()
    activity.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_match_moves_not_creates_even_with_allow_create() -> None:
    # allow_create only fires when NOTHING matches — a matched lead still
    # moves, never spawns a duplicate.
    mocks = _repo_mocks(by_email=_lead())
    result = await _run(_applicant(), mocks, allow_create=True)
    assert result is not None
    mocks["insert_lead"].assert_not_awaited()
    mocks["update_stage"].assert_awaited_once()


@pytest.mark.asyncio
async def test_repo_crash_never_raises() -> None:
    mocks = _repo_mocks()
    mocks["find_lead_by_applicant"] = AsyncMock(side_effect=RuntimeError("db down"))
    result = await _run(_applicant(), mocks)
    assert result is None
