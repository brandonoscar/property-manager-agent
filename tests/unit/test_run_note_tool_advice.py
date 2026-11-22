"""A run note never forbids the instruction a tool's answer gives.

From 2026-10-01 the Rentvine run note told the model never to send the user to
reconnect. That was written before Rentvine had a Connectors card (#1075).
Since then a rejected Rentvine key is answered "Ask the user to reconnect
Rentvine on the Connectors page" (``rentvine_tools._sanitize``), and a rejected
credential on the shared toolkit "Reconnect <system> on the Connectors page"
(``pms.gated_write.refusal``). The model was handed both, and the user's one
remedy hung on which it obeyed (rule 16).

Both sides are read (rule 15): the list each run note forbids, parsed from the
note itself, and the instruction each credentials answer gives. Which failure
earns the instruction is decided in code, by the failure's class; the note
only defers to it.
"""

from __future__ import annotations

import re

import pytest

from helixis.agents import rentvine_tools, run_pms
from helixis.pms import gated_write, registry
from helixis.pms.errors import Failure as PmsFailure
from helixis.pms.errors import PmsApiError
from helixis.rentvine.client import RentvineAPIError
from helixis.rentvine.transport import Failure as RentvineFailure

_FORBIDDEN = re.compile(r"Never tell the user to ([^:]+):")


def _forbidden(note: str) -> set[str]:
    """What the note's "Never tell the user to ..." sentence lists."""
    found = _FORBIDDEN.search(note)
    assert found, "the note no longer has its dead-end sentence: this test reads nothing"
    return {part.strip() for part in re.split(r",| or ", found.group(1)) if part.strip()}


def test_the_parser_reads_the_list_it_is_given() -> None:
    """The parser itself, so an empty set below means the note and not the regex."""
    note = "Call it X. Never tell the user to restart, re-ask or switch assistants: no."
    assert _forbidden(note) == {"restart", "re-ask", "switch assistants"}


def test_a_rejected_rentvine_key_is_answered_with_what_the_note_allows() -> None:
    answer = rentvine_tools._sanitize(
        RentvineAPIError(401, RentvineFailure.CREDENTIALS, False, "nope"),
        "create the work order",
    )
    assert "reconnect Rentvine on the Connectors page" in answer
    note = run_pms.RENTVINE_RUN_NOTE
    assert "reconnect" not in _forbidden(note)
    # The note defers to the tools rather than freeing the model to volunteer it.
    assert "reconnect Rentvine only when a tool's answer says to" in note


def _toolkit_systems() -> list[str]:
    return [
        key
        for key, found in registry.PMS.items()
        if not found.live_tools and key != registry.RENTVINE
    ]


def test_every_toolkit_system_is_read() -> None:
    assert set(_toolkit_systems()) >= {registry.RENTMANAGER, registry.PROPERTYWARE}


@pytest.mark.parametrize("key", _toolkit_systems())
def test_a_toolkit_run_note_allows_reconnecting_only_where_there_is_a_card(key: str) -> None:
    """A connector with its own run note has a card and gated writes, whose
    credentials answer says to reconnect: its note must not forbid that. A
    system with no connector yet has nothing to reconnect, so its note keeps
    the word on the forbidden list."""
    name = registry.name_of(key)
    assert name
    answer = gated_write.refusal(
        PmsApiError(401, PmsFailure.CREDENTIALS, False, "refused", sent=False),
        action="create the work order",
        name=name,
    )
    assert f"Reconnect {name} on the Connectors page" in answer
    note = run_pms.run_note_for(key)
    built = registry.connector_attr(key, "sync", "RUN_NOTE")
    if isinstance(built, str) and built:
        assert note == built
        assert "reconnect" not in _forbidden(note), (
            f"{name}'s run note forbids the instruction its credentials answer gives"
        )
    else:
        assert "reconnect" in _forbidden(note), (
            f"{name} has no connector, so there is nothing to reconnect (rule 16)"
        )
