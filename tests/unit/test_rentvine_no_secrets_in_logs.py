"""Merge-gate test 2 of 3 (``design/rentvine-terms-rules.md``, "Before you
merge anything"): **no Rentvine secret reaches a log**.

Terms rule 11: secrets are never logged, never sent to a model provider, never
in a prompt or agent context. Two ways they could, both found writing this:

* **An error body that echoes the request.** The client's error carries
  Rentvine's response body, and the retry log, the sync, the routes and the
  sync-state row all print it. A server or proxy that echoes the request
  (key, secret, or the Basic header, in either order) would put a working
  credential in the logs. The client now masks them (``RentvineClient._scrub``).
* **The settings repr.** ``RentvineSettings`` printed ``api_key`` and
  ``api_secret``, so any settings object in a log line, an exception or a
  Sentry frame carried both. They are now ``repr=False``.

Behavioural half: every read and the one write are driven against a fake
Rentvine that answers 401, 403, 429 and 500 with the credentials echoed back,
and every log record, exception and sync result is searched. Structural half:
an AST walk fails on any logging call in the Rentvine code that passes a
credential field.
"""

from __future__ import annotations

import ast
import base64
import logging
from pathlib import Path

import httpx
import pytest

from helixis.rentvine import client as rvclient
from helixis.rentvine import sync
from helixis.settings import RentvineSettings, Settings

from .test_rentvine_sync import _fake_pool

REPO = Path(__file__).resolve().parents[2]

# Built at runtime: a key-shaped literal here trips the repo's gitleaks scan
# (it did, on 9022423; that finding is pinned in .gitleaksignore).
KEY = "-".join(("rv", "fake", "key", "for", "tests"))
SECRET = "-".join(("rv", "fake", "secret", "for", "tests"))
CREDENTIALS = (
    KEY,
    SECRET,
    base64.b64encode(f"{KEY}:{SECRET}".encode()).decode(),
    base64.b64encode(f"{SECRET}:{KEY}".encode()).decode(),
)
#: What a misbehaving server or proxy might send back: every form of the secret.
ECHO = (
    f"denied for {KEY}:{SECRET}; Authorization: Basic {CREDENTIALS[2]} "
    f"(swapped: Basic {CREDENTIALS[3]})"
)


def _settings() -> RentvineSettings:
    return RentvineSettings(enabled=True, account_code="acme", api_key=KEY, api_secret=SECRET)


def _client(status: int) -> rvclient.RentvineClient:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=ECHO)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=(KEY, SECRET))
    return rvclient.RentvineClient(_settings(), client=http)


def _leaks(text: str) -> list[str]:
    return [c for c in CREDENTIALS if c in text]


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rvclient, "RETRY_BACKOFF_S", (0.0,) * len(rvclient.RETRY_BACKOFF_S))
    monkeypatch.setattr(
        rvclient, "RATE_LIMIT_BACKOFF_S", (0.0,) * len(rvclient.RATE_LIMIT_BACKOFF_S)
    )


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    """Every record as a handler would format it, args and tracebacks included."""
    out = []
    for record in caplog.records:
        out.append(record.getMessage())
        if record.exc_info and record.exc_info[1] is not None:
            out.append(repr(record.exc_info[1]))
    return "\n".join(out)


