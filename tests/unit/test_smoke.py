"""Unit tests for the real-services smoke harness.

These tests never hit a real external service — every check is
monkeypatched to return a deterministic :class:`CheckResult`. The
goal is to validate the orchestration shape (parallel gather,
timeout handling, exit semantics, output formatting, redaction),
not the individual service integrations.

Integration coverage for the actual service calls happens implicitly
when ``python -m helixis.smoke`` runs against real creds in CI's
daily health-check job; failing that surfaces the live regression
rather than relying on unit-test mocks of third-party SDKs.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.smoke import checks as _checks
from helixis.smoke import runner as _runner
from helixis.smoke.checks import CheckResult, _is_coherent_one_word_ack
from helixis.smoke.output import _redact, format_json, format_pretty

# ── Redaction ───────────────────────────────────────────────────────


def test_redact_openai_key() -> None:
    assert _redact("auth failed: sk-abc123XYZdef9876") == "auth failed: sk-[REDACTED]"


def test_redact_jwt() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signaturepart"
    assert _redact(f"bad token {jwt}") == "bad token [REDACTED-JWT]"


def test_redact_long_hex_string() -> None:
    long_hex = "abcdef" * 8  # 48 chars
    assert _redact(f"key={long_hex}") == "key=[REDACTED]"  # key= path wins


def test_redact_postgres_url() -> None:
    assert (
        _redact("conn failed: postgres://user:hunter2@host:5432/db")
        == "conn failed: postgres://[REDACTED]@host:5432/db"
    )


def test_redact_field_value_token() -> None:
    assert _redact("request had token=abc123def") == "request had token=[REDACTED]"


def test_redact_preserves_non_secret_text() -> None:
    assert _redact("200 OK") == "200 OK"
    assert _redact("no active connection for GMAIL") == "no active connection for GMAIL"


# ── Pretty formatter ────────────────────────────────────────────────


def test_format_pretty_all_pass() -> None:
    results = [
        CheckResult("buildium_api", "pass", 420, "200 OK"),
        CheckResult("composio_gmail", "pass", 180, "active"),
    ]
    output = format_pretty("2026-05-26T07:00:14Z", True, 2.4, results)
    assert "Helixis smoke — 2026-05-26T07:00:14Z" in output
    assert "✓ buildium_api" in output
    assert "✓ composio_gmail" in output
    assert "PASS — 2/2 checks green in 2.4s" in output
    assert "Exit 1." not in output


def test_format_pretty_one_fail() -> None:
    results = [
        CheckResult("buildium_api", "pass", 420, "200 OK"),
        CheckResult("composio_gmail", "fail", 180, "EXPIRED token"),
    ]
    output = format_pretty("2026-05-26T07:00:14Z", False, 2.4, results)
    assert "✗ composio_gmail" in output
    assert "EXPIRED token" in output
    assert "FAIL — 1/2 green, 1 broken: composio_gmail" in output
    assert "Exit 1." in output


def test_format_pretty_skipped_renders_circle() -> None:
    results = [
        CheckResult("buildium_api", "pass", 420, "200 OK"),
        CheckResult("langfuse", "skipped", 0, "not configured"),
    ]
    output = format_pretty("2026-05-26T07:00:14Z", True, 0.5, results)
    assert "○ langfuse" in output
    assert "skipped: not configured" in output
    # Skipped does NOT count as green, but does NOT fail overall.
    assert "PASS — 1/2 checks green" in output


# ── JSON formatter ──────────────────────────────────────────────────


def test_format_json_shape_matches_spec() -> None:
    results = [
        CheckResult("buildium_api", "pass", 420, "200 OK"),
        CheckResult("composio_gmail", "fail", 180, "EXPIRED token (status: REQUIRES_REAUTH)"),
    ]
    output = format_json("2026-05-26T07:00:14Z", False, 2.4, results)
    payload = json.loads(output)

    assert payload["timestamp"] == "2026-05-26T07:00:14Z"
    assert payload["passed"] is False
    assert payload["duration_seconds"] == 2.4
    assert len(payload["checks"]) == 2
    assert payload["failed_checks"] == ["composio_gmail"]

    first = payload["checks"][0]
    assert first == {
        "name": "buildium_api",
        "status": "pass",
        "latency_ms": 420,
        "detail": "200 OK",
    }


def test_format_json_redacts_secrets() -> None:
    results = [
        CheckResult("llm_router", "fail", 100, "auth: sk-realLookingKey12345678"),
    ]
    output = format_json("2026-05-26T07:00:14Z", False, 0.1, results)
    payload = json.loads(output)
    assert "sk-realLookingKey12345678" not in output
    assert "[REDACTED]" in payload["checks"][0]["detail"]


# ── Timeout wrapper ─────────────────────────────────────────────────


async def test_run_with_timeout_passes_through_result() -> None:
    async def healthy() -> CheckResult:
        return CheckResult("x", "pass", 50, "200 OK")

    out = await _runner._run_with_timeout("x", healthy())
    assert out.status == "pass"
    assert out.detail == "200 OK"


async def test_run_with_timeout_converts_hang_to_fail() -> None:
    """A coroutine that exceeds the timeout is failed with detail='timeout'."""

    async def hangs_forever() -> CheckResult:
        await asyncio.sleep(10)
        return CheckResult("never", "pass", 0, "never")

    # Explicit tiny budget keeps the test fast.
    out = await _runner._run_with_timeout("hangs", hangs_forever(), timeout=0.05)
    assert out.status == "fail"
    assert out.detail == "timeout"


def test_llm_router_gets_a_larger_budget_than_default() -> None:
    """The live-LLM check needs more headroom than the other checks — a
    real Gemini completion runs well past the old blanket 5s budget."""
    assert _runner._timeout_for("llm_router") > _runner._timeout_for("buildium_api")
    assert _runner._timeout_for("buildium_api") == _runner._DEFAULT_CHECK_TIMEOUT_SECONDS
    # The default must comfortably cover a cold DB connect / external HTTP.
    assert _runner._DEFAULT_CHECK_TIMEOUT_SECONDS >= 10.0


async def test_run_with_timeout_uses_per_check_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no explicit timeout, the wrapper picks the check's budget by name."""
    monkeypatch.setattr(_runner, "_CHECK_TIMEOUT_OVERRIDES", {"slowcheck": 0.05})

    async def slow() -> CheckResult:
        await asyncio.sleep(10)
        return CheckResult("slowcheck", "pass", 0, "never")

    out = await _runner._run_with_timeout("slowcheck", slow())
    assert out.status == "fail"
    assert out.detail == "timeout"


