"""Lead conversations feed the drafting assist — the loop that was never closed.

``crm_lead_messages`` has stored every text both directions since 0029, and
nothing read it back. The assist saw the current lead's thread and the
company's uploaded documents, and knew nothing about the replies the manager
had already approved. It could not improve from use because use went nowhere.

Everything here fails SILENTLY by design (an enhancement, never load-bearing —
gotchas 7 and 17), which is exactly why it needs testing: a broken write and a
company with nothing to remember yet are the same empty string, and only a test
that reaches in can tell them apart.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.crm import lead_memory
from helixis.db.repositories.leads import MessageRow


def _lead(unit: str = "Maple Court 4B") -> Any:
    lead = MagicMock()
    lead.id = "lead-1"
    lead.unit_label = unit
    return lead


def _msg(direction: str, body: str) -> MessageRow:
    return MessageRow(
        company_id="c1",
        lead_id="lead-1",
        direction=direction,
        channel="sms",
        body=body,
        external_id=None,
        raw={},
    )


LONG = "Yes — we allow cats at Maple Court with a $25/month pet rent, no deposit."


@pytest.mark.asyncio
async def test_an_approved_reply_is_remembered_with_its_property(monkeypatch) -> None:
    """⚠ The unit rides ALONG with the fact, not separately.

    "We allow cats" means something different at Maple Court than
    portfolio-wide, and a fact recalled without its property is the confident
    false claim this repo keeps re-learning (gotcha 41).
    """
    manager = MagicMock()
    manager.add = AsyncMock(return_value="m1")
    monkeypatch.setattr(lead_memory, "get_memory_manager", lambda: manager)

    await lead_memory.remember_approved_reply(
        company_id="c1", lead=_lead(), body=LONG, channel="sms", user_id="u1"
    )

    manager.add.assert_awaited_once()
    content = manager.add.await_args.args[0]
    assert "Maple Court 4B" in content
    assert "pet rent" in content
    # Company-scoped, always (gotcha 47).
    assert manager.add.await_args.args[1] == "c1"
    assert manager.add.await_args.kwargs["extra_metadata"]["lead_id"] == "lead-1"


@pytest.mark.asyncio
async def test_an_acknowledgement_is_not_worth_an_extraction_call(monkeypatch) -> None:
    """ "Sounds good — see you then" teaches the model nothing and costs a call."""
    manager = MagicMock()
    manager.add = AsyncMock()
    monkeypatch.setattr(lead_memory, "get_memory_manager", lambda: manager)

    await lead_memory.remember_approved_reply(
        company_id="c1", lead=_lead(), body="Sounds good!", channel="sms"
    )
    manager.add.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("boom", [RuntimeError("mem0 down"), SystemExit(1)])
async def test_a_failed_write_never_costs_a_reply_that_already_went_out(
    monkeypatch, boom: BaseException
) -> None:
    """⚠ ``SystemExit`` is in the parametrize on purpose.

    It is a ``BaseException``, so it slips past ``except Exception`` — and a
    spaCy-backed transitive path really does call ``sys.exit`` in the
    egress-restricted container (gotcha 7). The reply has ALREADY been sent and
    logged by the time this runs; raising here would turn a memory blip into a
    500 on a successful send.
    """
    manager = MagicMock()
    manager.add = AsyncMock(side_effect=boom)
    monkeypatch.setattr(lead_memory, "get_memory_manager", lambda: manager)

    await lead_memory.remember_approved_reply(
        company_id="c1", lead=_lead(), body=LONG, channel="sms"
    )  # must not raise


@pytest.mark.asyncio
async def test_recall_queries_what_the_lead_actually_asked(monkeypatch) -> None:
    """⚠ Not the lead's name.

    A name matches every past reply that greeted anyone and none of the ones
    that answer this question — the most confidently irrelevant four lines
    available.
    """
    manager = MagicMock()
    manager.search = AsyncMock(return_value=[{"memory": "Cats allowed, $25/mo."}])
    monkeypatch.setattr(lead_memory, "get_memory_manager", lambda: manager)

    block = await lead_memory.recall_for_lead(
        company_id="c1",
        lead=_lead(),
        messages=[_msg("outbound", "Hi Jordan!"), _msg("inbound", "Do you allow cats?")],
    )

    assert manager.search.await_args.args[0] == "Do you allow cats?"
    assert manager.search.await_args.args[1] == "c1"
    assert "Cats allowed" in block
    # ⚠ Hedged, and labelled as PAST replies. These come back by semantic
    # similarity, so a close-but-wrong hit is expected and the prompt has to be
    # able to ignore one. Presented as policy, a neighbouring property's pet
    # rule becomes this property's answer.
    assert "past approved replies" in block.lower()


@pytest.mark.asyncio
async def test_recall_reads_the_latest_question_not_the_first(monkeypatch) -> None:
    manager = MagicMock()
    manager.search = AsyncMock(return_value=[])
    monkeypatch.setattr(lead_memory, "get_memory_manager", lambda: manager)

    await lead_memory.recall_for_lead(
        company_id="c1",
        lead=_lead(),
        messages=[
            _msg("inbound", "Is 4B still available?"),
            _msg("outbound", "It is — want to tour?"),
            _msg("inbound", "Do you allow cats?"),
        ],
    )
    assert manager.search.await_args.args[0] == "Do you allow cats?"


@pytest.mark.asyncio
async def test_nothing_to_recall_yet_returns_empty_not_a_header(monkeypatch) -> None:
    """The common state on day one, and it must add NOTHING to the prompt.

    A "here is what you said before:" header over an empty list invites the
    model to fill it in.
    """
    manager = MagicMock()
    manager.search = AsyncMock(return_value=[])
    monkeypatch.setattr(lead_memory, "get_memory_manager", lambda: manager)

    block = await lead_memory.recall_for_lead(
        company_id="c1", lead=_lead(), messages=[_msg("inbound", "Do you allow cats?")]
    )
    assert block == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("boom", [RuntimeError("embedder 403"), SystemExit(1)])
async def test_a_failed_recall_never_blocks_the_drawer(monkeypatch, boom: BaseException) -> None:
    manager = MagicMock()
    manager.search = AsyncMock(side_effect=boom)
    monkeypatch.setattr(lead_memory, "get_memory_manager", lambda: manager)

    block = await lead_memory.recall_for_lead(
        company_id="c1", lead=_lead(), messages=[_msg("inbound", "Do you allow cats?")]
    )
    assert block == ""


@pytest.mark.asyncio
async def test_no_memory_manager_is_a_no_op_in_both_directions(monkeypatch) -> None:
    """Memory is optional — the `[memory]` extra may not be installed."""
    monkeypatch.setattr(lead_memory, "get_memory_manager", lambda: None)
    await lead_memory.remember_approved_reply(
        company_id="c1", lead=_lead(), body=LONG, channel="sms"
    )
    assert (
        await lead_memory.recall_for_lead(
            company_id="c1", lead=_lead(), messages=[_msg("inbound", "Cats?")]
        )
        == ""
    )


def test_the_route_actually_calls_both_halves() -> None:
    """⚠ A behavioural test cannot see "wired to nothing" (gotcha 62).

    ``comms_triage`` was complete, unit-tested and called by nothing for weeks.
    The write goes in ``_log_outbound_and_advance`` specifically — the tail
    BOTH channels pass through — so adding a third channel cannot skip it.
    """
    import ast
    import inspect

    from helixis.api.routes import crm

    tree = ast.parse(inspect.getsource(crm))
    called_in: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                called_in.setdefault(inner.func.id, set()).add(node.name)

    assert "_log_outbound_and_advance" in called_in.get("remember_approved_reply", set()), (
        "the write must sit in the tail both channels share, or a new channel "
        "silently stops teaching the assist anything"
    )
    assert called_in.get("recall_for_lead"), "nothing reads memory back into the draft"


# ── the working layer: what we know about THIS lead, right now ──────
#
# Distinct from the durable company memory above, and the distinction is the
# design. An approved reply is a fact about how the company operates and lives
# forever. What a lead has told us is a working picture of one conversation:
# true until they say something else, and worthless once the lead is closed.
#
# It already existed as `signals` + `draft_reply` — and was computed ONCE, on
# first open, then cached with nothing to invalidate it. So a lead who texted
# three more times still got the signals worked out from their first sentence.
# Static in the wrong sense: frozen rather than current.


def test_every_message_clears_the_working_assist() -> None:
    """⚠ Inside ``insert_message``, not at the call sites.

    Messages arrive in four places — inbound SMS, inbound call, the outbound
    tail, email — and clearing at each is the fan-out a fifth silently reopens.
    That fan-out is how the assist came to be frozen at first contact.
    """
    import ast
    import inspect

    from helixis.db.repositories import leads as leads_module

    tree = ast.parse(inspect.getsource(leads_module))
    inside = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id == "clear_lead_assist"
    }
    assert "insert_message" in inside, (
        "the assist must be cleared where every message lands, or a new arrival "
        "path leaves a lead described by something they said days ago"
    )


@pytest.mark.asyncio
async def test_an_inbound_message_invalidates_the_cached_picture() -> None:
    """The behavioural half — the AST guard above cannot see what it does."""
    from helixis.db.repositories import leads as leads_module

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"id": "m1"})
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    await leads_module.insert_message(pool, _msg("inbound", "Actually, I have a dog."))

    cleared = [c for c in conn.execute.await_args_list if "signals = NULL" in c.args[0]]
    assert cleared, "a new message left the previous signals in place"
    # ⚠ draft_generated_at too. Leaving the stamp behind describes a freshness
    # the row no longer has, which is worse than no stamp at all.
    assert "draft_generated_at = NULL" in cleared[0].args[0]


@pytest.mark.asyncio
async def test_a_message_that_did_not_insert_clears_nothing() -> None:
    """A redelivered Twilio webhook is not new information.

    ``insert_message`` is idempotent on the MessageSid and returns no row on a
    replay. Clearing there would make every provider retry throw away a good
    assist and pay for another generation.
    """
    from helixis.db.repositories import leads as leads_module

    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value=None)  # ON CONFLICT → nothing back
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    await leads_module.insert_message(pool, _msg("inbound", "duplicate delivery"))
    assert not [c for c in conn.execute.await_args_list if "signals = NULL" in c.args[0]]


@pytest.mark.asyncio
async def test_clearing_never_raises_into_the_webhook() -> None:
    """Best-effort, like its sibling setters.

    Worst case the stale assist survives one more read — exactly where it
    already was. Raising would turn a cache miss into a failed inbound webhook,
    and Twilio would retry a message that already landed.
    """
    from helixis.db.repositories import leads as leads_module

    pool = MagicMock()
    pool.acquire.side_effect = RuntimeError("pool exhausted")
    await leads_module.clear_lead_assist(
        pool, company_id="c1", lead_id="lead-1", reason="test"
    )  # must not raise


def test_closing_a_lead_drops_its_working_state() -> None:
    """The other end of "until the task moves or is complete"."""
    import ast
    import inspect

    from helixis.api.routes import crm

    src = inspect.getsource(crm)
    tree = ast.parse(src)
    in_status_route = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        for inner in ast.walk(node)
        if isinstance(inner, ast.Attribute) and inner.attr == "clear_lead_assist"
    }
    assert in_status_route, "nothing clears the assist when a lead is closed"
