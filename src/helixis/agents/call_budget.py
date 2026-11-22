"""A per-run budget on repeated Buildium reads — the structural anti-fan-out guard.

**Wording does not hold.** PR #850 rewrote two tool docstrings to steer the
model off per-unit hydration and pointed it at the portfolio-wide tool. The
live battery re-ran and the loop simply MOVED: 78 calls to
``buildium_list_work_orders_for_unit`` became 40 of those *plus* 72 calls to
``buildium_get_work_order``, and the run still died on the 240-second
wall-clock deadline with no answer. A cap the model cannot talk its way past
is the fix; better prose is not.

Two mechanisms, both scoped to ONE agent run by a ``ContextVar`` (the same
per-task pattern as the confirm-gate emitter and the memory context):

* **Memo** — an identical read (same tool, same arguments) returns the first
  call's answer instead of a second round-trip, with a short note so the model
  can see it is repeating itself. This is what kills the cheap waste the
  battery found: ``delinquency_report`` called 3× and
  ``list_open_work_orders`` called 8× in a single turn, each one a fresh
  Buildium fetch of bytes the agent already had.
* **Cap** — a per-record tool refuses after N *distinct* calls in a run, and
  the refusal names the portfolio-wide tool that answers the question in one
  shot. There is also a run-wide ceiling across all capped tools, because
  eight tools each capped at eight is not a bound on anything.

Two safety properties this must never violate:

* **Writes are never memoized and never capped.** Their idempotency and
  confirm gates already own repeat-suppression, and a cap that could silence a
  write would be a correctness hole, not a cost saving. ``_is_budgeted`` is a
  read-verb allowlist, so an unrecognized tool name falls through untouched.
* **A write invalidates the memo.** If the agent closes a work order and then
  re-reads it to confirm, it must see the NEW state — so any write invocation
  in the run clears every cached read.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from helixis.connectors.tool_gate import is_write_tool
from helixis.observability.reason_codes import record_reason_code

logger = logging.getLogger(__name__)

# Tool-name prefixes that identify a READ. Everything else — create_, update_,
# close_, post_, record_, add_, upload_, share_, renew_, send_ — falls through
# the guard untouched. Allowlist, not denylist: a write tool named in a way
# nobody anticipated must be ignored by this module, never capped.
_READ_PREFIXES: tuple[str, ...] = (
    "buildium_list_",
    "buildium_get_",
    "buildium_count_",
    "buildium_search_",
    "buildium_find_",
    "buildium_maintenance_history_",
)

#: The mirror-backed and Maps-backed reads, which don't share the Buildium
#: naming convention. Named one by one on purpose: ``remember_contact_alias``
#: sits in the same registry and WRITES, so a prefix rule would be wrong here.
#: The battery caught ``delinquency_report`` re-run 3× in one turn and 7× in
#: another — the same SQL, the same answer, three times the wall clock.
_MIRROR_READS: frozenset[str] = frozenset(
    {
        "portfolio_overview",
        "portfolio_review",
        "delinquency_report",
        "expiring_leases",
        "rent_roll",
        "lease_payment_history",
        "property_snapshot",
        "unpaid_bills",
        "spending_report",
        "vendor_performance",
        "leasing_funnel",
        "open_tasks",
        "open_work_orders",
        "contact_lookup",
        "bank_balances",
        "properties_near",
        "property_hazards",
        "jurisdiction_lookup",
        "find_places_near_property",
    }
)

#: Billed reads against an OUTSIDE provider that are neither mirror-backed nor
#: Buildium-prefixed nor Composio-shaped, so nothing above catches them.
#:
#: ⚠ Kept separate from ``_MIRROR_READS`` deliberately. Three guards read that
#: set as "the mirror tool registry" — ``test_call_budget`` asserts it is a
#: subset of the registered mirror tools, and ``test_mirror_freshness_contract``
#: demands an implementation for every member. ``web_search`` is in neither, so
#: filing it there would fail two tests that are right.
#:
#: ``web_search`` was unbudgeted until 2026-09-04: no memo, no out-of-time
#: refusal. Every other read family has both. Two consequences, both paid in
#: real money — an identical query repeated inside one run bought a second
#: Tavily call and a second set of tokens for a byte-identical answer (the
#: ``delinquency_report`` 3x/7x shape from gotcha 25), and a search fired past
#: the soft deadline returned sources with no turn left to write them up
#: (gotcha 32's "a refusal is free to the ledger and expensive on the clock").
#:
#: ⚠ No ``PER_RECORD_CAPS`` entry on purpose. A cap counts DISTINCT calls, and
#: distinct web queries are legitimate research — triangulating three phrasings
#: is the tool working, not a fan-out loop. What was missing is the memo and the
#: clock, not a ceiling.
_EXTERNAL_READS: frozenset[str] = frozenset({"web_search"})


@dataclass(frozen=True)
class PerRecordCap:
    """How many distinct calls one record-at-a-time tool gets, and the steer."""

    limit: int
    steer: str


#: The record-at-a-time reads. Each entry's ``steer`` names the tool that
#: answers a portfolio question in ONE call — the refusal is only useful if it
#: tells the model where to go instead.
PER_RECORD_CAPS: dict[str, PerRecordCap] = {
    # ⚠ The read-back tools added by the write-without-read census. Each is a
    # per-record read, so each needs a cap — a new one that skips this registry
    # re-opens the N+1 loop gotchas 25/32/34/37 took four PRs to bound, and the
    # cap is the only thing that has ever stopped it.
    "buildium_list_vendor_notes": PerRecordCap(
        6,
        "For a question about vendors generally use `buildium_list_vendors` or "
        "`buildium_search_vendors`. Notes are a single-vendor read.",
    ),
    "buildium_get_task_history": PerRecordCap(
        6,
        "For open work across the portfolio use `buildium_list_tasks` or "
        "`buildium_open_maintenance_by_unit`. History is a single-task read.",
    ),
    "buildium_list_lease_departures": PerRecordCap(
        6,
        "For lease questions across the portfolio use "
        "`buildium_list_active_leases` or "
        "`buildium_list_upcoming_lease_expirations`.",
    ),
    "buildium_get_unit": PerRecordCap(
        8,
        "For every unit at a property use `buildium_get_property`. For "
        "vacancies use `buildium_list_vacant_units`.",
    ),
    "buildium_list_bill_payments": PerRecordCap(
        6,
        "For payables across the portfolio use `buildium_list_unpaid_bills` "
        "(live) or `unpaid_bills` (the mirror rollup). Payment history is a "
        "single-bill read and cannot be looped over a vendor's whole history.",
    ),
    "buildium_get_bill": PerRecordCap(
        6,
        "For payables across the portfolio use `buildium_list_unpaid_bills` "
        "(live) or `unpaid_bills` (the mirror rollup) — both already carry the "
        "amount and the vendor. Opening one bill at a time to total a vendor's "
        "exposure is the fan-out that kills the run.",
    ),
    "buildium_get_work_order": PerRecordCap(
        8,
        "For maintenance across more than one unit use "
        "`buildium_open_maintenance_by_unit` (whole portfolio, one call) or "
        "`buildium_list_open_work_orders`.",
    ),
    "buildium_list_work_orders_for_unit": PerRecordCap(
        6,
        "For maintenance across more than one unit use "
        "`buildium_open_maintenance_by_unit` — the whole portfolio's open work "
        "orders AND tasks, grouped by unit, in one call.",
    ),
    "buildium_get_lease": PerRecordCap(
        8,
        "For lease questions across the portfolio use "
        "`buildium_list_active_leases`, `buildium_get_rent_roll`, or "
        "`buildium_list_upcoming_lease_expirations`.",
    ),
    "buildium_get_tenant": PerRecordCap(
        8,
        "For questions about many residents use `buildium_list_tenants` or "
        "`buildium_list_overdue_balances`.",
    ),
    "buildium_find_tenant_for_unit": PerRecordCap(
        8,
        "For questions about many residents use `buildium_list_tenants` or "
        "`buildium_get_rent_roll`, which already pairs units with residents.",
    ),
    "buildium_get_property": PerRecordCap(
        8,
        "For questions spanning properties use `buildium_list_properties`.",
    ),
    "buildium_get_owner": PerRecordCap(
        8,
        "For questions spanning owners use `buildium_list_owners`.",
    ),
    "buildium_get_vendor": PerRecordCap(
        8,
        "For questions spanning vendors use `buildium_list_vendors` or `buildium_search_vendors`.",
    ),
    "property_snapshot": PerRecordCap(
        6,
        "For questions spanning properties use `buildium_list_properties` or "
        "`buildium_open_maintenance_by_unit` — one property at a time is the "
        "fan-out that kills the run.",
    ),
    "buildium_list_recent_lease_transactions": PerRecordCap(
        8,
        "For money owed across the portfolio use "
        "`buildium_list_overdue_balances`, which totals every lease in one call.",
    ),
}

#: Distinct reads ONE connector toolkit gets per run.
#:
#: ⚠ **The connector half of the fan-out bound, and it was missing entirely.**
#: Every one of the 15 entries in ``PER_RECORD_CAPS`` is a Buildium tool. Gmail,
#: Drive, Calendar, Docs and Sheets had a memo and nothing else — and a memo
#: only collapses IDENTICAL arguments, so a model that varies its search terms
#: sails straight through. Measured on a live turn: "are there any leases not
#: being fully enforced" issued **~40 ``GMAIL_FETCH_EMAILS`` calls**, none
#: refused, none counted against ``RUN_RECORD_BUDGET`` (which summed only names
#: in ``PER_RECORD_CAPS``). The question never needed email at all.
#:
#: ⚠ **DERIVED, not enumerated — this is the standard for every connector we
#: add.** Composio tools are fetched per USER at runtime (drive alone carries
#: ~186 schemas), so there is no import-time list to hand-cap and any registry
#: would be stale the day a toolkit ships a new verb. The rule is structural
#: instead: a Composio read is capped because of its SHAPE, so a connector
#: added next month is bounded on arrival with nobody remembering to do it.
CONNECTOR_READ_CAP = 6

#: Where to send a model that has hit a connector's ceiling. Keyed by toolkit
#: because the steer is only useful if it names the thing that answers the
#: question — a bare "stop" leaves it to guess, and guessing is what produced
#: the 40 calls. A toolkit with no entry gets the generic steer, which still
#: says the one thing that always applies: this source has answered.
_CONNECTOR_STEERS: dict[str, str] = {
    "GMAIL": (
        "Email is a poor source for portfolio facts — rent, balances, leases, "
        "work orders and vendors all live in Buildium and the mirror rollups, "
        "which answer in one call. Search email only for a message you already "
        "have reason to believe exists."
    ),
    "GOOGLEDRIVE": (
        "Company documents answer POLICY questions (an SOP, a template, a "
        "signed lease). Operational state — who owes what, what is open — is "
        "Buildium's, not a document's."
    ),
    "GOOGLEDOCS": "Read the document you identified rather than searching again.",
    "GOOGLESHEETS": "Read the sheet you identified rather than searching again.",
    "GOOGLECALENDAR": "Scheduling only. Property facts are in Buildium.",
    "OUTLOOK": (
        "Email is a poor source for portfolio facts — those live in Buildium "
        "and the mirror rollups."
    ),
}

_DEFAULT_CONNECTOR_STEER = (
    "This connector has already answered as much as it can this turn. If the "
    "question is about properties, residents, money or maintenance, Buildium "
    "and the mirror rollups are the source — they answer in one call."
)


def _connector_toolkit(name: str) -> str:
    """``GMAIL_FETCH_EMAILS`` -> ``GMAIL``. Empty for anything else."""
    if not _is_composio_read(name):
        return ""
    head = name.split("_", 1)[0]
    return head if head else ""


def cap_group(name: str) -> str:
    """The budget one tool shares.

    ⚠ Connector tools are budgeted per TOOLKIT, not per tool, and that is the
    load-bearing difference. Per-tool would let ``GMAIL_FETCH_EMAILS`` and
    ``GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID`` spend six each — and the live run
    that prompted this used BOTH. A connector gets one budget however many
    verbs it ships, so adding a tool to a toolkit cannot raise its ceiling.
    """
    return _connector_toolkit(name) or name


def is_capped_group(group: str) -> bool:
    """Does this budget GROUP have a ceiling?

    ⚠ Not ``cap_for(group)``. A group is a toolkit (``GMAIL``) or a tool name
    (``buildium_get_property``), and a bare toolkit has no verb — so
    ``is_write_tool`` fails closed on it, ``_is_composio_read`` says no, and
    ``cap_for`` returns None for the very connector it is capping. Asking the
    wrong question there made the run-wide ceiling read zero while five Gmail
    slots were filled; a test caught it.
    """
    if group in PER_RECORD_CAPS:
        return True
    # Toolkits are the first segment of a SCREAMING_SNAKE slug, so they carry
    # no underscore. Alphabetic and upper is the whole shape.
    return bool(group) and group.isupper() and group.isalpha()


def cap_for(name: str) -> PerRecordCap | None:
    """The cap governing ``name``: explicit for ours, derived for connectors."""
    explicit = PER_RECORD_CAPS.get(name)
    if explicit is not None:
        return explicit
    toolkit = _connector_toolkit(name)
    if not toolkit:
        return None
    return PerRecordCap(
        CONNECTOR_READ_CAP,
        _CONNECTOR_STEERS.get(toolkit, _DEFAULT_CONNECTOR_STEER),
    )


#: Ceiling on DISTINCT per-record calls across every capped tool in one run.
#: Without this, nine tools capped at eight is a 72-call budget — exactly the
#: fan-out that killed the battery runs. At roughly two seconds a call this
#: keeps record-by-record hydration under half a minute of the 240s deadline.
RUN_RECORD_BUDGET = 14

#: Refused calls never touched the budget — only executed ones did. Battery v4
#: Q11 issued **66 ``get_work_order`` calls, 52 of them refused**, and died on
#: the wall clock with ``record_calls`` at 8 of 14: the cap bound perfectly and
#: bought nothing, because a refusal is free to the LEDGER and expensive on the
#: CLOCK (each one still costs a model turn). Past this many refusals the run
#: has demonstrated it is looping, and the message stops being advice.
RUN_REFUSAL_CEILING = 12

#: Fraction of the run's wall clock after which budgeted reads stop executing.
#: v4 lost Q5, Q7 and Q11 — 39% of the battery's spend — to the hard deadline
#: with ZERO characters returned, because the model was still hydrating records
#: when the run was killed. Nothing ever told it time was short. The ledger is
#: consulted on every read, so it is the one channel that can.
SOFT_DEADLINE_FRACTION = 0.7

_OUT_OF_TIME = (
    "STOP READING — this run is nearly out of time. Do NOT call another lookup; "
    "the next thing you produce must be the ANSWER, written from what you have "
    "already gathered. A partial answer that names what you could not check is "
    "worth everything; running out of time mid-lookup returns nothing at all."
)

#: ⚠ The SAME event, told to a reader who cannot act on the message above.
#:
#: The ledger is a ``ContextVar``, so it propagates into ``.as_tool()`` sub-runs
#: and refuses a SPECIALIST's reads with orchestrator-shaped instructions. But a
#: specialist has no user and writes no final answer — "the next thing you
#: produce must be the ANSWER" is not something it can do. Live evidence from
#: one 757-second run, four ``buildium_agent`` consults, zero data:
#:
#:     "I attempted to pull the vendor list, but the tool call didn't return
#:      usable vendor data — it came back with an interrupted/incomplete
#:      response."
#:     "If you'd like, my first move would be to pull the full active vendor
#:      list…"
#:
#: The first reads our own deadline as a provider fault, so the orchestrator
#: retries. The second asks permission from a program that cannot grant it —
#: two models talking while the clock runs out. Neither is the model being
#: careless; both are the honest response to a message aimed at somebody else.
_OUT_OF_TIME_SPECIALIST = (
    "STOP READING — the run is nearly out of time. Do NOT call another lookup "
    "and do NOT propose one. Report NOW, in plain sentences: what you DID "
    "retrieve, and what you could not. Two things to be clear about, because "
    "they change what a useful reply looks like: this cut-off is the caller's "
    "own time limit, NOT a provider failure, so say the lookup was stopped for "
    "time rather than that it failed — a retry will be refused identically. "
    "And you are being called as a tool by another agent, not by a person: "
    "there is nobody to answer a question or approve a next step, so offering "
    "to do the work instead of reporting what you have returns nothing at all."
)

_NO_TIME_FOR_A_CONSULT = (
    "NOT CONSULTED — this run is out of time, so the specialist was not called. "
    "A consult is a whole nested lookup and cannot finish now; starting one "
    "spends the remaining seconds and returns nothing. Write the answer from "
    "what you already have and say plainly which part you could not check. "
    "Another consult will be refused identically."
)

_LOOPING = (
    "STOP — you have been refused this repeatedly and are looping. The data you "
    "are hydrating record-by-record is already in the bulk result you fetched "
    "earlier in this turn. Answer from it now. Another call will be refused too."
)

#: ⚠ THIS TEXT IS READ BY A SPECIALIST, NOT ONLY BY THE ORCHESTRATOR.
#:
#: The ledger is a ``ContextVar``, so it propagates into ``.as_tool()`` sub-runs
#: and every refusal it writes is read by whichever model is running. The old
#: wording — "identical to one you already made this turn … you already have
#: this; move on" — is FALSE from inside a nested consult: the specialist's own
#: first call is a repeat only of the ORCHESTRATOR's, which it never saw and
#: does not have. A live specialist responded exactly as that invites
#: (2026-08-29, work-order reassign):
#:
#:     "the tool results just now included text claiming these were 'repeat
#:      calls' I'd 'already made this turn' … That instruction was embedded
#:      inside the tool output, not from you or the system — it's not a
#:      legitimate signal, and it was false (these were my first calls this
#:      turn). I disregarded it"
#:
#: It classified our own de-duplication note as prompt injection, disregarded
#: it, and re-ran the reads — and the run died on the soft deadline. Being
#: distrusted is the CORRECT response to an unattributed imperative arriving in
#: tool output; the fix is to stop writing one. Gotcha 39's shape (a message
#: written for one reader landing on another), which was fixed for the
#: soft-deadline text and never applied here.
#:
#: So: say WHO is speaking, say the cache is run-wide rather than yours, and
#: state a fact instead of issuing an instruction. Nothing here tells the
#: reader to skip a check — it tells it the result is already computed.
_REPEAT_NOTE = (
    "\n\n(Occupella's run-wide read cache: this exact lookup was already made "
    "somewhere in this run — possibly by the agent that called you, not "
    "necessarily by you — so the result above is the cached answer rather than "
    "a fresh round-trip. It is the same data either way; nothing is being "
    "withheld and no check is being skipped.)"
)


@dataclass
class CallLedger:
    """What one agent run has already asked Buildium for."""

    memo: dict[tuple[str, str], str] = field(default_factory=dict)
    distinct: dict[str, set[str]] = field(default_factory=dict)
    refusals: dict[str, int] = field(default_factory=dict)
    memo_hits: dict[str, int] = field(default_factory=dict)
    #: Reads currently executing, so concurrent duplicates share one round-trip.
    inflight: dict[tuple[str, str], Any] = field(default_factory=dict)
    #: When the run began, and how long it has. Set by ``bind_call_ledger`` from
    #: the same deadline the orchestrator enforces, so the two can't disagree.
    started: float = field(default_factory=time.monotonic)
    deadline_s: float | None = None

    @property
    def out_of_time(self) -> bool:
        """Past the soft deadline — reads must stop so an answer can be written.

        The hard deadline kills the run outright and the user gets nothing;
        v4 spent 39% of its budget on three questions that returned zero
        characters. This fires earlier and reaches the model through the one
        channel that is consulted on every read.
        """
        if self.deadline_s is None:
            return False
        return (time.monotonic() - self.started) >= self.deadline_s * SOFT_DEADLINE_FRACTION

    @property
    def elapsed_s(self) -> float:
        """Seconds since ``bind_call_ledger``, on the ledger's OWN clock.

        ⚠ NOT the number the deadline tests. This starts at bind time and
        never pauses; the watchdog's ``active`` accumulator starts later and
        PAUSES during a confirm wait. Keeping both on the run row is the only
        way to see the gap between them.
        """
        return time.monotonic() - self.started

    @property
    def looping(self) -> bool:
        """Refused so often that the model is clearly not taking the hint."""
        return sum(self.refusals.values()) >= RUN_REFUSAL_CEILING

    @property
    def record_calls(self) -> int:
        """Distinct per-record calls made across every capped tool.

        ⚠ Asks ``cap_for``, not ``PER_RECORD_CAPS`` — and that one word is why
        ~40 Gmail calls never touched the run budget. The membership test named
        the Buildium registry, so connector reads were invisible to the
        run-wide ceiling even while they were the thing consuming the turn.
        """
        return sum(len(v) for k, v in self.distinct.items() if is_capped_group(k))

    def invalidate(self) -> None:
        """Drop every cached read — called after a write, so a read-back verify
        sees the new state rather than the pre-write snapshot."""
        self.memo.clear()

    def summary(self) -> dict[str, Any]:
        """What the budget did this run, for ONE Langfuse event at run end.

        Battery v2 could not tell whether the cap fired: the refusal and
        repeat strings live in specialist tool output, and only orchestrator-
        level tool results reach the SSE stream (36 events against ~190 real
        calls). Inferring it from per-tool counts is what hid the concurrency
        hole for a whole run. This makes it a measurement.
        """
        return {
            "record_calls": self.record_calls,
            "run_record_budget": RUN_RECORD_BUDGET,
            "memo_hits": sum(self.memo_hits.values()),
            "refusals": sum(self.refusals.values()),
            "memo_hits_by_tool": dict(self.memo_hits),
            "refusals_by_tool": dict(self.refusals),
            "refusal_ceiling": RUN_REFUSAL_CEILING,
            "hit_refusal_ceiling": self.looping,
            "ran_out_of_time": self.out_of_time,
            # ⚠ Recorded so a reader can tell LOOP MODE from the row alone.
            # None here means the run had no wall-clock deadline at all, which
            # is a completely different diagnosis from "had one and passed it"
            # — and reading a row that omitted this field cost a wrong
            # hypothesis on the 757s run (the key was ABSENT, which SQL returns
            # as NULL, which reads as "deadline was None").
            "deadline_s": self.deadline_s,
            "elapsed_s": round(self.elapsed_s, 1),
            "distinct_by_tool": {k: len(v) for k, v in self.distinct.items()},
        }


_ledger_var: ContextVar[CallLedger | None] = ContextVar("helixis_call_ledger", default=None)

#: True while the current task is executing INSIDE a specialist consult.
#: Set by ``specialist_budget.cap_specialist_tool`` around the sub-run, so a
#: refusal can be worded for whoever is actually reading it. ContextVars are
#: copied into each Task, so a concurrent orchestrator read is unaffected by a
#: specialist consult running beside it.
_in_specialist: ContextVar[bool] = ContextVar("helixis_in_specialist", default=False)


def enter_specialist_consult() -> Any:
    """Mark the current context as inside a specialist sub-run."""
    return _in_specialist.set(True)


def leave_specialist_consult(token: Any) -> None:
    try:
        _in_specialist.reset(token)
    except ValueError:  # pragma: no cover - token from a different context
        logger.debug("specialist marker token belonged to another context")


def in_specialist_consult() -> bool:
    return _in_specialist.get()


def specialist_out_of_time() -> str | None:
    """The refusal for a NEW specialist consult, or ``None`` to allow it.

    Lives here because the deadline lives here: ``specialist_budget`` knows
    about a cap and nothing about the clock, which is exactly why nothing
    stopped the four futile consults on the 757-second run.
    """
    ledger = _ledger_var.get()
    if ledger is None or not ledger.out_of_time:
        return None
    return _NO_TIME_FOR_A_CONSULT


def current_call_ledger() -> CallLedger | None:
    """The ledger for the current run, or ``None`` when nothing bound one."""
    return _ledger_var.get()


def bind_call_ledger(
    ledger: CallLedger | None = None, *, deadline_s: float | None = None
) -> object:
    """Start a fresh budget for this run; returns the token to reset with.

    ``deadline_s`` is the SAME wall clock the orchestrator enforces. Passing it
    here is what lets reads stop early enough to leave a turn for the answer —
    without it the ledger has no clock and only the hard cutoff applies, which
    is the state that returned zero characters on three of twelve questions.
    """
    if ledger is None:
        ledger = CallLedger()
    if deadline_s is not None:
        ledger.deadline_s = deadline_s
    return _ledger_var.set(ledger)


def reset_call_ledger(token: object) -> None:
    """Unbind the ledger bound by :func:`bind_call_ledger`."""
    _ledger_var.reset(token)  # type: ignore[arg-type]


def _is_composio_read(name: str) -> bool:
    """A Composio slug that PROVABLY reads.

    Until 2026-08-27 no Composio tool was budgeted at all — ``_MIRROR_READS``
    and ``_READ_PREFIXES`` are entirely Buildium — so Gmail, Drive, Docs,
    Sheets, Calendar and Outlook reads had no memo, no cap and no
    soft-deadline channel. Measured consequence on one live turn: the
    orchestrator consulted ``gmail_agent`` FOUR times, and because each
    ``.as_tool()`` sub-run starts cold, each one re-fetched the same inbox
    from scratch — 28 model calls and $0.83 for one question about one email.

    Two independent guards, both fail-closed:

    * **Namespace.** Composio slugs are SCREAMING_SNAKE
      (``GMAIL_FETCH_EMAILS``); every tool this repo writes itself is lower
      snake_case. The case alone separates the namespaces, so no Buildium or
      mirror name can fall through this branch and pick up a memo it was
      never reviewed for.
    * **Verb.** The read verdict is ``tool_gate.is_write_tool``, which is
      already the fail-closed source of truth for this exact question — an
      unknown, new, or write-verbed slug is a WRITE and is left untouched.

    ⚠ That second direction is the load-bearing one. A memo replays a stored
    result for an identical call, so memoizing a WRITE would let a second
    send/update return the first one's success text without executing — or
    let a read-back verify see pre-write state. ``_is_budgeted`` is an
    allowlist for precisely this reason (gotcha 25), and this branch keeps
    that property by deferring to the classifier that already fails closed
    rather than pattern-matching slugs a second time (gotcha 46: the rule
    lives once).
    """
    return bool(name) and name.isupper() and not is_write_tool(name)


def _is_budgeted(name: str) -> bool:
    """True for a READ. Fails closed: an unrecognized name is untouched."""
    return (
        name in _MIRROR_READS
        or name in _EXTERNAL_READS
        or any(name.startswith(prefix) for prefix in _READ_PREFIXES)
        or _is_composio_read(name)
    )


def _arg_key(args_json: str) -> str:
    """A stable key for one argument set.

    ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` are the same call, so the
    keys are sorted. Anything unparseable is used verbatim — a memo miss is
    always safe, a false memo hit is not.
    """
    try:
        parsed = json.loads(args_json or "{}")
    except (TypeError, ValueError):
        return args_json or ""
    if not isinstance(parsed, dict):
        return args_json or ""
    return json.dumps(parsed, sort_keys=True, default=str)


def _refusal(name: str, cap: PerRecordCap, ledger: CallLedger) -> str:
    group = cap_group(name)
    seen = len(ledger.distinct.get(group, ()))
    over_run_budget = ledger.record_calls >= RUN_RECORD_BUDGET
    reached = (
        f"I've already looked up {ledger.record_calls} records one at a time this turn"
        if over_run_budget
        else f"I've already searched {group} {seen} different ways this turn"
        if group != name
        else f"I've already called `{name}` on {seen} different records this turn"
    )
    return (
        f"{reached}, which is the cap — pulling them one by one is what makes a "
        f"turn run out of time before it can answer. {cap.steer} Answer the "
        f"user with the portfolio-wide data you already have, and say plainly "
        f"which records you did not open individually."
    )


def budget_tool(tool: Any) -> Any:
    """Return a copy of ``tool`` whose invocation goes through the run budget.

    A copy, so the module-level tool singletons stay unwrapped — direct unit
    tests of a tool keep hitting the real implementation, and the wrapping is
    only what the agent gets.
    """
    name = str(getattr(tool, "name", "") or "")
    inner = getattr(tool, "on_invoke_tool", None)
    if not name or not callable(inner):
        return tool

    budgeted = _is_budgeted(name)
    cap = cap_for(name)
    # Buildium tools budget alone; a connector's tools share one budget so a
    # toolkit cannot buy headroom by shipping another verb.
    group = cap_group(name)

    async def _invoke(ctx: Any, args_json: str) -> Any:
        ledger = _ledger_var.get()
        if ledger is None:
            return await inner(ctx, args_json)
        if not budgeted:
            # A write. Never capped, never cached — and it makes every cached
            # read stale, so drop them all.
            result = await inner(ctx, args_json)
            ledger.invalidate()
            return result

        key = _arg_key(args_json)
        cached = ledger.memo.get((name, key))
        if cached is not None:
            # Counted, not reason-coded: a memo hit is ROUTINE and fires dozens
            # of times a run. REASON_CODES is the "the agent bailed out"
            # vocabulary and its events are WARNING level — flooding it with
            # normal cache hits would wreck the one dashboard that answers how
            # often Helixis actually gives up. The per-run summary carries
            # these totals instead (``ledger_summary``).
            ledger.memo_hits[name] = ledger.memo_hits.get(name, 0) + 1
            return cached + _REPEAT_NOTE

        # An identical call already in flight: await ITS result rather than
        # opening a second round-trip. The SDK issues a turn's tool calls
        # concurrently, so duplicates land together and neither would see the
        # other's memo entry.
        inflight = ledger.inflight.get((name, key))
        if inflight is not None:
            ledger.memo_hits[name] = ledger.memo_hits.get(name, 0) + 1
            shared = await asyncio.shield(inflight)
            return shared + _REPEAT_NOTE if isinstance(shared, str) else shared

        # Out of time: stop reading so there is a turn left to ANSWER in.
        # Checked before the per-tool cap because it applies to every budgeted
        # read, capped or not — a run that spends its last seconds on a fresh
        # lookup returns nothing at all, which is what happened to Q5/Q7/Q11.
        if ledger.out_of_time:
            ledger.refusals[name] = ledger.refusals.get(name, 0) + 1
            record_reason_code("call_budget_refused", detail=f"{name} out-of-time")
            return _OUT_OF_TIME_SPECIALIST if in_specialist_consult() else _OUT_OF_TIME

        seen = ledger.distinct.setdefault(group, set())
        # ⚠ Namespaced by tool. The slot set is per GROUP, so for a connector
        # it spans several tools — two of them called with the same argument
        # shape are genuinely different reads and must not collide into one.
        slot = f"{name}:{key}"
        if cap is not None and slot not in seen:
            over = len(seen) >= cap.limit or ledger.record_calls >= RUN_RECORD_BUDGET
            if over:
                ledger.refusals[name] = ledger.refusals.get(name, 0) + 1
                record_reason_code(
                    "call_budget_refused",
                    detail=f"{group} distinct={len(seen)}/{cap.limit} run={ledger.record_calls}",
                )
                logger.info(
                    "call budget refused %s (distinct=%d cap=%d run=%d/%d)",
                    name,
                    len(seen),
                    cap.limit,
                    ledger.record_calls,
                    RUN_RECORD_BUDGET,
                )
                return _refusal(name, cap, ledger) if not ledger.looping else _LOOPING
            # RESERVE the slot before awaiting. The cap is check-then-act, and
            # the SDK runs a turn's tool calls CONCURRENTLY — recording the key
            # only after the await let ten parallel calls all read an empty
            # ledger, all pass the gate, and all execute. That is exactly how
            # battery v2's Q7 made ten calls each to four per-record tools
            # under a run budget of 14. Reserving here closes the window:
            # there is no await between the check and the write, so within one
            # event loop this is atomic.
            seen.add(slot)

        task: asyncio.Future[Any] = asyncio.ensure_future(inner(ctx, args_json))
        ledger.inflight[(name, key)] = task
        try:
            result = await task
        except BaseException:
            # A failed read must not consume a slot the agent could spend on a
            # call that works.
            seen.discard(slot)
            raise
        finally:
            ledger.inflight.pop((name, key), None)
        seen.add(slot)
        if isinstance(result, str):
            ledger.memo[(name, key)] = result
        return result

    wrapped = copy.copy(tool)
    wrapped.on_invoke_tool = _invoke
    return wrapped


def budget_tools(tools: Sequence[Any]) -> list[Any]:
    """Wrap a whole registry. Order and identity-by-name are preserved."""
    return [budget_tool(t) for t in tools]


__all__ = [
    "PER_RECORD_CAPS",
    "RUN_RECORD_BUDGET",
    "CallLedger",
    "PerRecordCap",
    "bind_call_ledger",
    "budget_tool",
    "budget_tools",
    "current_call_ledger",
    "enter_specialist_consult",
    "in_specialist_consult",
    "leave_specialist_consult",
    "reset_call_ledger",
    "specialist_out_of_time",
]
