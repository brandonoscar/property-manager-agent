"""The payment grace window, and the warning that precedes every cutoff.

`past_due_since` was written by two webhook handlers since billing shipped and
read by NOBODY: `load_state` did not even select it, and `past_due` sits in
`_SERVING_STATUSES`. So on a Stripe account configured to leave a failed
subscription `past_due` — which is a dashboard toggle no test and no grep can
see — a customer whose card died kept the whole product, free, forever.

The other half is that nothing ever WARNED. Both the trial clock and the
delinquency were enforced in silence, so the first a customer knew of either
was a refusal mid-question.

⚠ Read both directions on every rule here (gotcha 38). A grace window that
never fires is the bug being fixed; one that fires early cuts off a customer
while their bank is still retrying, which is worse than the bug.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime, timedelta

from helixis.billing.gate import (
    PAYMENT_GRACE_DAYS,
    TRIAL_NOTICE_DAYS,
    BillingState,
    check_turn_allowed,
    load_state,
)
from helixis.billing.notices import notice_for
from helixis.billing.plans import PLANS


def _state(
    *,
    plan: str = "starter",
    status: str = "active",
    past_due_since: datetime | None = None,
    trial_ends_at: datetime | None = None,
    turns_used: int = 0,
) -> BillingState:
    return BillingState(
        plan=PLANS[plan],
        status=status,
        seats=1,
        turns_used=turns_used,
        turns_allowed=1000,
        trial_ends_at=trial_ends_at,
        period_end=None,
        past_due=status == "past_due",
        past_due_since=past_due_since,
    )


def _ago(days: float) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _ahead(days: float) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


# ── the grace window ─────────────────────────────────────────────────


def test_a_fresh_payment_failure_keeps_full_service() -> None:
    """Rule 2 still holds for the window it was written for. Cutting service
    the hour an invoice fails punishes the customer for their bank."""
    state = _state(status="past_due", past_due_since=_ago(1))
    assert state.grace_days_left == PAYMENT_GRACE_DAYS - 1


def test_service_stops_once_the_window_is_spent() -> None:
    """The defect. Without this the company is served indefinitely and no
    error, log line or failing test says so — gotcha 81's shape, where a gap
    plus a permissive default is invisible by construction."""
    state = _state(status="past_due", past_due_since=_ago(PAYMENT_GRACE_DAYS + 1))
    assert state.grace_days_left == 0


def test_the_window_is_longer_than_stripes_own_retry_schedule() -> None:
    """This is a BACKSTOP, not a competing dunning policy. Stripe's Smart
    Retries run ~4 attempts over 2-3 weeks; a window shorter than that starts
    cutting people off mid-retry, which is the failure mode rule 2 exists to
    prevent. If someone shortens this, they are changing policy, not tuning."""
    assert PAYMENT_GRACE_DAYS >= 21


def test_a_working_card_has_no_countdown() -> None:
    assert _state().grace_days_left is None


def test_a_delinquency_with_no_recorded_start_has_no_countdown() -> None:
    """⚠ The safe direction, and it is reachable: a row that went `past_due`
    before `past_due_since` was populated has the status and not the date.
    Counting from `now` would give that company a fresh 21 days — generous but
    honest — while counting from epoch would lock them out instantly on the
    strength of a NULL. Decline to assert (gotcha 55)."""
    assert _state(status="past_due", past_due_since=None).grace_days_left is None


# ── what the gate does with it ───────────────────────────────────────


async def test_the_gate_refuses_once_the_grace_window_is_spent() -> None:
    """Wired, not merely computed — gotcha 62's rule. A property nothing reads
    is the `comms_triage` failure."""
    import helixis.billing.gate as gate_mod

    async def _fake(_company_id: str) -> BillingState:
        return _state(status="past_due", past_due_since=_ago(PAYMENT_GRACE_DAYS + 2))

    original = gate_mod.load_state
    gate_mod.load_state = _fake  # type: ignore[assignment]
    try:
        block = await check_turn_allowed("c1")
    finally:
        gate_mod.load_state = original  # type: ignore[assignment]

    assert block is not None
    assert block.reason == "payment_failed"
    # Names the fix. A refusal with blame and no exit is the shape gate.py's
    # own BillingBlock docstring forbids.
    assert "Billing" in block.message


async def test_the_gate_still_serves_inside_the_window() -> None:
    import helixis.billing.gate as gate_mod

    async def _fake(_company_id: str) -> BillingState:
        return _state(status="past_due", past_due_since=_ago(2))

    original = gate_mod.load_state
    gate_mod.load_state = _fake  # type: ignore[assignment]
    try:
        assert await check_turn_allowed("c1") is None
    finally:
        gate_mod.load_state = original  # type: ignore[assignment]


async def test_payment_failure_is_reported_before_running_out_of_turns() -> None:
    """Order matters: 'your card failed' and 'you're out of requests' need
    OPPOSITE actions from the reader, and telling somebody to buy more turns
    when their card is declined sends them to a page that cannot help."""
    import helixis.billing.gate as gate_mod

    async def _fake(_company_id: str) -> BillingState:
        return BillingState(
            plan=PLANS["starter"],
            status="past_due",
            seats=1,
            turns_used=99_999,  # also exhausted
            turns_allowed=10,
            trial_ends_at=None,
            period_end=None,
            past_due=True,
            past_due_since=_ago(PAYMENT_GRACE_DAYS + 5),
        )

    original = gate_mod.load_state
    gate_mod.load_state = _fake  # type: ignore[assignment]
    try:
        block = await check_turn_allowed("c1")
    finally:
        gate_mod.load_state = original  # type: ignore[assignment]

    assert block is not None
    assert block.reason == "payment_failed"


# ── the warning that comes first ─────────────────────────────────────


def test_a_healthy_paid_plan_is_told_nothing() -> None:
    """A banner that is always there stops being read."""
    assert notice_for(_state()) is None


def test_a_failing_card_warns_with_the_days_remaining() -> None:
    notice = notice_for(_state(status="past_due", past_due_since=_ago(1)))
    assert notice is not None
    assert notice.reason == "payment_failed"
    assert notice.severity == "critical"
    # The DEADLINE has to be in the sentence. "Update your card" with no clock
    # is the copy that was already there and that nobody acted on.
    assert f"{PAYMENT_GRACE_DAYS - 1} more days" in notice.message


def test_the_last_day_of_grace_is_not_pluralised() -> None:
    """'1 days' is the tell that nobody read the string they shipped."""
    notice = notice_for(_state(status="past_due", past_due_since=_ago(PAYMENT_GRACE_DAYS - 1)))
    assert notice is not None
    assert "1 more day" in notice.message
    assert "1 more days" not in notice.message


def test_a_spent_window_produces_no_notice_because_the_block_speaks() -> None:
    """At zero days the gate REFUSES, and `block_message` is what the user
    reads. A warning beside a refusal is two authors on one screen."""
    assert notice_for(_state(status="past_due", past_due_since=_ago(PAYMENT_GRACE_DAYS))) is None


def test_a_trial_warns_only_near_the_end() -> None:
    early = notice_for(_state(plan="trial", status="trialing", trial_ends_at=_ahead(13)))
    assert early is None

    late = notice_for(
        _state(plan="trial", status="trialing", trial_ends_at=_ahead(TRIAL_NOTICE_DAYS - 1))
    )
    assert late is not None
    assert late.reason == "trial_ending"


def test_the_last_day_of_a_trial_escalates() -> None:
    """The fifth day is a reminder; the last is today's problem. One severity
    for both trains people to ignore the banner for four days."""
    day_five = notice_for(_state(plan="trial", status="trialing", trial_ends_at=_ahead(5)))
    day_one = notice_for(_state(plan="trial", status="trialing", trial_ends_at=_ahead(0.5)))
    assert day_five is not None and day_five.severity == "warning"
    assert day_one is not None and day_one.severity == "critical"


def test_an_undated_trial_is_never_warned_about() -> None:
    """⚠ Mirrors `_trial_expired`, which refuses to treat a NULL as an expiry.
    Warning about a deadline the gate will not enforce announces a cutoff that
    is not coming."""
    assert notice_for(_state(plan="trial", status="trialing", trial_ends_at=None)) is None


def test_an_unreadable_state_produces_no_notice() -> None:
    """`load_state` returns None on a database blip. Manufacturing "your trial
    is ending" out of a failed query is a false alarm about somebody's money —
    the gate's rule-1 asymmetry, pointed at what we SAY."""
    assert notice_for(None) is None