# ── Runner orchestration ────────────────────────────────────────────


def _named_fake(name: str, status: str, detail: str = "ok") -> object:
    """For checks that don't take a `name` arg (buildium / postgres / llm / langfuse)."""

    async def _fake(*_args: object, **_kwargs: object) -> CheckResult:
        return CheckResult(name, status, 100, detail)

    return _fake


def _composio_fake(status: str, detail: str = "active") -> object:
    """For ``check_composio_toolkit`` — takes name as its 2nd positional arg
    so per-toolkit checks (gmail / calendar / drive) get distinct names."""

    async def _fake(_settings: object, name: str, _toolkit: str, _user_id: str) -> CheckResult:
        return CheckResult(name, status, 100, detail)

    return _fake


def _patch_all_checks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    buildium: str = "pass",
    composio: str = "pass",
    postgres: str = "pass",
    storage_buckets: str = "pass",
    geocoder: str = "pass",
    web_search: str = "pass",
    webhook_queue: str = "pass",
    llm: str = "pass",
    model_slots: str = "pass",
    memory: str = "pass",
    judge: str = "pass",
    langfuse: str = "pass",
    twilio: str = "pass",
    stripe: str = "pass",
    billing_rows: str = "pass",
) -> None:
    monkeypatch.setattr(_checks, "check_buildium", _named_fake("buildium_api", buildium))
    monkeypatch.setattr(_checks, "check_composio_toolkit", _composio_fake(composio))
    monkeypatch.setattr(_checks, "check_postgres", _named_fake("supabase_postgres", postgres))
    monkeypatch.setattr(
        _checks, "check_storage_buckets", _named_fake("storage_buckets", storage_buckets)
    )
    monkeypatch.setattr(_checks, "check_geocoder", _named_fake("geocoder", geocoder))
    # ⚠ Not optional. Left unpatched, the REAL check runs and posts to Tavily
    # from a unit test — a billed network call inside the suite.
    monkeypatch.setattr(_checks, "check_web_search", _named_fake("web_search", web_search))
    monkeypatch.setattr(_checks, "check_webhook_queue", _named_fake("webhook_queue", webhook_queue))
    monkeypatch.setattr(_checks, "check_llm_router", _named_fake("llm_router", llm))
    monkeypatch.setattr(_checks, "check_model_slots", _named_fake("model_slots", model_slots))
    monkeypatch.setattr(_checks, "check_memory", _named_fake("memory", memory))
    monkeypatch.setattr(_checks, "check_judge", _named_fake("judge", judge))
    monkeypatch.setattr(_checks, "check_langfuse", _named_fake("langfuse", langfuse))
    monkeypatch.setattr(_checks, "check_twilio", _named_fake("twilio_sms", twilio))
    # ⚠ Not optional, for `check_web_search`'s reason one line up: left
    # unpatched the REAL check runs and GETs api.stripe.com from a unit test.
    monkeypatch.setattr(_checks, "check_stripe", _named_fake("stripe", stripe))
    # ⚠ Not optional either: unpatched it opens a real asyncpg pool to the
    # configured Supabase DB from a unit test.
    monkeypatch.setattr(_checks, "check_billing_rows", _named_fake("billing_rows", billing_rows))


