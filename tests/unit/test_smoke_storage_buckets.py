"""The storage_buckets smoke check — the tripwire for the missing-bucket class
(code references a bucket, Supabase doesn't have it, nothing fails until the
first real upload; exactly how codeexec_artifacts shipped missing)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.smoke.checks import _REQUIRED_BUCKETS, check_storage_buckets

pytestmark = pytest.mark.asyncio


def _settings(url: str = "https://proj.supabase.co", key: str = "svc-key") -> Any:
    s = MagicMock()
    s.supabase.url = url
    s.supabase.service_key = key
    return s


class _FakeClient:
    """httpx.AsyncClient stand-in returning a per-bucket status code."""

    def __init__(self, statuses: dict[str, int]) -> None:
        self._statuses = statuses
        self.requested: list[str] = []

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    async def get(self, url: str, headers: dict[str, str]) -> Any:
        self.requested.append(url)
        resp = MagicMock()
        bucket = url.rsplit("/", 1)[-1]
        resp.status_code = self._statuses.get(bucket, 404)
        return resp


async def test_all_buckets_present_passes() -> None:
    client = _FakeClient({b: 200 for b in _REQUIRED_BUCKETS})
    with patch("httpx.AsyncClient", return_value=client):
        out = await check_storage_buckets(_settings())
    assert out.status == "pass"
    # Every required bucket was actually probed.
    assert len(client.requested) == len(_REQUIRED_BUCKETS)


async def test_missing_bucket_fails_and_names_it() -> None:
    statuses = {b: 200 for b in _REQUIRED_BUCKETS}
    statuses["codeexec_artifacts"] = 404
    client = _FakeClient(statuses)
    with patch("httpx.AsyncClient", return_value=client):
        out = await check_storage_buckets(_settings())
    assert out.status == "fail"
    assert "codeexec_artifacts (404)" in out.detail


async def test_unconfigured_storage_fails_closed() -> None:
    out = await check_storage_buckets(_settings(url="", key=""))
    assert out.status == "fail"
    assert "not set" in out.detail


async def test_network_error_fails_with_exception_name() -> None:
    boom = MagicMock()
    boom.__aenter__ = AsyncMock(side_effect=ConnectionError("no route"))
    with patch("httpx.AsyncClient", return_value=boom):
        out = await check_storage_buckets(_settings())
    assert out.status == "fail"
    assert "ConnectionError" in out.detail


def test_required_buckets_match_code_references() -> None:
    # The check must cover every bucket the backend writes to.
    from helixis.codeexec import storage_io

    assert storage_io._ARTIFACTS_BUCKET in _REQUIRED_BUCKETS
    assert storage_io._DOCUMENTS_BUCKET in _REQUIRED_BUCKETS
