"""The document and image tools must never read what only the server can reach.

Andrew's audit (2026-09-29), confirmed in code: ``extract_document_fields``
and ``assess_image`` took a model-chosen ``file_path`` and read it with
``Path.read_bytes()``, and fetched a model-chosen ``url`` with bare httpx. The
Document agent is always on, so a signed-in user could ask for the fields of
``/proc/self/environ`` (the server's keys) or of ``http://169.254.169.254/``.

Two halves, each read from the side that matters:

* the model side: the JSON schema the SDK actually sends has no
  ``file_path``, and an injected one reads nothing;
* the fetch side: a URL that resolves anywhere non-public is refused before
  any request, and an allowed one is sent to the address that was checked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from agents.tool_context import ToolContext

from helixis.agents import document_tools as dt
from helixis.agents import image_tools as it
from helixis.security import public_url as pu
from helixis.settings import Settings

_MODEL_FACING = (dt.extract_document_fields, it.assess_image)


def _settings() -> Settings:
    return Settings(
        env="test",
        llm={"default_model": "gemini/gemini-2.5-flash", "google_api_key": "fake-g"},
        memory={"enabled": False},
        document={"request_timeout_seconds": 1.0, "max_bytes": 1024 * 1024},
    )


def _resolves_to(*addresses: str) -> Any:
    async def fake(_host: str, _port: int) -> list[str]:
        return list(addresses)

    return fake


# ── the model side ────────────────────────────────────────────────────


@pytest.mark.parametrize("tool", _MODEL_FACING, ids=lambda t: t.name)
def test_the_schema_the_model_sees_has_no_file_path(tool: Any) -> None:
    """Read from the SDK's own schema, the bytes that go to the model, not
    from the Python signature (which a decorator could disagree with)."""
    props = tool.params_json_schema["properties"]
    assert "file_path" not in props
    # Still a working tool: the two inputs a user can legitimately give.
    assert {"base64_content", "url"} <= set(props)
    assert tool.params_json_schema.get("additionalProperties") is False


@pytest.mark.parametrize("tool", _MODEL_FACING, ids=lambda t: t.name)
async def test_an_injected_file_path_reads_nothing(tool: Any, tmp_path: Path) -> None:
    """A model that sends ``file_path`` anyway (a stale prompt, an injected
    document) gets nothing back from that file."""
    secret = tmp_path / "environ"
    secret.write_text("HELIXIS_DATA_KEY=sentinel-not-a-real-key")
    ctx = ToolContext(context=None, tool_name=tool.name, tool_call_id="c1", tool_arguments="{}")
    llm = AsyncMock(side_effect=AssertionError("the file's bytes reached a model"))
    with patch("litellm.acompletion", llm):
        out = await tool.on_invoke_tool(
            ctx, json.dumps({"file_path": str(secret), "mime_type": "text/plain"})
        )
    assert "sentinel-not-a-real-key" not in str(out)
    assert "attachment_unavailable" in str(out)
    llm.assert_not_awaited()


# ── the fetch side ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/api/v1/health",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://0.0.0.0/",
        "http://10.0.0.5/internal",
        "file:///etc/passwd",
        "ftp://example.com/lease.pdf",
    ],
)
async def test_a_url_that_is_not_public_is_refused_before_any_request(url: str) -> None:
    get = AsyncMock(side_effect=AssertionError("a refused URL was fetched"))
    with patch.object(httpx.AsyncClient, "get", get):
        result = await dt.extract_document_fields_impl(
            url=url, mime_type="application/pdf", settings=_settings()
        )
    assert result.warnings == ["attachment_unavailable"]
    get.assert_not_awaited()


@pytest.mark.parametrize(
    "addresses",
    [("10.1.2.3",), ("100.64.0.9",), ("93.184.216.34", "127.0.0.1"), ("fd00::1",)],
    ids=["private", "carrier-nat", "one-private-record", "ipv6-unique-local"],
)
async def test_a_name_resolving_anywhere_private_is_refused(
    addresses: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name is what an attacker controls, so the check is on what it
    resolves to. One private record among public ones is the usual shape of a
    rebinding name, so ANY non-public answer refuses."""
    monkeypatch.setattr(pu, "_resolve", _resolves_to(*addresses))
    request, reason = await pu.vet_public_url("https://docs.example.com/lease.pdf")
    assert request is None
    assert reason == "non_public_address"


async def test_an_allowed_fetch_goes_to_the_checked_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ Checking one lookup and letting httpx do another is the rebinding
    hole. The request must be sent to the vetted IP, with the real name as the
    Host header and as the TLS server name (so the certificate is still
    checked against it), and with redirects off."""
    monkeypatch.setattr(pu, "_resolve", _resolves_to("2606:4700::1111", "93.184.216.34"))
    ok = MagicMock(status_code=200, content=b"%PDF-1.7 lease")
    get = AsyncMock(return_value=ok)
    with patch.object(httpx.AsyncClient, "get", get):
        data, warning = await dt._fetch_bytes(
            file_path=None,
            base64_content=None,
            url="https://docs.example.com/a/lease.pdf?token=abc",
            max_bytes=1024,
            timeout_seconds=1.0,
        )
    assert (data, warning) == (b"%PDF-1.7 lease", None)
    sent_url = get.await_args.args[0]
    assert sent_url == "https://93.184.216.34/a/lease.pdf?token=abc"  # IPv4 preferred
    assert get.await_args.kwargs["headers"] == {"Host": "docs.example.com"}
    assert get.await_args.kwargs["extensions"] == {"sni_hostname": "docs.example.com"}


async def test_a_redirect_is_a_failed_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its target was never checked, so it is neither followed nor read."""
    monkeypatch.setattr(pu, "_resolve", _resolves_to("93.184.216.34"))
    moved = MagicMock(status_code=302, content=b"<a href='http://127.0.0.1/'>moved</a>")
    with patch.object(httpx.AsyncClient, "get", AsyncMock(return_value=moved)):
        data, warning = await dt._fetch_bytes(
            file_path=None,
            base64_content=None,
            url="https://docs.example.com/lease.pdf",
            max_bytes=1024,
            timeout_seconds=1.0,
        )
    assert (data, warning) == (None, "attachment_unavailable")


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("2606:4700::1111", True),
        ("127.0.0.1", False),
        ("10.0.0.1", False),
        ("172.16.5.4", False),
        ("192.168.1.1", False),
        ("100.64.0.1", False),
        ("169.254.169.254", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("::1", False),
        ("fe80::1%eth0", False),
        ("::ffff:127.0.0.1", False),
        ("2002:7f00:1::", False),
        ("not-an-ip", False),
    ],
)
def test_public_address_classification(address: str, public: bool) -> None:
    assert pu.is_public_address(address) is public
