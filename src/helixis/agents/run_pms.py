"""Which property-management system this run's company is on, resolved once.

⚠ **WHY THIS EXISTS: A RENTVINE CUSTOMER WAS TOLD TO CONNECT BUILDIUM.** The
PMS specialist carries the live Buildium tools, and every one of them opens
with ``buildium_tools._open_api``, which answers "Buildium is not connected ...
connect Buildium on the Connectors page" for any company without a Buildium
credential. A Rentvine company never has one (0100's trigger forbids holding
both). So "list the open work orders" on a Rentvine workspace went: route to the
specialist, call ``buildium_list_open_work_orders``, relay the instruction to
connect a system the customer does not use. Measured 2026-09-25 by
``scripts/rentvine_ai_e2e.py``: 7 of 10 questions routed there, and every one
that did failed.

The fix is structural, not a prompt line (CLAUDE.md rule 13): on a Rentvine run
the live Buildium tools are HIDDEN from the model (the SDK's per-run
``is_enabled``), so it cannot pick them. The mirror tools, which read what
Rentvine's sync writes, are what it has instead. :data:`RENTVINE_RUN_NOTE` only
names the system, so a reply does not call it Buildium.

⚠ **A ContextVar, like ``clock_prompt._RUN_TIMEZONE``**, and for the same
reason: the alternative threads the answer through ``create_orchestrator`` into
every agent factory and both fast-routed branches. It is bound in
``run_agent_streaming`` and propagates into ``asyncio.to_thread`` and into
specialist sub-runs (contextvars are copied, and this value is only read).

⚠ **A failed lookup HIDES NOTHING.** ``db.company_pms`` fails open to
``'buildium'``, which here means a Rentvine run during a database blip sees the
Buildium tools, which is exactly the behaviour before this module. The other
direction (hiding a Buildium customer's tools on a blip) would take their
whole product away for a turn, so it is the one this must never do.
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Callable, Iterable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from helixis.pms import registry

logger = logging.getLogger(__name__)

RENTVINE = "rentvine"
BUILDIUM = registry.BUILDIUM

_RUN_PMS: ContextVar[str | None] = ContextVar("helixis_run_pms", default=None)

#: The ``helixis.pms`` systems (Rent Manager, Propertyware) whose write card
#: this run's person did not approve: declined, or its window ran out.
#: :func:`pms_write_tool_enabled` hides that system's writes for the rest of
#: the run, so the model cannot put a second card in front of someone who just
#: turned the first one down. It did, in evals 531 and 534 (2026-10-06): the
#: same write called again after "Okay, I didn't create the work order", and
#: no prompt wording bounds that (rule 13).
#:
#: ⚠ A MUTABLE set, bound fresh by :func:`bind_run_pms` and only ever added
#: to, never re-``set``: the SDK runs each tool call in an asyncio Task, which
#: copies the context, so a ``ContextVar.set`` inside the tool would never
#: reach the next turn's ``is_enabled``. The bound object does, the way
#: ``confirm_budget`` crosses the same boundary (gotcha 42).
_RUN_NOT_APPROVED: ContextVar[set[str] | None] = ContextVar(
    "helixis_run_not_approved", default=None
)

#: The ``helixis.pms`` systems with a write in flight in this run: its card is
#: waiting on the person, or the write is still reading and planning before
#: it. :func:`claim_write_card` turns away every other write to that system
#: until :func:`release_write_card`, so the person is never shown two cards
#: for one system at once.
#:
#: ⚠ WHY HIDING WAS NOT ENOUGH: :data:`_RUN_NOT_APPROVED` hides writes from the
#: model's NEXT turn, but the SDK runs every tool call of one reply together
#: (a Task each), before any card is answered. Evals 533 and 534 called their
#: write two and three times in one reply on 2026-10-07 (#1097), so a second
#: card was already on its way when the first was declined. A rule applied
#: between turns cannot stop calls that arrive in one reply (gotcha 211).
#:
#: ⚠ Mutated, never re-``set``, for the same reason as the set above: each
#: tool call runs in a Task that copies the context.
_RUN_CARD_OPEN: ContextVar[set[str] | None] = ContextVar("helixis_run_card_open", default=None)

#: Added to a Rentvine run's input. Names the system and what it does not send,
#: so the answer uses the customer's word for their own software. The tools
#: being hidden is the guarantee; this is wording.
#:
#: ⚠ It never names the other PMS. "Rentvine, not Buildium" was echoed back
#: verbatim ("connected to Rentvine, not Buildium"), sandbox run 2026-10-01.
#: ⚠ It never forbids reconnecting. It did from 2026-10-01, before Rentvine
#: had a card, while ``rentvine_tools._sanitize`` answers a rejected key with
#: exactly that instruction; whether to give it is the tool's call, by the
#: failure's class (``test_run_note_tool_advice``).
#: ⚠ Lease rent and current tenant links ARE synced (lease export, 2026-10-01):
#: saying otherwise made the agent decline questions it could answer.
#: Owner-property links were listed as missing while the mapper wrote
#: ``property_ids = []``; without that line the agent was asked "who owns X"
#: and told the user to restart the question instead. Since 2026-10-01 the sync
#: fills them from Rentvine's portfolios (``sync._link_owner_properties``), but
#: only from complete reads, so a property can still show no owner: the note
#: says that means "not known here", never "unowned". Vendor bills sync since
#: the same day (``sync._sync_bills``), their lines on the same payables read
#: (``SYNC_STATE_STAGE`` maps ``helixis_bill_lines`` to the bills stage). Rent
#: balances sync from the lease export (``sync._replace_balances``), and each
#: lease's charges and payments ride on its rent read
#: (``getLease?includes=transactions``); ``mirror_tools.lease_payment_history``
#: reads them (2026-10-03).
RENTVINE_RUN_NOTE = (
    "\n\n## This workspace's property-management system\n"
    "This workspace runs on Rentvine, and you are its Rentvine Agent. Answer "
    "from Occupella's synced Rentvine data: properties, units, leases (dates, "
    "status and rent), tenants and which current tenant is on which lease, "
    "owners (names, and which properties each owns), vendors, work orders and "
    "vendor bills (what is owed, to whom, and when it is due), rent "
    "balances (who owes rent and how much), and rent payments (what each "
    "lease was charged and paid, and when), and turnovers (the make-ready "
    "checklist for a unit with a move-out date). If no owner is shown for a "
    "property, Occupella does not have its owner: say so, and never say the "
    "property has no owner. Spending by category and by property is "
    "available too; spend charged to a portfolio or management ledger belongs "
    "to no single property and is reported separately. Not available in "
    "Occupella yet: tasks and how much is in each bank account. "
    "Say plainly that a missing item is not available in Occupella yet "
    "and can be checked in Rentvine; never say it is zero, empty or none. "
    "Call the system Rentvine and by no other name. Never tell the user to "
    "restart, re-ask, re-route or switch assistants: you are the assistant for "
    "this workspace and there is no other one to send them to. Tell the user to "
    "reconnect Rentvine only when a tool's answer says to."
)

#: Prose names on a Rentvine run, most specific first. Lower-case identifiers
#: (``buildium_agent``, ``buildium_list_*``) are tool and agent names, never
#: shown to the user, and must stay as the SDK registered them.
_RENTVINE_WORDING = (
    ("Buildium Agent", "Rentvine Agent"),
    ("Buildium specialist", "Rentvine specialist"),
    ("Buildium", "Rentvine"),
)

#: What else marks a block as Buildium-only, beyond the hidden tools' own
#: names: a wildcard over them (``buildium_get_*``), Buildium's API-key screen,
#: and the Settings → Connectors path: those blocks walk through Buildium's card
#: (its keys, its Sync data button), which Rentvine's card (#1075) does not
#: match. A Rentvine run's remedy comes from its tools' answers instead
#: (``rentvine_tools._sanitize``).
#: Case-SENSITIVE: prose says "Buildium" and is reworded, not dropped (the
#: first cut ignored case and took "Out of scope" and "Confirmation" with it).
#: ``buildium_agent`` is the specialist's registered name and stays.
_BUILDIUM_ONLY = (r"\bbuildium_(?!agent\b)", r"Developer Tools", r"Settings → Connectors")

#: Phrases cut before the blocks are read, so the block they sit in survives.
_RENTVINE_CUTS = (re.compile(r"\s*/ the live buildium vendor tools"),)

#: Sections of the specialist prompt that are wrong for Rentvine as a WHOLE,
#: replaced by heading. Each says something no Rentvine run can do: fall back
#: to a live read tool (there is none), reconnect on a Connectors screen
#: (those blocks name Buildium's card and its keys), restart so "the
#: orchestrator routes" elsewhere (the dead end this module exists to remove).
#: ``test_every_section_swap_matches`` fails if a heading moves, so a swap
#: cannot silently stop matching.
RENTVINE_SECTIONS: tuple[tuple[str, str], ...] = (
    (
        "## Out of scope",
        "## Out of scope\n\n"
        "You handle this workspace's Rentvine data only. If the user asks about "
        "email, calendar or files, say in one sentence that it is outside what "
        "you can do here. Do NOT pretend you can do it.\n\n",
    ),
    (
        "## When the Buildium connection is broken",
        "## When Rentvine cannot be reached\n\n"
        "If a tool says Occupella could not reach Rentvine or Rentvine refused "
        "the request, say you could not check that, in one short sentence, and "
        "answer the rest from the synced data. Never present it as an answer "
        'about their portfolio: "you have no work orders" is false when the '
        "tool never got in.\n\n",
    ),
    (
        "## Two data sources:",
        "## Where this workspace's data comes from\n\n"
        "Every read comes from Occupella's synced copy of Rentvine, through the "
        "mirror tools (``portfolio_overview``, ``property_snapshot``, "
        "``open_work_orders``, ``contact_lookup``, ``expiring_leases``, "
        "``rent_roll``, ``lease_payment_history`` and the rest). There is no "
        "live Rentvine read tool. "
        "When a mirror tool says data "
        "is not synced, say it is not available in Occupella yet and can be "
        "checked in Rentvine.\n\n",
    ),
)


def _section_re(heading: str) -> re.Pattern[str]:
    """``heading`` to the next ``##`` heading (or the end)."""
    return re.compile(rf"^{re.escape(heading)}.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)


@dataclass(frozen=True)
class RunPmsToken:
    """What :func:`bind_run_pms` bound, for :func:`reset_run_pms`."""

    pms: Token[str | None]
    not_approved: Token[set[str] | None]
    card_open: Token[set[str] | None]


def bind_run_pms(pms: str | None) -> RunPmsToken:
    """Bind this run's PMS, an empty record of the writes its person did not
    approve, and an empty record of the writes in flight. Reset the token when
    the run ends."""
    return RunPmsToken(
        _RUN_PMS.set(pms or None), _RUN_NOT_APPROVED.set(set()), _RUN_CARD_OPEN.set(set())
    )


def reset_run_pms(token: RunPmsToken) -> None:
    _RUN_CARD_OPEN.reset(token.card_open)
    _RUN_NOT_APPROVED.reset(token.not_approved)
    _RUN_PMS.reset(token.pms)


def note_write_not_approved(key: str) -> None:
    """Record that a ``key`` write card was not approved in this run, so
    :func:`pms_write_tool_enabled` hides ``key``'s writes until it ends.

    Outside a bound run (a test, a script) there is no record and this does
    nothing: unbound means unbounded, as ``confirm_budget`` reads it.
    """
    turned_down = _RUN_NOT_APPROVED.get()
    if turned_down is not None:
        turned_down.add(key)


def claim_write_card(key: str) -> bool:
    """Take ``key``'s one write in flight for this run, or say it is taken.

    False while another ``key`` write is in flight, or once one of its cards
    was not approved (the model's next turn no longer sees the writes, but a
    call from the same reply still arrives here). True otherwise, and the
    claim holds until :func:`release_write_card`. Outside a bound run (a test,
    a script) always True: unbound means unbounded, as ``confirm_budget``
    reads it.

    ⚠ No ``await`` between the check and the add. Every tool call of one
    reply runs in the same event loop, so with none, two calls cannot both
    see the slot free.
    """
    in_flight = _RUN_CARD_OPEN.get()
    if in_flight is None:
        return True
    if key in in_flight or key in (_RUN_NOT_APPROVED.get() or ()):
        return False
    in_flight.add(key)
    return True


def release_write_card(key: str) -> None:
    """Give back the claim :func:`claim_write_card` took: the write finished,
    whatever its outcome (approved, declined, refused, raised, cancelled)."""
    in_flight = _RUN_CARD_OPEN.get()
    if in_flight is not None:
        in_flight.discard(key)


def run_on_rentvine() -> bool:
    """True only when this run's company was positively resolved to Rentvine."""
    return _RUN_PMS.get() == RENTVINE


def run_pms_spec() -> registry.PmsSpec | None:
    """The system this run was positively resolved to, or None (unbound, a
    failed lookup, or a value the registry does not know)."""
    return registry.spec(_RUN_PMS.get())


def run_on_mirror_pms() -> bool:
    """True when this run's company was positively resolved to a system read
    from the mirror only: every PMS but Buildium, Rentvine included."""
    return registry.is_mirror_pms(_RUN_PMS.get())


async def resolve_run_pms(company_id: str) -> str | None:
    """This run's PMS, or ``None``. Never raises: see the module docstring."""
    if not company_id:
        return None
    try:
        from helixis.db.company_pms import company_pms
        from helixis.db.engine import get_pool

        return await company_pms(get_pool(), company_id)
    except Exception:  # noqa: BLE001 — a routing refinement must never fail a turn
        logger.warning("could not resolve the company's PMS for this run", exc_info=True)
        return None


def buildium_live_tool_enabled(_ctx: Any, _agent: Any) -> bool:
    """The SDK's per-run ``is_enabled`` for a live Buildium tool.

    Shown on a Buildium run and on an unresolved one (a failed lookup hides
    nothing: see the module docstring). Hidden on every other system's run,
    because each live Buildium tool answers "connect Buildium" there. Before
    Rent Manager and Propertyware this read ``not run_on_rentvine()``, which
    would have shown them all."""
    return _RUN_PMS.get() in (None, BUILDIUM)


def rentvine_write_tool_enabled(_ctx: Any, _agent: Any) -> bool:
    """The SDK's per-run ``is_enabled`` for the Rentvine creates (work order,
    file, charge, payment). On any other run each could only refuse, and its
    schema would change a Buildium customer's prompt (ADR 0010). A failed PMS
    lookup hides them, like :func:`rentvine_mirror_tool_enabled`."""
    return run_on_rentvine()


def rentvine_status_tool_enabled(_ctx: Any, _agent: Any) -> bool:
    """The SDK's per-run ``is_enabled`` for Rentvine's status change, the one
    Rentvine write older than run gating.

    ⚠ Hidden ONLY on a run positively resolved to a connector built on
    ``helixis.pms`` (Rent Manager, Propertyware). There, with that system's
    writes off, it was the one status tool the model saw, and its answer
    ("connect Rentvine on the Connectors page") sent the user to a system they
    do not use. Buildium, Rentvine and unresolved runs see it as they always
    have: hiding it there would change a Buildium customer's prompt (ADR 0010).
    """
    found = run_pms_spec()
    return found is None or found.key in (BUILDIUM, RENTVINE)


def pms_write_tool_enabled(key: str) -> Callable[[Any, Any], bool]:
    """The SDK's per-run ``is_enabled`` for a write tool of a connector built
    on ``helixis.pms`` (Rent Manager, Propertyware).

    Shown only on a run positively resolved to ``key``, and only while that
    system's ``writes_enabled`` switch is on (design P4: reads only until
    Brandon turns writes on). Both are read on every run, so a redeploy that
    turns writes off hides the tools on the next turn. A failed PMS lookup
    hides them, like :func:`rentvine_write_tool_enabled`.

    ⚠ The tool is hidden, and ``pms.gated_write`` checks the same switch again
    before any card: this is what the model sees, that is what can happen.
    Buildium and Rentvine have their own write gates, so asking for theirs is
    a bug and raises.

    Also hidden for the rest of the run once one of ``key``'s write cards was
    not approved (:data:`_RUN_NOT_APPROVED`): all of that system's writes, not
    only the one turned down, because a second card reached by another write
    is the same second card. A request for two writes in one message then
    gets the second only when asked again, which is the cheaper mistake.

    Hiding reaches the model's next turn only. Calls that arrive together in
    one reply are bounded by :func:`claim_write_card` instead.
    """
    found = registry.spec(key)
    if found is None or found.live_tools or key == RENTVINE:
        raise ValueError(f"{key!r} is not a system whose writes go through pms.gated_write")

    def _enabled(_ctx: Any, _agent: Any) -> bool:
        turned_down = _RUN_NOT_APPROVED.get() or ()
        return _RUN_PMS.get() == key and registry.writes_enabled(key) and key not in turned_down

    _enabled.__name__ = f"{key}_write_tool_enabled"
    return _enabled


def rentvine_mirror_tool_enabled(_ctx: Any, _agent: Any) -> bool:
    """The SDK's per-run ``is_enabled`` for a mirror tool shown only where the
    live Buildium tools are hidden: one whose question a live Buildium tool
    already answers on a Buildium run (``mirror_tools.rent_roll``). A failed PMS
    lookup hides it, the same direction :func:`buildium_live_tool_enabled`
    fails in.

    ⚠ The name is historical. Rentvine was the first system read from the
    mirror only, and the rule is "every such system", so a Rent Manager or
    Propertyware run gets these tools too."""
    return run_on_mirror_pms()


def rentvine_wording(text: str) -> str:
    """``text`` as a Rentvine run's agent reads it: the specialist is the
    Rentvine Agent, and the other PMS is not named anywhere.

    The specialist's prompt is written for Buildium. Told "You are the Buildium
    Agent", it answered a Rentvine owner question with "I'm the Buildium
    specialist ... restart your question so it routes to the Rentvine-aware
    assistant", an assistant that does not exist (sandbox run 2026-10-01,
    CLAUDE.md rule 16).

    ⚠ Blocks that name a hidden live tool go FIRST, whole. Reworded, a line
    like "``buildium_list_active_leases`` — current rent roll" read as advice
    for this run: the model called the hidden tool, the SDK could not find it,
    and the turn died (2 of 3 sandbox rent-roll runs, 0 of 3 on main). A name
    the model never reads is a name it cannot call; prose saying "don't call
    it" is not a bound (rule 13)."""
    for heading, replacement in RENTVINE_SECTIONS:
        # A function, not a string: the replacement is literal text, never a template.
        text = _section_re(heading).sub(lambda _m: replacement, text)  # noqa: B023
    for cut in _RENTVINE_CUTS:
        text = cut.sub("", text)
    text = _drop_blocks_naming(text, _hidden_tool_names())
    for old, new in _RENTVINE_WORDING:
        text = text.replace(old, new)
    return text


def _mirror_sections(name: str) -> tuple[tuple[str, str], ...]:
    """:data:`RENTVINE_SECTIONS` with ``name`` in place of "Rentvine"."""
    return (
        (
            "## Out of scope",
            "## Out of scope\n\n"
            f"You handle this workspace's {name} data only. If the user asks about "
            "email, calendar or files, say in one sentence that it is outside what "
            "you can do here. Do NOT pretend you can do it.\n\n",
        ),
        (
            "## When the Buildium connection is broken",
            f"## When {name} cannot be reached\n\n"
            f"If a tool says Occupella could not reach {name} or {name} refused "
            "the request, say you could not check that, in one short sentence, and "
            "answer the rest from the synced data. Never present it as an answer "
            'about their portfolio: "you have no work orders" is false when the '
            "tool never got in.\n\n",
        ),
        (
            "## Two data sources:",
            "## Where this workspace's data comes from\n\n"
            f"Every read comes from Occupella's synced copy of {name}, through the "
            "mirror tools (``portfolio_overview``, ``property_snapshot``, "
            "``open_work_orders``, ``contact_lookup``, ``expiring_leases``, "
            "``rent_roll``, ``lease_payment_history`` and the rest). There is no "
            f"live {name} read tool. "
            "When a mirror tool says data "
            "is not synced, say it is not available in Occupella yet and can be "
            f"checked in {name}.\n\n",
        ),
    )


def mirror_pms_wording(text: str, name: str) -> str:
    """``text`` as a run on the mirror-only system ``name`` reads it.

    The same pass as :func:`rentvine_wording` with the system's name filled in,
    for Rent Manager and Propertyware. ⚠ ``mirror_pms_wording(t, "Rentvine")``
    must equal ``rentvine_wording(t)`` on the live specialist prompt
    (``test_pms_run_wording``): that is what proves this is Rentvine's measured
    wording and not a new one, without routing Rentvine's runs through it."""
    for heading, replacement in _mirror_sections(name):
        # A function, not a string: the replacement is literal text, never a template.
        text = _section_re(heading).sub(lambda _m: replacement, text)  # noqa: B023
    for cut in _RENTVINE_CUTS:
        text = cut.sub("", text)
    text = _drop_blocks_naming(text, _hidden_tool_names())
    for old, new in (
        ("Buildium Agent", f"{name} Agent"),
        ("Buildium specialist", f"{name} specialist"),
        ("Buildium", name),
    ):
        text = text.replace(old, new)
    return text


def _connector_not_built_note(name: str) -> str:
    """The run note for a mirror-only system whose connector exposes no
    ``RUN_NOTE`` yet. Nothing is synced, so it says so, in the shape of
    :data:`RENTVINE_RUN_NOTE` (same rules: name the system, never zero, never
    send the user elsewhere)."""
    return (
        "\n\n## This workspace's property-management system\n"
        f"This workspace runs on {name}, and you are its {name} Agent. "
        f"Occupella does not sync {name} data yet, so nothing about this "
        "workspace's properties, units, leases, tenants, owners, vendors, work "
        "orders or money is available here. Say plainly that it is not available "
        f"in Occupella yet and can be checked in {name}; never say it is zero, "
        f"empty or none. Call the system {name} and by no other name. Never tell "
        "the user to restart, re-ask, re-route, reconnect or switch assistants: "
        "you are the assistant for this workspace and there is no other one to "
        "send them to."
    )


def run_note_for(pms: str | None) -> str:
    """What a run on ``pms`` adds to the model's input.

    Rentvine: :data:`RENTVINE_RUN_NOTE`. Another mirror-only system: its
    connector's ``sync.RUN_NOTE``, or the not-built note until that exists.
    Buildium or an unresolved run: nothing, which is what they always got."""
    if pms == RENTVINE:
        return RENTVINE_RUN_NOTE
    found = registry.spec(pms)
    if found is None or found.live_tools:
        return ""
    note = registry.connector_attr(found.key, "sync", "RUN_NOTE")
    return note if isinstance(note, str) and note else _connector_not_built_note(found.name)


def run_note() -> str:
    """:func:`run_note_for` this run's bound PMS."""
    return run_note_for(_RUN_PMS.get())


def _hidden_tool_names() -> frozenset[str]:
    """The live Buildium tools :func:`hide_on_rentvine` hides. Imported here,
    not at module load: ``buildium_agent`` imports this module."""
    from helixis.agents.buildium_tools import buildium_tools

    return frozenset(t.name for t in buildium_tools())


_ITEM_RE = re.compile(r"^(\s*)(?:[-*•]|\d+[.)])\s")
_NUMBERED_RE = re.compile(r"^(\d+)([.)]\s)")


def _drop_blocks_naming(text: str, names: frozenset[str]) -> str:
    """``text`` without every block that names one of ``names`` or matches
    :data:`_BUILDIUM_ONLY`.

    A block is a list item with its continuation lines and deeper sub-items,
    or a paragraph; a heading or a blank line ends one. Whole blocks, never
    lines: most mentions sit mid-sentence in a wrapped item, and cutting the
    line alone left a sentence with no start. Then: headings left with nothing
    under them go, and a numbered list that lost an item is renumbered."""
    named = [r"\b(?:" + "|".join(sorted(map(re.escape, names))) + r")\b"] if names else []
    pattern = re.compile("|".join([*named, *_BUILDIUM_ONLY]))
    blocks: list[list[str]] = []
    indent: int | None = None  # the open block's item indent; None = paragraph
    for line in text.split("\n"):
        item = _ITEM_RE.match(line)
        starts = (
            not line.strip()
            or line.lstrip().startswith("#")
            or not blocks
            or not blocks[-1][-1].strip()
            or blocks[-1][-1].lstrip().startswith("#")
            or (item is not None and (indent is None or len(item.group(1)) <= indent))
        )
        if starts:
            blocks.append([line])
            indent = len(item.group(1)) if item else None
        else:
            blocks[-1].append(line)
    kept = _renumber(_drop_empty_headings([b for b in blocks if not pattern.search("\n".join(b))]))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line for b in kept for line in b))


