"""Tests for the free-form image assessment tool (#533).

Exercises ``assess_image_impl`` (the underscored entry) with an injected
``completion_fn`` so no real Gemini call happens — mirrors the document_tools
test harness. Covers the schema's no-invention validators, graceful
degradation, and the draft-only maintenance-photo path.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.agents import image_tools as it
from helixis.settings import Settings


def _settings(max_bytes: int = 8 * 1024 * 1024) -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "gemini/gemini-3.1-pro-preview", "google_api_key": "fake-g"},
        memory={"enabled": False},
        document={
            "request_timeout_seconds": 1.0,
            "max_bytes": max_bytes,
            "min_confidence_for_pass": 0.6,
        },
    )


def _llm_response(content: str) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


def _scripted(content: str) -> AsyncMock:
    return AsyncMock(return_value=_llm_response(content))


def _maintenance_json(**over) -> str:
    payload = {
        "image_kind": "maintenance_photo",
        "confidence": 0.86,
        "issue_summary": "Active water leak under the kitchen sink; cabinet base is soaked.",
        "likely_trade": "plumbing",
        "severity": "high",
        "suggested_action": "Dispatch a plumber today; shut the under-sink valve meanwhile.",
        "draft_work_order": {
            "scope": "Repair leaking P-trap under kitchen sink; dry and inspect cabinet.",
            "trade": "plumbing",
            "priority": "urgent",
        },
        "transcription": "",
        "summary": "Kitchen sink is leaking — likely plumbing, high severity.",
        "warnings": [],
    }
    payload.update(over)
    return json.dumps(payload)


# ── schema validators ────────────────────────────────────────────


def test_schema_accepts_full_maintenance_photo() -> None:
    payload = json.loads(_maintenance_json())
    payload["source_ref"] = {"kind": "file", "id": "/tmp/leak.jpg"}
    a = it.ImageAssessment.model_validate(payload)
    assert a.image_kind == "maintenance_photo"
    assert a.likely_trade == "plumbing"
    assert a.draft_work_order is not None and a.draft_work_order.priority == "urgent"


def test_schema_rejects_maintenance_photo_without_issue_summary() -> None:
    payload = json.loads(_maintenance_json(issue_summary=""))
    payload["source_ref"] = {"kind": "file", "id": "/tmp/x.jpg"}
    with pytest.raises(Exception):
        it.ImageAssessment.model_validate(payload)


def test_schema_accepts_empty_issue_summary_with_warning() -> None:
    payload = json.loads(
        _maintenance_json(issue_summary="", warnings=["issue_summary: too blurry"])
    )
    payload["source_ref"] = {"kind": "file", "id": "/tmp/x.jpg"}
    a = it.ImageAssessment.model_validate(payload)
    assert a.issue_summary == ""


def test_schema_rejects_notice_without_transcription() -> None:
    payload = {
        "image_kind": "handwritten_notice",
        "confidence": 0.7,
        "transcription": "",
        "summary": "A note.",
        "warnings": [],
        "source_ref": {"kind": "file", "id": "/tmp/note.jpg"},
    }
    with pytest.raises(Exception):
        it.ImageAssessment.model_validate(payload)


# ── graceful degradation ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_input_returns_attachment_unavailable() -> None:
    result = await it.assess_image_impl(settings=_settings())
    assert "attachment_unavailable" in result.warnings
    assert result.image_kind == "other"


@pytest.mark.asyncio
async def test_unsupported_mime_type_returns_warning() -> None:
    result = await it.assess_image_impl(
        base64_content="aGk=", mime_type="application/zip", settings=_settings()
    )
    assert "unsupported_mime_type" in result.warnings


@pytest.mark.asyncio
async def test_oversize_returns_too_large(tmp_path: Path) -> None:
    f = tmp_path / "big.txt"
    f.write_text("x" * 5000)
    result = await it.assess_image_impl(
        file_path=str(f), mime_type="text/plain", settings=_settings(max_bytes=1000)
    )
    assert "attachment_too_large" in result.warnings


# ── happy path + retries (via completion_fn injection) ────────────


@pytest.mark.asyncio
async def test_happy_path_maintenance_photo(tmp_path: Path) -> None:
    f = tmp_path / "leak.txt"  # text/plain eval mode standing in for the photo
    f.write_text("A photo showing water pooling under a kitchen sink cabinet.")
    result = await it.assess_image_impl(
        file_path=str(f),
        mime_type="text/plain",
        settings=_settings(),
        completion_fn=_scripted(_maintenance_json()),
    )
    assert result.image_kind == "maintenance_photo"
    assert result.likely_trade == "plumbing"
    assert result.severity == "high"
    assert result.draft_work_order is not None
    assert not result.warnings


@pytest.mark.asyncio
async def test_unparseable_json_returns_invalid_assessment(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("something")
    result = await it.assess_image_impl(
        file_path=str(f),
        mime_type="text/plain",
        settings=_settings(),
        completion_fn=_scripted("not json at all"),
    )
    assert any("invalid_assessment" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_retry_recovers_after_bad_first_attempt(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("leak photo")
    fn = AsyncMock(side_effect=[_llm_response("garbage"), _llm_response(_maintenance_json())])
    result = await it.assess_image_impl(
        file_path=str(f), mime_type="text/plain", settings=_settings(), completion_fn=fn
    )
    assert result.image_kind == "maintenance_photo"
    assert "retried_once_after_malformed_json" in result.warnings
    assert fn.await_count == 2


@pytest.mark.asyncio
async def test_pii_stripped_from_free_text(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("note")
    leaky = _maintenance_json(
        suggested_action="Call tenant at 415-555-0199 or jane@example.com to schedule.",
    )
    result = await it.assess_image_impl(
        file_path=str(f),
        mime_type="text/plain",
        settings=_settings(),
        completion_fn=_scripted(leaky),
    )
    assert "415-555-0199" not in result.suggested_action
    assert "jane@example.com" not in result.suggested_action