async def test_runner_returns_true_when_all_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every check returns pass → run_smoke returns True, prints PASS."""
    _patch_all_checks(monkeypatch)

    result = await _runner.run_smoke(json_output=False)
    assert result is True

    captured = capsys.readouterr()
    assert "PASS" in captured.out
    assert "FAIL" not in captured.out


async def test_runner_returns_false_when_any_check_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """One fail → run_smoke returns False, prints FAIL + Exit 1."""
    _patch_all_checks(monkeypatch, buildium="fail")

    result = await _runner.run_smoke(json_output=False)
    assert result is False

    captured = capsys.readouterr()
    assert "FAIL" in captured.out
    assert "Exit 1." in captured.out
    assert "buildium_api" in captured.out


async def test_runner_skipped_does_not_fail_overall(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A skipped check (e.g. unconfigured Langfuse) doesn't flip the exit code."""
    _patch_all_checks(monkeypatch, langfuse="skipped")

    result = await _runner.run_smoke(json_output=False)
    assert result is True

    captured = capsys.readouterr()
    assert "PASS" in captured.out
    assert "skipped" in captured.out


async def test_runner_json_output_is_valid_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_all_checks(monkeypatch)

    result = await _runner.run_smoke(json_output=True)
    assert result is True

    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
    assert len(payload["checks"]) == 17
    assert payload["failed_checks"] == []
    # All expected check names are present, regardless of return order.
    names = {c["name"] for c in payload["checks"]}
    assert names == {
        "buildium_api",
        "composio_gmail",
        "composio_calendar",
        "composio_drive",
        "supabase_postgres",
        "storage_buckets",
        "geocoder",
        "web_search",
        "webhook_queue",
        "llm_router",
        "model_slots",
        "memory",
        "judge",
        "langfuse",
        "twilio_sms",
        "stripe",
        "billing_rows",
    }


# ── CLI entry-point exit codes ──────────────────────────────────────


def test_cli_exit_code_zero_on_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """``python -m helixis.smoke`` returns 0 when run_smoke returns True."""
    from helixis.smoke import __main__ as smoke_main

    async def fake_run(*, json_output: bool, out_path: str | None = None) -> bool:
        return True

    monkeypatch.setattr(smoke_main, "run_smoke", fake_run)
    assert smoke_main.main([]) == 0


def test_cli_exit_code_one_on_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit 1 when any check fails — matters for GitHub Actions."""
    from helixis.smoke import __main__ as smoke_main

    async def fake_run(*, json_output: bool, out_path: str | None = None) -> bool:
        return False

    monkeypatch.setattr(smoke_main, "run_smoke", fake_run)
    assert smoke_main.main([]) == 1


def test_cli_accepts_json_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, bool] = {}

    async def fake_run(*, json_output: bool, out_path: str | None = None) -> bool:
        captured["json_output"] = json_output
        return True

    from helixis.smoke import __main__ as smoke_main

    monkeypatch.setattr(smoke_main, "run_smoke", fake_run)
    smoke_main.main(["--json"])
    assert captured["json_output"] is True

    smoke_main.main([])
    assert captured["json_output"] is False


# ── llm_router matcher ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "reply",
    ["OK", "ok", "Ok", "Okay", "okay.", "OK!", "yes", "Sure", "hi", "Hello", "affirmative"],
)
def test_llm_ack_matches_expected_words(reply: str) -> None:
    """The accepted-ack list covers Gemini's typical rephrasings."""
    assert _is_coherent_one_word_ack(reply)


@pytest.mark.parametrize("reply", ["roger", "noted", "yep", "1"])
def test_llm_ack_accepts_other_short_one_word_replies(reply: str) -> None:
    """Any one-word reply <20 chars without whitespace is accepted —
    the goal is "LLM responded coherently", not "produced exact text"."""
    assert _is_coherent_one_word_ack(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "   ",
        "Sure, I can help with that — what would you like to know?",
        "Here is a longer multi-line\nresponse with detail.",
        "x" * 25,  # one word but too long → looks like a token, not an ack
    ],
)
def test_llm_ack_rejects_empty_or_verbose(reply: str) -> None:
    assert not _is_coherent_one_word_ack(reply)


async def test_check_llm_router_pass_on_okay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gemini replying "Okay" should PASS — the previous strict
    'OK substring' check failed this case, which motivated the rewrite.
    """
    from helixis.settings import Settings

    settings = Settings()
    settings.llm.default_model = "gemini/gemini-2.5-flash"
    settings.llm.google_api_key = "test-key"

    class FakeResponse:
        class Choice:
            class Message:
                content = "Okay"

            message = Message()

        choices = [Choice()]

    import litellm

    async def fake_acompletion(**_kwargs: object) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    result = await _checks.check_llm_router(settings)
    assert result.status == "pass"
    assert result.detail == "Okay"


async def test_check_llm_router_fail_on_empty_response(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.settings import Settings

    settings = Settings()
    settings.llm.default_model = "gemini/gemini-2.5-flash"
    settings.llm.google_api_key = "test-key"

    class FakeResponse:
        class Choice:
            class Message:
                content = ""

            message = Message()

        choices = [Choice()]

    import litellm

    async def fake_acompletion(**_kwargs: object) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    result = await _checks.check_llm_router(settings)
    assert result.status == "fail"
    assert result.detail == "LLM returned empty response"


async def test_check_llm_router_fail_on_verbose_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verbose / off-pattern reply includes a truncated snippet."""
    from helixis.settings import Settings

    settings = Settings()
    settings.llm.default_model = "gemini/gemini-2.5-flash"
    settings.llm.google_api_key = "test-key"

    long_reply = "I'm sorry, I cannot reply with only one word. " * 5

    class FakeResponse:
        class Choice:
            class Message:
                content = long_reply

            message = Message()

        choices = [Choice()]

    import litellm

    async def fake_acompletion(**_kwargs: object) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    result = await _checks.check_llm_router(settings)
    assert result.status == "fail"
    assert "did not match expected pattern" in result.detail
    # Snippet is bounded to 100 chars
    assert len(result.detail) <= len("LLM response did not match expected pattern: ''") + 100


