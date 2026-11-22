"""There is no rent-change surface, and the agent has to say so accurately.

A property manager asking "raise the rent on 4B to $1,400" is asking for a
Buildium write that does not exist here. Measured 2026-08-29: no rent or
recurring-charge endpoint is wrapped at all — ``Leases`` has no ``rent``
method, ``GET /leases/{id}/rent`` is unwrapped, and ``buildium_renew_lease``
is deliberately dark because a renewal creates a real lease term with no clean
undo (gotcha 22).

⚠ **The fix is prose, deliberately, and this file explains why rather than
apologising for it.** Building the write would mean inventing a Buildium
request body from the docs, which this codebase has been burned by four times
(gotchas 15/24/25/53) and which its own rule forbids: ground a write on a live
production probe or do not ship it. There are no Buildium credentials in this
environment to probe with. So the honest deliverable is an agent that answers
correctly and offers the path that does exist — a renewal offer carrying a
proposed rent — instead of one that quietly attempts nothing and reports
success.

⚠ **"Proposed" and "changed" are different words and the difference is the
whole point.** A renewal offer is a document a human sends. Even an ACCEPTED
offer is not a Buildium write. An agent that says "I've raised the rent to
$1,400" after drafting one has made a confident false claim about money — the
gotcha 41/51 shape pointed at a lease.
"""

from __future__ import annotations

import inspect

from helixis.agents import buildium_agent
from helixis.agents.buildium_tools import buildium_tools
from helixis.buildium.endpoints.leases import Leases


def _instructions() -> str:
    return buildium_agent.BUILDIUM_AGENT_INSTRUCTIONS


# ── the gap is real ──────────────────────────────────────────────────


def test_no_rent_write_reaches_buildium() -> None:
    """The premise. If this ever stops being true the prose below becomes a
    false limitation, which is worse than none — it would make the agent
    refuse something it can now do."""
    lease_methods = {m for m in dir(Leases) if not m.startswith("_")}
    assert not {m for m in lease_methods if "rent" in m.lower()}, (
        "a rent endpoint is now wrapped — update the agent prose, which "
        "currently tells users the capability does not exist"
    )


def test_no_registered_tool_writes_rent() -> None:
    """⚠ Both sides (gotcha 28). The prose says "you cannot apply it"; this
    reads the REGISTRY, so the claim and the tool surface cannot drift into
    disagreeing. A tool added without updating the prose fails here."""
    names = {t.name for t in buildium_tools()}
    rent_writers = {
        n
        for n in names
        if "rent" in n and any(v in n for v in ("set", "update", "change", "raise", "apply"))
    }
    assert not rent_writers, f"rent-writing tool(s) registered but undocumented: {rent_writers}"


def test_lease_renewal_stays_dark() -> None:
    """The one endpoint that COULD change a rent is renewal, and it is dark on
    purpose: irreversible, with no clean undo (gotcha 22). If it goes live the
    rent story changes and this file must be revisited."""
    assert "buildium_renew_lease" not in {t.name for t in buildium_tools()}


# ── the agent says the true thing ────────────────────────────────────


def test_the_prose_states_the_limit() -> None:
    """Without this the model reaches for the nearest adjacent action — which
    for "raise the rent" is drafting an offer and describing it as done."""
    text = _instructions().lower()
    assert "rent" in text and "propose it, you cannot apply it" in text


def test_the_prose_names_the_path_that_does_exist() -> None:
    """⚠ A prohibition with no permitted substitute is an instruction to
    improvise — gotcha 52's lesson, learned on fair housing and applying just
    as well here. Occupella CAN draft a renewal offer carrying a proposed
    rent, and saying so is what stops the model inventing something else."""
    text = _instructions().lower()
    assert "renewal offer" in text
    assert "proposed rent" in text


def test_the_prose_forbids_reporting_a_rent_as_changed() -> None:
    """⚠ THE claim that must never be made. Drafting an offer and saying "I've
    raised the rent" is a confident false statement about money, and an
    accepted offer still is not a Buildium write."""
    text = _instructions().lower()
    for verb in ("changed", "updated", "raised"):
        assert verb in text, f"the prohibition must name {verb!r} explicitly"
    assert "never report a rent as" in text


def test_the_limit_is_stated_beside_the_other_one_of_its_shape() -> None:
    """AP bills are the same shape — readable, not writable — and were already
    documented. Keeping the two together is what makes the pattern legible
    rather than looking like a one-off carve-out."""
    text = _instructions()
    bills = text.index("cannot create or\n  pay a VENDOR bill")
    rent = text.index("you can propose it, you cannot apply it")
    assert 0 < rent - bills < 900, "the two 'read but not write' limits have drifted apart"


def test_the_agent_actually_receives_these_instructions() -> None:
    """⚠ Prose in a constant nothing interpolates is a rule that does not
    exist. Reads the factory, because this repo has shipped exactly that: a
    fencing rule present in one prompt and absent from the two beside it."""
    src = inspect.getsource(buildium_agent.create_buildium_agent)
    assert "BUILDIUM_AGENT_INSTRUCTIONS" in src
