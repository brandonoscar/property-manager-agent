"""Every new company gets a trial row, and the trial length has one home.

The gap this guards is silent by construction, which is why it needs a test
rather than a comment. `billing/gate.py` treats a missing `helixis_billing` row
as ALLOW — correctly, so a database blip cannot lock out a paying customer —
so a company that never got a row is an unmetered free account that no error,
log line or failing test would surface. 0087 backfilled the companies alive on
2026-09-02 and gave later ones nothing.

⚠ These are SOURCE-level assertions against the migration text, and that is the
only honest option here: the rule lives in a Postgres trigger, so no Python
test can execute it and a fake pool would answer whatever it was handed
(gotcha 79's lesson — a wrong SQL path is invisible to types and to mocks).
What they CAN pin is that the statement says what the product says, and that
nobody moves the interval without moving `TRIAL_DAYS`.

⚠ Scoped to STATEMENTS, never the whole file (gotchas 40/59). The migration's
header quotes the failure modes in order to explain them, so a file-wide scan
for `update helixis_billing` or `after insert` fires on the prose describing
the guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from helixis.billing.gate import _SERVING_STATUSES
from helixis.billing.plans import PLANS, TRIAL_DAYS

REPO = Path(__file__).resolve().parents[2]
MIGRATION = REPO / "supabase" / "migrations" / "0088_billing_row_on_company_create.sql"


def _sql() -> str:
    """The migration with `--` comment lines stripped.

    The header deliberately quotes the broken shapes it exists to prevent, so
    every assertion below has to read the STATEMENTS. A comment is never a
    statement — dropping the comment lines is the SQL equivalent of walking the
    AST instead of grepping the source.
    """
    text = MIGRATION.read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("--"))


def _trigger_body() -> str:
    """Just `grant_billing_trial`'s body.

    ⚠ The whole-file read is not specific enough for the value assertions, and
    a mutation proved it: the window backfill also writes `'trialing'`, so
    flipping the TRIGGER's status to `'canceled'` — which would lock out every
    new signup — left a file-wide `"'trialing'" in sql` check green. The two
    statements are separate rules that merely share vocabulary, so anything
    asserting what a NEW company receives has to read the function that grants
    it.
    """
    body = re.search(r"as\s+\$function\$(.*?)\$function\$", _sql(), re.DOTALL)
    assert body, "grant_billing_trial's body not found"
    return body.group(1)


def test_the_migration_exists() -> None:
    """A guard that silently finds no file passes forever (gotcha 49)."""
    assert MIGRATION.is_file(), f"missing {MIGRATION}"
    assert _sql().strip(), "the migration is nothing but comments"


def test_the_trial_length_matches_the_plan_the_product_sells() -> None:
    """The interval is a literal because SQL cannot import Python. This is the
    join that stops it drifting from the number the customer is quoted."""
    intervals = set(re.findall(r"interval\s+'(\d+)\s+days'", _sql()))
    assert intervals == {str(TRIAL_DAYS)}, (
        f"migration grants {sorted(intervals)} days, plans.TRIAL_DAYS is "
        f"{TRIAL_DAYS}. Change both or neither."
    )


def test_the_granted_plan_and_status_are_ones_the_gate_serves() -> None:
    """A row the gate cannot read as serving is worse than no row: it turns
    fail-open into a lockout for every new signup at once."""
    # Read the TRIGGER, not the file — see _trigger_body. The values are taken
    # out of the statement and checked against the code that will read them,
    # rather than asserting a literal twice and calling that two sides.
    values = re.search(r"values\s*\(\s*NEW\.id\s*,\s*'(\w+)'\s*,\s*'(\w+)'", _trigger_body())
    assert values, "the trigger's INSERT ... VALUES row was not found"
    plan, status = values.group(1), values.group(2)

    assert plan in PLANS, f"the trigger grants plan {plan!r}, which plans.py does not define"
    assert PLANS[plan].price_usd == 0, (
        f"the trigger grants {plan!r}, a PAID plan — a new signup would be "
        "given something nobody agreed to pay for"
    )
    assert status in _SERVING_STATUSES, (
        f"the trigger writes status {status!r}, which gate._SERVING_STATUSES "
        "does not serve — every new company would be locked out on arrival"
    )


def test_the_trigger_fires_after_insert_on_companies() -> None:
    """AFTER INSERT on `companies` is what makes this cover all five creation
    paths at once. BEFORE would run against a row that may still fail, and any
    other table would silently cover nothing."""
    sql = _sql()
    trigger = re.search(
        r"create\s+trigger\s+(\w+)\s+after\s+insert\s+on\s+public\.companies\b"
        r".*?execute\s+function\s+public\.(\w+)\(\)",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert trigger, "no AFTER INSERT trigger on public.companies in the statements"
    assert trigger.group(2) == "grant_billing_trial"
    assert f"drop trigger if exists {trigger.group(1)}" in sql.lower(), (
        "re-applying the migration must be idempotent — drop the trigger first"
    )


def test_the_insert_cannot_double_grant() -> None:
    """`handle_new_user` is re-triggerable and the backfill re-runs; both must
    be no-ops the second time."""
    body = _sql()
    assert body.lower().count("on conflict (company_id) do nothing") >= 2, (
        "both the trigger insert and the window backfill need ON CONFLICT DO NOTHING"
    )


def test_a_billing_failure_cannot_fail_the_signup() -> None:
    """The deliberate asymmetry: a missing billing row is a handled state, a
    failed company insert is the front door of the product falling over.

    This is the one assertion most likely to be 'tidied' away by someone who
    reads a swallowed exception as sloppiness, so it says why in the failure.
    """
    sql = _sql().lower()
    assert "exception when others then" in sql, (
        "grant_billing_trial must swallow its own failure: a billing problem "
        "must never be able to fail a company insert"
    )
    assert "raise warning" in sql, "a swallowed failure with no warning is invisible"
    assert sql.count("return new;") >= 2, (
        "both the happy path and the exception handler must return NEW, or a "
        "billing failure aborts the row that triggered it"
    )


def test_the_standing_check_view_exists() -> None:
    """`migration_check.py` cannot see a data-only backfill (gotchas 53/59), so
    the way you confirm this landed is a view that must read zero rows."""
    assert re.search(
        r"create\s+(or\s+replace\s+)?view\s+helixis_billing_missing",
        _sql(),
        re.IGNORECASE,
    ), "no helixis_billing_missing view — nothing can confirm the backfill ran"


# ── The checkout upsert ───────────────────────────────────────────────────────
# A plain UPDATE lands on nothing when the row is absent, which discards the
# Stripe customer id and makes the next checkout mint a second Stripe customer
# for the same company. 0088 means the row is always there; this pins the
# second line, because the failure is invisible and hits a paying customer.

CHECKOUT = REPO / "src" / "helixis" / "api" / "routes" / "billing.py"


def test_checkout_upserts_the_stripe_customer_id() -> None:
    source = CHECKOUT.read_text(encoding="utf-8")
    # Statement-scoped: the comment above the call quotes "update
    # helixis_billing" to explain the bug, so a file-wide search for it would
    # match the explanation and pass whichever operator the code used.
    statements = "".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    ).lower()
    assert "insert into helixis_billing (company_id, stripe_customer_id)" in statements, (
        "the checkout route must UPSERT — a plain UPDATE silently drops the "
        "Stripe customer id when the billing row is missing"
    )
    assert "on conflict (company_id) do update set" in statements

    stripe_writes = re.findall(r"update helixis_billing set stripe_customer_id", statements)
    assert not stripe_writes, (
        "a bare UPDATE of stripe_customer_id is back in the checkout path; it must be the upsert"
    )


@pytest.mark.parametrize("field", ["trial_ends_at", "period_end"])
def test_the_checkout_upsert_does_not_grant_a_trial(field: str) -> None:
    """The trial length has ONE home (0088). A second statement setting it here
    is a second source of truth for how long a customer gets free — the class
    of bug gotchas 25/53 keep recording, pointed at revenue."""
    source = CHECKOUT.read_text(encoding="utf-8")
    insert = re.search(
        r"insert into helixis_billing.*?on conflict \(company_id\) do update set.*?\"",
        source,
        re.IGNORECASE | re.DOTALL,
    )
    assert insert, "checkout upsert not found"
    assert field not in insert.group(0), (
        f"the checkout upsert sets {field}; trial dates belong only to migration 0088"
    )