# ── Composio diagnostics ────────────────────────────────────────────


class _FakeComposioClient:
    """Stand-in for ComposioClient used in Composio-check tests.

    Only needs ``initialize``, ``available``, and ``list_connections``
    — the check no longer calls ``is_connected``.
    """

    def __init__(self, connections: list[dict[str, object]]) -> None:
        self._connections = connections
        self.available = True

    async def initialize(self) -> None:
        return None

    def list_connections(self, _user_id: str) -> list[dict[str, object]]:
        return self._connections


def _patch_composio_client(
    monkeypatch: pytest.MonkeyPatch, connections: list[dict[str, object]]
) -> None:
    from helixis.connectors import composio_client as _composio_module

    monkeypatch.setattr(
        _composio_module,
        "ComposioClient",
        lambda _settings: _FakeComposioClient(connections),
    )


async def test_check_composio_no_connections_skips_not_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero connections → SKIPPED, not failed. The API responded, so the
    service is up; a stale smoke account with no connections is a config
    issue, not an outage, and must not red-gate the daily healthcheck."""
    from helixis.settings import Settings

    settings = Settings()
    settings.composio.api_key = "test-key"
    _patch_composio_client(monkeypatch, [])

    result = await _checks.check_composio_toolkit(settings, "composio_gmail", "GMAIL", "user-abc")
    assert result.status == "skipped"
    assert result.failed is False
    assert "user-abc" in result.detail
    assert "HELIXIS_SMOKE_COMPANY_ID" in result.detail


async def test_check_composio_all_expired_and_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Has connections, none active → detail breaks down expired vs dropped."""
    from helixis.settings import Settings

    settings = Settings()
    settings.composio.api_key = "test-key"
    _patch_composio_client(
        monkeypatch,
        [
            {"toolkit": "GMAIL", "status": "EXPIRED", "connection_id": "c1"},
            {"toolkit": "GMAIL", "status": "DROPPED", "connection_id": "c2"},
            {"toolkit": "GMAIL", "status": "DROPPED", "connection_id": "c3"},
            # An unrelated toolkit should be ignored.
            {"toolkit": "GOOGLECALENDAR", "status": "ACTIVE", "connection_id": "c4"},
        ],
    )

    result = await _checks.check_composio_toolkit(settings, "composio_gmail", "GMAIL", "user-abc")
    assert result.status == "fail"
    assert result.detail == "no active connections — 1 expired, 2 dropped"


async def test_check_composio_active_passes_with_connection_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Active connection → pass detail names the connection_id + age."""
    from helixis.settings import Settings

    settings = Settings()
    settings.composio.api_key = "test-key"
    _patch_composio_client(
        monkeypatch,
        [
            {
                "toolkit": "GMAIL",
                "status": "ACTIVE",
                "connection_id": "conn-xyz",
                "connected_at": "2026-05-25T07:00:00Z",
            }
        ],
    )

    result = await _checks.check_composio_toolkit(settings, "composio_gmail", "GMAIL", "user-abc")
    assert result.status == "pass"
    assert "conn-xyz" in result.detail
    assert "active" in result.detail


# ── Platform shim ───────────────────────────────────────────────────


def test_encoding_shim_is_noop_on_non_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows-only stdout reconfigure shim in ``__main__`` must
    not call ``sys.stdout.reconfigure`` on Linux/macOS. We replicate
    the shim's conditional inline with a mocked stdout — guards against
    a future refactor that drops the ``sys.platform == 'win32'`` check
    and accidentally reconfigures stdout everywhere.

    (Importing ``helixis.smoke.__main__`` separately confirms the shim
    itself doesn't crash at module-load time on non-Windows CI; this
    test additionally pins the behavior.)
    """
    import sys
    from unittest.mock import MagicMock

    # The module must import cleanly on the current (non-win32) platform.
    from helixis.smoke import __main__ as smoke_main  # noqa: F401

    fake_stdout = MagicMock()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    # Replicate the conditional from helixis/smoke/__main__.py verbatim.
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    fake_stdout.reconfigure.assert_not_called()


