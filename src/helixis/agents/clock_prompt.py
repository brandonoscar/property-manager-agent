"""The current date, injected into every specialist's system prompt.

Live-found 2026-08-23 by the role matrix. Three probes carrying a relative date
("effective the last day of next month", an undated payment) never reached the
tool they were testing, because the agent stopped to ask what today was. Its own
explanation was exact: *"none of my tools expose the system clock directly."*

That was true. There is no clock tool in the registry — the single exception is
``GOOGLECALENDAR_GET_CURRENT_DATE_TIME``, which belongs to the calendar agent
alone and only when Google Calendar is connected. Meanwhile the orchestrator's
routing prompt instructed it not to ask for the date because "today's date is
always available to the specialists via their tools". It was not.

⚠ A tool would be the wrong fix even if one existed. A clock lookup costs a
turn, can be forgotten, and can fail — and the failure mode is the one seen
here: the agent asks the user for something the system already knows. A date in
the prompt is present before the first token, costs nothing, and cannot be
skipped.

The timezone matters and is not cosmetic. A property manager's "today" is
local: a move-out dated 2026-09-30 is a different lease month depending on which
side of midnight you compute it from. ``America/New_York`` is the default the
calendar agent already uses for the same reason.

⚠ **AND UNTIL 2026-09-10 THAT PARAGRAPH DESCRIBED A GUARANTEE THIS MODULE DID
NOT DELIVER.** ``tz_name`` defaulted to ``DEFAULT_TIMEZONE`` and **all eleven
production call sites of ``with_current_date`` passed no zone at all** — every
specialist and the orchestrator were told "Today is … in America/New_York"
regardless of whose books they were reading. Migration 0098 added
``companies.timezone`` precisely because this file was hardcoding it (gotcha
91), the sweep and two other consumers were taught to read it, and the AGENT
PROMPT — the thing gotcha 91's own reasoning is about — was never wired up.
Gotcha 25's fan-out recurring inside the fix for gotcha 25, and gotcha 48's
comment-cannot-fail on top: the docstring went on asserting a property the
code had stopped having.

The zone now arrives through a per-run ContextVar (``bind_run_timezone``, bound
once in ``run_agent_streaming``), so all eleven sites are correct without any of
them being edited and a twelfth is correct the day it is written. Passing
``tz_name`` explicitly still wins, which is what the fixed-clock tests use.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from helixis.common.company_clock import DEFAULT_TIMEZONE

#: Marker every specialist prompt must carry. The structural test greps for it,
#: so a new agent that forgets the block fails rather than silently asking the
#: user what day it is.
CURRENT_DATE_MARKER = "## Current date"

#: The timezone of the company this run belongs to, or ``None`` outside a run.
#:
#: ⚠ A ContextVar rather than a parameter ON PURPOSE. The alternative is
#: threading a zone through ``create_orchestrator`` into all eight
#: ``create_*_agent`` factories and both fast-routed branches — the per-caller
#: fan-out that gotchas 25/32/81 all end at, where the one caller who forgets is
#: silently exempt and looks identical to one that remembered. This is the same
#: mechanism the confirm emitter, the call ledger and the specialist ledger
#: already use for per-run state.
#:
#: ⚠ It survives ``asyncio.to_thread``, which is how ``run_agent_streaming``
#: builds the orchestrator. VERIFIED rather than recalled: a var set to
#: ``Pacific/Honolulu`` and read inside ``to_thread`` comes back Honolulu,
#: because ``to_thread`` runs the call under ``contextvars.copy_context()``.
_RUN_TIMEZONE: ContextVar[str | None] = ContextVar("helixis_run_timezone", default=None)


def bind_run_timezone(tz_name: str | None) -> Token[str | None]:
    """Bind this run's company timezone. Reset the token when the run ends."""
    return _RUN_TIMEZONE.set(tz_name or None)


def reset_run_timezone(token: Token[str | None]) -> None:
    _RUN_TIMEZONE.reset(token)


def run_timezone() -> str:
    """This run's zone, or the default outside a run.

    ⚠ Falls back rather than raising, which is the same posture
    ``company_clock`` takes and for the same reason: an unresolvable zone must
    cost the customisation, never the turn. A run with no zone bound behaves
    byte-for-byte as it did before this existed.
    """
    return _RUN_TIMEZONE.get() or DEFAULT_TIMEZONE


def current_date_block(*, now: datetime | None = None, tz_name: str | None = None) -> str:
    """The date preamble for a system prompt.

    ``now`` is injectable ON PURPOSE. A test that computes today the same way
    the code does proves only that two identical expressions agree; it passes
    just as happily against a prompt that says nothing. Pinning a FIXED clock is
    what makes the assertion mean something.

    ``tz_name=None`` means "this run's company zone" — the default, so a caller
    that says nothing gets the RIGHT answer instead of a New York one. An
    explicit zone still wins, which is what the fixed-clock tests rely on.
    """
    tz_name = tz_name or run_timezone()
    zone: tzinfo
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        # A bad or unavailable zone must not cost the agent its date entirely:
        # a wrong-by-hours date still beats no date, and the substitution is
        # printed so the discrepancy is visible rather than silent.
        # ⚠ The fallback is datetime.UTC, NOT ZoneInfo("UTC"). Both reach this
        # branch on a host with no tzdata (Windows ships none), and a fallback
        # that raises the same error it is catching is not a fallback.
        zone = UTC
        tz_name = f"UTC (requested {tz_name!r} unavailable)"
    stamp = (now or datetime.now(zone)).astimezone(zone)
    return (
        f"\n\n{CURRENT_DATE_MARKER}\n\n"
        f"Today is {stamp:%A, %d %B %Y} ({stamp:%Y-%m-%d}) in {tz_name}.\n"
        'Resolve relative dates yourself — "today", "tomorrow", "next month", '
        '"the last day of next month", "in 30 days" — and state the absolute date '
        "you used. NEVER ask the user what today's date is; you have it here.\n"
    )


def with_current_date(
    instructions: str, *, now: datetime | None = None, tz_name: str | None = None
) -> str:
    """``instructions`` plus the date block. Every specialist goes through this."""
    return instructions + current_date_block(now=now, tz_name=tz_name)


__all__ = [
    "CURRENT_DATE_MARKER",
    "bind_run_timezone",
    "current_date_block",
    "reset_run_timezone",
    "run_timezone",
    "with_current_date",
]
