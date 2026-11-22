"""Nothing could leave the chat, and the way out was wired to a dark subsystem.

Occupella computes rent rolls, delinquency reports and portfolio reviews, and
the only way to get one out was to select the text and copy it. Every
downstream thing a property manager does with a report — send it to an owner,
open it in a spreadsheet, attach it to a filing — starts with a file.

⚠ The last mile already existed. ``persist_artifacts`` uploads company-scoped
bytes and returns a signed URL, and its own docstring says it is *"how
'generate an owner statement / CSV' reaches the user"*. It was reachable only
from the code sandbox, which is dark and must stay dark until the E2B keys ship
(gotcha 33) — so the single path out of the chat ran through a disabled
subsystem, and serialising a table the model already holds never needed a
sandbox at all.

⚠ **CSV injection is the real hazard here, and it is reachable rather than
theoretical.** A cell beginning ``=``, ``+``, ``-`` or ``@`` is executed as a
formula by Excel and Sheets. Buildium free text is attacker-writable — a
resident types the work-order description — and the person opening the export
is typically an OWNER receiving a file their property manager sent, so it
arrives fully trusted.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from helixis.agents import export_tools
from helixis.agents.export_tools import (
    MAX_EXPORT_ROWS,
    _defuse,
    _export_table_impl,
    _safe_filename,
    _to_csv,
)

_URL = "https://storage.example/signed/abc"


def _persist(captured: dict[str, Any]) -> Any:
    async def _fake(artifacts: dict[str, bytes], *, company_id: str, settings: Any) -> list[dict]:
        captured["artifacts"] = artifacts
        captured["company_id"] = company_id
        name = next(iter(artifacts))
        return [{"name": name, "size": len(artifacts[name]), "url": _URL}]

    return _fake


async def _run(title: str, columns: list, rows: list, captured: dict[str, Any]) -> str:
    with (
        patch.object(export_tools, "_export_table_impl", _export_table_impl),
        patch("helixis.agents.mirror_tools._company_id", return_value="co-1"),
        patch("helixis.codeexec.storage_io.persist_artifacts", _persist(captured)),
        patch("helixis.settings.get_settings", return_value=object()),
    ):
        return await _export_table_impl(title, json.dumps(columns), json.dumps(rows))


# ── CSV injection ────────────────────────────────────────────────────


@pytest.mark.parametrize("lead", ["=", "+", "-", "@", "\t", "\r"])
def test_a_formula_lead_is_neutralised(lead: str) -> None:
    """⚠ THE hazard. ``=cmd|'/c calc'!A1`` in a tenant note becomes code
    running on the recipient's machine when they open the file."""
    out = _defuse(f'{lead}HYPERLINK("http://evil")')
    assert out.startswith("'"), f"{lead!r} lead was not defused"


def test_defusing_preserves_the_value() -> None:
    """⚠ Prefixed, not stripped. Deleting characters from somebody's note to
    make it safe silently changes the record — and a rent of "-450" must still
    read as -450 to whoever opens the file."""
    assert _defuse("-450") == "'-450"
    assert "450" in _defuse("-450")


def test_ordinary_values_are_untouched() -> None:
    """Both directions (gotcha 38). Quoting every cell would make the whole
    export look mangled, which is how a safety measure gets switched off."""
    for value in ("Dana Reed", "1200.50", "3C", "", "a-b"):
        assert _defuse(value) == value


def test_headers_are_defused_too() -> None:
    """A column name can be model-written or user-supplied; the header row is
    not a safe zone."""
    data, _, _ = _to_csv(["=BAD()"], [["x"]])
    assert data.decode().startswith('"\'=BAD()"') or data.decode().startswith("'=BAD()")


@pytest.mark.asyncio
async def test_injection_survives_the_whole_path() -> None:
    """⚠ Reads the BYTES that would actually be uploaded, not the helper. A
    defused helper whose caller bypasses it is this branch's recurring
    defect — the guard has to look at what leaves."""
    captured: dict[str, Any] = {}
    await _run("notes", ["note"], [["=cmd|'/c calc'!A1"]], captured)
    body = next(iter(captured["artifacts"].values())).decode()
    assert "\n'=cmd" in body or "\n\"'=cmd" in body, body


# ── truncation is never silent ───────────────────────────────────────


def test_a_truncated_export_says_so() -> None:
    """A spreadsheet holding the first 5,000 of 12,000 rows is one somebody
    totals up and acts on. Gotcha 26, with the stakes raised: the chat at
    least shows a footer, a file shows nothing."""
    _, written, truncated = _to_csv(["a"], [[i] for i in range(MAX_EXPORT_ROWS + 10)])
    assert written == MAX_EXPORT_ROWS
    assert truncated is True


def test_an_untruncated_export_is_not_flagged() -> None:
    _, written, truncated = _to_csv(["a"], [[1], [2]])
    assert written == 2
    assert truncated is False


