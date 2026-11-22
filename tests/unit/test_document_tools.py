"""Unit tests for the document extraction tool.

Covers:

* DocumentExtraction schema enforces the no-invention rule
  (carry-over #2 from PR #143 audit): a null required field without a
  matching warning is a ValidationError.
* All terminal warning paths land cleanly without raising:
  attachment_unavailable, attachment_too_large, unsupported_mime_type,
  llm_unavailable, llm_unauthorized, invalid_extraction, empty_document
  (via the model's own response shape).
* Fixture loading from file_path, base64, URL, and the no-input case.
* Activity-log write happens, identity-rule sanitizer fires on the
  document summary even when the model echoes a Buildium-shaped ID.

We test ``extract_document_fields_impl`` (the underscored entry)
because the public ``extract_document_fields`` is wrapped by the
OpenAI Agents SDK's ``@function_tool`` decorator into a FunctionTool
object that isn't directly callable.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import litellm
import pytest
from pydantic import ValidationError

from helixis.agents import document_tools as dt
from helixis.settings import Settings

# ── helpers ──────────────────────────────────────────────────────


def _settings(max_bytes: int = 8 * 1024 * 1024) -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "gemini/gemini-2.5-flash", "google_api_key": "fake-g"},
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


def _scripted_completion(content: str) -> AsyncMock:
    return AsyncMock(return_value=_llm_response(content))


def _valid_lease_extraction_json() -> str:
    return json.dumps(
        {
            "doc_type": "lease",
            "confidence": 0.92,
            "fields": {
                "tenant_name": "Maria J. Lopez",
                "property_address": "240 Elm Street, Brooklyn, NY 11215",
                "unit": "2A",
                "monthly_rent": 2450.00,
                "lease_start": "2026-04-01",
                "lease_end": "2027-03-31",
                "signed_date": "2026-03-12",
            },
            "summary": "Twelve-month lease for Maria J. Lopez at 240 Elm Street.",
            "warnings": [],
        }
    )


# ── schema validator (carry-over #2) ─────────────────────────────


def test_schema_accepts_fully_populated_lease() -> None:
    """All required fields present → validates."""
    payload = json.loads(_valid_lease_extraction_json())
    payload["source_ref"] = {"kind": "file", "id": "/tmp/lease.txt"}
    extraction = dt.DocumentExtraction.model_validate(payload)
    assert extraction.doc_type == "lease"
    assert extraction.fields["tenant_name"] == "Maria J. Lopez"
    assert extraction.warnings == []


def test_schema_accepts_null_required_field_with_matching_warning() -> None:
    """A null required field IS valid as long as warnings name it."""
    payload = json.loads(_valid_lease_extraction_json())
    payload["source_ref"] = {"kind": "file", "id": "/tmp/lease.txt"}
    payload["fields"]["signed_date"] = None
    payload["warnings"] = ["signed_date: signature page illegible"]
    extraction = dt.DocumentExtraction.model_validate(payload)
    assert extraction.fields["signed_date"] is None


def test_schema_rejects_null_required_field_without_warning() -> None:
    """Carry-over #2 enforcement: null + no warning → ValidationError."""
    payload = json.loads(_valid_lease_extraction_json())
    payload["source_ref"] = {"kind": "file", "id": "/tmp/lease.txt"}
    payload["fields"]["tenant_name"] = None
    # No warning entry naming tenant_name → schema must reject.
    with pytest.raises(ValidationError) as excinfo:
        dt.DocumentExtraction.model_validate(payload)
    assert "tenant_name" in str(excinfo.value)
    assert "no warning references it" in str(excinfo.value)


def test_schema_rejects_when_required_field_missing_from_dict() -> None:
    """A required field that's absent from ``fields`` entirely is
    treated the same as null — must have a warning."""
    payload = json.loads(_valid_lease_extraction_json())
    payload["source_ref"] = {"kind": "file", "id": "/tmp/lease.txt"}
    del payload["fields"]["monthly_rent"]
    with pytest.raises(ValidationError) as excinfo:
        dt.DocumentExtraction.model_validate(payload)
    assert "monthly_rent" in str(excinfo.value)


def test_schema_other_doctype_has_no_required_fields() -> None:
    """``doc_type='other'`` is the catch-all — no validator triggers."""
    extraction = dt.DocumentExtraction(
        doc_type="other",
        confidence=0.1,
        fields={},
        summary="",
        source_ref=dt.SourceRef(kind="file", id="/tmp/x.txt"),
        warnings=["doc_type_uncertain"],
    )
    assert extraction.doc_type == "other"


