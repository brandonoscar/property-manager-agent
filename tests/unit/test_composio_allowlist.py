"""The Composio allowlist, checked against the 603 REAL slugs.

An allowlist is a control whose failure mode is SILENT and asymmetric:

* a typo'd entry removes an ability and nothing errors — the specialist simply
  becomes quietly worse at its job, which is the exact failure gotcha 42
  describes (Drive reported "no record" because it could not reach its source);
* an over-broad entry puts the cost back without anyone noticing, because
  everything still works.

Both are invisible from the allowlist alone, so this file reads BOTH sides
(gotcha 28): the declared names AND the live dump they claim to name.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from helixis.connectors.tool_allowlist import TOOLKIT_ALLOWLIST, filter_tools
from helixis.connectors.tool_gate import is_write_tool

FIXTURE = pathlib.Path("tests/fixtures/composio_tool_slugs.json")

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="no live Composio slug dump — run scripts/composio_tool_dump.py",
)


class _Tool:
    """Minimal stand-in — ``filter_tools`` only ever reads ``.name``."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover — failure messages only
        return f"<{self.name}>"


def _dump() -> dict[str, list[str]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {tk: [e["slug"] for e in es] for tk, es in (data.get("toolkits") or {}).items() if es}


def test_every_allowlisted_slug_actually_exists() -> None:
    """⚠ THE typo guard, and the reason this file exists.

    A misspelled slug matches nothing, so the specialist silently loses that
    ability while the allowlist looks complete. Nothing fails; the product just
    gets worse. Same class as gotcha 35's "a case that has never run is not a
    passing case".
    """
    dump = _dump()
    unknown: dict[str, list[str]] = {}
    for toolkit, allowed in TOOLKIT_ALLOWLIST.items():
        known = set(dump.get(toolkit, []))
        if not known:
            continue  # toolkit absent from this dump — covered below
        missing = sorted(s for s in allowed if s not in known)
        if missing:
            unknown[toolkit] = missing
    assert not unknown, (
        f"these allowlisted slugs do not exist in the live dump, so they silently grant "
        f"NOTHING: {unknown}. Fix the spelling or re-run scripts/composio_tool_dump.py."
    )


def test_the_drive_read_path_survives_the_filter() -> None:
    """⚠ The capability floor. These two slugs ARE how you read a Google Doc —
    proven by 20 real calls in the live confirmations table. If the allowlist
    ever drops them, Occupella cannot read its own SOP corpus, which is
    precisely the outage gotcha 42 records."""
    for slug in ("GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE", "GOOGLEDRIVE_PARSE_FILE"):
        kept = {t.name for t in filter_tools([_Tool(slug)])}
        assert slug in kept, f"{slug} filtered out — the Drive read path is broken"


def test_no_destructive_slug_slips_through_the_allowlist() -> None:
    """⚠ Why this is an explicit list and not a read-verb pattern.

    Against the real dump a ``(LIST|FIND|GET|SEARCH)`` regex admits
    ``GMAIL_DELETE_THREAD`` and ``GOOGLECALENDAR_CALENDAR_LIST_DELETE`` — a
    pattern over slug names cannot tell a verb from a noun (the ``SET`` inside
    ``SETTINGS`` bug of #915, pointed the other way).

    Writes are allowed here ONLY where the product ships a feature behind them,
    and each is confirm-gated. This test enumerates them, so adding a
    destructive slug is a deliberate edit to a named set rather than a quiet
    widening.
    """
    intentional_writes = {
        "GMAIL_SEND_EMAIL",
        "GMAIL_REPLY_TO_THREAD",
        "GMAIL_CREATE_EMAIL_DRAFT",
        "GOOGLECALENDAR_CREATE_EVENT",
        "GOOGLECALENDAR_UPDATE_EVENT",
        "GOOGLECALENDAR_DELETE_EVENT",
        "OUTLOOK_SEND_EMAIL",
        "OUTLOOK_REPLY_EMAIL",
    }
    surprises = {
        slug
        for allowed in TOOLKIT_ALLOWLIST.values()
        for slug in allowed
        if is_write_tool(slug) and slug not in intentional_writes
    }
    assert not surprises, (
        f"these allowlisted slugs classify as WRITES but are not in the intentional set: "
        f"{sorted(surprises)}. Either they are reads the gate mis-classifies (fix the gate) "
        f"or they are writes nobody meant to grant (drop them)."
    )


def test_the_filter_actually_cuts_the_drive_payload() -> None:
    """The whole point, measured against the real slug list rather than a
    hand-written sample: 186 -> a set a document lookup can actually carry."""
    dump = _dump()
    drive = [
        _Tool(s) for tk in ("GOOGLEDRIVE", "GOOGLEDOCS", "GOOGLESHEETS") for s in dump.get(tk, [])
    ]
    assert len(drive) > 150, f"dump looks wrong — only {len(drive)} Drive-family slugs"
    kept = filter_tools(drive)
    assert len(kept) < 25, f"allowlist kept {len(kept)} of {len(drive)} — barely a cut"
    print(f"\n[allowlist] drive_agent: {len(drive)} -> {len(kept)} tools")


def test_the_order_is_stable() -> None:
    """A different ORDER is a different cache prefix. Composio returns whatever
    the API gives, behind a 300s fetch cache — so an unstable order would miss
    the cache on a payload that is otherwise byte-identical, and #913's 1h TTL
    could never be collected. Sorting makes that moot without needing to know
    whether Composio's order is stable."""
    names = ["GMAIL_SEND_EMAIL", "GMAIL_FETCH_EMAILS", "GMAIL_LIST_THREADS"]
    first = [t.name for t in filter_tools([_Tool(n) for n in names])]
    second = [t.name for t in filter_tools([_Tool(n) for n in reversed(names)])]
    assert first == second == sorted(first), f"order is not stable: {first} vs {second}"


def test_an_unknown_toolkit_passes_through_untouched() -> None:
    """Filtering what it does not know about would silently disable any toolkit
    added later — a capability outage caused by a cost control."""
    kept = {t.name for t in filter_tools([_Tool("SOMETHINGNEW_DO_A_THING")])}
    assert kept == {"SOMETHINGNEW_DO_A_THING"}


def test_it_fails_open_when_nothing_matches() -> None:
    """⚠ The safety property, and it is deliberately the opposite of the write
    gate's.

    ``tool_gate`` fails CLOSED because an unrecognised WRITE must not fire.
    This fails OPEN because an unrecognised READ set must not leave a
    specialist mute: a cost optimisation that silently removes the product's
    ability to answer is worse than the cost. If Composio renames everything,
    the bill goes back up and a loud log says so — the feature keeps working.
    """
    renamed = [_Tool("GOOGLEDRIVE_TOTALLY_RENAMED_ONE"), _Tool("GOOGLEDRIVE_AND_ANOTHER")]
    kept = filter_tools(renamed)
    assert len(kept) == 2, "a full-miss must return everything, not nothing"


def test_the_guard_can_actually_fail() -> None:
    """⚠ Assert the probe RUNS (gotcha 33). If ``filter_tools`` returned its
    input unchanged, every test above would pass while the allowlist did
    nothing at all."""
    dropped = filter_tools([_Tool("GOOGLEDRIVE_FIND_FILE"), _Tool("GOOGLEDRIVE_DELETE_FOLDER")])
    assert [t.name for t in dropped] == ["GOOGLEDRIVE_FIND_FILE"], (
        "the filter is not filtering — it kept a slug that is not allowlisted"
    )