def test_payment_failure_outranks_a_trial_ending() -> None:
    """At most one banner, and the one with a hard deadline and a fix wins."""
    both = _state(
        plan="trial",
        status="past_due",
        past_due_since=_ago(1),
        trial_ends_at=_ahead(1),
    )
    notice = notice_for(both)
    assert notice is not None
    assert notice.reason == "payment_failed"


# ── the column is actually read ──────────────────────────────────────


def test_load_state_selects_the_delinquency_date() -> None:
    """⚠ The whole defect was a column written and never SELECTed, so the
    property above would compute perfectly against a field that is always
    None. A behavioural test of `grace_days_left` cannot see that (gotcha 62)
    — this reads the query."""
    source = pathlib.Path(load_state.__code__.co_filename).read_text(encoding="utf-8")
    tree = ast.parse(source)
    selects = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "from helixis_billing" in node.value
    ]
    assert selects, "no billing SELECT found — the scan is pointed at nothing"
    assert any("past_due_since" in q for q in selects), (
        "load_state does not read past_due_since, so every grace window is None "
        "and the cutoff can never fire"
    )


def test_the_banner_prefers_the_servers_words_over_its_own() -> None:
    """The frontend half of the same rule. `bannerFor` composed its own
    past-due sentence promising service "while your bank retries" — true until
    the grace cutoff shipped, then a promise the gate had stopped keeping.

    ⚠ A source-level guard on purpose: the TypeScript is not importable here,
    and what has to hold is that the SERVER'S field is consulted before the
    local fallback. Reading `status.notice` after composing local text would
    pass any behavioural test written against a backend that sends one.
    """
    banner = (
        pathlib.Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "components"
        / "TrialBanner.tsx"
    ).read_text(encoding="utf-8")

    # ⚠ CODE lines only, and this guard needed it twice over. The first cut
    # compared `banner.index("status.notice")` against `index("status.pastDue")`
    # over the whole file — and the docstring NAMES `status.notice` while
    # explaining why it exists, so the comparison was reading the comment.
    # Measured: deleting the entire server-notice branch left this test GREEN
    # while two vitest cases went red. A guard satisfied by the paragraph
    # describing it is not a guard (gotcha 40, arriving from the other side).
    code = [line for line in banner.splitlines() if not line.lstrip().startswith(("*", "//", "/*"))]
    notice_lines = [i for i, line in enumerate(code) if "status.notice" in line]
    past_due_lines = [i for i, line in enumerate(code) if "status.pastDue" in line]
    assert notice_lines, (
        "TrialBanner no longer reads the server's notice at all — every warning "
        "is now locally composed and cannot state a payment deadline"
    )
    assert past_due_lines, "the past-due fallback vanished — scan is pointed at nothing"
    assert notice_lines[0] < past_due_lines[0], (
        "the local past-due fallback is consulted before the server's notice, "
        "so a customer sees copy that cannot know the deadline"
    )
    # ⚠ Scoped to CODE lines, not the file (gotcha 40/59). The docstring above
    # quotes the removed sentence in order to explain why it went, so a
    # whole-file scan fires on the paragraph documenting the guard — measured,
    # on the correct file. Stripping comments wholesale is the move that
    # eventually goes blind, so this asserts something narrower and true: the
    # phrase may appear in prose ABOUT the code and never in the code, and a
    # real reintroduction lands in a `text:` value which is not a comment line.
    offenders = [
        line
        for line in banner.splitlines()
        if "while your bank retries" in line and not line.lstrip().startswith(("*", "//", "/*"))
    ]
    assert not offenders, (
        f"the unbounded-continuity promise is back in code: {offenders}. "
        "It is false once PAYMENT_GRACE_DAYS exists."
    )
