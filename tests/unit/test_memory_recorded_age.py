"""An injected memory says WHEN it was written down.

Coverage item 140. ``search_for_context`` read ``category`` and ``stored_by``
off each hit's metadata and dropped ``stored_at`` — which the writer puts there
on every row. Measured on helixis-test before changing anything: **585 of 585
memories carry a populated ``stored_at``, spanning 27 Jun to 7 Sep**. So a note
written in June was injected in September as an undated bullet, under a header
calling the block *"facts previously stored by your team"*.

The harm is not vagueness, it is a false claim by omission. A note phrased in
relative time — *"waive the late fee for the Chen tenancy this month"* — is
true when written and wrong when recalled eight weeks later, and nothing on the
line let the model tell those apart. Ninth instance of the shape gotchas
41/50/51/88/92/93/94/98 record: the field was already in hand and thrown away.

⚠ **Both sides are pinned here (gotcha 28).** The writer's metadata key and the
reader's lookup are the same string, and a behavioural test proves a rendered
line actually carries the age. Testing the writer and the reader separately is
exactly what let the boundary rot — the producer was correct, the consumer was
correct, and the key between them was read by nobody.

⚠ **No corpus test, and that is a limit rather than an oversight.** The 585-row
measurement lives in a database CI cannot reach (gotcha 96's constraint), so
these guards pin the CONTRACT — writer writes it, reader renders it — and the
row count stays a measurement recorded in the note above.
"""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from helixis.memory.manager import MemoryManager, recorded_age
from helixis.settings import Settings

_ORCHESTRATOR = (
    Path(__file__).resolve().parents[2] / "src" / "helixis" / "agents" / "orchestrator.py"
)

# The one string both halves must agree on. Typed ONCE here so a rename that
# reaches only one side fails rather than silently un-dating every memory.
STORED_AT_KEY = "stored_at"


class _FakeMem0:
    """Minimal stand-in: records what add() was given, replays it to search()."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def add(
        self, content: Any, *, user_id: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.rows.append(
            {"id": f"m{len(self.rows)}", "memory": str(content), "metadata": metadata or {}}
        )
        return {"results": [{"event": "ADD"}]}

    async def search(self, query: str, *, filters: dict[str, Any], top_k: int = 10):
        return {"results": self.rows[:top_k]}


@pytest.fixture
def manager() -> MemoryManager:
    mgr = MemoryManager(Settings())
    mgr._mem0 = _FakeMem0()
    mgr._initialized = True
    return mgr


# ── the helper, across the shapes the live rows and a future writer produce ──


@pytest.mark.parametrize(
    ("stored_at", "expected"),
    [
        ("2026-09-10T04:00:00+00:00", "today"),
        ("2026-09-09T04:00:00+00:00", "yesterday"),
        ("2026-09-04T00:00:00+00:00", "6 days ago"),
        ("2026-08-20T00:00:00+00:00", "3 weeks ago"),
        # The genuine oldest live row on helixis-test.
        ("2026-06-27T00:38:53.491871+00:00", "2 months ago"),
    ],
)
def test_ages_render_at_a_useful_granularity(stored_at: str, expected: str) -> None:
    now = datetime(2026, 9, 10, 5, 0, tzinfo=UTC)
    assert recorded_age(stored_at, now=now) == expected


def test_a_naive_timestamp_is_read_as_utc_rather_than_raising() -> None:
    """Every live row carries an offset; a future writer might not.

    Comparing a naive value against an aware ``now`` raises ``TypeError``
    outright, which on this path would take out the whole injected block.
    """
    now = datetime(2026, 9, 10, 5, 0, tzinfo=UTC)
    assert recorded_age("2026-09-01T00:00:00", now=now) == "9 days ago"


def test_a_future_timestamp_clamps_instead_of_counting_backwards() -> None:
    """Clock skew must not render 'recorded in 3 days'."""
    now = datetime(2026, 9, 10, 5, 0, tzinfo=UTC)
    assert recorded_age("2026-09-13T00:00:00+00:00", now=now) == "today"


@pytest.mark.parametrize("junk", ["", "   ", "not-a-date", None, 12345, {"a": 1}])
def test_an_undateable_value_declines_rather_than_guessing(junk: Any) -> None:
    """Gotchas 55/57: no age beats an invented one, on a line a model reads
    as fact."""
    assert recorded_age(junk) == ""


# ── the boundary that actually broke ──


def test_the_writer_stores_the_key_the_reader_looks_up(manager: MemoryManager) -> None:
    """BOTH SIDES, from one constant (gotcha 28).

    The producer and the consumer were each individually correct for months;
    what nobody checked was that the reader looked up what the writer wrote.
    """
    writer = inspect.getsource(MemoryManager.add)
    reader = inspect.getsource(MemoryManager.search_for_context)
    assert f'"{STORED_AT_KEY}"' in writer, "add() no longer stores the timestamp"
    assert STORED_AT_KEY in reader, "search_for_context no longer reads the timestamp"


@pytest.mark.asyncio
async def test_an_injected_line_carries_when_it_was_written_down(
    manager: MemoryManager,
) -> None:
    """The defect's own direction, end to end through the real methods."""
    with patch("helixis.memory.manager.write_audit"):
        await manager.add(
            "Waive the late fee for the Chen tenancy this month",
            company_id="c1",
            stored_by="system",
        )
    # Age the stored row so it is unambiguously not today.
    row = manager._mem0.rows[0]  # type: ignore[attr-defined]
    row["metadata"][STORED_AT_KEY] = (datetime.now(UTC) - timedelta(days=70)).isoformat()

    with patch("helixis.memory.manager.write_audit"):
        block = await manager.search_for_context("late fee", company_id="c1")

    assert "Waive the late fee" in block
    assert "recorded 2 months ago" in block, block


