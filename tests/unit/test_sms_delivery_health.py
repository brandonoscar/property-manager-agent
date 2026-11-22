"""A message that never arrived has to leave a trace somebody reads.

WHAT BROKE
----------
``helixis_sms_log`` (migration 0069) records the whole delivery lifecycle and
Twilio's ``ErrorCode``. Measured 2026-09-09 by grepping every reader in the
tree: **written by three paths, read by exactly one query** —
``sms_digest._already_sent_today``, a ``SELECT 1`` for the daily cap. Nothing
anywhere read ``status`` or ``error_code``.

So a text to a landline came back ``undelivered`` with 30006 (checklist #89),
was written down, and the number stayed in the consent table looking healthy.
And the quieter one: a carrier that filters a message as spam sends **no
receipt at all** (checklist #77), so the row sits at ``sent`` for ever.

THE SPLIT THAT MATTERS
----------------------
``UNDELIVERED`` is Twilio saying it failed. ``NO_RECEIPT`` is Twilio saying
nothing. Two values would force one of them to be a lie — either inventing a
failure that was never reported, or hiding one that was. A manager replaces a
bad number and re-sends a silent one; those are different actions.

⚠ Both directions are guarded throughout. A classifier that answers
``NO_RECEIPT`` for everything closes the reported gap and is useless, and it
would pass every one-sided test written to catch the original defect
(gotcha 38).

⚠ SMS is dark until A2P approval, so this is pre-launch hardening. The log
holds no failed production sends today; the loop being open is the defect.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.comms import delivery_health as dh

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
COMPANY = "5ca1ab1e-0000-4000-8000-000000000004"

_DIGEST = Path("src/helixis/reminders/sms_digest.py")
_ROUTES = Path("src/helixis/api/routes/sms.py")


# ── the classifier ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "age_min", "expected"),
    [
        ("delivered", 1, dh.DELIVERED),
        ("delivered", 10_000, dh.DELIVERED),
        # Twilio said it failed. 30006 (landline) arrives as `undelivered`.
        ("undelivered", 1, dh.UNDELIVERED),
        ("failed", 1, dh.UNDELIVERED),
        # Handed over, nothing back yet, still inside the window.
        ("queued", 1, dh.PENDING),
        ("sent", 29, dh.PENDING),
        # Past the window with no receipt — the carrier-filter shape.
        ("sent", 31, dh.NO_RECEIPT),
        ("queued", 60 * 24, dh.NO_RECEIPT),
    ],
)
def test_each_status_gets_its_own_answer(status: str, age_min: int, expected: str) -> None:
    assert dh.classify(status, NOW - timedelta(minutes=age_min), NOW) == expected


def test_a_stuck_message_is_not_reported_as_a_failure() -> None:
    """The whole reason for four values. Twilio reported nothing about this
    message; calling it `undelivered` would be a claim it never made."""
    stuck = dh.classify("sent", NOW - timedelta(hours=5), NOW)
    assert stuck == dh.NO_RECEIPT
    assert stuck != dh.UNDELIVERED


def test_a_delivered_message_is_never_reported_as_stuck() -> None:
    """The other direction (gotcha 38). A classifier that says NO_RECEIPT for
    everything closes the gap on paper and tells the manager their working
    numbers are broken — which is how a signal gets switched off."""
    assert dh.classify("delivered", NOW - timedelta(days=30), NOW) == dh.DELIVERED


def test_an_unknown_status_declines_to_confirm_arrival() -> None:
    """Unrecognised must not read as delivered. Same direction as
    `maintenance_status.is_open` — over-report a visible unknown rather than
    quietly assert success (gotcha 50)."""
    assert dh.classify("gronk", NOW, NOW) == dh.NO_RECEIPT
    assert dh.classify(None, NOW, NOW) == dh.NO_RECEIPT


def test_a_row_with_no_timestamp_is_not_called_stuck() -> None:
    """No age means no basis for a verdict about age — pending, not stuck."""
    assert dh.classify("sent", None, NOW) == dh.PENDING


def test_a_naive_timestamp_does_not_raise() -> None:
    """asyncpg hands back naive UTC on some paths; comparing it to an aware
    `now` raises TypeError, which on a settings page is a 500 rather than a
    degraded panel."""
    assert dh.classify("sent", datetime(2026, 9, 9, 6, 0), NOW) == dh.NO_RECEIPT


# ── the summary ──────────────────────────────────────────────────────


def _pool(rows: list[dict] | None = None, *, raises: Exception | None = None) -> MagicMock:
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=raises) if raises else AsyncMock(return_value=rows or [])
    return pool


@pytest.mark.asyncio
async def test_the_summary_counts_each_outcome_separately() -> None:
    rows = [
        {"status": "delivered", "created_at": NOW - timedelta(hours=1)},
        {"status": "delivered", "created_at": NOW - timedelta(hours=2)},
        {"status": "undelivered", "created_at": NOW - timedelta(hours=3)},
        {"status": "sent", "created_at": NOW - timedelta(hours=4)},  # no receipt
        {"status": "sent", "created_at": NOW - timedelta(minutes=2)},  # pending
    ]
    summary = await dh.summarize(_pool(rows), COMPANY, now=NOW)
    assert (summary.delivered, summary.undelivered) == (2, 1)
    assert (summary.no_receipt, summary.pending) == (1, 1)
    assert summary.readable


@pytest.mark.asyncio
async def test_an_unreadable_log_reports_none_not_zero() -> None:
    """⚠ Zero says "nothing failed"; None says "nothing was checked". Telling
    a manager their texting is healthy on the strength of a query that never
    ran is the failure this whole module exists to stop (gotchas 57/70/89)."""
    summary = await dh.summarize(_pool(raises=RuntimeError("boom")), COMPANY, now=NOW)
    assert summary.delivered is None
    assert summary.undelivered is None
    assert summary.no_receipt is None
    assert not summary.readable
    assert summary.window_days == 7


@pytest.mark.asyncio
async def test_the_summary_is_company_scoped_and_outbound_only() -> None:
    """Gotcha 47. An inbound row is somebody texting US — counting it as a
    delivery would inflate the healthy number with traffic we never sent."""
    pool = _pool([])
    await dh.summarize(pool, COMPANY, now=NOW)
    sql, *args = pool.fetch.await_args.args
    assert "company_id = $1::uuid" in sql
    assert "direction = 'outbound'" in sql
    assert args[0] == COMPANY


# ── the per-number outcome ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_last_outcome_carries_the_error_code() -> None:
    """30006 is the landline. It is the difference between "re-send" and
    "this number cannot receive texts, replace it"."""
    rows = [
        {
            "phone": "+15551234567",
            "status": "undelivered",
            "error_code": "30006",
            "created_at": NOW - timedelta(hours=1),
        }
    ]
    out = await dh.last_outcome_by_phone(_pool(rows), COMPANY, now=NOW)
    assert out == {"+15551234567": (dh.UNDELIVERED, "30006")}


@pytest.mark.asyncio
async def test_the_last_outcome_takes_the_most_recent_send_per_number() -> None:
    pool = _pool([])
    await dh.last_outcome_by_phone(pool, COMPANY, now=NOW)
    sql, *_ = pool.fetch.await_args.args
    assert "distinct on (phone)" in sql.lower(), (
        "without DISTINCT ON this reports whichever row Postgres returned "
        "first — a healthy send from last month over today's failure"
    )
    assert "order by phone, created_at desc" in sql.lower()


@pytest.mark.asyncio
async def test_an_unreadable_log_leaves_the_consent_list_renderable() -> None:
    """An empty map, so every row shows "no messages sent" — which is honest
    and is NOT a claim about delivery."""
    assert await dh.last_outcome_by_phone(_pool(raises=RuntimeError("boom")), COMPANY) == {}


# ── the daily cap ────────────────────────────────────────────────────


def _cap_query() -> str:
    """The SQL string `_already_sent_today` actually builds."""
    tree = ast.parse(_DIGEST.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_already_sent_today"
    )
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "fetchrow":
            return "".join(
                a.value for a in ast.walk(node.args[0]) if isinstance(a, ast.Constant)
            ).lower()
    raise AssertionError("_already_sent_today no longer calls fetchrow")


def test_a_message_that_never_arrived_does_not_spend_the_daily_cap() -> None:
    """It was accepted, written down and never delivered. Counting it means
    the manager gets NOTHING that day and no one is told why."""
    assert "status = any(" in _cap_query(), (
        "the cap counts every logged row again, so an undelivered digest "
        "silently buys a day of silence"
    )


def test_the_cap_still_counts_a_message_with_no_outcome_yet() -> None:
    """⚠ The other direction, and the one that costs a real person a second
    text. `queued`/`sent` must NOT be excluded — re-sending on the strength of
    a receipt that has not come back is how one blip becomes two messages
    (gotchas 55/57: unknown counts as spent)."""
    sql = _cap_query()
    assert "queued" not in sql and "'sent'" not in sql, (
        "the cap excludes in-flight rows, so a slow receipt means a second "
        "digest to the same person"
    )


def test_the_two_modules_share_one_list_of_terminal_statuses() -> None:
    """⚠ A second spelling would silently change who gets notified — gotcha
    73's `"approved"` in three modules. The digest imports the constant; it
    does not retype the values."""
    src = _DIGEST.read_text(encoding="utf-8")
    assert "TERMINAL_FAILURE_STATUSES" in src
    assert "'undelivered'" not in src and '"undelivered"' not in src, (
        "sms_digest names a terminal status literally instead of importing "
        "the one list — the two can now drift"
    )
    assert dh.TERMINAL_FAILURE_STATUSES == frozenset({"undelivered", "failed"})


# ── the wiring (a behavioural test cannot see a caller that stops asking) ──


def _calls_in(path: Path, fn_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef) and n.name == fn_name
    )
    return {
        getattr(n.func, "attr", None) or getattr(n.func, "id", "")
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
    }


def test_the_consent_list_asks_for_the_outcomes() -> None:
    """A well-tested reader nothing calls is the `comms_triage` failure
    (gotchas 62/66/69). The landline case is only closed where somebody can
    act on it, and that is this list."""
    assert "last_outcome_by_phone" in _calls_in(_ROUTES, "list_consent")


def test_the_status_endpoint_asks_for_the_summary() -> None:
    assert "summarize" in _calls_in(_ROUTES, "sms_status")


def test_the_wiring_guard_can_say_no() -> None:
    """Proof it can return False. A walk that matched nothing would pass
    against a route that dropped the call (gotcha 45)."""
    assert "last_outcome_by_phone" not in _calls_in(_ROUTES, "sms_status")