def _is_heading(block: list[str]) -> bool:
    """A ``##`` heading, or a one-line label ending in a colon ("Lease tools:")."""
    line = block[0].strip()
    return len(block) == 1 and (line.startswith("#") or (line.endswith(":") and len(line) < 80))


def _rank(block: list[str]) -> int:
    """``#`` count for a heading; labels rank below every heading."""
    line = block[0].strip()
    return len(line) - len(line.lstrip("#")) if line.startswith("#") else 9


def _drop_empty_headings(blocks: list[list[str]]) -> list[list[str]]:
    """Headings whose whole section was dropped, read from the end so a
    heading over only empty labels goes too. "Property tools:" with nothing
    under it is a sentence with no end."""
    out: list[list[str]] = []
    for block in reversed(blocks):
        following = next((b for b in out if b[0].strip()), None)
        # Empty: nothing follows, or a peer or higher heading ends it at once.
        empty = following is None or (_is_heading(following) and _rank(following) <= _rank(block))
        if _is_heading(block) and empty:
            continue
        out.insert(0, block)
    return out


def _renumber(blocks: list[list[str]]) -> list[list[str]]:
    """Top-level "1. 2. 3." items numbered again in order; any other
    non-blank block restarts the count, as a new list would."""
    out: list[list[str]] = []
    n = 0
    for block in blocks:
        match = _NUMBERED_RE.match(block[0])
        if match:
            n += 1
            block = [_NUMBERED_RE.sub(rf"{n}\2", block[0], count=1), *block[1:]]
        elif block[0].strip():
            n = 0
        out.append(block)
    return out


