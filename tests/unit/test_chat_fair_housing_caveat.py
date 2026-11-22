"""A live chat answer that steers gets annotated — nothing looked before.

The deterministic gate ran on exactly two Inbox SEND handlers. The assistant's
own answers — which is where a manager reads "yes, you can deny that" — were
covered by the prompt block and the 41x golden cases only. Those cases gate at
BUILD time. Nothing looked at a production reply, so the first anyone would
have known was a complaint.

⚠ ADVISORY and footer-only, on purpose. #271 pulled the runtime prose-gate
experiment and gotcha 42 records the rule that replaced it: a guard on prose
annotates, never blocks. Unlike the CRM lane there is no approved draft to
revise here, so a block would be a dead end rather than a "revise and resend".

The operator-facing half is the reason code, and that is the half with teeth —
prose the user may not read is not a control.
"""

from __future__ import annotations

from helixis.agents.claim_guard import apply_fair_housing_caveat, fair_housing_violations
from helixis.observability.reason_codes import REASON_CODES


def test_a_steering_answer_is_detected() -> None:
    rules = fair_housing_violations("Yes, you can deny the emotional support animal.")
    assert rules, "a live steering answer went unflagged"


def test_an_ordinary_answer_is_not() -> None:
    """Both directions (gotcha 38). Every substantive reply gets scanned, so a
    detector that fired broadly would caveat the whole product and teach the
    manager to ignore the footer — which is how a real signal dies."""
    for ordinary in (
        "Unit 4B has an open balance of $1,240 as of today.",
        "Three work orders are open at Redbud Lane; the oldest is 12 days.",
        "I can't share information about a resident's protected characteristics.",
    ):
        assert fair_housing_violations(ordinary) == [], ordinary


def test_the_body_is_never_rewritten() -> None:
    """#271's rule. The caveat is appended; the answer the model wrote is
    delivered intact, so a false positive costs a line of noise and never an
    answer."""
    body = "Yes, you can deny the emotional support animal."
    out = apply_fair_housing_caveat(body, ["steering:esa_deny_ok"])
    assert body in out
    assert len(out) > len(body)
    assert "Fair Housing Act" in out


def test_no_rules_means_no_change() -> None:
    body = "Rent is $1,450."
    assert apply_fair_housing_caveat(body, []) == body


def test_the_caveat_names_which_rule_matched() -> None:
    """A footer saying only "this may be a problem" cannot be acted on. Naming
    the rule is what lets a reader check the claim rather than trust it."""
    out = apply_fair_housing_caveat("x", ["steering:esa_deny_ok", "steering:voucher_refuse_ok"])
    assert "esa_deny_ok" in out
    assert "voucher_refuse_ok" in out


def test_the_detector_never_raises() -> None:
    """A guard on prose must not be able to cost the turn — the whole reason
    the caveats around it are advisory."""
    for junk in ("", None):
        assert fair_housing_violations(junk) == []  # type: ignore[arg-type]


def test_the_reason_code_is_in_the_vocabulary() -> None:
    """gotcha 28's both-sides rule, and it has bitten this exact mechanism:
    ``record_reason_code`` warns and DROPS a code not in the set, so the
    operator-facing half would silently emit nothing while the footer kept
    appearing — the control would look present and be absent."""
    assert "fair_housing_flagged" in REASON_CODES


def test_the_orchestrator_records_it_on_the_live_path() -> None:
    """Reads the real call site: a detector nothing calls is worth nothing,
    and the two halves — caveat and reason code — must fire together."""
    import ast
    import inspect

    from helixis.agents import orchestrator

    src = inspect.getsource(orchestrator)
    tree = ast.parse(src)
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "fair_housing_violations" in calls
    assert "apply_fair_housing_caveat" in calls
    assert "fair_housing_flagged" in src


def test_it_shares_the_send_handlers_gate() -> None:
    """One tuned implementation. A second copy would drift, and the drift
    would be invisible until a steering answer went out on whichever lane had
    the older rules (gotcha 25, pointed at a safety check)."""
    import inspect

    from helixis.agents import claim_guard

    assert "check_draft" in inspect.getsource(claim_guard.fair_housing_violations)