def test_schema_warning_substring_match_is_loose() -> None:
    """Warning text need only contain the field name as a substring,
    not equal it. This keeps the model's diagnostic wording flexible."""
    payload = json.loads(_valid_lease_extraction_json())
    payload["source_ref"] = {"kind": "file", "id": "/tmp/lease.txt"}
    payload["fields"]["lease_end"] = None
    payload["warnings"] = ["the lease_end clause was redacted by hand"]
    dt.DocumentExtraction.model_validate(payload)  # no raise


def test_schema_confidence_must_be_in_unit_interval() -> None:
    with pytest.raises(ValidationError):
        dt.DocumentExtraction(
            doc_type="other",
            confidence=1.5,
            fields={},
            summary="",
            source_ref=dt.SourceRef(kind="file", id="x"),
            warnings=[],
        )


def test_source_ref_url_drops_signing_token_under_512_cap() -> None:
    """Regression (#461 dogfood): a signed Supabase Storage URL carries a
    ~500-char JWT in ``?token=…``; the full URL exceeded SourceRef.id's 512 cap
    and raised a ValidationError that killed digest extraction. The url path must
    store the stable object path (no query string), well under 512."""
    signed = "https://x.supabase.co/storage/v1/object/sign/company_documents/cid/doc.pdf?token=" + (
        "a" * 700
    )
    ref = dt._make_source_ref(file_path=None, base64_content=None, url=signed)
    assert ref is not None
    assert ref.kind == "url"
    assert "?" not in ref.id  # the ephemeral token is gone
    assert ref.id.endswith("/company_documents/cid/doc.pdf")
    assert len(ref.id) <= 512  # no ValidationError


def test_source_ref_file_path_is_capped() -> None:
    ref = dt._make_source_ref(file_path="/" + "d" * 900, base64_content=None, url=None)
    assert ref is not None and len(ref.id) <= 512


# ── fetch paths ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_input_returns_attachment_unavailable() -> None:
    """No file/base64/url provided → graceful unavailable."""
    result = await dt.extract_document_fields_impl(settings=_settings())
    assert result.warnings == ["attachment_unavailable"]
    assert result.doc_type == "other"
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_missing_file_returns_attachment_unavailable() -> None:
    result = await dt.extract_document_fields_impl(
        file_path="/tmp/definitely-not-a-real-file-xyz.txt",
        mime_type="text/plain",
        settings=_settings(),
    )
    assert result.warnings == ["attachment_unavailable"]


@pytest.mark.asyncio
async def test_oversize_file_returns_too_large(tmp_path: Path) -> None:
    """File exceeds max_bytes → attachment_too_large, no LLM call."""
    big = tmp_path / "big.txt"
    big.write_text("x" * 200)
    settings_small_cap = _settings(max_bytes=50)
    completion = _scripted_completion("{}")
    with patch.object(litellm, "acompletion", completion):
        result = await dt.extract_document_fields_impl(
            file_path=str(big),
            mime_type="text/plain",
            settings=settings_small_cap,
        )
    assert result.warnings == ["attachment_too_large"]
    completion.assert_not_called()


@pytest.mark.asyncio
async def test_unsupported_mime_type_returns_warning() -> None:
    result = await dt.extract_document_fields_impl(
        base64_content=base64.b64encode(b"hello").decode(),
        mime_type="application/x-foo",
        settings=_settings(),
    )
    assert result.warnings == ["unsupported_mime_type"]


@pytest.mark.asyncio
async def test_bad_base64_returns_attachment_unavailable() -> None:
    result = await dt.extract_document_fields_impl(
        base64_content="this is not base64 !!!",
        mime_type="image/png",
        settings=_settings(),
    )
    assert result.warnings == ["attachment_unavailable"]


async def _public(_host: str, _port: int) -> list[str]:
    """``example.test`` never resolves, so without this every URL test below
    would stop at the public-address check and pass without reaching httpx."""
    return ["93.184.216.34"]


@pytest.mark.asyncio
async def test_url_fetch_failure_returns_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("helixis.security.public_url._resolve", _public)
    mock_get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    with patch.object(httpx.AsyncClient, "get", mock_get):
        result = await dt.extract_document_fields_impl(
            url="https://example.test/lease.pdf",
            mime_type="application/pdf",
            settings=_settings(),
        )
    assert result.warnings == ["attachment_unavailable"]
    mock_get.assert_awaited_once()


@pytest.mark.asyncio
async def test_url_4xx_returns_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("helixis.security.public_url._resolve", _public)
    bad = MagicMock()
    bad.status_code = 404
    bad.content = b""
    mock_get = AsyncMock(return_value=bad)
    with patch.object(httpx.AsyncClient, "get", mock_get):
        result = await dt.extract_document_fields_impl(
            url="https://example.test/missing.pdf",
            mime_type="application/pdf",
            settings=_settings(),
        )
    assert result.warnings == ["attachment_unavailable"]
    mock_get.assert_awaited_once()