# ── Model-slot retired-slug guard (#537) ────────────────────────────


def _slot_settings(default: str, **slots: str):  # noqa: ANN201
    """Settings with a default_model + arbitrary named model slots."""
    from helixis.settings import Settings

    s = Settings()
    s.llm.default_model = default
    s.llm.google_api_key = "test-key"
    s.llm.models = {name: {"model": model} for name, model in slots.items()}
    return s


async def test_model_slots_skips_when_no_extra_slots() -> None:
    """Every slot equals default_model → nothing new to validate → skip."""
    s = _slot_settings(
        "gemini/gemini-3.1-pro-preview",
        reasoning="gemini/gemini-3.1-pro-preview",  # same as default → not probed
    )
    result = await _checks.check_model_slots(s)
    assert result.status == "skipped"
    assert "no non-default model slots" in result.detail


async def test_model_slots_skips_without_key() -> None:
    """Distinct slots but no provider key → can't validate → skip, not fail."""
    s = _slot_settings("gemini/gemini-3.1-pro-preview", browser="gemini/gemini-3.5-flash")
    s.llm.google_api_key = ""
    result = await _checks.check_model_slots(s)
    assert result.status == "skipped"
    assert "no provider API key" in result.detail


async def test_model_slots_pass_when_all_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every distinct non-default slug answers → pass, naming the slugs."""
    import litellm

    async def fake_acompletion(**_kwargs: object) -> object:
        return object()  # any non-raising return means the slug resolves

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    s = _slot_settings(
        "gemini/gemini-3.1-pro-preview",
        browser="gemini/gemini-3.5-flash",
        analysis="gemini/gemini-3.1-pro-preview",  # == default, skipped
    )
    result = await _checks.check_model_slots(s)
    assert result.status == "pass"
    # Only the distinct non-default slug is probed + reported.
    assert "gemini/gemini-3.5-flash" in result.detail
    assert "1 slot(s) resolve" in result.detail


async def test_model_slots_fail_on_retired_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 (retired preview slug) → fail, naming the dead slug — the #537
    outage-early-warning this check exists for."""
    import litellm

    async def fake_acompletion(*, model: str, **_kwargs: object) -> object:
        if model == "gemini/gemini-3.5-flash":
            raise litellm.NotFoundError("model not found", model=model, llm_provider="gemini")
        return object()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    s = _slot_settings(
        "gemini/gemini-3.1-pro-preview",
        browser="gemini/gemini-3.5-flash",  # the retired one
    )
    result = await _checks.check_model_slots(s)
    assert result.status == "fail"
    assert "unreachable" in result.detail
    assert "gemini/gemini-3.5-flash" in result.detail
    assert "NotFoundError" in result.detail


# ── Memory health check (silent-degradation guard, #537-adjacent audit) ──


def _memory_settings(*, enabled: bool = True, key: str = "test-key"):  # noqa: ANN202
    from helixis.settings import Settings

    s = Settings()
    s.memory.enabled = enabled
    s.llm.voyage_api_key = key
    s.llm.anthropic_api_key = "test-anthropic-key"
    return s


async def test_check_memory_skips_when_disabled() -> None:
    result = await _checks.check_memory(_memory_settings(enabled=False))
    assert result.status == "skipped"
    assert "disabled" in result.detail


async def test_check_memory_fails_when_enabled_without_a_key() -> None:
    """⚠ This asserted ``skipped`` until 2026-08-29, and the assertion was
    the defect written down. Memory is ``enabled: true`` in BOTH shipped
    configs, so a missing embedder key is not "nothing to probe" — it is
    memory that returns ``[]`` for every recall through the by-design swallow
    in ``manager.search``, i.e. the exact silent rot this check was built for
    (gotcha 17: a 403 on the embedder ran 19 days undetected). The daily
    smoke went green on it.

    The disabled case above still skips, so this is not a blanket
    strictening — it is the distinction between a feature switched off and a
    feature switched on and unprovisioned. Kept beside its counterpart on
    purpose: both directions, or the fix is a fresh false alarm (gotcha 38).
    """
    result = await _checks.check_memory(_memory_settings(key=""))
    assert result.status == "fail"
    assert "VOYAGE_API_KEY" in result.detail


