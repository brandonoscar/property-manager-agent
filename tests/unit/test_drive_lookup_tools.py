"""The indexed Drive search — one call instead of sixteen turns.

Why it exists (measured 2026-08-26, ``orchestrator_runs``, 14 days): a turn
that consults ``drive_agent`` averages 583-751s; a turn that does not averages
70-227s, every day, all fourteen. Same question, same day: five
``buildium_agent`` consults finished in 175s while two Buildium plus two Drive
took 561s. Every slow run contains ``drive_agent``; no fast run does.

The turns go on search → identify → rule out a superseded copy → open → read,
sequentially. ``company_knowledge/drive.py`` already caches every Drive file's
title daily, so the search half needs no API call at all — which is what this
collapses.

⚠ The corpus fixtures below are the REAL live titles (read from ``drive_index``
on 2026-08-26, 209 files / 2 companies), not invented ones. The SOP-01 pair in
particular is a real trap sitting in the real Drive, and inventing a tidier
corpus would have tested a document set that does not exist.
"""

from __future__ import annotations

import pytest

from helixis.agents.drive_lookup_tools import (
    _drive_find_files_impl,
    _terms,
    render_results,
    score_title,
)

#: Verbatim from the live index. Both SOP-01s are really there.
LIVE_TITLES = [
    "SOP-01 Maintenance Triage and Vendor Dispatch",
    "SOP-01 Maintenance Triage v2.0 (SUPERSEDED - do not use)",
    "SOP-02 Delinquency and Late Rent Escalation",
    "SOP-03 Lease Renewal and Non-Renewal",
    "Vendor Insurance and Approval Tracker 2026",
    "Basic facts",
    "Untitled document",
    "buildium_webhook_events.xlsx",
]


def _rank(query: str) -> list[str]:
    terms = _terms(query)
    scored = [(score_title(t, terms), t) for t in LIVE_TITLES]
    return [t for s, t in sorted(scored, key=lambda p: -p[0]) if s > 0]


# ── ranking against the real corpus ─────────────────────────────────────────


def test_the_live_sop_beats_the_superseded_one() -> None:
    """⚠ The trap this tool exists to defuse, and it is a REAL pair in the
    REAL Drive. Quoting the superseded SOP's spend tiers as current policy
    would be worse than returning nothing — and the marker is in the title, so
    one indexed search settles what would otherwise cost two file opens."""
    ranked = _rank("maintenance triage SOP")
    assert ranked[0] == "SOP-01 Maintenance Triage and Vendor Dispatch"
    superseded = "SOP-01 Maintenance Triage v2.0 (SUPERSEDED - do not use)"
    assert superseded in ranked, "a superseded file is DEMOTED, never hidden"
    assert ranked.index(superseded) > ranked.index(ranked[0])


def test_a_superseded_file_is_demoted_not_dropped() -> None:
    """Hiding a file the corpus contains is the absence-claim failure of
    gotcha 26 wearing a helpful costume. The user may want the old one."""
    live = score_title("SOP-01 Maintenance Triage and Vendor Dispatch", ["maintenance"])
    old = score_title("SOP-01 Maintenance Triage v2.0 (SUPERSEDED)", ["maintenance"])
    assert 0 < old < live, (
        "a matched superseded title must stay POSITIVE — callers filter on > 0 to "
        "mean 'a term matched', so a negative score turns the demotion into a hide"
    )
    assert _rank("v2.0 superseded maintenance"), "still reachable when asked for"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("delinquency", "SOP-02 Delinquency and Late Rent Escalation"),
        ("lease renewal", "SOP-03 Lease Renewal and Non-Renewal"),
        ("vendor insurance", "Vendor Insurance and Approval Tracker 2026"),
        ("webhook events spreadsheet", "buildium_webhook_events.xlsx"),
    ],
)
def test_real_questions_reach_the_right_real_file(query: str, expected: str) -> None:
    assert _rank(query)[0] == expected


def test_a_whole_word_beats_a_substring() -> None:
    """ "lease" must not be outranked by a title that merely contains the
    letters — "release", "released"."""
    terms = _terms("lease")
    assert score_title("Smith Lease 2026", terms) > score_title("Press Release 2026", terms)


def test_noise_words_do_not_match_everything() -> None:
    """With 209 files, "the document" must not rank all of them."""
    assert _terms("find the document for me please") == []
    assert _rank("find the document for me please") == []


# ── the wording contract ────────────────────────────────────────────────────


def test_no_match_forbids_concluding_the_document_is_absent() -> None:
    """⚠ Gotcha 26's rule, and gotcha 42's live failure: Drive has ALREADY
    reported "no record" over a corpus that contained the document. The result
    text has to close that reading itself, because nothing downstream can tell
    a title miss from an empty Drive."""
    out = render_results("nonexistent thing", [], total_indexed=209, limit=8)
    assert "209" in out, "state the scope that was actually searched"
    assert "TITLES ONLY" in out.upper()
    assert "do NOT tell the user the document does not exist" in out


def test_a_truncated_list_says_it_is_not_evidence_of_absence() -> None:
    rows = [
        {"file_id": f"f{i}", "title": f"Lease {i}", "mime_type": "", "path": None} for i in range(8)
    ]
    out = render_results("lease", rows, total_indexed=209, limit=8)
    assert "NOT evidence" in out


def test_a_short_list_makes_no_truncation_claim() -> None:
    rows = [{"file_id": "f1", "title": "Lease A", "mime_type": "", "path": None}]
    assert "NOT evidence" not in render_results("lease", rows, total_indexed=209, limit=8)