# ── LLM paths ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_returns_validated_extraction(tmp_path: Path) -> None:
    fixture = tmp_path / "lease.txt"
    fixture.write_text("(synthetic lease text)")
    completion = _scripted_completion(_valid_lease_extraction_json())
    with patch.object(litellm, "acompletion", completion):
        result = await dt.extract_document_fields_impl(
            file_path=str(fixture),
            mime_type="text/plain",
            settings=_settings(),
        )
    assert result.doc_type == "lease"
    assert result.fields["tenant_name"] == "Maria J. Lopez"
    assert result.warnings == []
    assert result.source_ref.kind == "file"
    completion.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_timeout_returns_unavailable(tmp_path: Path) -> None:
    fixture = tmp_path / "f.txt"
    fixture.write_text("doc")
    completion = AsyncMock(side_effect=litellm.Timeout("slow", "gemini", "gemini-pro"))
    with patch.object(litellm, "acompletion", completion):
        result = await dt.extract_document_fields_impl(
            file_path=str(fixture),
            mime_type="text/plain",
            settings=_settings(),
        )
    assert result.warnings == ["llm_unavailable"]


@pytest.mark.asyncio
async def test_llm_auth_failure_returns_unauthorized(tmp_path: Path) -> None:
    fixture = tmp_path / "f.txt"
    fixture.write_text("doc")
    completion = AsyncMock(
        side_effect=litellm.AuthenticationError("bad key", "gemini", "gemini-pro")
    )
    with patch.object(litellm, "acompletion", completion):
        result = await dt.extract_document_fields_impl(
            file_path=str(fixture),
            mime_type="text/plain",
            settings=_settings(),
        )
    assert result.warnings == ["llm_unauthorized"]


@pytest.mark.asyncio
async def test_llm_unparseable_json_returns_invalid_extraction(tmp_path: Path) -> None:
    fixture = tmp_path / "f.txt"
    fixture.write_text("doc")
    completion = _scripted_completion("this is not JSON {")
    with patch.object(litellm, "acompletion", completion):
        result = await dt.extract_document_fields_impl(
            file_path=str(fixture),
            mime_type="text/plain",
            settings=_settings(),
        )
    assert any(w.startswith("invalid_extraction") for w in result.warnings)


@pytest.mark.asyncio
async def test_llm_invented_null_without_warning_rejected(tmp_path: Path) -> None:
    """Carry-over #2 in action: model returns a null required field
    without a warning naming it → tool surfaces invalid_extraction
    instead of shipping the silent-null result."""
    fixture = tmp_path / "f.txt"
    fixture.write_text("doc")
    bad = json.dumps(
        {
            "doc_type": "lease",
            "confidence": 0.85,
            "fields": {
                "tenant_name": None,
                "property_address": "240 Elm Street",
                "unit": "2A",
                "monthly_rent": 2450.00,
                "lease_start": "2026-04-01",
                "lease_end": "2027-03-31",
                "signed_date": "2026-03-12",
            },
            "summary": "Lease at 240 Elm Street.",
            "warnings": [],
        }
    )
    completion = _scripted_completion(bad)
    with patch.object(litellm, "acompletion", completion):
        result = await dt.extract_document_fields_impl(
            file_path=str(fixture),
            mime_type="text/plain",
            settings=_settings(),
        )
    assert any(w.startswith("invalid_extraction") for w in result.warnings)
    assert any("tenant_name" in w for w in result.warnings)
    # The tool returns an empty/degraded result, NOT the model's
    # silent-null output.
    assert result.fields == {}


@pytest.mark.asyncio
async def test_llm_response_not_a_dict_returns_invalid_extraction(tmp_path: Path) -> None:
    fixture = tmp_path / "f.txt"
    fixture.write_text("doc")
    completion = _scripted_completion('["not", "a", "dict"]')
    with patch.object(litellm, "acompletion", completion):
        result = await dt.extract_document_fields_impl(
            file_path=str(fixture),
            mime_type="text/plain",
            settings=_settings(),
        )
    assert any(w.startswith("invalid_extraction") for w in result.warnings)


# ── activity log ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_activity_log_swallows_missing_pool(tmp_path: Path) -> None:
    """No DB pool → tool still returns cleanly."""
    fixture = tmp_path / "f.txt"
    fixture.write_text("doc")
    completion = _scripted_completion(_valid_lease_extraction_json())
    with patch.object(litellm, "acompletion", completion):
        result = await dt.extract_document_fields_impl(
            file_path=str(fixture),
            mime_type="text/plain",
            settings=_settings(),
        )
    assert result.doc_type == "lease"