STATUSES = [401, 403, 429, 500]


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.asyncio
async def test_every_read_path_keeps_the_secret_out_of_logs_and_errors(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    raised: list[str] = []
    for read in (
        lambda c: c.get("/properties"),
        lambda c: c.get_rows("/leases"),
        lambda c: c.iter_all("/properties"),
        lambda c: c.get_page("/properties", page=1, page_size=1),
    ):
        rvclient._holds.clear()
        client = _client(status)
        with pytest.raises(rvclient.RentvineAPIError) as exc:
            await read(client)
        raised += [str(exc.value), exc.value.detail]
    assert "denied for" in "\n".join(raised)  # the body did come through ...
    assert _leaks("\n".join(raised)) == []  # ... with the credentials masked
    assert _leaks(_logged(caplog)) == []


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.asyncio
async def test_the_one_write_keeps_the_secret_out_of_its_error(status: int) -> None:
    with pytest.raises(rvclient.RentvineAPIError) as exc:
        await _client(status).post(
            "/maintenance/work-orders/{workOrderID}", json_body={}, path_params={"workOrderID": 1}
        )
    assert _leaks(str(exc.value) + exc.value.detail) == []


@pytest.mark.parametrize("status", [401, 500])
@pytest.mark.asyncio
async def test_a_failed_sync_keeps_the_secret_out_of_logs_and_its_result(
    status: int, caplog: pytest.LogCaptureFixture
) -> None:
    """``result.failed`` is written to the sync-state row and shown as a stale
    reason, so it is output too, not just the log."""
    caplog.set_level(logging.DEBUG)
    app = Settings()
    app = app.model_copy(update={"rentvine": app.rentvine.model_copy(update={"enabled": True})})
    result = await sync.sync_company(_fake_pool(), "c", settings=app, client=_client(status))
    assert result.failed  # it really failed, so the error path ran
    assert _leaks(repr(result.failed)) == []
    assert _leaks(_logged(caplog)) == []


def test_a_settings_object_never_prints_the_credential() -> None:
    settings = _settings()
    for shown in (repr(settings), str(settings), f"{settings}", repr({"s": settings})):
        assert _leaks(shown) == [], shown
    # ... and the values themselves are untouched.
    assert settings.api_key == KEY and settings.api_secret == SECRET


def test_a_client_never_prints_the_credential() -> None:
    client = rvclient.RentvineClient(_settings(), client=httpx.AsyncClient())
    assert _leaks(repr(client)) == []
    assert _leaks(repr(vars(client).get("_settings"))) == []


# ── structural: no logging call passes a credential ─────────────────────────

#: Names that hold a Rentvine credential somewhere in this code.
_CREDENTIAL_NAMES = {
    "api_key",
    "api_secret",
    "webhook_secret",
    "encrypted_api_key",
    "encrypted_api_secret",
    "encrypted_webhook_secret",
    "secret",
    "password",
}
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _rentvine_sources() -> list[Path]:
    files = sorted((REPO / "src/helixis/rentvine").glob("*.py"))
    files += sorted((REPO / "scripts").glob("rentvine_*.py"))
    files += [
        REPO / "src/helixis/api/routes/rentvine.py",
        REPO / "src/helixis/agents/rentvine_tools.py",
    ]
    return [f for f in files if f.exists()]


def _is_log_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "print"
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _LOG_METHODS
        and (
            isinstance(func.value, ast.Name)
            and func.value.id in {"logger", "logging", "log", "_log"}
        )
    )


def _credential_refs(node: ast.AST) -> list[str]:
    found = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in _CREDENTIAL_NAMES:
            found.append(sub.attr)
        elif isinstance(sub, ast.Name) and sub.id in _CREDENTIAL_NAMES:
            found.append(sub.id)
        elif (
            isinstance(sub, ast.Subscript)
            and isinstance(sub.slice, ast.Constant)
            and sub.slice.value in _CREDENTIAL_NAMES
        ):
            found.append(str(sub.slice.value))
    return found


def _offenders(paths: list[Path], root: Path) -> list[str]:
    out = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_log_call(node):
                args = [*node.args, *(k.value for k in node.keywords)]
                refs = sorted({r for arg in args for r in _credential_refs(arg)})
                if refs:
                    out.append(f"{path.relative_to(root)}:{node.lineno} logs {refs}")
    return out


def test_no_logging_call_in_the_rentvine_code_passes_a_credential() -> None:
    assert _offenders(_rentvine_sources(), REPO) == []


def test_the_structural_check_finds_a_planted_offender(tmp_path: Path) -> None:
    """The walk must be able to fail: a guard that finds nothing proves nothing."""
    planted = tmp_path / "rentvine_planted.py"
    planted.write_text(
        "import logging\nlogger = logging.getLogger(__name__)\n"
        "def f(settings, row):\n"
        "    logger.info('using %s', settings.api_secret)\n"
        "    print(row['encrypted_api_key'])\n"
        "    logger.info('fine: %s', settings.account_code)\n",
        encoding="utf-8",
    )
    assert _offenders([planted], tmp_path) == [
        "rentvine_planted.py:4 logs ['api_secret']",
        "rentvine_planted.py:5 logs ['encrypted_api_key']",
    ]
