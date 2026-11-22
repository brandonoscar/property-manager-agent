"""The E2B substrate — contract fidelity and the fail-closed guarantees.

These run WITHOUT an E2B key against a fake SDK, so they pin the logic we own:
deadline ownership, the never-raise contract, egress configuration, and the
type coercions the real SDK forced on us. Whether the microVM actually contains
a process is a property of E2B, measured by the live probe and pinned in
``test_codeexec_containment`` — not something a fake can tell us.

The one thing a fake CAN tell us, and the live probe can't, is that we never
ship a configuration we only *believe* is locked down. E2B's ``allow_out=[]``
imposes nothing — an empty allowlist reads as allow-everything — so the test
below asserts on the exact kwargs we send rather than on any notion of intent.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from helixis.codeexec import e2b_substrate as sub
from helixis.settings import Settings


def _settings(**over: Any) -> Settings:
    cx = {"substrate": "e2b", "e2b_api_key": "e2b_test_key", **over}
    return Settings(env="test", memory={"enabled": False}, codeexec=cx)


class _FakeFiles:
    def __init__(self, store: dict[str, Any], listing: list[Any] | None = None) -> None:
        self._store = store
        self._listing = listing or []

    def write(self, path: str, content: Any) -> None:
        self._store[path] = content

    def read(self, path: str, format: str | None = None) -> Any:  # noqa: A002
        if path not in self._store:
            raise FileNotFoundError(path)
        data = self._store[path]
        if format == "bytes":
            # The real SDK returns bytearray here, which is the whole point of
            # the coercion under test.
            return bytearray(data if isinstance(data, bytes | bytearray) else str(data).encode())
        return data

    def list(self, path: str) -> list[Any]:
        return self._listing


class _FakeSandbox:
    """Stands in for ``e2b_code_interpreter.Sandbox``."""

    created_with: dict[str, Any] = {}

    def __init__(
        self,
        *,
        result_json: str = "null",
        error: Any = None,
        hang_s: float = 0.0,
        listing: list[Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> None:
        self.sandbox_id = "sbx_fake"
        self.killed = False
        self._error = error
        self._hang_s = hang_s
        store = dict(files or {})
        store[sub._RESULT_PATH] = result_json
        self.files = _FakeFiles(store, listing)

    def run_code(self, code: str, on_stdout=None, on_stderr=None, timeout=None):  # noqa: ANN001
        self.requested_timeout = timeout
        if on_stdout:
            on_stdout(SimpleNamespace(line="hello\n"))
        if self._hang_s:
            time.sleep(self._hang_s)
        return SimpleNamespace(error=self._error)

    def kill(self) -> None:
        self.killed = True


def _install(sandbox: _FakeSandbox):
    """Patch sandbox creation, recording the kwargs we'd send to E2B."""

    def _create(settings: Any) -> Any:
        _FakeSandbox.created_with = sub._create_sandbox_kwargs(settings)
        return sandbox

    return patch.object(sub, "_create_sandbox", _create)


# ── the deadline is ours ──────────────────────────────────────────────────


def test_our_deadline_is_honoured_not_e2bs() -> None:
    """E2B's own timeout fires at 2x the requested value and raises. If we let
    it own the deadline, a 10s ceiling would return at 20s as an exception.
    So we enforce ours on the worker thread and return a clean result."""
    sandbox = _FakeSandbox(hang_s=5.0)
    started = time.monotonic()
    with _install(sandbox):
        result = sub.run_code_e2b(
            "result = 1", wall_clock_s=0.5, max_artifact_bytes=1024, settings=_settings()
        )
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert result.ok is False
    assert elapsed < 3.0, f"our deadline did not bind: {elapsed:.1f}s"


def test_partial_stdout_survives_the_timeout() -> None:
    """Strictly better than the subprocess path, which lost buffered output on
    SIGKILL. The streaming callback captured it before the kill."""
    with _install(_FakeSandbox(hang_s=5.0)):
        result = sub.run_code_e2b(
            "result = 1", wall_clock_s=0.5, max_artifact_bytes=1024, settings=_settings()
        )
    assert "hello" in result.stdout


def test_the_e2b_backstop_is_looser_than_our_deadline() -> None:
    """A backstop that could pre-empt us would reintroduce the raising path we
    just removed. Requesting 1.5x lands it at ~3x after E2B's 2x doubling."""
    sandbox = _FakeSandbox()
    with _install(sandbox):
        sub.run_code_e2b(
            "result = 1", wall_clock_s=10.0, max_artifact_bytes=1024, settings=_settings()
        )
    effective = sandbox.requested_timeout * sub._E2B_OBSERVED_TIMEOUT_FACTOR
    assert sandbox.requested_timeout > 10.0
    assert effective >= 20.0


# ── never raise ───────────────────────────────────────────────────────────


def test_a_missing_key_fails_closed_and_never_runs_locally() -> None:
    """The failure mode that would undo the whole change: selecting e2b, not
    finding a key, and quietly executing on the same-uid local path instead."""
    with patch.object(sub, "_create_sandbox", side_effect=sub.E2BUnavailableError("no key")):
        result = sub.run_code_e2b(
            "result = 1", wall_clock_s=5.0, max_artifact_bytes=1024, settings=_settings()
        )
    assert result.ok is False
    assert "not available" in (result.error or "")