@pytest.mark.asyncio
async def test_activity_log_writes_with_sanitized_summary(tmp_path: Path) -> None:
    """With memory context bound, write_activity is called and the
    description is identity-rule sanitized even when the model's
    summary contains a Buildium-shaped ID."""
    from helixis.memory.context import memory_context

    fixture = tmp_path / "f.txt"
    fixture.write_text("doc")
    leaky = json.dumps(
        {
            "doc_type": "lease",
            "confidence": 0.9,
            "fields": {
                "tenant_name": "Maria Lopez",
                "property_address": "240 Elm Street",
                "unit": "2A",
                "monthly_rent": 2450.00,
                "lease_start": "2026-04-01",
                "lease_end": "2027-03-31",
                "signed_date": "2026-03-12",
            },
            "summary": ("Lease for Tenant #12345 at 240 Elm Street, linked to Property #98765."),
            "warnings": [],
        }
    )
    completion = _scripted_completion(leaky)
    fake_pool = MagicMock()
    write_called: dict[str, Any] = {}

    async def _fake_write(pool: Any, entry: Any) -> None:
        write_called["entry"] = entry

    with (
        patch.object(litellm, "acompletion", completion),
        patch.object(dt.db_engine, "get_pool", return_value=fake_pool),
        patch.object(dt, "write_activity", new=_fake_write),
        memory_context(company_id="co-1", user_id="u-1", session_id="s-1"),
    ):
        await dt.extract_document_fields_impl(
            file_path=str(fixture),
            mime_type="text/plain",
            settings=_settings(),
        )

    entry = write_called["entry"]
    assert entry.action_type == "document_extracted"
    assert entry.company_id == "co-1"
    assert entry.user_id == "u-1"
    assert "Tenant #12345" not in entry.description
    assert "Property #98765" not in entry.description
    assert "[id-redacted]" in entry.description
    assert entry.raw["doc_type"] == "lease"
    assert entry.raw["confidence"] == 0.9
    assert "tenant_name" in entry.raw["field_keys"]


@pytest.mark.asyncio
async def test_activity_log_summary_strips_email_and_phone(tmp_path: Path) -> None:
    """The document path's log sanitizer strips the email/phone PII classes
    that PR #246 added to the shared redactor — not just Buildium IDs.

    Closes #250 (document leg). The sibling test above only proves IDs are
    redacted; this proves the *new* entity types are too, so a regression that
    bypasses redact() (or a Presidio/regex break) on the document path can't
    slip an email/phone from a doc summary into the activity-log description.
    Deterministic (scripted completion) — no live Gemini, unlike a golden case.
    """
    from helixis.memory.context import memory_context

    fixture = tmp_path / "f.txt"
    fixture.write_text("doc")
    leaky = json.dumps(
        {
            "doc_type": "lease",
            "confidence": 0.9,
            "fields": {
                "tenant_name": "Maria Lopez",
                "property_address": "240 Elm Street",
                "unit": "2A",
                "monthly_rent": 2450.00,
                "lease_start": "2026-04-01",
                "lease_end": "2027-03-31",
                "signed_date": "2026-03-12",
            },
            "summary": (
                "Lease for Maria Lopez at 240 Elm Street; reach the tenant at "
                "sarah.chen@example.com or (212) 555-0188."
            ),
            "warnings": [],
        }
    )
    completion = _scripted_completion(leaky)
    fake_pool = MagicMock()
    write_called: dict[str, Any] = {}

    async def _fake_write(pool: Any, entry: Any) -> None:
        write_called["entry"] = entry

    with (
        patch.object(litellm, "acompletion", completion),
        patch.object(dt.db_engine, "get_pool", return_value=fake_pool),
        patch.object(dt, "write_activity", new=_fake_write),
        memory_context(company_id="co-1", user_id="u-1", session_id="s-1"),
    ):
        await dt.extract_document_fields_impl(
            file_path=str(fixture),
            mime_type="text/plain",
            settings=_settings(),
        )

    description = write_called["entry"].description
    # The PII classes added by #246 must not survive into the log description.
    assert "sarah.chen@example.com" not in description
    assert "555-0188" not in description
    assert "(212) 555-0188" not in description
    # ...while non-PII context is preserved (redaction is targeted, not a nuke).
    assert "240 Elm Street" in description