def reword_for_rentvine(agents: Iterable[Any]) -> None:
    """On a Rentvine run, apply :func:`rentvine_wording` to each agent's
    instructions; on another mirror-only system's run (Rent Manager,
    Propertyware), :func:`mirror_pms_wording` with its name. In place: every
    factory builds a fresh ``Agent`` per turn. A no-op on a Buildium or
    unresolved run, so a Buildium customer's prompt is unchanged.

    ⚠ The name is historical. The call sites keep it because
    ``test_rentvine_chat_routing`` counts them by name (every fast route and
    ``create_orchestrator`` must reword), and that pin covers every system."""
    found = run_pms_spec()
    if found is None or found.live_tools:
        return
    for agent in agents:
        if isinstance(agent.instructions, str):
            if found.key == RENTVINE:
                agent.instructions = rentvine_wording(agent.instructions)
            else:
                agent.instructions = mirror_pms_wording(agent.instructions, found.name)


def hide_on_rentvine(tools: Iterable[Any]) -> list[Any]:
    """Copies of ``tools`` that the model cannot see on a Rentvine run, or on
    any other system's run but Buildium's (:func:`buildium_live_tool_enabled`;
    the name is historical).

    Copies, so the module-level singletons keep ``is_enabled=True`` and every
    other registry that reads them (the 32x negative cases, the cost-drift
    census) is unchanged. ``call_budget.budget_tool`` copies with
    ``copy.copy`` too, so the flag survives being wrapped after this.
    """
    out = []
    for tool in tools:
        hidden = copy.copy(tool)
        hidden.is_enabled = buildium_live_tool_enabled
        out.append(hidden)
    return out


__all__ = [
    "RENTVINE",
    "RENTVINE_RUN_NOTE",
    "RENTVINE_SECTIONS",
    "RunPmsToken",
    "bind_run_pms",
    "buildium_live_tool_enabled",
    "claim_write_card",
    "hide_on_rentvine",
    "mirror_pms_wording",
    "note_write_not_approved",
    "pms_write_tool_enabled",
    "release_write_card",
    "rentvine_mirror_tool_enabled",
    "rentvine_write_tool_enabled",
    "reset_run_pms",
    "rentvine_wording",
    "resolve_run_pms",
    "reword_for_rentvine",
    "run_note",
    "run_note_for",
    "run_on_mirror_pms",
    "run_on_rentvine",
    "run_pms_spec",
]
