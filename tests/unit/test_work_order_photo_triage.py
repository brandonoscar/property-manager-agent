"""Tests for the Buildium work-order photo triage (#533 entry point 2).

The Buildium API (task history + presigned download), the image fetch, and the
vision assessor are all mocked, so no network / real Gemini is touched — these
lock the plumbing: history→file-ref parsing, the presigned fetch (incl.
non-image / oversize / error), the assess→draft mapping, the activity record,
and the best-effort "never break the webhook" posture.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from helixis.agents.image_tools import DraftWorkOrder, ImageAssessment, SourceRef
from helixis.buildium.schemas import WorkOrder, WorkOrderTaskRef
from helixis.settings import Settings
from helixis.webhooks import work_order_photo_triage as t


def _settings() -> Settings:
    return Settings(env="test", memory={"enabled": False})


def _wo(*, task_id: int | None = 555, wo_id: int = 99) -> WorkOrder:
    return WorkOrder(
        Id=wo_id,
        Task=WorkOrderTaskRef(Id=task_id) if task_id is not None else None,
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


def _fake_api(*, history=None, download_url="https://s3/presigned"):
    api = MagicMock()
    api.tasks.list_history = AsyncMock(return_value=history or [])
    api.tasks.download_history_file_url = AsyncMock(return_value={"DownloadUrl": download_url})
    return api


# ── history → file-ref parsing ───────────────────────────────────


def test_history_file_refs_reads_documented_keys() -> None:
    history = [
        {"Id": 10, "FileIds": [1, 2]},
        {"Id": 11, "FileIds": []},
        {"Id": 12},  # no FileIds
        {"FileIds": [3]},  # no Id — skipped
        {"Id": 13, "FileIds": [4]},
    ]
    refs = t._history_file_refs(history, limit=10)
    assert refs == [(10, 1), (10, 2), (13, 4)]


def test_history_file_refs_respects_limit() -> None:
    history = [{"Id": 10, "FileIds": [1, 2, 3, 4]}]
    assert t._history_file_refs(history, limit=2) == [(10, 1), (10, 2)]


def test_history_file_refs_empty() -> None:
    assert t._history_file_refs([], limit=3) == []
    assert t._history_file_refs(None, limit=3) == []  # type: ignore[arg-type]


# ── presigned image fetch ────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_image_returns_bytes_and_mime(monkeypatch) -> None:
    _fake_httpx(monkeypatch, content=b"IMG", headers={"content-type": "image/jpeg"})
    out = await t._fetch_image("https://s3/presigned")
    assert out == (b"IMG", "image/jpeg")


@pytest.mark.asyncio
async def test_fetch_image_none_for_empty_url() -> None:
    assert await t._fetch_image("") is None


@pytest.mark.asyncio
async def test_fetch_image_none_for_non_image(monkeypatch) -> None:
    _fake_httpx(monkeypatch, headers={"content-type": "application/pdf"})
    assert await t._fetch_image("https://s3/x") is None


@pytest.mark.asyncio
async def test_fetch_image_oversize_returns_none(monkeypatch) -> None:
    _fake_httpx(monkeypatch, content=b"x" * (9 * 1024 * 1024))
    assert await t._fetch_image("https://s3/x") is None


@pytest.mark.asyncio
async def test_fetch_image_swallows_errors(monkeypatch) -> None:
    _fake_httpx(monkeypatch, raises=httpx.ConnectError("boom"))
    assert await t._fetch_image("https://s3/x") is None


# ── assess orchestration ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_assess_work_order_photos_orchestrates(monkeypatch) -> None:
    _fake_httpx(monkeypatch, content=b"IMG", headers={"content-type": "image/jpeg"})
    api = _fake_api(history=[{"Id": 10, "FileIds": [1]}])
    with patch.object(t, "assess_image_impl", new=AsyncMock(return_value=_assessment())) as ai:
        out = await t.assess_work_order_photos(_wo(), api, settings=_settings())
    assert len(out) == 1 and out[0].image_kind == "maintenance_photo"
    api.tasks.download_history_file_url.assert_awaited_once_with(555, 10, 1)
    assert ai.await_args.kwargs["mime_type"] == "image/jpeg"
    assert ai.await_args.kwargs["base64_content"]


@pytest.mark.asyncio
async def test_assess_work_order_photos_no_task() -> None:
    api = _fake_api()
    out = await t.assess_work_order_photos(_wo(task_id=None), api, settings=_settings())
    assert out == []
    api.tasks.list_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_assess_work_order_photos_caps_at_max(monkeypatch) -> None:
    _fake_httpx(monkeypatch)
    api = _fake_api(history=[{"Id": 10, "FileIds": [1, 2, 3, 4, 5]}])
    with patch.object(t, "assess_image_impl", new=AsyncMock(return_value=_assessment())):
        out = await t.assess_work_order_photos(_wo(), api, settings=_settings(), max_photos=2)
    assert len(out) == 2
    assert api.tasks.download_history_file_url.await_count == 2


@pytest.mark.asyncio
async def test_assess_work_order_photos_history_failure(monkeypatch) -> None:
    api = _fake_api()
    api.tasks.list_history = AsyncMock(side_effect=RuntimeError("api down"))
    out = await t.assess_work_order_photos(_wo(), api, settings=_settings())
    assert out == []


@pytest.mark.asyncio
async def test_assess_work_order_photos_skips_bad_file(monkeypatch) -> None:
    # first file fetch returns a non-image, second succeeds → one assessment.
    api = _fake_api(history=[{"Id": 10, "FileIds": [1, 2]}])
    resp_bad = MagicMock(
        content=b"x", headers={"content-type": "application/pdf"}, raise_for_status=MagicMock()
    )
    resp_ok = MagicMock(
        content=b"IMG", headers={"content-type": "image/png"}, raise_for_status=MagicMock()
    )
    client = MagicMock()
    client.get = AsyncMock(side_effect=[resp_bad, resp_ok])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=cm))
    with patch.object(t, "assess_image_impl", new=AsyncMock(return_value=_assessment())):
        out = await t.assess_work_order_photos(_wo(), api, settings=_settings())
    assert len(out) == 1


# ── triage: record drafts ────────────────────────────────────────


@pytest.mark.asyncio
async def test_triage_records_draft(monkeypatch) -> None:
    pool = MagicMock()
    with (
        patch.object(t, "assess_work_order_photos", new=AsyncMock(return_value=[_assessment()])),
        patch.object(t, "write_activity", new=AsyncMock()) as wa,
    ):
        n = await t.triage_work_order_photos(
            _wo(), _fake_api(), pool, settings=_settings(), company_id="co-1", account_id=42
        )
    assert n == 1
    entry = wa.await_args.args[1]
    assert entry.action_type == "maintenance_photo_drafted"
    assert entry.company_id == "co-1"
    assert entry.entity_type == "work_order"
    assert entry.entity_id == 99
    assert entry.source_label == "Buildium work-order photo"
    assert "work-order photo" in entry.description  # source named in the draft
    assert entry.raw["likely_trade"] == "plumbing"


@pytest.mark.asyncio
async def test_triage_skips_non_actionable() -> None:
    notice = ImageAssessment(
        image_kind="handwritten_notice",
        confidence=0.9,
        transcription="quiet hours",
        source_ref=SourceRef(kind="base64", id="x"),
    )
    with (
        patch.object(t, "assess_work_order_photos", new=AsyncMock(return_value=[notice])),
        patch.object(t, "write_activity", new=AsyncMock()) as wa,
    ):
        n = await t.triage_work_order_photos(
            _wo(), _fake_api(), MagicMock(), settings=_settings(), company_id="c", account_id=1
        )
    assert n == 0
    wa.assert_not_awaited()


@pytest.mark.asyncio
async def test_triage_never_raises() -> None:
    with patch.object(
        t, "assess_work_order_photos", new=AsyncMock(side_effect=RuntimeError("kaboom"))
    ):
        n = await t.triage_work_order_photos(
            _wo(), _fake_api(), MagicMock(), settings=_settings(), company_id="c", account_id=1
        )
    assert n == 0