async def test_check_memory_passes_on_healthy_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable embedder + vector search → pass."""
    from helixis.memory import manager as _mgr

    fake = MagicMock()
    fake.initialize = AsyncMock()
    fake.available = True
    fake.health_probe = AsyncMock(return_value=(True, "embedder + vector search reachable"))
    monkeypatch.setattr(_mgr, "MemoryManager", lambda _s: fake)

    result = await _checks.check_memory(_memory_settings())
    assert result.status == "pass"
    assert "reachable" in result.detail


async def test_check_memory_fails_on_embedder_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """The silent-degradation case: init succeeds, but the query-embed 403s —
    the check must FAIL (not skip), surfacing the BatchEmbedContents 403 that
    ran ~19 days undetected."""
    from helixis.memory import manager as _mgr

    fake = MagicMock()
    fake.initialize = AsyncMock()
    fake.available = True
    fake.health_probe = AsyncMock(
        return_value=(False, "PermissionDenied: 403 BatchEmbedContents blocked")
    )
    monkeypatch.setattr(_mgr, "MemoryManager", lambda _s: fake)

    result = await _checks.check_memory(_memory_settings())
    assert result.status == "fail"
    assert "403" in result.detail and "BatchEmbedContents" in result.detail


async def test_check_memory_fails_when_init_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from helixis.memory import manager as _mgr

    fake = MagicMock()
    fake.initialize = AsyncMock()
    fake.available = False
    monkeypatch.setattr(_mgr, "MemoryManager", lambda _s: fake)

    result = await _checks.check_memory(_memory_settings())
    assert result.status == "fail"
    assert "unavailable" in result.detail


async def test_memory_health_probe_surfaces_search_error() -> None:
    """MemoryManager.health_probe returns (False, err) instead of swallowing —
    the inverse of search() which returns [] on the same failure."""
    from helixis.memory.manager import MemoryManager

    mgr = MemoryManager(_memory_settings())
    mgr._initialized = True
    fake_mem0 = MagicMock()
    fake_mem0.search = AsyncMock(side_effect=RuntimeError("403 PERMISSION_DENIED"))
    fake_mem0.add = AsyncMock(return_value={"results": []})
    fake_mem0.delete_all = AsyncMock(return_value=None)
    mgr._mem0 = fake_mem0

    ok, detail = await mgr.health_probe()
    assert ok is False
    assert "403" in detail
    # and it probed a throwaway partition, never real tenant data
    assert fake_mem0.search.await_args.kwargs["filters"] == {"user_id": "__smoke_health__"}


async def test_memory_health_probe_ok_when_search_returns() -> None:
    from helixis.memory.manager import MemoryManager

    mgr = MemoryManager(_memory_settings())
    mgr._initialized = True
    fake_mem0 = MagicMock()
    fake_mem0.search = AsyncMock(return_value={"results": []})  # empty is fine — embed worked
    # The probe now also runs EXTRACTION (mem0 add) — the surface that broke
    # silently on 2026-08-23 while search kept working.
    fake_mem0.add = AsyncMock(return_value={"results": []})
    fake_mem0.delete_all = AsyncMock(return_value=None)
    mgr._mem0 = fake_mem0

    ok, detail = await mgr.health_probe()
    assert ok is True
    assert "reachable" in detail


async def test_memory_health_probe_fails_when_extraction_raises() -> None:
    """THE regression. On 2026-08-23 mem0's fact-extraction LLM raised
    ``Messages.create() got an unexpected keyword argument 'temperature'`` on
    every post-turn write. Recall kept working, so the probe — which only
    exercised query-embed and vector search — stayed green while the assistant
    silently stopped learning. Sentry caught it; the health check did not."""
    from helixis.memory.manager import MemoryManager

    mgr = MemoryManager(_memory_settings())
    mgr._initialized = True
    fake_mem0 = MagicMock()
    fake_mem0.search = AsyncMock(return_value={"results": []})
    fake_mem0.add = AsyncMock(
        side_effect=TypeError("Messages.create() got an unexpected keyword argument 'temperature'")
    )
    fake_mem0.delete_all = AsyncMock(return_value=None)
    mgr._mem0 = fake_mem0

    ok, detail = await mgr.health_probe()
    assert ok is False
    assert "extraction failed" in detail
    assert "temperature" in detail


async def test_memory_health_probe_survives_a_failed_sweep() -> None:
    """The probe row is inert text in a partition nothing reads, so a delete
    failure must degrade the detail line rather than report memory broken. A
    check that cries wolf over its own cleanup gets muted, and then it
    protects nothing."""
    from helixis.memory.manager import MemoryManager

    mgr = MemoryManager(_memory_settings())
    mgr._initialized = True
    fake_mem0 = MagicMock()
    fake_mem0.search = AsyncMock(return_value={"results": []})
    fake_mem0.add = AsyncMock(return_value={"results": []})
    fake_mem0.delete_all = AsyncMock(side_effect=RuntimeError("pool exhausted"))
    mgr._mem0 = fake_mem0

    ok, detail = await mgr.health_probe()
    assert ok is True
    assert "left behind" in detail


async def test_memory_health_probe_fails_on_foreign_model_stamp() -> None:
    """Model-drift guard: rows stamped with a model OTHER than the configured
    one fail the probe — a same-dimension embedder swap must trip the daily
    smoke instead of silently corrupting retrieval."""
    from helixis.memory.manager import MemoryManager

    mgr = MemoryManager(_memory_settings())
    mgr._initialized = True
    fake_mem0 = MagicMock()
    fake_mem0.search = AsyncMock(return_value={"results": []})
    fake_mem0.add = AsyncMock(return_value={"results": []})
    fake_mem0.delete_all = AsyncMock(return_value=None)
    mgr._mem0 = fake_mem0
    mgr._stored_embedding_models = AsyncMock(  # type: ignore[method-assign]
        return_value={"some-old-model"}
    )

    ok, detail = await mgr.health_probe()
    assert ok is False
    assert "some-old-model" in detail
    assert "re-embed" in detail.lower()


async def test_memory_health_probe_tolerates_matching_and_unstamped_rows() -> None:
    """Matching stamps pass; an empty stamp set (pre-stamp history or no
    db_url) is unknown, never foreign — no false alarms."""
    from helixis.memory.manager import MemoryManager

    mgr = MemoryManager(_memory_settings())
    mgr._initialized = True
    fake_mem0 = MagicMock()
    fake_mem0.search = AsyncMock(return_value={"results": []})
    fake_mem0.add = AsyncMock(return_value={"results": []})
    fake_mem0.delete_all = AsyncMock(return_value=None)
    mgr._mem0 = fake_mem0

    configured = mgr.settings.memory.embedding_model
    for stamps in (set(), {configured}):
        mgr._stored_embedding_models = AsyncMock(  # type: ignore[method-assign]
            return_value=stamps
        )
        ok, detail = await mgr.health_probe()
        assert ok is True, f"stamps={stamps} should pass, got: {detail}"


def test_smoke_trace_sets_top_level_timestamp() -> None:
    """Regression guard for the 1970-01-01 smoke traces (Langfuse audit).

    The trace's own ``timestamp`` MUST be a real top-level field — not only
    inside ``metadata`` — or Langfuse defaults it to the epoch and the daily
    smoke traces land dated 1970 and sort to the bottom of the timeline.
    """
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    client = MagicMock()
    _checks._emit_smoke_trace(client)

    client.trace.assert_called_once()
    kwargs = client.trace.call_args.kwargs
    assert "timestamp" in kwargs, "trace() must set a top-level timestamp, not only metadata"
    ts = kwargs["timestamp"]
    assert isinstance(ts, datetime)
    # Sanity: it's ~now (this decade), not the epoch.
    assert ts.year >= 2026
    # And it's timezone-aware UTC (Langfuse expects tz-aware).
    assert ts.tzinfo is not None
    assert ts.utcoffset() == datetime.now(UTC).utcoffset()
    client.flush.assert_called_once()


# ── Crash-resilience + post-migration fixes (observability hygiene PR) ─


async def test_run_with_timeout_converts_crash_to_fail() -> None:
    """A check that RAISES (not just hangs) is failed, never propagated —
    so one buggy check can't crash the whole run + blank the JSON output."""

    async def boom() -> CheckResult:
        raise RuntimeError("kaboom")

    out = await _runner._run_with_timeout("x", boom())
    assert out.status == "fail"
    assert "RuntimeError" in out.detail and "kaboom" in out.detail


