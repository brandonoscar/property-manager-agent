"""Tests for ``scripts/smoke.py`` using httpx.MockTransport.

The script driver function is callable directly so we don't have to spawn
a subprocess. Each test points the client at a deterministic mock that
replies with the desired status codes and asserts the exit code.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

# Capture the real AsyncClient class before any test patches it. Tests
# patch ``smoke.httpx.AsyncClient`` (which is the same module attribute);
# without this stash, ``httpx.AsyncClient(transport=…)`` inside a test
# would resolve to the patched mock and we'd build a Mock instead of a
# real client.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _load_smoke_module():
    """Import scripts/smoke.py without it being on the package path."""
    repo_root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_helixis_smoke", repo_root / "scripts" / "smoke.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_helixis_smoke"] = mod
    spec.loader.exec_module(mod)
    return mod


smoke = _load_smoke_module()


# ─────────────────────────────────────────────────────────────────────
# Mock builders
# ─────────────────────────────────────────────────────────────────────


class _AsyncChunks(httpx.AsyncByteStream):
    """Minimal AsyncByteStream wrapping a list of byte chunks. Required
    so MockTransport responses pass httpx's ``send(stream=True)`` shape
    assertion."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):  # type: ignore[override]
        for c in self._chunks:
            yield c

    async def aclose(self) -> None:
        return None


def _async_chunks(chunks: list[bytes]) -> _AsyncChunks:
    return _AsyncChunks(chunks)


def _all_healthy_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v1/health":
        return httpx.Response(200, json={"status": "ok"})
    if path == "/api/v1/health/ready":
        return httpx.Response(200, json={"status": "ready"})
    if path == "/api/v1/agent/run":
        # Stream-mode response — httpx asserts that the response stream
        # is an AsyncByteStream when send(stream=True) is used. Wrap the
        # bytes in an async generator factory.
        return httpx.Response(200, stream=_async_chunks([b'data: {"type":"thinking"}\n\n']))
    if path in (
        "/api/v1/webhooks/test",
        "/api/v1/memory/search",
        "/api/v1/billing/usage",
    ):
        # Soft endpoints — present in this scenario, return 200.
        return httpx.Response(200, json={"ok": True})
    return httpx.Response(404)


def _required_failing_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v1/health":
        return httpx.Response(500, json={"error": "broken"})
    return _all_healthy_handler(request)


def _missing_optional_handler(request: httpx.Request) -> httpx.Response:
    """Required endpoints healthy; optional endpoints all 404."""
    path = request.url.path
    if path == "/api/v1/health":
        return httpx.Response(200, json={"status": "ok"})
    if path == "/api/v1/health/ready":
        return httpx.Response(200, json={"status": "ready"})
    if path == "/api/v1/agent/run":
        return httpx.Response(200, stream=_async_chunks([b'data: {"type":"thinking"}\n\n']))
    return httpx.Response(404)


# ─────────────────────────────────────────────────────────────────────
# Driver helper
# ─────────────────────────────────────────────────────────────────────


async def _run_with_transport(transport: httpx.MockTransport) -> int:
    """Invoke ``smoke.run_smoke`` with httpx routed through ``transport``.

    Monkeypatches ``smoke.httpx.AsyncClient`` to a wrapper that always
    constructs a real client (``_REAL_ASYNC_CLIENT``) with our MockTransport
    spliced in.
    """
    from unittest.mock import patch

    def _factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _REAL_ASYNC_CLIENT(*args, **kwargs)  # type: ignore[arg-type]

    with patch.object(smoke.httpx, "AsyncClient", side_effect=_factory):
        return await smoke.run_smoke(
            base_url="http://test",
            token="fake-jwt",
            company_id="demo-company",
        )


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_smoke_exits_0_when_all_healthy(capsys: pytest.CaptureFixture[str]) -> None:
    transport = httpx.MockTransport(_all_healthy_handler)
    rc = await _run_with_transport(transport)
    assert rc == 0
    out = capsys.readouterr().out
    assert "/api/v1/health" in out
    assert "✅" in out


@pytest.mark.asyncio
async def test_smoke_exits_1_when_required_check_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = httpx.MockTransport(_required_failing_handler)
    rc = await _run_with_transport(transport)
    assert rc == 1
    out = capsys.readouterr().out
    assert "❌" in out


@pytest.mark.asyncio
async def test_smoke_soft_fails_404_optional_without_failing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Required endpoints up, optional endpoints 404 → exit 0 with TODO notes."""
    transport = httpx.MockTransport(_missing_optional_handler)
    rc = await _run_with_transport(transport)
    assert rc == 0
    out = capsys.readouterr().out
    # Soft 404s log a TODO note and DO NOT flip the exit code. They render
    # with the success icon because ok=True (404 is the expected/handled
    # state for these endpoints until the corresponding phase ships).
    assert "TODO" in out
    assert "404" in out


@pytest.mark.asyncio
async def test_smoke_exits_1_when_token_missing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = await smoke.run_smoke(
        base_url="http://test",
        token=None,
        company_id="demo-company",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "HELIXIS_SMOKE_TOKEN" in err