@pytest.mark.asyncio
async def test_the_reply_carries_the_truncation_warning() -> None:
    """The flag has to reach the sentence the user reads, or it is a field
    nobody consults."""
    captured: dict[str, Any] = {}
    out = await _run("big", ["a"], [[i] for i in range(MAX_EXPORT_ROWS + 5)], captured)
    assert "TRUNCATED" in out
    assert "NOT the complete set" in out


@pytest.mark.asyncio
async def test_a_normal_export_makes_no_truncation_claim() -> None:
    captured: dict[str, Any] = {}
    out = await _run("small", ["a"], [[1]], captured)
    assert "TRUNCATED" not in out
    assert _URL in out


# ── scope and honesty ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_upload_is_company_scoped() -> None:
    """⚠ Gotcha 47 on a path that hands out a URL. An export written outside
    the caller's company is another tenant's data behind a link."""
    captured: dict[str, Any] = {}
    await _run("x", ["a"], [[1]], captured)
    assert captured["company_id"] == "co-1"


@pytest.mark.asyncio
async def test_no_company_means_no_file() -> None:
    with patch("helixis.agents.mirror_tools._company_id", return_value=""):
        out = await _export_table_impl("x", '["a"]', "[[1]]")
    assert "without knowing which company" in out


@pytest.mark.asyncio
async def test_an_empty_table_produces_no_file() -> None:
    """An empty CSV is a file somebody downloads, opens, and reads as "there
    is nothing" — which may be true or may be a failed query. Say it in
    words instead."""
    captured: dict[str, Any] = {}
    out = await _run("x", ["a"], [], captured)
    assert "no rows to export" in out
    assert "artifacts" not in captured, "nothing should have been uploaded"


@pytest.mark.asyncio
async def test_a_storage_failure_does_not_cost_the_answer() -> None:
    """The figures were already correct in the reply. An export failing must
    degrade to "I couldn't save it", never to a lost turn."""
    with (
        patch("helixis.agents.mirror_tools._company_id", return_value="co-1"),
        patch(
            "helixis.codeexec.storage_io.persist_artifacts",
            AsyncMock(side_effect=RuntimeError("storage down")),
        ),
        patch("helixis.settings.get_settings", return_value=object()),
    ):
        out = await _export_table_impl("x", '["a"]', "[[1]]")
    assert "still correct" in out


@pytest.mark.asyncio
async def test_the_reply_states_the_expiry() -> None:
    """A signed URL is a bearer capability with a short life. A user who
    doesn't know it expires files the link and finds it dead later."""
    captured: dict[str, Any] = {}
    out = await _run("x", ["a"], [[1]], captured)
    assert "expires" in out


# ── input handling ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_malformed_input_is_explained_not_crashed() -> None:
    # The company check runs BEFORE parsing (nothing can be done without it),
    # so the patch is required to reach the parse branch at all — without it
    # this asserted against the no-company message and looked like a parsing
    # bug.
    with patch("helixis.agents.mirror_tools._company_id", return_value="co-1"):
        out = await _export_table_impl("x", "not json", "[[1]]")
    assert "couldn't read that table" in out


@pytest.mark.asyncio
async def test_rows_may_be_objects_or_arrays() -> None:
    """The model will produce whichever shape it was already holding, and
    refusing one of them buys nothing but a retry."""
    captured: dict[str, Any] = {}
    with (
        patch("helixis.agents.mirror_tools._company_id", return_value="co-1"),
        patch("helixis.codeexec.storage_io.persist_artifacts", _persist(captured)),
        patch("helixis.settings.get_settings", return_value=object()),
    ):
        out = await _export_table_impl("x", '["name","rent"]', '[{"name":"Dana","rent":1200}]')
    assert _URL in out
    assert "Dana" in next(iter(captured["artifacts"].values())).decode()


def test_the_filename_is_safe() -> None:
    """The title is model-written and becomes a storage path segment."""
    assert _safe_filename("Rent Roll — Q3/2026") == "rent-roll-q3-2026.csv"
    assert _safe_filename("") == "export.csv"
    assert _safe_filename("../../etc/passwd").endswith(".csv")
    assert ".." not in _safe_filename("../../etc/passwd")


# ── it is reachable ──────────────────────────────────────────────────


def test_the_export_is_registered_on_the_agent_that_holds_the_table() -> None:
    """⚠ The mistake this repo has made four times over: a capability wired to
    an agent that cannot reach the data it needs. The router is what writes
    the final answer, so it is what holds the finished table — registering the
    export on a specialist would give it nothing to export.

    Measured precedent: four card renderers were registered on specialists
    only and rendered ZERO times in 514 runs.
    """
    import inspect

    from helixis.agents import orchestrator

    src = inspect.getsource(orchestrator)
    assert "export_table," in src, "export_table is not in the router's tool list"
    assert "from helixis.agents.export_tools import export_table" in src