def test_main_survives_systemexit_from_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """gotcha 7: a spaCy/mem0 sys.exit() bubbling out of the run must not kill
    the process with a bare traceback — main()'s top-level guard catches it and
    still emits parseable JSON. (It can't be caught per-check: CPython re-raises
    SystemExit out of the event loop, past the awaiter.)"""
    from helixis.smoke import __main__ as smoke_main

    def _sysexit(*_a: object, **_k: object) -> bool:
        raise SystemExit(1)

    monkeypatch.setattr(smoke_main, "run_smoke", _sysexit)
    rc = smoke_main.main(["--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False
    assert payload["checks"][0]["name"] == "smoke_runner"


def test_main_emits_valid_json_on_catastrophic_crash(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If run_smoke raises before emitting output (boot/settings crash), main()
    still prints ONE parseable failing result — not the opaque 'smoke output
    missing or malformed' the workflow reports (#686/#706/#723)."""
    from helixis.smoke import __main__ as smoke_main

    def _boom(*_a: object, **_k: object) -> bool:
        raise RuntimeError("Settings blew up at boot")

    monkeypatch.setattr(smoke_main, "run_smoke", _boom)
    rc = smoke_main.main(["--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False
    assert payload["checks"][0]["name"] == "smoke_runner"
    assert payload["checks"][0]["status"] == "fail"
    assert "Settings blew up at boot" in payload["checks"][0]["detail"]


async def test_langfuse_v3_api_mismatch_degrades_to_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """langfuse 3.x drops the v2 client.trace() API — that's a pin drift, not
    an outage, so the (optional) langfuse check SKIPs rather than red-gates."""
    import sys
    import types

    fake = types.ModuleType("langfuse")

    class _FakeLangfuse:
        def __init__(self, **_kw: object) -> None:
            pass

    fake.Langfuse = _FakeLangfuse  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langfuse", fake)

    def _v3_boom(_client: object) -> None:
        raise AttributeError("'Langfuse' object has no attribute 'trace'")

    monkeypatch.setattr(_checks, "_emit_smoke_trace", _v3_boom)

    settings = MagicMock()
    settings.langfuse.enabled = True
    settings.langfuse.public_key = "pk"
    settings.langfuse.secret_key = "sk"
    settings.langfuse.host = "https://langfuse.example"

    out = await _checks.check_langfuse(settings)
    assert out.status == "skipped"
    assert "langfuse<3" in out.detail or "3.x" in out.detail


def _judge_settings(*, key: str = "sk-ant-xxx") -> MagicMock:
    settings = MagicMock()
    settings.llm.models = {"reasoning": {"model": "anthropic/claude-sonnet-5"}}
    settings.llm.default_model = "anthropic/claude-sonnet-5"
    settings.llm.anthropic_api_key = key
    return settings


async def test_check_judge_skips_without_key() -> None:
    result = await _checks.check_judge(_judge_settings(key=""))
    assert result.status == "skipped"


async def test_check_judge_passes_when_score_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live judge that returns a score → pass, with the overall in the detail."""
    from helixis.evals import escalation_judge as _ej

    async def _fake_judge(**_kwargs: object) -> object:
        return _ej.EscalationJudgeScore(1.0, 0.9, 0.9, 0.93, "clear and honest")

    monkeypatch.setattr(_ej, "judge_escalation_reply", _fake_judge)
    result = await _checks.check_judge(_judge_settings())
    assert result.status == "pass"
    assert "0.93" in result.detail


async def test_check_judge_fails_on_silent_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The silent-death case: the judge swallows a failed call to None. The
    check must FAIL (not skip), surfacing the dead metric — this is exactly the
    temperature=0-on-claude-5 regression turned into a daily [smoke] alert."""
    from helixis.evals import escalation_judge as _ej

    async def _none(**_kwargs: object) -> object:
        return None

    monkeypatch.setattr(_ej, "judge_escalation_reply", _none)
    result = await _checks.check_judge(_judge_settings())
    assert result.status == "fail"
    assert "silently dead" in result.detail


async def test_llm_router_omits_temperature(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sonnet 5 rejects temperature=0 — the check must not send a temperature."""
    import litellm

    captured: dict[str, object] = {}

    async def _fake_acompletion(**kwargs: object) -> object:
        captured.update(kwargs)
        msg = MagicMock()
        msg.content = "ok"
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]
        return resp

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    settings = MagicMock()
    settings.llm.default_model = "anthropic/claude-sonnet-5"
    settings.llm.anthropic_api_key = "sk-ant-xxx"

    out = await _checks.check_llm_router(settings)
    assert out.status == "pass"
    assert "temperature" not in captured  # never send it (Sonnet 5 rejects 0)


async def test_json_output_stays_clean_when_a_check_prints_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dependency banner print()'d to stdout during a check must NOT pollute
    the --json output. CI runs `python -m helixis.smoke --json > smoke_output.json`;
    any stray stdout line there breaks jq → the workflow's "output missing or
    malformed" fallback. Check-time stdout is redirected to stderr."""
    _patch_all_checks(monkeypatch)

    async def _noisy(*_a: object, **_k: object) -> CheckResult:
        print("Mem0 Platform is optimized for this type of workload — get a key at app.mem0.ai")
        return CheckResult("memory", "pass", 10, "ok")

    monkeypatch.setattr(_checks, "check_memory", _noisy)

    result = await _runner.run_smoke(json_output=True)
    assert result is True

    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # stdout is ONLY parseable JSON
    assert payload["passed"] is True
    assert "Mem0 Platform" not in captured.out  # the banner went to stderr
    assert "Mem0 Platform" in captured.err


async def test_runner_out_path_survives_stdout_pollution(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    """--out writes the JSON directly to the file, so stray stdout writes
    (import-time banners — the 2026-08-18 'missing or malformed' recurrence)
    can never corrupt the machine-readable result."""
    import json as _json

    _patch_all_checks(monkeypatch)
    out = tmp_path / "smoke_output.json"

    # Simulate a library banner already sitting on stdout before the run.
    print("Mem0 Platform is optimized for production!")
    result = await _runner.run_smoke(json_output=True, out_path=str(out))
    assert result is True

    # stdout is polluted (banner + JSON) — but the FILE holds exactly the JSON.
    parsed = _json.loads(out.read_text(encoding="utf-8"))
    assert parsed["passed"] is True
    assert len(parsed["checks"]) == 17
    captured = capsys.readouterr()
    assert "Mem0 Platform" in captured.out


def test_main_crash_path_writes_out_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    """A crash before any check still leaves a parseable single-result JSON
    at --out (the workflow parses the file, not stdout)."""
    import json as _json

    from helixis.smoke import __main__ as _main

    def _boom(**_kwargs: object) -> bool:
        raise SystemExit(1)

    monkeypatch.setattr(_main, "run_smoke", lambda **kw: _boom(**kw))
    out = tmp_path / "smoke_output.json"

    rc = _main.main(["--json", "--out", str(out)])
    assert rc == 1
    parsed = _json.loads(out.read_text(encoding="utf-8"))
    assert parsed["passed"] is False
    assert parsed["failed_checks"] == ["smoke_runner"]