def test_provider_failure_comes_back_as_a_result_not_an_exception() -> None:
    """A 429 over the 20-sandbox ceiling, or an outage, must be readable by the
    agent loop rather than propagating into it."""
    with patch.object(sub, "_create_sandbox", side_effect=RuntimeError("429 sandbox limit")):
        result = sub.run_code_e2b(
            "result = 1", wall_clock_s=5.0, max_artifact_bytes=1024, settings=_settings()
        )
    assert result.ok is False
    assert "429" in (result.error or "")


def test_a_user_exception_uses_e2bs_structured_fields() -> None:
    """Better than the subprocess path, which scraped the last stderr line."""
    err = SimpleNamespace(name="ZeroDivisionError", value="division by zero", traceback="tb...")
    with _install(_FakeSandbox(error=err)):
        result = sub.run_code_e2b(
            "result = 1/0", wall_clock_s=5.0, max_artifact_bytes=1024, settings=_settings()
        )
    assert result.ok is False
    assert result.error == "ZeroDivisionError: division by zero"
    assert "tb..." in result.stderr


# ── egress ────────────────────────────────────────────────────────────────


def test_egress_denial_never_uses_an_empty_allowlist() -> None:
    """THE trap. ``allow_out=[]`` imposes nothing in E2B's API — an empty
    allowlist reads as allow-everything, which is exactly how you ship a
    sandbox you believe is locked down and isn't. Assert on the kwargs."""
    kwargs = sub._create_sandbox_kwargs(_settings())
    assert kwargs.get("allow_internet_access") is False
    assert "allow_out" not in kwargs


def test_egress_can_be_deliberately_left_open_but_not_by_default() -> None:
    default_on = Settings(
        env="test", memory={"enabled": False}, codeexec={"substrate": "e2b", "e2b_api_key": "k"}
    )
    assert default_on.codeexec.e2b_deny_egress is True
    opened = sub._create_sandbox_kwargs(_settings(e2b_deny_egress=False))
    assert "allow_internet_access" not in opened


def test_a_custom_template_is_passed_through_only_when_set() -> None:
    assert "template" not in sub._create_sandbox_kwargs(_settings())
    assert sub._create_sandbox_kwargs(_settings(e2b_template="helixis-py"))["template"] == (
        "helixis-py"
    )


# ── the contract's type edges ─────────────────────────────────────────────


def test_artifact_bytes_are_bytes_not_bytearray() -> None:
    """``files.read(format="bytes")`` returns a bytearray. ``CodeResult`` says
    bytes, and the difference silently breaks equality assertions in the ~50
    tests that already exist."""
    listing = [SimpleNamespace(name="out.csv", size=6, type="file")]
    sandbox = _FakeSandbox(listing=listing, files={"/home/user/out/out.csv": b"a,b\n1\n"})
    with _install(sandbox):
        result = sub.run_code_e2b(
            "result = 1", wall_clock_s=5.0, max_artifact_bytes=1024, settings=_settings()
        )
    assert result.artifacts == ["out.csv"]
    assert type(result.artifact_contents["out.csv"]) is bytes


def test_an_oversized_artifact_keeps_its_name_but_not_its_bytes() -> None:
    """Same contract the subprocess path promised — the caller must still see
    that a file was produced. E2B's listing gives sizes, so we skip it without
    reading it at all."""
    listing = [SimpleNamespace(name="big.csv", size=10_000, type="file")]
    sandbox = _FakeSandbox(listing=listing, files={"/home/user/out/big.csv": b"x" * 10_000})
    with _install(sandbox):
        result = sub.run_code_e2b(
            "result = 1", wall_clock_s=5.0, max_artifact_bytes=100, settings=_settings()
        )
    assert result.artifacts == ["big.csv"]
    assert "big.csv" not in result.artifact_contents


def test_the_sandbox_is_always_destroyed() -> None:
    """Billing is per-second and the account ceiling is 20 concurrent. A leaked
    VM is both a cost and a capacity bug."""
    sandbox = _FakeSandbox()
    with _install(sandbox):
        sub.run_code_e2b(
            "result = 1", wall_clock_s=5.0, max_artifact_bytes=1024, settings=_settings()
        )
    for _ in range(50):
        if sandbox.killed:
            break
        time.sleep(0.02)
    assert sandbox.killed is True


def test_input_filenames_cannot_escape_the_input_dir() -> None:
    """Same basename flattening the subprocess path had — a traversal in a
    caller-supplied name must not place a file outside INPUT_DIR."""
    sandbox = _FakeSandbox()
    with _install(sandbox):
        sub.run_code_e2b(
            "result = 1",
            input_files={"../../etc/passwd": b"nope", "fine.csv": b"a,b\n"},
            wall_clock_s=5.0,
            max_artifact_bytes=1024,
            settings=_settings(),
        )
    written = set(sandbox.files._store)
    assert "/home/user/in/passwd" in written
    assert not any(".." in p for p in written)


# ── the boot guard ────────────────────────────────────────────────────────


def test_the_capability_cannot_go_live_on_the_local_substrate() -> None:
    """The guarantee the executor's fallback leans on. Without it, anything
    that made ``substrate`` unreadable would route agent code back onto the
    same-uid path that leaks HELIXIS_DATA_KEY — and it would look fine."""
    from helixis.settings import CodeExecSettings

    assert CodeExecSettings(enabled=True, substrate="e2b").enabled is True
    with pytest.raises(ValueError, match="SUBSTRATE=e2b"):
        CodeExecSettings(enabled=True, substrate="subprocess")
    with pytest.raises(ValueError, match="SUBSTRATE=e2b"):
        CodeExecSettings(enabled=True)
