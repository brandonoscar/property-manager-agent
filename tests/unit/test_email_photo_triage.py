"""Tests for the email image-attachment photo triage (#533 entry point 3).

Composio (GMAIL_GET_ATTACHMENT), the presigned R2 download, and the vision
assessor are all mocked, so no network / real Gemini is touched — these lock
the plumbing: the GMAIL_GET_ATTACHMENT result parse, the presigned fetch (incl.
non-image / oversize / error / octet-stream fallback), the image pre-filter,
the assess→draft mapping, the activity record, the per-email cap, and the
best-effort "never break the poll" posture.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from helixis.agents.image_tools import DraftWorkOrder, ImageAssessment, SourceRef
from helixis.settings import Settings
from helixis.webhooks import email_photo_triage as t
from helixis.webhooks.email_triage import EmailAttachmentRef, InboundEmail


def _settings() -> Settings:
    return Settings(env="test", memory={"enabled": False})


def _ref(
    aid: str = "att-1", filename: str = "leak.jpg", mime: str = "image/jpeg"
) -> EmailAttachmentRef:
    return EmailAttachmentRef(attachment_id=aid, filename=filename, mime_type=mime)


def _email(*, mid: str = "m1", attachments=()) -> InboundEmail:
    return InboundEmail(
        message_id=mid,
        from_email="tenant@known.com",
        subject="Leak under the sink",
        body_text="body",
        received_at="2026-07-11T00:00:00Z",
        attachments=tuple(attachments),
    )


def _assessment(**over) -> ImageAssessment:
    kw = dict(
        image_kind="maintenance_photo",
        confidence=0.9,
        issue_summary="Active leak under the kitchen sink.",
        likely_trade="plumbing",
        severity="high",
        suggested_action="Send a plumber today.",
        draft_work_order=DraftWorkOrder(
            scope="Repair the P-trap.", trade="plumbing", priority="urgent"
        ),
        source_ref=SourceRef(kind="base64", id="x"),
    )
    kw.update(over)
    return ImageAssessment(**kw)


def _get_attachment_result(s3url="https://temp.r2/obj", mimetype="image/jpeg") -> dict:
    """A live-shaped GMAIL_GET_ATTACHMENT result."""
    return {
        "data": {"file": {"s3url": s3url, "mimetype": mimetype, "name": "leak.jpg"}},
        "error": None,
        "successful": True,
    }


def _composio(*, attachment_result=None) -> MagicMock:
    c = MagicMock()
    c.execute_tool = MagicMock(return_value=attachment_result or _get_attachment_result())
    return c


def _fake_httpx(monkeypatch, *, content=b"\xff\xd8\xffdata", status=200, headers=None, raises=None):
    resp = MagicMock()
    resp.content = content
    resp.headers = headers or {"content-type": "image/jpeg"}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "bad", request=None, response=None
        )
    get = AsyncMock(return_value=resp)
    if raises is not None:
        get.side_effect = raises
    client = MagicMock()
    client.get = get
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=cm))
    return get


# ── GMAIL_GET_ATTACHMENT result parse ────────────────────────────


def test_extract_attachment_download_live_shape() -> None:
    out = t.extract_attachment_download(_get_attachment_result("https://s3/x", "image/png"))
    assert out == ("https://s3/x", "image/png")


def test_extract_attachment_download_top_level_file() -> None:
    # Tolerant of the file block sitting at the top level (wrapper drift).
    out = t.extract_attachment_download({"file": {"s3url": "https://s3/y"}})
    assert out == ("https://s3/y", "")


def test_extract_attachment_download_none_when_no_url() -> None:
    assert t.extract_attachment_download({"data": {"file": {"mimetype": "image/png"}}}) is None
    assert t.extract_attachment_download({}) is None
    assert t.extract_attachment_download(None) is None


# ── image pre-filter ─────────────────────────────────────────────


def test_is_image_ref_accepts_image_and_blank() -> None:
    assert t._is_image_ref(_ref(mime="image/png"))
    assert t._is_image_ref(_ref(mime=""))  # blank → candidate (declared type lies)


def test_is_image_ref_rejects_non_image() -> None:
    assert not t._is_image_ref(_ref(mime="application/pdf"))


# ── presigned download ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_image_returns_bytes_and_mime(monkeypatch) -> None:
    _fake_httpx(monkeypatch, content=b"IMG", headers={"content-type": "image/jpeg"})
    assert await t._download_image("https://s3/x") == (b"IMG", "image/jpeg")


@pytest.mark.asyncio
async def test_download_image_uses_fallback_mime_for_octet_stream(monkeypatch) -> None:
    # R2 sometimes serves octet-stream — fall back to Composio's reported type.
    _fake_httpx(monkeypatch, content=b"IMG", headers={"content-type": "application/octet-stream"})
    assert await t._download_image("https://s3/x", fallback_mime="image/png") == (
        b"IMG",
        "image/png",
    )


@pytest.mark.asyncio
async def test_download_image_none_for_non_image_no_fallback(monkeypatch) -> None:
    _fake_httpx(monkeypatch, headers={"content-type": "application/octet-stream"})
    assert await t._download_image("https://s3/x") is None


@pytest.mark.asyncio
async def test_download_image_oversize_returns_none(monkeypatch) -> None:
    _fake_httpx(monkeypatch, content=b"x" * (9 * 1024 * 1024))
    assert await t._download_image("https://s3/x") is None


@pytest.mark.asyncio
async def test_download_image_swallows_errors(monkeypatch) -> None:
    _fake_httpx(monkeypatch, raises=httpx.ConnectError("boom"))
    assert await t._download_image("https://s3/x") is None


@pytest.mark.asyncio
async def test_download_image_none_for_empty_url() -> None:
    assert await t._download_image("") is None


# ── fetch one attachment (execute_tool → download) ───────────────


@pytest.mark.asyncio
async def test_fetch_attachment_image_orchestrates(monkeypatch) -> None:
    _fake_httpx(monkeypatch, content=b"IMG", headers={"content-type": "image/jpeg"})
    composio = _composio()
    out = await t._fetch_attachment_image(composio, user_id="u1", message_id="m1", ref=_ref())
    assert out == (b"IMG", "image/jpeg")
    _, kwargs = composio.execute_tool.call_args
    assert kwargs["slug"] == t.GMAIL_GET_ATTACHMENT_SLUG
    assert kwargs["arguments"]["message_id"] == "m1"
    assert kwargs["arguments"]["attachment_id"] == "att-1"
    assert kwargs["arguments"]["file_name"] == "leak.jpg"


@pytest.mark.asyncio
async def test_fetch_attachment_image_synthesizes_file_name(monkeypatch) -> None:
    _fake_httpx(monkeypatch)
    composio = _composio()
    await t._fetch_attachment_image(
        composio, user_id="u1", message_id="m1", ref=_ref(aid="abcdef123456789", filename="")
    )
    _, kwargs = composio.execute_tool.call_args
    assert kwargs["arguments"]["file_name"].startswith("attachment-")


@pytest.mark.asyncio
async def test_fetch_attachment_image_none_when_execute_returns_none(monkeypatch) -> None:
    composio = MagicMock()
    composio.execute_tool = MagicMock(return_value=None)
    out = await t._fetch_attachment_image(composio, user_id="u1", message_id="m1", ref=_ref())
    assert out is None


# ── assess orchestration ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_assess_email_photos_orchestrates(monkeypatch) -> None:
    _fake_httpx(monkeypatch, content=b"IMG", headers={"content-type": "image/jpeg"})
    composio = _composio()
    with patch.object(t, "assess_image_impl", new=AsyncMock(return_value=_assessment())) as ai:
        out = await t.assess_email_photos(
            composio, user_id="u1", message_id="m1", attachments=(_ref(),), settings=_settings()
        )
    assert len(out) == 1 and out[0].image_kind == "maintenance_photo"
    assert ai.await_args.kwargs["mime_type"] == "image/jpeg"
    assert ai.await_args.kwargs["base64_content"]


@pytest.mark.asyncio
async def test_assess_email_photos_skips_non_image_refs(monkeypatch) -> None:
    composio = _composio()
    with patch.object(t, "assess_image_impl", new=AsyncMock(return_value=_assessment())):
        out = await t.assess_email_photos(
            composio,
            user_id="u1",
            message_id="m1",
            attachments=(_ref(mime="application/pdf"),),
            settings=_settings(),
        )
    assert out == []
    composio.execute_tool.assert_not_called()  # PDF never fetched


@pytest.mark.asyncio
async def test_assess_email_photos_caps_at_max(monkeypatch) -> None:
    _fake_httpx(monkeypatch)
    composio = _composio()
    refs = tuple(_ref(aid=f"a{i}") for i in range(5))
    with patch.object(t, "assess_image_impl", new=AsyncMock(return_value=_assessment())):
        out = await t.assess_email_photos(
            composio,
            user_id="u1",
            message_id="m1",
            attachments=refs,
            settings=_settings(),
            max_photos=2,
        )
    assert len(out) == 2
    assert composio.execute_tool.call_count == 2


@pytest.mark.asyncio
async def test_assess_email_photos_skips_bad_attachment(monkeypatch) -> None:
    # First download fails outright, second succeeds → one assessment. (A
    # non-image content-type alone doesn't fail it — Composio's reported
    # image mimetype rescues an octet-stream/mislabelled download by design.)
    resp_ok = MagicMock(
        content=b"IMG", headers={"content-type": "image/png"}, raise_for_status=MagicMock()
    )
    client = MagicMock()
    client.get = AsyncMock(side_effect=[httpx.ConnectError("boom"), resp_ok])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=cm))
    composio = _composio()
    with patch.object(t, "assess_image_impl", new=AsyncMock(return_value=_assessment())):
        out = await t.assess_email_photos(
            composio,
            user_id="u1",
            message_id="m1",
            attachments=(_ref(aid="a1"), _ref(aid="a2")),
            settings=_settings(),
        )
    assert len(out) == 1


@pytest.mark.asyncio
async def test_assess_email_photos_never_raises_on_execute_error() -> None:
    composio = MagicMock()
    composio.execute_tool = MagicMock(side_effect=RuntimeError("composio down"))
    out = await t.assess_email_photos(
        composio, user_id="u1", message_id="m1", attachments=(_ref(),), settings=_settings()
    )
    assert out == []


# ── triage: record drafts ────────────────────────────────────────


@pytest.mark.asyncio
async def test_triage_records_draft() -> None:
    pool = MagicMock()
    emails = [_email(attachments=(_ref(),))]
    with (
        patch.object(t, "assess_email_photos", new=AsyncMock(return_value=[_assessment()])),
        patch.object(t, "write_activity", new=AsyncMock()) as wa,
    ):
        n = await t.triage_email_photos(
            pool, _composio(), user_id="u1", company_id="co-1", emails=emails, settings=_settings()
        )
    assert n == 1
    entry = wa.await_args.args[1]
    assert entry.action_type == "maintenance_photo_drafted"
    assert entry.company_id == "co-1"
    assert entry.source == "webhook"
    assert entry.source_label == "Email photo attachment"
    assert "an email photo" in entry.description  # source named in the draft
    assert entry.raw["message_id"] == "m1"
    assert entry.raw["from"] == "tenant@known.com"
    assert entry.raw["likely_trade"] == "plumbing"


@pytest.mark.asyncio
async def test_triage_skips_emails_without_attachments() -> None:
    emails = [_email(mid="no-att", attachments=())]
    with (
        patch.object(t, "assess_email_photos", new=AsyncMock(return_value=[_assessment()])) as ae,
        patch.object(t, "write_activity", new=AsyncMock()) as wa,
    ):
        n = await t.triage_email_photos(
            MagicMock(),
            _composio(),
            user_id="u1",
            company_id="c",
            emails=emails,
            settings=_settings(),
        )
    assert n == 0
    ae.assert_not_awaited()
    wa.assert_not_awaited()


@pytest.mark.asyncio
async def test_triage_skips_non_actionable_image() -> None:
    notice = ImageAssessment(
        image_kind="handwritten_notice",
        confidence=0.9,
        transcription="quiet hours",
        source_ref=SourceRef(kind="base64", id="x"),
    )
    emails = [_email(attachments=(_ref(),))]
    with (
        patch.object(t, "assess_email_photos", new=AsyncMock(return_value=[notice])),
        patch.object(t, "write_activity", new=AsyncMock()) as wa,
    ):
        n = await t.triage_email_photos(
            MagicMock(),
            _composio(),
            user_id="u1",
            company_id="c",
            emails=emails,
            settings=_settings(),
        )
    assert n == 0
    wa.assert_not_awaited()


@pytest.mark.asyncio
async def test_triage_never_raises() -> None:
    emails = [_email(attachments=(_ref(),))]
    with patch.object(t, "assess_email_photos", new=AsyncMock(side_effect=RuntimeError("kaboom"))):
        n = await t.triage_email_photos(
            MagicMock(),
            _composio(),
            user_id="u1",
            company_id="c",
            emails=emails,
            settings=_settings(),
        )
    assert n == 0


@pytest.mark.asyncio
async def test_triage_write_failure_is_swallowed() -> None:
    emails = [_email(attachments=(_ref(),))]
    with (
        patch.object(t, "assess_email_photos", new=AsyncMock(return_value=[_assessment()])),
        patch.object(t, "write_activity", new=AsyncMock(side_effect=RuntimeError("db down"))),
    ):
        n = await t.triage_email_photos(
            MagicMock(),
            _composio(),
            user_id="u1",
            company_id="c",
            emails=emails,
            settings=_settings(),
        )
    assert n == 0  # write failed → not counted, but no raise
