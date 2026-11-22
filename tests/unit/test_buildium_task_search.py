"""Task listing — the surface behind battery v2's worst answer.

Q9 asked "look at the water heater task in Montgomery — is the priority
right?". The agent replied that no such task existed "at the Montgomery
property or anywhere else in the portfolio", self-rated *Confidence: likely,
absence verified two ways*. Task 39431, "URGENT: water heater leaking into
the closet", was sitting at priority Low the whole time.

The tool made that answer possible: it took no arguments, showed 25 of 179
tasks, and offered no way to see the rest. A confident absence from a
truncated page is the most dangerous output this product can produce, so
these pin the three things that stop it — search across ALL tasks, an honest
"no" that says what was searched, and a truncation footer that forbids the
inference.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from helixis.agents import buildium_tools as bt
from helixis.buildium.schemas import Task


def _task(task_id: int, title: str, *, status="New", priority="Normal", property_id=37948, desc=""):
    """A task in the shape production ACTUALLY sends: the value under
    ``TaskStatus``, top-level ``Status`` absent, ``Priority`` present, and the
    property nested. Verified across all 179 live rows 2026-08-21."""
    return Task.model_validate(
        {
            "Id": task_id,
            "Title": title,
            "Description": desc,
            "TaskStatus": status,
            "Priority": priority,
            "Property": {"Id": property_id},
        }
    )


_TASKS = [
    _task(39431, "URGENT: water heater leaking into the closet", priority="Low"),
    *[_task(1000 + i, f"Routine filter change {i}") for i in range(60)],
    _task(50001, "Roof inspection", property_id=99999),
    _task(50002, "Old water heater swap", status="Completed", priority="Low"),
]


def _patched(tasks=_TASKS, properties=()):
    api = AsyncMock()
    api.tasks.list = AsyncMock(return_value=tasks)
    api.rentals.list_properties = AsyncMock(return_value=list(properties))
    return patch.object(bt, "_open_api", AsyncMock(return_value=(AsyncMock(), api, None)))


@pytest.mark.asyncio
async def test_search_finds_a_task_buried_past_the_truncation_point() -> None:
    """The exact Q9 failure. Task 39431 sits behind 60 routine tasks, so an
    unfiltered list never shows it — search must reach it anyway."""
    with _patched():
        out = await bt._list_tasks_impl(search="water heater")
    assert "URGENT: water heater leaking into the closet" in out
    assert "Routine filter change" not in out


@pytest.mark.asyncio
async def test_search_surfaces_the_priority_the_question_is_about() -> None:
    """ "Is the priority right?" is unanswerable if the tool never shows it.
    Priority lives under the ``Priority`` alias, which the snake_case read
    silently missed before it was typed."""
    with _patched():
        out = await bt._list_tasks_impl(search="water heater")
    assert "priority Low" in out


@pytest.mark.asyncio
async def test_a_completed_task_is_not_listed_as_open() -> None:
    """``Status`` is NULL on every production row and ``TaskStatus`` holds the
    value, so ``status or "New"`` called completed tasks open."""
    with _patched():
        out = await bt._list_tasks_impl(search="water heater")
    assert "Old water heater swap" not in out


@pytest.mark.asyncio
async def test_an_empty_search_says_what_it_searched() -> None:
    """The one honest "no" — and it has to state the scope, or it reads as a
    portfolio-wide absence when it was a filtered one."""
    with _patched():
        out = await bt._list_tasks_impl(search="swimming pool")
    assert "No open task(s) matching 'swimming pool'" in out
    assert f"searched all {len(_TASKS)} tasks" in out


@pytest.mark.asyncio
async def test_a_truncated_list_forbids_concluding_absence() -> None:
    """An unfiltered call is still a page. It must say so in a way the model
    can't read past — this is the sentence that would have stopped Q9."""
    with _patched():
        out = await bt._list_tasks_impl()
    assert "TRUNCATED" in out
    assert "do NOT conclude a task is absent" in out
    assert "search=" in out