def test_a_superseded_row_is_flagged_in_the_output() -> None:
    rows = [
        {
            "file_id": "x",
            "title": "SOP-01 Maintenance Triage v2.0 (SUPERSEDED - do not use)",
            "mime_type": "application/vnd.google-apps.document",
            "path": None,
        }
    ]
    assert "superseded" in render_results("sop", rows, total_indexed=209, limit=8).lower()


def test_results_carry_the_file_id_to_open_next() -> None:
    """The whole point of returning titles is that ONE read follows. Without
    the id the agent has to search again to find it."""
    rows = [{"file_id": "1AbC", "title": "Smith Lease", "mime_type": "", "path": None}]
    assert "1AbC" in render_results("smith", rows, total_indexed=209, limit=8)


# ── degradation: every failure must refuse to speak for the user's Drive ────


@pytest.mark.asyncio
async def test_no_company_context_does_not_claim_the_drive_is_empty() -> None:
    out = await _drive_find_files_impl("sop")
    assert "do not report the drive as empty" in out.lower()
    assert "our limitation" in out.lower()


@pytest.mark.asyncio
async def test_a_db_error_does_not_claim_the_document_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ Gotcha 42 in one line: OUR failure must never be rendered as a fact
    about the SOURCE. That is exactly how "Max turns exceeded" became "Drive
    has no record" over a corpus that had the file."""
    from helixis.memory.context import MemoryContext, _memory_var

    async def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("pool exploded")

    monkeypatch.setattr("helixis.agents.drive_lookup_tools._search_index", _boom)
    token = _memory_var.set(MemoryContext(company_id="c1", user_id="u1", session_id="s1"))
    try:
        out = await _drive_find_files_impl("maintenance sop")
    finally:
        _memory_var.reset(token)

    assert "index was unavailable" in out or "could not be read" in out
    assert "our side" in out.lower()
    assert "empty" in out.lower() and "not" in out.lower()


@pytest.mark.asyncio
async def test_an_empty_index_names_the_two_real_causes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero indexed files means "not connected" or "not indexed yet" — two
    fixable states. It does NOT mean the Drive is empty, and the difference is
    what the user needs to hear."""
    from helixis.memory.context import MemoryContext, _memory_var

    # ⚠ Three values now: the index's own freshness stamp rides alongside the
    # rows and the count (checklist #117). A fake that kept returning two
    # raised inside the impl and landed on the error branch — the fixture
    # breaking on a new return value is the gate working (gotcha 89).
    async def _empty(*_a: object, **_k: object) -> tuple[list, int, None]:
        return [], 0, None

    monkeypatch.setattr("helixis.agents.drive_lookup_tools._search_index", _empty)
    token = _memory_var.set(MemoryContext(company_id="c1", user_id="u1", session_id="s1"))
    try:
        out = await _drive_find_files_impl("sop")
    finally:
        _memory_var.reset(token)

    assert "not connected" in out.lower() or "no google" in out.lower()
    assert "has not run yet" in out
    assert "does NOT mean" in out


@pytest.mark.asyncio
async def test_limit_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model asking for 5000 results must not pull the whole index into the
    prompt — that is the token cost this tool exists to remove."""
    from helixis.memory.context import MemoryContext, _memory_var

    seen: dict[str, int] = {}

    async def _capture(_c: str, _t: list[str], limit: int) -> tuple[list, int, None]:
        seen["limit"] = limit
        return [], 209, None

    monkeypatch.setattr("helixis.agents.drive_lookup_tools._search_index", _capture)
    token = _memory_var.set(MemoryContext(company_id="c1", user_id="u1", session_id="s1"))
    try:
        await _drive_find_files_impl("sop", limit=5000)
        assert seen["limit"] == 25
        await _drive_find_files_impl("sop", limit=0)
        assert seen["limit"] == 8
    finally:
        _memory_var.reset(token)


# ── wiring: the tool has to actually be ON the agent ────────────────────────


def test_the_specialist_registers_the_tool_when_connected() -> None:
    """Reads BOTH sides (gotcha 28). A tool nothing registers is a tool that
    has never run — and the prompt now tells the agent to start with it, so a
    missing registration makes the instruction a lie."""
    from helixis.agents.drive_agent import create_drive_agent
    from helixis.settings import get_settings

    settings = get_settings()
    connected = create_drive_agent(settings, drive_tools=[object()])
    assert "drive_find_files" in {getattr(t, "name", "") for t in connected.tools}

    # Not connected: no index search either — it could only ever end in
    # "found it, can't open it".
    disconnected = create_drive_agent(settings, drive_tools=None)
    assert "drive_find_files" not in {getattr(t, "name", "") for t in disconnected.tools}


def test_the_prompt_tells_the_agent_to_search_once() -> None:
    """Four rounds of prose failed to bound specialist fan-out (gotchas
    25/32/34), so prose alone is not the fix here either — the index IS the
    structural change. But the instruction still has to name the tool, or the
    model has no reason to prefer it over walking folders."""
    from helixis.agents.drive_agent import DRIVE_AGENT_INSTRUCTIONS

    assert "drive_find_files" in DRIVE_AGENT_INSTRUCTIONS
    assert "ONCE" in DRIVE_AGENT_INSTRUCTIONS
    assert "NEVER evidence" in DRIVE_AGENT_INSTRUCTIONS