@pytest.mark.asyncio
async def test_activity_log_records_failure_warning() -> None:
    """Even on terminal failures, an activity row is written so the
    Activity page shows the agent tried."""
    from helixis.memory.context import memory_context

    fake_pool = MagicMock()
    write_called: dict[str, Any] = {}

    async def _fake_write(pool: Any, entry: Any) -> None:
        write_called["entry"] = entry

    with (
        patch.object(dt.db_engine, "get_pool", return_value=fake_pool),
        patch.object(dt, "write_activity", new=_fake_write),
        memory_context(company_id="co-1", user_id="u-1", session_id="s-1"),
    ):
        await dt.extract_document_fields_impl(
            file_path="/tmp/missing-xyz.pdf",
            mime_type="application/pdf",
            settings=_settings(),
        )

    entry = write_called["entry"]
    assert entry.raw["warnings"] == ["attachment_unavailable"]
    assert entry.raw["confidence"] == 0.0


# ── retry budget on malformed-JSON / schema-validation failure ───
#
# Case 021 (extract_invoice) reproduced byte-for-byte 3/3 against
# real Gemini 2.5 Flash at temperature=0: the model emits a bare ``,``
# instead of ``},`` on the first line_items entry, producing
# unparseable JSON. The retry path below recovers from that class of
# transient model failure with a single correction-nudge re-prompt,
# stamping ``retried_once_after_malformed_json`` on the recovered
# result so the observable evidence is visible to evals + Activity.


@pytest.mark.asyncio
async def test_retry_recovers_from_malformed_json_on_first_attempt(
    tmp_path: Path,
) -> None:
    """First attempt returns invalid JSON → correction-nudge retry
    fires → second attempt returns a valid extraction → result is
    returned with the ``retried_once_after_malformed_json`` warning
    appended."""
    fixture = tmp_path / "doc.txt"
    fixture.write_text("(synthetic invoice text)")

    # First response intentionally invalid (missing }, after the first
    # line_items entry — same shape as the real Gemini case 021 bug);
    # second response is valid.
    malformed = (
        '{"doc_type": "invoice", "confidence": 0.9, '
        '"fields": {"vendor_name": "X", "invoice_number": "1", '
        '"line_items": [{"description": "a", "amount": 10.0 , '
        '{"description": "b", "amount": 20.0}], '
        '"subtotal": 30.0, "total": 30.0, '
        '"invoice_date": "2026-01-01", "due_date": "2026-02-01"}, '
        '"summary": "Inv", "warnings": []}'
    )
    valid = json.dumps(
        {
            "doc_type": "invoice",
            "confidence": 0.95,
            "fields": {
                "vendor_name": "Acme Co",
                "invoice_number": "INV-001",
                "line_items": [
                    {"description": "diag", "amount": 100.0},
                ],
                "subtotal": 100.0,
                "total": 108.0,
                "invoice_date": "2026-01-15",
                "due_date": "2026-02-14",
            },
            "summary": "Invoice from Acme Co for $108.",
            "warnings": [],
        }
    )

    responses = [_llm_response(malformed), _llm_response(valid)]
    completion = AsyncMock(side_effect=responses)

    with patch.object(litellm, "acompletion", completion):
        result = await dt.extract_document_fields_impl(
            file_path=str(fixture),
            mime_type="text/plain",
            settings=_settings(),
        )

    assert result.doc_type == "invoice"
    assert result.fields["vendor_name"] == "Acme Co"
    assert result.fields["total"] == 108.0
    assert "retried_once_after_malformed_json" in result.warnings
    # Exactly two LLM calls — original + one retry. Not three.
    assert completion.await_count == 2

    # The retry's prompt must carry the correction nudge naming the
    # validation error so the model has actionable signal.
    retry_messages = completion.await_args_list[1].kwargs["messages"]
    nudge = retry_messages[-1]
    assert nudge["role"] == "user"
    assert "not valid JSON" in nudge["content"]
    assert "schema" in nudge["content"]

    # Issue #158 Shape D — the nudge MUST quote a snippet of the
    # broken attempt-0 response so the model has concrete repair
    # material, NOT just an abstract "your response was bad"
    # assertion (which reproduced byte-identical malformed output at
    # temperature=0). The snippet is framed in user-role context with
    # an explicit "do NOT continue from this" instruction to avoid
    # self-conditioning.
    assert "<<<" in nudge["content"] and ">>>" in nudge["content"]
    assert malformed in nudge["content"]
    assert "Do NOT treat it as a prefix to continue" in nudge["content"]
    # The retry must NOT replay the broken output as an assistant turn
    # — self-conditioning risk. All replayed material lives in the
    # final user-role nudge.
    assert all(m["role"] != "assistant" for m in retry_messages)
    # No-invention rule is restated in the nudge so the safety
    # property is preserved across the retry.
    assert "no-invention" in nudge["content"]