@pytest.mark.asyncio
async def test_property_scope_filters_client_side() -> None:
    """Production IGNORES ?propertyids on /tasks (gotcha 24), so the scope has
    to be applied here, on the nested Property.Id."""
    from types import SimpleNamespace

    props = [SimpleNamespace(id=99999, name="Roofline Apartments", address=None)]
    with _patched(properties=props):
        out = await bt._list_tasks_impl(property_name="Roofline")
    assert "Roof inspection" in out
    assert "water heater" not in out.lower()


@pytest.mark.asyncio
async def test_an_unknown_property_is_an_honest_miss_not_an_empty_list() -> None:
    with _patched(properties=[]):
        out = await bt._list_tasks_impl(property_name="Atlantis")
    assert "couldn't find a property matching 'Atlantis'" in out


@pytest.mark.asyncio
async def test_a_search_phrase_carrying_the_place_name_still_matches() -> None:
    """The v3 failure, exactly. Asked about "the water heater task in
    Montgomery", the agent searched 'Montgomery water heater' — not a
    substring of "URGENT: water heater leaking into the closet" — so the tool
    reported no match and claimed it had searched all 90 tasks. All words must
    match across text AND location, not the phrase verbatim."""
    from types import SimpleNamespace

    props = [SimpleNamespace(id=37948, name="AL — Montgomery", address=None)]
    with _patched(properties=props):
        out = await bt._list_tasks_impl(search="Montgomery water heater")
    assert "URGENT: water heater leaking into the closet" in out


@pytest.mark.asyncio
async def test_word_order_does_not_matter() -> None:
    with _patched():
        out = await bt._list_tasks_impl(search="heater water")
    assert "URGENT: water heater leaking into the closet" in out


@pytest.mark.asyncio
async def test_all_words_must_match_not_just_one() -> None:
    """Any-word matching would make the tool useless — "roof water heater"
    would return every task mentioning any of the three."""
    with _patched():
        out = await bt._list_tasks_impl(search="water heater helicopter")
    assert "URGENT" not in out
    assert "No open task(s) matching" in out


# ── ambiguity rows must be self-contained (#10) ─────────────────────────────


class _Addr:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Tenant:
    def __init__(self, first, last, city, state, email):
        self.first_name, self.last_name, self.email = first, last, email
        self.address = _Addr(address_line1="1 Main St", city=city, state=state, postal_code="00000")


def _whitfields():
    from helixis.agents.buildium_tools import _format_tenant_ambiguity

    return _format_tenant_ambiguity(
        "Whitfield",
        [
            _Tenant("Marcus", "Whitfield", "Montgomery", "AL", "marcus@example.com"),
            _Tenant("Deja", "Whitfield", "Lansing", "MI", "deja@example.com"),
        ],
    )


def test_every_candidate_carries_its_own_address() -> None:
    """⚠ #10, as a shape rule.

    The old list carried name + email only, so an agent needing the address
    had to call ``buildium_list_tenants`` and match rows across two separate
    tool results by name. That cross-reference is the join it miswired — it
    paired Marcus Whitfield (Montgomery) with 1700 W Washington St, Phoenix,
    which belongs to Elena Vasquez, a third person not named Whitfield.

    The fix is not a better instruction to join carefully. It is deleting the
    join: the facts that tell these people apart travel on the same line as
    the name, so a candidate cannot be described by quoting one row and
    reading a field off another.
    """
    out = _whitfields()
    for line in out.splitlines():
        if not line.startswith("- "):
            continue
        # The identifying fact and the name must appear together, or the
        # cross-reference this exists to remove is back.
        assert "Whitfield" in line
        assert ("Montgomery" in line) or ("Lansing" in line), (
            f"a candidate line with no location forces a second lookup: {line!r}"
        )


def test_the_two_candidates_are_never_on_one_line() -> None:
    """Montgomery and Lansing on the same row would be the miswiring made
    structural rather than prevented."""
    for line in _whitfields().splitlines():
        assert not ("Montgomery" in line and "Lansing" in line)


def test_the_result_says_they_are_different_people_and_asks() -> None:
    """The failure was not the refusal to write — the agent refused, correctly.
    It was justifying that refusal with a confident false claim. A false
    premise that happens to produce a safe outcome is not safe behaviour."""
    out = _whitfields().lower()
    assert "different people" in out
    assert "name all of them" in out
    assert "do not combine" in out
