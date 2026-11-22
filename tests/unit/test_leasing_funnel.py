"""The leasing pipeline existed for the CRM page and not for the agent.

``leads.py`` computes stage counts, month-over-month intake, leased totals and
a going-cold list. All of it was reachable only from the CRM REST route, so
"how's leasing going", "how many applications this month" and "who needs a
follow-up" had no rollup at all.

⚠ **The interesting part is what an EMPTY board means, and it took a
measurement to get right.** Live-verified 2026-08-29: 28 applicants exist
across the three companies and **0 leads carry ``source='buildium'``**. That
reads exactly like a broken applicant→lead sync, and it is not one:
``sync_lead_from_applicant`` takes ``allow_create=False`` from the BACKFILL by
design — its own comment says a historical import must stay link/move-only and
"can't mass-create a lead for every past applicant" — while only the live
applicant webhook creates. So a company whose applicants were imported
historically has a legitimately empty board.

I was one step from filing that as a defect. It is gotcha 47's lesson exactly:
correct behaviour and broken behaviour look identical from the outside, and
only checking the other half distinguishes them. The tool has to make the same
distinction the diagnosis did, or it tells a property manager they have no
leasing activity while seven applicants sit in their Buildium.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from helixis.agents import mirror_tools
from helixis.agents.mirror_tools import _leasing_funnel_impl
from helixis.db.pms_coverage import PMS_NAME, Coverage
from helixis.pms import registry


@dataclass
class _Metrics:
    new_this_month: int = 4
    new_prev_month: int = 2
    replies_this_month: int = 1
    active: int = 6
    leased: int = 3
    total: int = 9


@dataclass
class _Lead:
    name: str | None = "Dana Reed"
    email: str | None = None
    phone: str | None = None
    stage: str = "contacted"


async def _run(
    *,
    by_stage: dict[str, int] | None = None,
    metrics: Any = None,
    cold: list[_Lead] | None = None,
    applicants: int = 0,
    boom: bool = False,
) -> str:
    repo = AsyncMock()
    if boom:
        repo.count_leads_by_stage.side_effect = RuntimeError("db down")
    else:
        repo.count_leads_by_stage.return_value = by_stage or {}
    repo.lead_metrics.return_value = metrics if metrics is not None else _Metrics()
    repo.cold_leads.return_value = cold or []
    repo.LEAD_STAGES = ("new", "contacted", "scheduled", "applied", "leased")

    class _Conn:
        async def fetchval(self, *a: object, **k: object) -> int:
            return applicants

    class _Pool:
        def acquire(self) -> object:
            class _Ctx:
                async def __aenter__(self) -> _Conn:
                    return _Conn()

                async def __aexit__(self, *exc: object) -> bool:
                    return False

            return _Ctx()

    with (
        patch.object(mirror_tools, "_company_id", return_value="co-1"),
        patch.object(mirror_tools, "get_pool", return_value=_Pool()),
        patch.object(mirror_tools, "_freshness_line", AsyncMock(return_value="(synced)")),
        patch("helixis.db.repositories.leads.count_leads_by_stage", repo.count_leads_by_stage),
        patch("helixis.db.repositories.leads.lead_metrics", repo.lead_metrics),
        patch("helixis.db.repositories.leads.cold_leads", repo.cold_leads),
        patch(
            "helixis.db.repositories.leads.LEAD_STAGES",
            ("new", "contacted", "scheduled", "applied", "leased"),
        ),
    ):
        return await _leasing_funnel_impl()


# ── the empty board, which is the whole point ────────────────────────


@pytest.mark.asyncio
async def test_an_empty_board_with_applicants_explains_itself() -> None:
    """⚠ THE finding. Blue Ridge has 7 applicants and 0 leads, and that is
    correct: the backfill links rather than creates, by design. Reporting "no
    leasing activity" would be a false claim about somebody's business."""
    out = await _run(metrics=_Metrics(total=0), applicants=7)
    assert "7 applicant(s) ARE mirrored" in out
    assert "expected rather than broken" in out
    assert "do not report the company as having no leasing activity" in out.lower()


@pytest.mark.asyncio
async def test_a_buildium_company_keeps_the_backfill_explanation() -> None:
    buildium = AsyncMock(return_value=Coverage(registry.BUILDIUM))
    with patch.object(mirror_tools, "coverage_for", buildium):
        out = await _run(metrics=_Metrics(total=0), applicants=7)
    assert "7 applicant(s) ARE mirrored from Buildium" in out


@pytest.mark.asyncio
@pytest.mark.parametrize("pms", sorted(set(registry.PMS) - {registry.BUILDIUM}))
async def test_another_systems_applicants_are_named_as_its_own(pms: str) -> None:
    """Nothing turns another system's applications into leads, so "mirrored
    from Buildium" and the webhook story were false for every one of them
    (2026-10-07, found when DoorLoop began syncing applicants)."""
    with patch.object(mirror_tools, "coverage_for", AsyncMock(return_value=Coverage(pms))):
        out = await _run(metrics=_Metrics(total=0), applicants=4)
    assert f"4 applicant(s) ARE synced from {PMS_NAME[pms]}" in out
    assert "buildium" not in out.lower() and "webhook" not in out.lower()
    assert "do not report the company as having no leasing activity" in out.lower()