@pytest.mark.asyncio
async def test_retry_budget_caps_at_one_then_surfaces_invalid_extraction(
    tmp_path: Path,
) -> None:
    """Both the original attempt AND the retry produce malformed JSON.
    The tool surfaces ``invalid_extraction: ...`` rather than
    retry-hammering or shipping silent nulls. Total attempts capped
    at exactly 2 (``_MAX_RETRIES = 1``)."""
    fixture = tmp_path / "doc.txt"
    fixture.write_text("(synthetic doc)")

    malformed_first = '{"this is not": "valid", "missing_braces": '
    malformed_second = "still not valid json {{ broken }}"

    completion = AsyncMock(
        side_effect=[
            _llm_response(malformed_first),
            _llm_response(malformed_second),
        ]
    )

    with patch.object(litellm, "acompletion", completion):
        result = await dt.extract_document_fields_impl(
            file_path=str(fixture),
            mime_type="text/plain",
            settings=_settings(),
        )

    # The terminal warning is the invalid_extraction marker — NOT the
    # retried_once_after_malformed_json marker (which is reserved for
    # the recovered-success case).
    assert any(w.startswith("invalid_extraction") for w in result.warnings)
    assert "retried_once_after_malformed_json" not in result.warnings
    # Empty/degraded result — the model's second-attempt output is
    # NOT shipped. Carry-over #2 contract holds across the retry.
    assert result.fields == {}
    assert result.confidence == 0.0
    # Exactly 2 attempts. Not 3. Not infinite.
    assert completion.await_count == 2


@pytest.mark.asyncio
async def test_retry_nudge_truncates_oversized_broken_response(
    tmp_path: Path,
) -> None:
    """Issue #158 Shape D — when attempt-0's broken response is larger
    than ``_BROKEN_RESPONSE_SNIPPET_MAX_CHARS``, the nudge quotes only
    the leading slice and appends a truncation marker. Stops a
    pathologically large failed response from blowing out the retry
    context window."""
    fixture = tmp_path / "doc.txt"
    fixture.write_text("(synthetic doc)")

    # 4000-char broken response — well above the 2000-char cap.
    oversize_broken = "{" + ("x" * 4000)  # never parseable, will trigger retry
    valid = json.dumps(
        {
            "doc_type": "other",
            "confidence": 0.5,
            "fields": {},
            "summary": "ok",
            "warnings": [],
        }
    )

    completion = AsyncMock(side_effect=[_llm_response(oversize_broken), _llm_response(valid)])
    with patch.object(litellm, "acompletion", completion):
        await dt.extract_document_fields_impl(
            file_path=str(fixture),
            mime_type="text/plain",
            settings=_settings(),
        )

    retry_messages = completion.await_args_list[1].kwargs["messages"]
    nudge_content = retry_messages[-1]["content"]
    # The full 4000-char broken response must NOT be in the nudge —
    # only the leading 2000 chars + truncation marker.
    assert oversize_broken not in nudge_content
    assert oversize_broken[: dt._BROKEN_RESPONSE_SNIPPET_MAX_CHARS] in nudge_content
    assert "[...truncated]" in nudge_content


# ── Docling PDF pre-parse (optional [document] extra) ─────────────────────────


def _settings_docling(use_docling: bool) -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "gemini/gemini-2.5-flash", "google_api_key": "fake-g"},
        memory={"enabled": False},
        document={
            "request_timeout_seconds": 1.0,
            "max_bytes": 8 * 1024 * 1024,
            "min_confidence_for_pass": 0.6,
            "use_docling": use_docling,
        },
    )


def _pdf_b64() -> str:
    # Minimal bytes; content is irrelevant because _pdf_to_text is mocked.
    return base64.b64encode(b"%PDF-1.4 minimal").decode()


@pytest.mark.asyncio
async def test_pdf_use_docling_false_takes_multimodal_path() -> None:
    """Default (use_docling=False): a PDF still goes to Gemini as a base64
    image_url block — today's behavior, unchanged. _pdf_to_text not consulted."""
    completion = _scripted_completion(_valid_lease_extraction_json())
    with (
        patch.object(litellm, "acompletion", completion),
        patch.object(dt, "_pdf_to_text", new=AsyncMock(return_value="should not be called")) as ptt,
    ):
        await dt.extract_document_fields_impl(
            base64_content=_pdf_b64(),
            mime_type="application/pdf",
            settings=_settings_docling(False),
        )
    ptt.assert_not_awaited()
    blocks = completion.await_args.kwargs["messages"][-1]["content"]
    assert any(b.get("type") == "image_url" for b in blocks)  # multimodal path
    assert not any("DOCUMENT TEXT" in str(b.get("text", "")) for b in blocks)


