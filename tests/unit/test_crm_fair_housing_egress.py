"""The lead-reply lane sends to PROSPECTS and had no deterministic gate.

``crm/draft.py`` says it itself: "Lead drafts are the highest fair-housing-risk
surface (outbound messages to prospects)" — steering liability under §3604
attaches to what you tell a prospective renter. It then calls the appended
prompt block "a hard guardrail", which is the part that was not true.

``guard_fair_housing`` — the tuned, negation-guarded ``check_draft`` — had
exactly TWO callers, ``send_tenant_message`` and ``reply_to_email``, both Inbox
handlers. ``POST /crm/leads/{id}/reply`` validated that the body was non-empty,
checked the manager role, and sent.

⚠ Gotcha 52 is why the prompt alone does not close this: the model declined a
protected-class question correctly and then OFFERED the proxies that same
prompt forbids by name, and the rubric judge caught that shape only sometimes.

⚠ The fail direction is deliberately asymmetric. A blocked legitimate reply is
visible and recoverable — a manager already approved the text, so a 422 is a
"revise and resend" prompt, not a silent drop. An unblocked steering message to
a prospect is a violation nobody sees. Both directions are pinned below,
because a gate that blocks ordinary replies gets switched off and then protects
nothing.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from helixis.api.routes.crm import _guard_fair_housing_egress

# ── it blocks what it must ───────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        # Measured against the real gate, not invented — my first three
        # examples were hand-written and two of them sailed through, which is
        # how this file learned what the gate actually covers.
        "That area is mostly white families, you'd fit right in.",
        "The building is predominantly hispanic right now.",
        "The neighborhood is about 80% white.",
        "Yes, absolutely a safe family-friendly part of town.",
        "I'd steer prospects toward the Oak Street units.",
        "Yes, you can deny the emotional support animal.",
    ],
)
def test_steering_language_is_refused(body: str) -> None:
    with pytest.raises(HTTPException) as caught:
        _guard_fair_housing_egress(body)
    assert caught.value.status_code == 422
    assert "fair-housing" in str(caught.value.detail)
    assert "Revise" in str(caught.value.detail)


# ── and lets ordinary work through ───────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        "Thanks for reaching out! The 2-bed at 120 4th St is available from the 1st. "
        "Rent is $1,450 and it includes water. Want to schedule a viewing this week?",
        "Hi Dana — yes, we allow pets with a $300 deposit. Happy to send the full "
        "pet policy over if that helps.",
        "Application fee is $45 and we run credit plus a background check on every "
        "applicant. Same process for everyone.",
        "Sorry for the delay! The unit you asked about is leased, but a similar one "
        "opens up on the 15th. Would you like the details?",
        "We can hold it for 48 hours once the application is in. Let me know.",
    ],
)
def test_an_ordinary_reply_goes_through(body: str) -> None:
    """The half that decides whether this gate survives contact with real use.
    A false positive here blocks a manager mid-workflow on text they already
    approved — and a guard that cries wolf gets removed, which is how a real
    control ends up protecting nothing (gotcha 49's lesson from the other
    direction)."""
    _guard_fair_housing_egress(body)  # must not raise


@pytest.mark.parametrize(
    "body",
    [
        "This building is really a young professionals' place, not many kids around.",
        "That's a great area — mostly young families like yourself, you'd fit right in.",
    ],
)
def test_familial_status_steering_in_prose_is_known_to_pass(body: str) -> None:
    """⚠ Asserts a GAP, on purpose, because wiring the gate here is a real
    improvement and NOT full coverage — and saying which decides how hard the
    next person looks.

    ``check_draft`` is tuned for agent-to-MANAGER advice — "yes, you can
    deny…", "I'd steer them toward…" — which is the shape the 41x golden
    cases exercise. A CRM lead reply is manager-to-PROSPECT prose, a different
    speaker, and the vocabulary does not fully carry over: ``demographic_makeup``
    requires a RACE word after "mostly", so "mostly young families like
    yourself" passes. Familial status is a protected class and that sentence is
    textbook steering.

    Deliberately not fixed here. Widening a tuned safety regex is exactly the
    change gotcha 52 warns about — a compliant reply NAMES the categories in
    order to refuse them, which is the 414/416 false-positive class, and this
    regex is shared with the two Inbox lanes where that shape is common. It
    needs its own change with its own false-positive corpus, not a quiet edit
    two days from release.

    When that lands, this test fails and forces the note to be rewritten rather
    than going stale (gotcha 48).
    """
    _guard_fair_housing_egress(body)  # currently passes — that is the finding


def test_an_empty_body_is_not_the_gates_problem() -> None:
    """The route rejects empty bodies before this runs; the gate must not
    invent a violation out of nothing."""
    _guard_fair_housing_egress("")


# ── the wiring, read from the route itself ───────────────────────────


def test_the_route_calls_the_gate_before_it_sends() -> None:
    """⚠ Order matters and is asserted, not assumed. A gate that runs after
    ``send_gmail_email`` is a log line, not a gate — the message has already
    left. Reads the real function source so a refactor that moves the call
    below the send fails here.
    """
    import ast
    import inspect

    from helixis.api.routes import crm

    tree = ast.parse(inspect.getsource(crm.reply_to_lead))
    order: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if name in {"_guard_fair_housing_egress", "send_gmail_email", "guarded_action"}:
                order.append(f"{name}@{node.lineno}")

    assert any(o.startswith("_guard_fair_housing_egress") for o in order), (
        "the CRM reply route does not call the fair-housing gate at all"
    )
    gate_line = min(int(o.split("@")[1]) for o in order if o.startswith("_guard_fair"))
    send_lines = [int(o.split("@")[1]) for o in order if not o.startswith("_guard_fair")]
    assert send_lines, "no send call found — this scan is stale, not the route"
    assert gate_line < min(send_lines), (
        "the gate runs AFTER the send — by then the message has already left"
    )


def test_the_gate_is_the_same_one_the_inbox_lanes_use() -> None:
    """One tuned implementation, three lanes. A second copy would drift, and
    the drift would be invisible until a steering message went out on whichever
    lane had the older rules (gotcha 25's rule, pointed at a safety check)."""
    import inspect

    from helixis.api.routes import crm

    assert "check_draft" in inspect.getsource(crm._guard_fair_housing_egress)
