"""The lease enricher knew the resident's surname and reconstructed a worse
answer by parsing the string it had just built.

``_resident_names`` JOINS ``FirstName`` and ``LastName`` into a display
string; the caller then did ``residents[0].split(" ")[0]`` and dropped the
surname. So ``requested_by_last_name`` was null on every lease event, while
the applicant and buildium-task enrichers beside it both fill it — and the
planner prompt calls that field *"the SENDER's name"* while
``summarizer/quality`` reads it as a grounding token. A renewal or move-out
draft therefore addressed the resident by first name only.

Tenth instance of the shape gotchas 41/50/51/88/92/93/94/98/101 record: **the
field was already in hand and thrown away.** Gotcha 41's prescription is the
fix used here — when a value is being reconstructed by parsing prose, delete
the parse and read the fields.

⚠ NOTHING PINNED THE OLD BEHAVIOUR, which is why it survived. A guard here has
to assert the surname ARRIVES, not merely that the call does not crash — the
same half-a-guard gotcha 99 records ("a guard that only asks did-it-crash
cannot distinguish a branch that works from one that renders nothing").
"""

from __future__ import annotations

import asyncio
from typing import Any

from helixis.agent.enrichment.lease_enricher import _first_resident_name_parts


def _lease(tenants: list[Any]) -> dict[str, Any]:
    return {"Tenants": tenants}


def test_the_surname_survives() -> None:
    """The defect itself."""
    first, last = _first_resident_name_parts(
        _lease([{"FirstName": "Doyle", "LastName": "Prather"}])
    )
    assert (first, last) == ("Doyle", "Prather")


def test_a_two_word_forename_is_not_truncated() -> None:
    """What the split could never have got right in either direction — and the
    reason reading the fields is better than a smarter split rather than merely
    shorter. ``"Mary Ann Van Dyke".split(" ")`` has no correct answer;
    ``FirstName``/``LastName`` do."""
    first, last = _first_resident_name_parts(
        _lease([{"FirstName": "Mary Ann", "LastName": "Van Dyke"}])
    )
    assert first == "Mary Ann"
    assert last == "Van Dyke"


def test_a_missing_half_is_none_not_empty_string() -> None:
    """``EnrichedTask`` types both as ``str | None`` and the summarizer's
    quality check tests LENGTH, so an empty string is a value that reads as
    present. Decline to assert rather than assert nothing (gotchas 55/57)."""
    first, last = _first_resident_name_parts(_lease([{"FirstName": "Doyle", "LastName": "  "}]))
    assert (first, last) == ("Doyle", None)


def test_no_tenants_yields_no_claim() -> None:
    assert _first_resident_name_parts(_lease([])) == (None, None)
    assert _first_resident_name_parts({}) == (None, None)
    assert _first_resident_name_parts(_lease(["not a dict", None])) == (None, None)


def test_the_first_named_tenant_wins_not_the_first_row() -> None:
    """A tenant row carrying no name at all must not consume the slot and
    leave the answer blank while a named resident sits behind it."""
    first, last = _first_resident_name_parts(
        _lease(
            [
                {"FirstName": "", "LastName": None},
                {"FirstName": "Aaron", "LastName": "Tillman"},
            ]
        )
    )
    assert (first, last) == ("Aaron", "Tillman")


def test_the_enricher_actually_puts_both_on_the_task() -> None:
    """The BOTH-SIDES half (gotcha 28). A helper that returns the right pair
    proves nothing about a caller that keeps using the old split — gotchas
    62/66/69/76 are four separate instances of exactly that in this repo.
    This drives the real enricher and reads the field off the task."""
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock, MagicMock

    from helixis.agent.enrichment.lease_enricher import LeaseEnricher

    lease_raw = {
        "Id": 115940,
        "LeaseStatus": "Active",
        "Tenants": [{"FirstName": "Aaron", "LastName": "Tillman"}],
    }
    api = MagicMock()

    async def _request(_method: str, path: str, **_kw):  # noqa: ANN202
        return lease_raw if path.startswith("/leases/") else {}

    api.client.request = AsyncMock(side_effect=_request)

    @asynccontextmanager
    async def _factory(_company_id):  # noqa: ANN001, ANN202
        yield api

    enricher = LeaseEnricher(api_factory=_factory)
    event = {"EventName": "Lease.Created", "LeaseId": 115940}
    enriched = asyncio.run(enricher.enrich(event, "c1"))

    assert enriched.task is not None
    assert enriched.task.requested_by_first_name == "Aaron"
    assert enriched.task.requested_by_last_name == "Tillman", (
        "the enricher is not carrying the surname onto the task — the helper "
        "can be perfect and the caller can still drop it"
    )