@pytest.mark.asyncio
async def test_pdf_use_docling_true_good_text_takes_text_path() -> None:
    """use_docling=True + parser returns usable text → TEXT path (no image_url);
    the parsed text rides in a DOCUMENT TEXT block + a docling warning lands."""
    completion = _scripted_completion(_valid_lease_extraction_json())
    parsed = "LEASE AGREEMENT\nTenant: Maria Lopez\nMonthly rent: 2450\n" * 3
    with (
        patch.object(litellm, "acompletion", completion),
        patch.object(dt, "_pdf_to_text", new=AsyncMock(return_value=parsed)),
    ):
        result = await dt.extract_document_fields_impl(
            base64_content=_pdf_b64(),
            mime_type="application/pdf",
            settings=_settings_docling(True),
        )
    blocks = completion.await_args.kwargs["messages"][-1]["content"]
    assert not any(b.get("type") == "image_url" for b in blocks)  # NOT multimodal
    assert any(
        "DOCUMENT TEXT" in str(b.get("text", "")) and "Maria Lopez" in str(b.get("text", ""))
        for b in blocks
    )
    assert "pdf parsed to text via docling" in result.warnings


@pytest.mark.asyncio
async def test_pdf_use_docling_true_none_falls_back_to_multimodal() -> None:
    """use_docling=True but parser returns None (unavailable / unusable text /
    image-only PDF) → FALL BACK to the multimodal path. Never ships empty text."""
    completion = _scripted_completion(_valid_lease_extraction_json())
    with (
        patch.object(litellm, "acompletion", completion),
        patch.object(dt, "_pdf_to_text", new=AsyncMock(return_value=None)),
    ):
        result = await dt.extract_document_fields_impl(
            base64_content=_pdf_b64(),
            mime_type="application/pdf",
            settings=_settings_docling(True),
        )
    blocks = completion.await_args.kwargs["messages"][-1]["content"]
    assert any(b.get("type") == "image_url" for b in blocks)  # fell back to multimodal
    assert "pdf parsed to text via docling" not in result.warnings


@pytest.mark.asyncio
async def test_pdf_to_text_import_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """docling-parse not installed → ImportError inside the thread → None
    (never raises; caller falls back)."""

    def _raise_import(_b: bytes) -> str:
        raise ImportError("no docling-parse")

    monkeypatch.setattr(dt, "_docling_extract_sync", _raise_import)
    out = await dt._pdf_to_text(b"%PDF", timeout_seconds=1.0)
    assert out is None


@pytest.mark.asyncio
async def test_pdf_to_text_parse_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any parse exception → None, not a raise."""

    def _boom(_b: bytes) -> str:
        raise RuntimeError("corrupt pdf")

    monkeypatch.setattr(dt, "_docling_extract_sync", _boom)
    assert await dt._pdf_to_text(b"%PDF", timeout_seconds=1.0) is None


@pytest.mark.asyncio
async def test_pdf_to_text_short_text_is_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Below the usable-text threshold (image-only PDF) → None → fallback."""
    monkeypatch.setattr(dt, "_docling_extract_sync", lambda _b: "  x  ")
    assert await dt._pdf_to_text(b"%PDF", timeout_seconds=1.0) is None