def test_only_buildium_turns_applicants_into_leads() -> None:
    """The other side of the message above (rule 15). Every call that turns an
    applicant into a lead is Buildium's backfill or Buildium's webhook; a
    caller for another system makes "Occupella does not turn X applications
    into pipeline leads" false, and this fails first."""
    import ast
    from pathlib import Path

    import helixis

    root = Path(helixis.__file__).parent
    callers = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "sync_lead_from_applicant":
                callers.add(path.relative_to(root).as_posix())
    assert callers == {"buildium/backfill.py", "webhooks/handlers/applicants.py"}


@pytest.mark.asyncio
async def test_an_empty_board_with_no_applicants_says_so_plainly() -> None:
    """Both directions. When there really is nothing, the elaborate caveat
    above would be noise — and a caveat that fires when it does not apply
    teaches the reader to skip it.

    Re-pointed 2026-10-02: "nothing" now also needs the applicants stage to
    have landed (a refused read leaves the table empty too)."""
    from datetime import UTC, datetime
    from unittest.mock import patch

    from helixis.agents import mirror_tools
    from helixis.db.repositories.mirror_sync_state import StageSync

    stamped = StageSync(synced_at=datetime.now(UTC), row_count=0)
    with patch.object(mirror_tools.sync_state, "get_stage_sync", AsyncMock(return_value=stamped)):
        out = await _run(metrics=_Metrics(total=0), applicants=0)
    assert "genuinely no leasing pipeline" in out
    assert "expected rather than broken" not in out


@pytest.mark.asyncio
async def test_an_empty_board_before_applications_ever_synced_makes_no_claim() -> None:
    from unittest.mock import patch

    from helixis.agents import mirror_tools

    with patch.object(mirror_tools.sync_state, "get_stage_sync", AsyncMock(return_value=None)):
        out = await _run(metrics=_Metrics(total=0), applicants=0)
    assert "genuinely no leasing pipeline" not in out
    assert "haven't synced" in out


@pytest.mark.asyncio
async def test_a_populated_board_makes_no_empty_claim() -> None:
    out = await _run(by_stage={"new": 2, "applied": 1})
    assert "genuinely no leasing pipeline" not in out
    assert "ARE mirrored" not in out


# ── the funnel itself ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_stage_appears_even_at_zero() -> None:
    """A stage missing from the output reads as a stage that does not exist.
    The repo already guarantees no holes; the rendering must not reintroduce
    them by skipping falsy counts."""
    out = await _run(by_stage={"new": 2})
    for stage in ("New", "Contacted", "Scheduled", "Applied", "Leased"):
        assert stage in out, f"{stage} missing — a hole in the funnel reads as an absent stage"


@pytest.mark.asyncio
async def test_the_month_comparison_carries_both_months() -> None:
    """ "4 new this month" is not an answer to "how's leasing going" without
    what it is being compared against — a single number invites a trend claim
    that nothing supports."""
    out = await _run(metrics=_Metrics(new_this_month=4, new_prev_month=2))
    assert "New this month: 4" in out
    assert "previous month 2" in out


@pytest.mark.asyncio
async def test_going_cold_leads_are_named() -> None:
    """ "3 going cold" is not actionable; the names are."""
    out = await _run(cold=[_Lead(name="Dana Reed"), _Lead(name="Priya N", stage="new")])
    assert "Dana Reed" in out and "Priya N" in out
    assert "going cold" in out


@pytest.mark.asyncio
async def test_no_cold_leads_produces_no_empty_section() -> None:
    """An empty "going cold" heading reads as a finding with the rows lost."""
    assert "going cold" not in await _run(cold=[])


@pytest.mark.asyncio
async def test_the_active_versus_lifetime_distinction_is_stated() -> None:
    """⚠ The stage breakdown counts ACTIVE leads and the lifetime total counts
    everything, so the columns do not add up — and an unexplained mismatch in
    a funnel is what somebody reports as a bug, or worse, reconciles by
    inventing an explanation."""
    out = await _run(by_stage={"new": 2})
    assert "lost leads are excluded" in out


# ── our failure is never their empty pipeline ────────────────────────


@pytest.mark.asyncio
async def test_a_query_failure_does_not_report_an_empty_pipeline() -> None:
    out = await _run(boom=True)
    assert "error on OUR side" in out
    assert "Do NOT report the company as having no leads" in out


# ── wiring ───────────────────────────────────────────────────────────


def test_the_rollup_is_registered_and_memoized() -> None:
    """A mirror read missing from ``_MIRROR_READS`` has no memo, no cap and no
    soft-deadline channel."""
    from helixis.agents.call_budget import _MIRROR_READS

    assert "leasing_funnel" in {t.name for t in mirror_tools.mirror_tools}
    assert "leasing_funnel" in _MIRROR_READS
