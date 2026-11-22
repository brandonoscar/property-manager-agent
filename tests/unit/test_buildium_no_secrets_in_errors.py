"""No Buildium credential reaches an error, a log or the page.

Buildium authenticates with two headers, ``x-buildium-client-id`` and
``x-buildium-client-secret``. The client built its errors from Buildium's raw
response body, and those errors are logged by the agent tools and the backfill
and returned to the page by the connection test. A server or proxy that echoes
the request headers would have put a working credential in all of them.
``BuildiumClient._scrub`` now masks both first.

Driven against a fake Buildium that echoes both credentials back on every error
status the client handles: the terminal ones at once, the retryable ones after
the retries run out. The page half, ``POST /buildium/test`` against the same
echo, is in ``test_buildium_routes.py`` next to that route's fixtures.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from helixis.buildium.client import BuildiumClient
from helixis.buildium.errors import BuildiumApiError
from helixis.settings import BuildiumSettings

# Built at runtime: a key-shaped literal here trips the repo's gitleaks scan
# (it did, on 0732772; that finding is pinned in .gitleaksignore).
CLIENT_ID = "-".join(("bdid", "fake", "id", "for", "tests"))
SECRET = "-".join(("bd", "fake", "secret", "for", "tests"))
CREDENTIALS = (CLIENT_ID, SECRET)
#: What a misbehaving server or proxy might send back.
ECHO = f"denied: x-buildium-client-id: {CLIENT_ID}; x-buildium-client-secret: {SECRET}"


def _leaks(text: str) -> list[str]:
    return [c for c in CREDENTIALS if c in text]


def _settings(client_id: str = CLIENT_ID, secret: str = SECRET) -> BuildiumSettings:
    # High rate so the per-account token bucket never blocks in-test.
    return BuildiumSettings(
        client_id=client_id,
        client_secret=secret,
        base_url="https://x.test",
        requests_per_sec=1000.0,
    )


def _echoing(status: int) -> httpx.MockTransport:
    return httpx.MockTransport(lambda _req: httpx.Response(status, text=ECHO))


def _client(status: int, settings: BuildiumSettings | None = None) -> BuildiumClient:
    settings = settings or _settings()
    http = httpx.AsyncClient(base_url="https://x.test/v1", transport=_echoing(status))
    return BuildiumClient(settings, http_client=http)


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    """Every record as a handler would format it, tracebacks included."""
    out = []
    for record in caplog.records:
        out.append(record.getMessage())
        if record.exc_info and record.exc_info[1] is not None:
            out.append(repr(record.exc_info[1]))
    return "\n".join(out)


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("helixis.buildium.client.asyncio.sleep", new=AsyncMock()):
        yield


TERMINAL = [400, 401, 403, 404, 422, 500]
RETRYABLE = [429, 502, 503, 504]


@pytest.mark.parametrize("status", TERMINAL + RETRYABLE)
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c: c.request("GET", "/rentals"), id="read"),
        pytest.param(lambda c: c.list_all("/rentals", None), id="paginated"),
        pytest.param(lambda c: c.request("PUT", "/tasks/1", body={"Title": "x"}), id="write"),
    ],
)
async def test_an_error_keeps_the_credentials_out_of_its_message_and_the_log(
    status: int, call, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(BuildiumApiError) as exc:
        await call(_client(status))
    shown = "\n".join([str(exc.value), repr(exc.value), *map(str, exc.value.args)])
    assert "denied:" in shown  # the body did come through ...
    assert _leaks(shown) == []  # ... with the credentials masked
    assert _leaks(_logged(caplog)) == []


async def test_one_credential_inside_the_other_leaves_no_fragment() -> None:
    """Masking the id first would turn the secret into ``***`` + its tail."""
    client_id, secret = "abcd1234", "abcd1234efgh5678"
    http = httpx.AsyncClient(
        base_url="https://x.test/v1",
        transport=httpx.MockTransport(lambda _r: httpx.Response(401, text=f"echo {secret}")),
    )
    buildium = BuildiumClient(_settings(client_id=client_id, secret=secret), http_client=http)
    with pytest.raises(BuildiumApiError) as exc:
        await buildium.request("GET", "/rentals")
    assert str(exc.value) == "Buildium 401 /rentals: echo ***"


async def test_a_body_without_the_credentials_is_passed_through_unchanged() -> None:
    """The scrub masks credentials, not Buildium's message."""
    body = '{"UserMessage":"The field Title is required.","Errors":[{"Key":"Title"}]}'
    http = httpx.AsyncClient(
        base_url="https://x.test/v1",
        transport=httpx.MockTransport(lambda _r: httpx.Response(422, text=body)),
    )
    with pytest.raises(BuildiumApiError) as exc:
        await BuildiumClient(_settings(), http_client=http).request("GET", "/rentals")
    assert str(exc.value) == f"Buildium 422 /rentals: {body}"


async def test_a_client_never_prints_the_credentials() -> None:
    buildium = _client(200)
    assert _leaks(repr(buildium)) == []
    assert _leaks(repr(vars(buildium).get("_settings"))) == []