@pytest.mark.asyncio
async def test_pdf_to_text_timeout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow parse exceeding the timeout → None (never blocks/raises). Uses a
    real sync sleep in the worker thread so asyncio.wait_for actually trips."""
    import time

    def _slow(_b: bytes) -> str:
        time.sleep(2.0)
        return "x" * 100

    monkeypatch.setattr(dt, "_docling_extract_sync", _slow)
    out = await dt._pdf_to_text(b"%PDF", timeout_seconds=0.1)
    assert out is None


# ── #250 document leg — contact-PII stripped from extracted fields ───────
#
# The log sanitizer (test_activity_log_summary_strips_email_and_phone) covers
# logs; this covers the extraction RESULT that reaches the specialist + reply.
# Deterministic (redact() at the tool boundary), so a doc's email/phone can
# never be echoed into chat regardless of model wording — and names, unit
# numbers, addresses, and amounts are preserved.


def test_redact_fields_strips_email_and_phone() -> None:
    out = dt._redact_fields(
        {
            "tenant_name": "Maria Lopez",
            "tenant_email": "maria.lopez@example.com",
            "tenant_phone": "(212) 555-0188",
            "property_address": "240 Elm Street, Unit 2A",
            "monthly_rent": "2450",
        }
    )
    # Contact PII gone…
    assert "maria.lopez@example.com" not in out["tenant_email"]
    assert "555-0188" not in out["tenant_phone"]
    # …non-contact fields preserved verbatim (redaction is targeted).
    assert out["tenant_name"] == "Maria Lopez"
    assert out["property_address"] == "240 Elm Street, Unit 2A"
    assert out["monthly_rent"] == "2450"


def test_redact_fields_passes_through_non_strings() -> None:
    out = dt._redact_fields({"rent": 2450, "active": True, "notes": None, "tags": ["a"]})
    assert out == {"rent": 2450, "active": True, "notes": None, "tags": ["a"]}


# ── #161: tolerant JSON coercion (fence / prose) on attempt 0 ────────


def test_coerce_json_clean_passthrough() -> None:
    data, coerced = dt._coerce_json_object('{"a": 1}')
    assert data == {"a": 1} and coerced is False


def test_coerce_json_strips_code_fence() -> None:
    data, coerced = dt._coerce_json_object('```json\n{"a": 1}\n```')
    assert data == {"a": 1} and coerced is True


def test_coerce_json_extracts_object_from_prose() -> None:
    data, coerced = dt._coerce_json_object('Here is the result:\n{"a": {"b": 2}}\nThanks!')
    assert data == {"a": {"b": 2}} and coerced is True


def test_coerce_json_brace_inside_string_is_ignored() -> None:
    data, coerced = dt._coerce_json_object('{"note": "a } brace in a string", "n": 1}')
    assert data == {"note": "a } brace in a string", "n": 1} and coerced is False


def test_coerce_json_genuinely_broken_still_raises() -> None:
    # Unbalanced (missing a closing brace) — must NOT be silently "recovered".
    import json as _json

    with pytest.raises(_json.JSONDecodeError):
        dt._coerce_json_object('{"a": {"b": 1}')


@pytest.mark.asyncio
async def test_fenced_attempt0_recovers_without_retry(tmp_path: Path) -> None:
    """A ```json-fenced (but otherwise valid) attempt-0 response must parse on
    the FIRST call — no correction-nudge retry (the #161 cost saving)."""
    fixture = tmp_path / "doc.txt"
    fixture.write_text("(synthetic invoice text)")

    fenced = "```json\n" + _valid_lease_extraction_json() + "\n```"
    completion = AsyncMock(side_effect=[_llm_response(fenced)])

    with patch.object(litellm, "acompletion", completion):
        result = await dt.extract_document_fields_impl(
            file_path=str(fixture),
            mime_type="text/plain",
            settings=_settings(),
        )

    assert completion.await_count == 1  # NO retry
    assert "retried_once_after_malformed_json" not in result.warnings
    assert "recovered_noisy_json_attempt_0" in result.warnings


# ── #250: document-path PII redaction on the RETURNED fields ─────────
# 035/046 cover the web leg; the document leg strips contact PII from the
# extracted field VALUES deterministically at the tool boundary (_redact_fields),
# so a parsed doc can't echo a tenant/vendor email or phone into chat. The
# activity-log sanitizer is already tested above; this covers result.fields,
# the value that actually reaches the specialist + the reply.


def test_redact_fields_strips_contact_pii_keeps_business_data() -> None:
    out = dt._redact_fields(
        {
            "vendor_name": "Acme Plumbing",
            "total": 480.0,
            "vendor_contact": "reach Sarah at sarah.chen@example.com or (212) 555-0188",
        }
    )
    assert out["vendor_name"] == "Acme Plumbing"  # name preserved
    assert out["total"] == 480.0  # non-string + dollar amount untouched
    assert "sarah.chen@example.com" not in out["vendor_contact"]
    assert "555-0188" not in out["vendor_contact"]


@pytest.mark.asyncio
async def test_extracted_fields_are_pii_redacted_end_to_end(tmp_path: Path) -> None:
    """A document whose extracted fields carry an email/phone must come back with
    those stripped from result.fields (deterministic — fake completion)."""
    fixture = tmp_path / "invoice.txt"
    fixture.write_text("(synthetic invoice with a contact line)")

    extraction = json.dumps(
        {
            "doc_type": "other",
            "confidence": 0.9,
            "fields": {
                "vendor_name": "Acme Plumbing",
                "vendor_contact": "Sarah Chen, sarah.chen@example.com, (212) 555-0188",
            },
            "summary": "Invoice from Acme Plumbing.",
            "warnings": [],
        }
    )
    completion = _scripted_completion(extraction)

    with patch.object(litellm, "acompletion", completion):
        result = await dt.extract_document_fields_impl(
            file_path=str(fixture),
            mime_type="text/plain",
            settings=_settings(),
        )

    assert result.fields["vendor_name"] == "Acme Plumbing"
    assert "sarah.chen@example.com" not in result.fields["vendor_contact"]
    assert "@example.com" not in result.fields["vendor_contact"]
    assert "555-0188" not in result.fields["vendor_contact"]
