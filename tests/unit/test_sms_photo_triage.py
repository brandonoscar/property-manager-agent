"""Tests for the inbound-MMS maintenance-photo triage (#533 entry point 1).

The Twilio media fetch + the vision assessor are mocked, so no network / real
Gemini is touched — these lock the plumbing: media parsing, the auth'd fetch,
the assess→draft mapping, and the best-effort "never break the webhook" posture.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from helixis.agents.image_tools import DraftWorkOrder, ImageAssessment, SourceRef
from helixis.settings import Settings
from helixis.webhooks import sms_photo_triage as t


def _settings() -> Settings:
    s = Settings(env="test", memory={"enabled": False})
    s.twilio.account_sid = "AC123"
    s.twilio.auth_token = "tok"
    return s


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


def _fake_httpx(monkeypatch, *, content=b"\x89PNGdata", status=200, headers=None, raises=None):
    resp = MagicMock()
    resp.content = content
    resp.headers = headers or {"content-type": "image/png"}
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


# ── media parsing ────────────────────────────────────────────────


def test_extract_media_parses_numbered_fields() -> None:
    params = {
        "NumMedia": "2",
        "MediaUrl0": "https://api.twilio.com/m/ME0",
        "MediaContentType0": "image/jpeg",
        "MediaUrl1": "https://api.twilio.com/m/ME1",
        "MediaContentType1": "application/pdf",
    }
    media = t.extract_media(params)
    assert [m.content_type for m in media] == ["image/jpeg", "application/pdf"]


def test_extract_media_handles_missing_or_bad_nummedia() -> None:
    assert t.extract_media({}) == []
    assert t.extract_media({"NumMedia": "oops"}) == []


def test_first_image_media_skips_non_images() -> None:
    params = {
        "NumMedia": "2",
        "MediaUrl0": "https://x/doc",
        "MediaContentType0": "application/pdf",
        "MediaUrl1": "https://x/pic",
        "MediaContentType1": "image/heic-but-image",
    }
    got = t.first_image_media(params)
    assert got is not None and got.url == "https://x/pic"
    assert (
        t.first_image_media({"NumMedia": "1", "MediaUrl0": "u", "MediaContentType0": "text/plain"})
        is None
    )


# ── media fetch ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_media_bytes_uses_basic_auth(monkeypatch) -> None:
    get = _fake_httpx(monkeypatch, content=b"IMG", headers={"content-type": "image/jpeg"})
    out = await t.fetch_media_bytes(
        t.Media("https://api.twilio.com/m/ME0", "image/jpeg"), account_sid="AC", auth_token="tok"
    )
    assert out == (b"IMG", "image/jpeg")
    # basic auth passed on the outbound GET
    assert get.await_args.kwargs["auth"] == ("AC", "tok")


@pytest.mark.asyncio
async def test_fetch_media_bytes_none_without_creds(monkeypatch) -> None:
    get = _fake_httpx(monkeypatch)
    assert (
        await t.fetch_media_bytes(t.Media("u", "image/png"), account_sid="", auth_token="") is None
    )
    get.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_media_bytes_oversize_returns_none(monkeypatch) -> None:
    _fake_httpx(monkeypatch, content=b"x" * (9 * 1024 * 1024))
    assert (
        await t.fetch_media_bytes(t.Media("u", "image/png"), account_sid="AC", auth_token="t")
        is None
    )


@pytest.mark.asyncio
async def test_fetch_media_bytes_swallows_errors(monkeypatch) -> None:
    _fake_httpx(monkeypatch, raises=httpx.ConnectError("boom"))
    assert (
        await t.fetch_media_bytes(t.Media("u", "image/png"), account_sid="AC", auth_token="t")
        is None
    )


# ── assess + draft mapping ───────────────────────────────────────


@pytest.mark.asyncio
async def test_assess_sms_photo_orchestrates(monkeypatch) -> None:
    _fake_httpx(monkeypatch, content=b"IMG", headers={"content-type": "image/jpeg"})
    params = {
        "NumMedia": "1",
        "MediaUrl0": "https://api.twilio.com/m/ME0",
        "MediaContentType0": "image/jpeg",
    }
    with patch.object(t, "assess_image_impl", new=AsyncMock(return_value=_assessment())) as ai:
        out = await t.assess_sms_photo(params, settings=_settings())
    assert out is not None and out.image_kind == "maintenance_photo"
    # the fetched bytes were handed to the assessor as base64 image
    assert ai.await_args.kwargs["mime_type"] == "image/jpeg"
    assert ai.await_args.kwargs["base64_content"]


@pytest.mark.asyncio
async def test_assess_sms_photo_none_without_image() -> None:
    assert await t.assess_sms_photo({"NumMedia": "0"}, settings=_settings()) is None


def test_draft_summary_for_maintenance_photo() -> None:
    s = t.draft_work_order_summary(_assessment())
    assert s is not None
    assert "Draft work order" in s and "plumbing" in s and "P-trap" in s
    assert "has not filed anything" in s  # draft-only disclaimer


def test_draft_summary_none_for_non_actionable() -> None:
    # a notice, not a maintenance photo
    notice = ImageAssessment(
        image_kind="handwritten_notice",
        confidence=0.9,
        transcription="hi",
        source_ref=SourceRef(kind="base64", id="x"),
    )
    assert t.draft_work_order_summary(notice) is None
    # a maintenance kind with no issue summary (guarded elsewhere) → None
    empty = _assessment(issue_summary="", warnings=["issue_summary: blurry"])
    assert t.draft_work_order_summary(empty) is None


# ── record + top-level triage ────────────────────────────────────


@pytest.mark.asyncio
async def test_record_photo_draft_writes_activity() -> None:
    pool = MagicMock()
    with patch.object(t, "write_activity", new=AsyncMock()) as wa:
        ok = await t.record_photo_draft(
            pool, company_id="co-1", from_number="+15551230000", assessment=_assessment()
        )
    assert ok is True
    entry = wa.await_args.args[1]
    assert entry.action_type == "maintenance_photo_drafted"
    assert entry.company_id == "co-1"
    assert entry.raw["likely_trade"] == "plumbing"


@pytest.mark.asyncio
async def test_record_photo_draft_skips_non_actionable() -> None:
    pool = MagicMock()
    notice = ImageAssessment(
        image_kind="other",
        confidence=0.0,
        source_ref=SourceRef(kind="base64", id="x"),
        warnings=["unreadable_image"],
    )
    with patch.object(t, "write_activity", new=AsyncMock()) as wa:
        ok = await t.record_photo_draft(
            pool, company_id="co-1", from_number="+1", assessment=notice
        )
    assert ok is False
    wa.assert_not_awaited()


@pytest.mark.asyncio
async def test_triage_inbound_photo_end_to_end(monkeypatch) -> None:
    _fake_httpx(monkeypatch, content=b"IMG", headers={"content-type": "image/png"})
    params = {
        "NumMedia": "1",
        "MediaUrl0": "https://api.twilio.com/m/ME0",
        "MediaContentType0": "image/png",
    }
    pool = MagicMock()
    with (
        patch.object(t, "assess_image_impl", new=AsyncMock(return_value=_assessment())),
        patch.object(t, "write_activity", new=AsyncMock()) as wa,
    ):
        ok = await t.triage_inbound_photo(
            params, settings=_settings(), pool=pool, company_id="co-1", from_number="+1"
        )
    assert ok is True
    wa.assert_awaited_once()


@pytest.mark.asyncio
async def test_triage_never_raises(monkeypatch) -> None:
    # assess blows up → triage swallows it and returns False, protecting the webhook 200.
    params = {"NumMedia": "1", "MediaUrl0": "u", "MediaContentType0": "image/png"}
    with patch.object(t, "assess_sms_photo", new=AsyncMock(side_effect=RuntimeError("kaboom"))):
        ok = await t.triage_inbound_photo(
            params, settings=_settings(), pool=MagicMock(), company_id="co-1", from_number="+1"
        )
    assert ok is False
