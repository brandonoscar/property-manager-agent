"""The registries the Buildium agent's tools come from, listed once.

Four places need the same list: the agent itself
(``buildium_agent.create_buildium_agent``), the cost-drift census
(``evals.cost_drift.write_tool_names``) and two release-safety tests
(``test_negative_should_not_act``, ``test_release_safety_gates``). Each kept
its own copy, and on the night Rentvine's registry landed two of the copies
were left behind. The three that only need names read
:func:`write_registry_tools` now; the agent reads :func:`connector_tools`
beside the registries it wraps.

A connector built on ``helixis.pms`` (Rent Manager, Propertyware) keeps its
tools in ``helixis.agents.<key>_tools`` behind a ``<key>_tools()`` factory,
and :func:`connector_tools` finds it lazily, the way ``pms.sync_cycle`` finds
a connector's ``sync_company``. A connector branch never edits the agent or
these lists (Brandon's plan: shared changes never ride inside a connector
branch).

⚠ EVERY CONNECTOR TOOL IS SHOWN ONLY ON ITS OWN SYSTEM'S RUN WITH ITS WRITES
SWITCHED ON. :func:`connector_tools` sets that itself
(``run_pms.pms_write_tool_enabled``), on copies, so a connector cannot forget
it, and a Buildium or Rentvine customer's prompt never grows (ADR 0010).

⚠ ONE CARD AT A TIME PER SYSTEM. The same copies of every tool that shows a
card (all but a connector's ``NO_CARD`` answers) take the system's one write
in flight before they run, and turn a second one away with no card
(:func:`_one_card_at_a_time`). A card the person did not approve also hides
that system's writes for the rest of the run.

What stays a hand-made edit, on purpose: a new write's name in
``test_release_safety_gates.EXPECTED_WRITE_TOOLS`` and in every 32x
should-not-act golden case. Those are review gates; a write passes them by
name or not at all.
"""

from __future__ import annotations

import copy
import importlib
from collections.abc import Awaitable, Callable
from types import ModuleType
from typing import Any

from helixis.agents.run_pms import (
    claim_write_card,
    note_write_not_approved,
    pms_write_tool_enabled,
    release_write_card,
)
from helixis.pms import registry
from helixis.pms.gated_write import held_back_reply, is_not_approved_reply


def tools_module_name(key: str) -> str:
    """Where a toolkit connector keeps its agent tools."""
    return f"helixis.agents.{key}_tools"


def _module(key: str) -> ModuleType | None:
    """``helixis.agents.<key>_tools``, or ``None`` while it does not exist.

    ⚠ Only a MISSING module reads as None (``registry.connector_attr``'s
    rule). An import that fails inside a module that exists is a bug and
    raises: hiding it would take a connector's tools off the agent in silence.
    """
    target = tools_module_name(key)
    try:
        return importlib.import_module(target)
    except ModuleNotFoundError as exc:
        if exc.name == target:
            return None
        raise


def _factory(module: ModuleType, key: str) -> Callable[[], list[Any]]:
    """The module's ``<key>_tools()`` factory. A module without one is a bug
    and raises, for the same reason as :func:`_module`."""
    factory = getattr(module, f"{key}_tools", None)
    if not callable(factory):
        raise TypeError(f"{module.__name__} defines no {key}_tools() factory")
    found: Callable[[], list[Any]] = factory
    return found


def _no_card(module: ModuleType) -> frozenset[str]:
    """The module's ``NO_CARD``: the names of its tools that send nothing and
    show no card, so they never wait for a write's card. Absent is none.

    ⚠ Leaving an answer out costs little: it waits for the card like a write.
    Listing a write here would let it show a second card, so
    ``test_pms_one_card_at_a_time`` checks that no listed tool reaches
    ``pms.gated_write`` and that every other tool does."""
    names: object = getattr(module, "NO_CARD", frozenset())
    if not isinstance(names, frozenset):
        raise TypeError(f"{module.__name__}.NO_CARD must be a frozenset of tool names")
    return names


def connector_tools() -> list[Any]:
    """Every toolkit connector's tools, as copies the model sees only on that
    system's run with its writes switched on.

    Copies, as ``run_pms.hide_on_rentvine`` makes them: the module-level
    singletons stay as the connector wrote them.
    """
    from helixis.pms import sync_cycle

    out: list[Any] = []
    for key in sync_cycle.systems():
        module = _module(key)
        if module is None:
            continue
        no_card = _no_card(module)
        shown = pms_write_tool_enabled(key)
        name = registry.name_of(key) or key
        for tool in _factory(module, key)():
            gated = copy.copy(tool)
            gated.is_enabled = shown
            if tool.name not in no_card:
                gated.on_invoke_tool = _one_card_at_a_time(gated.on_invoke_tool, key, name)
            out.append(gated)
    return out


def card_write_names() -> frozenset[str]:
    """The connector tools that show a card: every one :func:`connector_tools`
    puts behind :func:`_one_card_at_a_time`. The eval harness holds their
    mocked cards open the way a person's answer does."""
    from helixis.pms import sync_cycle

    names: set[str] = set()
    for key in sync_cycle.systems():
        module = _module(key)
        if module is not None:
            no_card = _no_card(module)
            names.update(t.name for t in _factory(module, key)() if t.name not in no_card)
    return frozenset(names)


def _one_card_at_a_time(
    invoke: Callable[[Any, str], Awaitable[Any]], key: str, name: str
) -> Callable[[Any, str], Awaitable[Any]]:
    """``invoke``, only while no other ``key`` write is in flight in this run.

    Otherwise the call is turned away before anything runs: no card, nothing
    sent, and a reply saying why (``gated_write.held_back_reply``). The SDK
    runs every call of one reply at once, so this is what keeps a second card
    from reaching a person who has not answered the first
    (``run_pms.claim_write_card``). The claim is given back when the write
    finishes, however it finishes.

    A reply saying the card was not approved (declined, or its window ran
    out) also hides ``key``'s writes for the rest of the run
    (``run_pms.note_write_not_approved``), before the claim is given back.

    ⚠ Around the SDK's invoke, not inside ``pms.gated_write``: the eval
    harness replaces each write's ``_impl``, so only a wrapper on the tool sees
    the calls and replies the write cases (530-534) produce. And here, once,
    so a connector added later cannot forget it.
    """

    async def _invoke(ctx: Any, args: str) -> Any:
        if not claim_write_card(key):
            return held_back_reply(name=name)
        try:
            reply = await invoke(ctx, args)
            if is_not_approved_reply(reply, name):
                note_write_not_approved(key)
        finally:
            release_write_card(key)
        return reply

    return _invoke


def write_registry_tools() -> list[Any]:
    """Every tool of every registry the agent's writes come from: Buildium's,
    SMS's, Rentvine's and each toolkit connector's.

    Read tools are included: callers filter (the 32x write set by verb).
    """
    from helixis.agents.buildium_tools import buildium_tools
    from helixis.agents.rentvine_tools import rentvine_tools
    from helixis.agents.sms_tools import sms_tools

    return [*buildium_tools(), *sms_tools(), *rentvine_tools(), *connector_tools()]


__all__ = ["card_write_names", "connector_tools", "tools_module_name", "write_registry_tools"]