@pytest.mark.asyncio
async def test_a_row_with_no_timestamp_renders_without_inventing_one(
    manager: MemoryManager,
) -> None:
    """The OTHER direction. Rendering '(recorded today)' for a row that carries
    no date would be a fresh false claim — the failure gotcha 51 records, in
    the direction that looks harmless."""
    with patch("helixis.memory.manager.write_audit"):
        await manager.add("Gate code is 4417", company_id="c1", stored_by="system")
    manager._mem0.rows[0]["metadata"].pop(STORED_AT_KEY, None)  # type: ignore[attr-defined]

    with patch("helixis.memory.manager.write_audit"):
        block = await manager.search_for_context("gate", company_id="c1")

    assert "Gate code is 4417" in block
    assert "recorded" not in block, block


@pytest.mark.asyncio
async def test_the_age_trails_the_fact_rather_than_prefixing_it(
    manager: MemoryManager,
) -> None:
    """Provenance must not push the fact off the start of the row.

    A model skimming an injected block reads the head of each line; leading
    with the age buries the thing the line exists to say.
    """
    with patch("helixis.memory.manager.write_audit"):
        await manager.add("Roof warranty expires 2027", company_id="c1", stored_by="system")

    with patch("helixis.memory.manager.write_audit"):
        block = await manager.search_for_context("roof", company_id="c1")

    line = block.splitlines()[0]
    assert line.index("Roof warranty") < line.index("recorded"), line


# ── the half that makes the dates mean something ──


def test_the_prompt_header_tells_the_model_the_notes_are_dated() -> None:
    """A date on every line is inert if the header still calls them timeless
    facts.

    ⚠ Pinned as a PROPERTY, not as the exact sentence. Gotcha 93 records that
    an assertion holds a defect in place as firmly as it catches one — a test
    demanding the current wording would have to be rewritten by anyone
    improving the prompt, which is how a guard gets deleted instead of
    re-pointed.
    """
    src = _ORCHESTRATOR.read_text(encoding="utf-8")
    start = src.index("## Relevant Company Memory")
    header = src[start : start + 900]
    # Strip comment lines: the block's own comment quotes the wording it
    # replaced in order to explain it (gotchas 40/59/91's trap).
    header = "\n".join(ln for ln in header.splitlines() if not ln.lstrip().startswith("#"))

    assert "written" in header or "stored earlier" in header, header
    assert "superseded" in header, "the header must admit an old note can be stale"
    assert "The following facts were previously stored by your team" not in header


def test_the_orchestrator_still_fences_the_recalled_block() -> None:
    """The dates change the wording around a block that is still UNTRUSTED.

    Stored memory can carry content auto-extracted from email or the web on an
    earlier turn (LED-009), so widening the header must not have loosened the
    fence. An AST walk, because a substring check would match the explanatory
    comment beside it.
    """
    tree = ast.parse(_ORCHESTRATOR.read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", "") == "wrap_untrusted"
        and any(
            isinstance(kw.value, ast.Constant) and kw.value.value == "stored company memory"
            for kw in n.keywords
        )
    ]
    assert len(calls) == 1, f"expected one fenced memory block, found {len(calls)}"
