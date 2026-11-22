"""CRM lead AI-assist generation — parsing + graceful degradation.

The LLM call (litellm.acompletion) is mocked; these tests exercise the
JSON parsing, the field extraction, and the "never load-bearing" degrade
path (any failure → empty LeadDraft).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from helixis.crm.draft import generate_lead_draft
from helixis.db.repositories.leads import LeadRow, MessageRow
from helixis.settings import Settings


def _settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": "test-secret-key-for-unit-tests-only"},
        memory={"enabled": False},
    )


def _lead(**kw: object) -> LeadRow:
    base: dict[str, object] = {
        "company_id": "co-1",
        "name": "Jordan",
        "kind": "renter",
        "stage": "new",
        "source": "sms",
        "note": "Is 4B available?",
    }
    base.update(kw)
    return LeadRow(**base)  # type: ignore[arg-type]


class _FakeResponse:
    """Minimal stand-in for LiteLLM's OpenAI-shaped response."""

    def __init__(self, content: str) -> None:
        message = type("_M", (), {"content": content})()
        self.choices = [type("_C", (), {"message": message})()]


async def test_parses_signals_and_draft():
    resp = _FakeResponse(
        '{"signals": ["Wants Maple Court 4B", "Tour this weekend"], '
        '"draft_reply": "Hi Jordan! Yes, it is available."}'
    )
    with patch("helixis.crm.draft.litellm.acompletion", AsyncMock(return_value=resp)):
        d = await generate_lead_draft(_settings(), lead=_lead(), messages=[])
    assert d.signals == ["Wants Maple Court 4B", "Tour this weekend"]
    assert d.draft_reply == "Hi Jordan! Yes, it is available."


async def test_caps_signals_and_drops_non_strings():
    resp = _FakeResponse('{"signals": ["a", "b", "c", "d", "e", 7, ""], "draft_reply": "ok"}')
    with patch("helixis.crm.draft.litellm.acompletion", AsyncMock(return_value=resp)):
        d = await generate_lead_draft(_settings(), lead=_lead(), messages=[])
    # capped at 4, non-strings + blanks removed
    assert d.signals == ["a", "b", "c", "d"]


async def test_degrades_to_empty_on_llm_error():
    with patch(
        "helixis.crm.draft.litellm.acompletion",
        AsyncMock(side_effect=RuntimeError("model down")),
    ):
        d = await generate_lead_draft(_settings(), lead=_lead(), messages=[])
    assert d.signals == []
    assert d.draft_reply is None


async def test_degrades_when_json_is_not_an_object():
    resp = _FakeResponse('"just a string, not JSON object"')
    with patch("helixis.crm.draft.litellm.acompletion", AsyncMock(return_value=resp)):
        d = await generate_lead_draft(_settings(), lead=_lead(), messages=[])
    assert d.signals == []
    assert d.draft_reply is None


async def test_uses_conversation_when_present():
    # Just assert it doesn't choke on messages + returns parsed output.
    resp = _FakeResponse('{"signals": ["x", "y"], "draft_reply": "hi"}')
    msgs = [
        MessageRow(company_id="co-1", lead_id="l1", direction="inbound", channel="sms", body="Hi"),
        MessageRow(
            company_id="co-1", lead_id="l1", direction="outbound", channel="sms", body="Hello"
        ),
    ]
    with patch("helixis.crm.draft.litellm.acompletion", AsyncMock(return_value=resp)) as m:
        d = await generate_lead_draft(_settings(), lead=_lead(), messages=msgs)
    assert d.draft_reply == "hi"
    # The conversation was rendered into the user message.
    sent = m.await_args.kwargs["messages"][1]["content"]
    assert "Them: Hi" in sent and "PM: Hello" in sent
