"""E2B microVM substrate for agent-authored Python.

Replaces the local-subprocess path, whose "scrubbed env" was never a boundary:
a probe read ``HELIXIS_DATA_KEY``, the Anthropic key and the Supabase service
key straight out of ``/proc/<ppid>/environ``, because the runner blocks
``os.system`` but never ``open()`` (gotcha 33). No patch fixes that on a
same-uid child. A separate kernel does.

Live-probed 2026-08-22 (``e2b-code-interpreter 2.9.1``), and every design
choice below comes from a measurement rather than the docs:

* **Containment holds.** ``/proc/1/environ`` inside the VM is 18 bytes —
  ``HOME=/`` and ``TERM=linux``. No secret is reachable, the E2B key itself is
  not in the sandbox, and a detached ``setsid`` grandchild cannot survive
  because ``kill()`` destroys the whole microVM in ~0.1s. That closes the one
  escape the process-group kill could not (a process can leave its group; it
  cannot leave its VM). The code runs as root — irrelevant here, since root
  inside a disposable VM owns nothing we care about.
* **Latency is a non-issue.** Cold create 0.155s median; a create plus three
  executions is 0.68s median, 1.4s worst — 0.3% of the agent's 240s deadline.
* **WE own the deadline, not E2B.** Its ``timeout`` raises (violating the
  never-raise contract), fires at *exactly* 2x the requested value (3s→6.01s,
  5s→10.01s, 15s→30.00s), and destroys the sandbox; ``request_timeout``
  bounded nothing at all (30s sleep returned at 30.30s with no error). So the
  execution runs on a worker thread and ``wall_clock_s`` is enforced here.
  E2B's own timeout is set as a loose backstop only.
* **Egress is denied, and the denial is checked.** ⚠ The API's biggest trap:
  ``allow_out=[]`` imposes **nothing** — an empty allowlist reads as
  allow-everything, which is exactly how you ship a sandbox you believe is
  locked down and isn't. We therefore use ``allow_internet_access=False``
  (probed: connect is accepted then dropped, TLS dies with ``SSLEOFError``, no
  bytes move) and never an empty allowlist.

Losing artifacts on a timeout is not a regression — the subprocess path lost
them too when its temp tree was torn down. Partial **stdout** is actually
better than before: the streaming callbacks capture it before the kill.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import PurePosixPath
from typing import Any

from helixis.codeexec.executor import CodeResult

logger = logging.getLogger(__name__)

SUBSTRATE = "e2b"

#: Paths inside the microVM. Absolute so the contract does not depend on cwd.
_INPUT_DIR = "/home/user/in"
_OUTPUT_DIR = "/home/user/out"
_RESULT_PATH = "/home/user/_result.json"

_MAX_STREAM_CHARS = 20_000

#: E2B's own timeout is a BACKSTOP, not the deadline — ours (enforced on the
#: worker thread) is authoritative. E2B measurably fires at 2x whatever you
#: request, so asking for 1.5x lands the backstop at ~3x our deadline: loose
#: enough never to pre-empt us, tight enough that an orphaned VM still dies if
#: our own thread somehow doesn't get there.
_E2B_BACKSTOP_REQUEST = 1.5
_E2B_OBSERVED_TIMEOUT_FACTOR = 2.0

#: Bounds the pool that runs blocking E2B calls. Provider ceiling is 20
#: concurrent sandboxes per account (probed: the 21st is refused with a 429
#: naming the limit), so queueing here is preferable to a provider error.
_MAX_CONCURRENT_SANDBOXES = 16

_pool = ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_SANDBOXES, thread_name_prefix="codeexec-e2b")


class E2BUnavailableError(RuntimeError):
    """The substrate is selected but cannot be used. Never silently degrade to
    the local subprocess — that path is the vulnerability this module exists to
    remove."""


def _wrap_code(code: str) -> str:
    """Bind the contract variables and persist ``result``.

    Same contract the subprocess runner provided: ``inputs`` / ``INPUT_DIR`` /
    ``OUTPUT_DIR`` in scope, whatever the code assigns to ``result`` comes
    back JSON-serialized. Wrapping rather than exec-ing keeps the user's code
    at module level so its tracebacks stay readable.
    """
    return (
        "import json as _json, os as _os\n"
        f"INPUT_DIR = {_INPUT_DIR!r}\n"
        f"OUTPUT_DIR = {_OUTPUT_DIR!r}\n"
        "_os.makedirs(INPUT_DIR, exist_ok=True)\n"
        "_os.makedirs(OUTPUT_DIR, exist_ok=True)\n"
        "with open('/home/user/_inputs.json') as _f:\n"
        "    inputs = _json.load(_f)\n"
        "result = None\n"
        "\n"
        f"{code}\n"
        "\n"
        "try:\n"
        "    _payload = _json.dumps(result)\n"
        "except (TypeError, ValueError):\n"
        "    _payload = _json.dumps({'_repr': repr(result)[:5000]})\n"
        f"with open({_RESULT_PATH!r}, 'w') as _f:\n"
        "    _f.write(_payload)\n"
    )


def _create_sandbox_kwargs(settings: Any) -> dict[str, Any]:
    """Build the ``Sandbox.create`` kwargs. Pure, so the security-relevant part
    is assertable without an API key or the SDK installed.

    Splitting this out is deliberate: the egress posture is the one thing a
    test must check by INSPECTING WHAT WE SEND, not by trusting intent.
    ``allow_out=[]`` imposes nothing in E2B's API, so "we configured an
    allowlist" and "egress is restricted" are not the same statement.

    Raises ``E2BUnavailableError`` on a missing key — fails CLOSED, because
    selecting the e2b substrate and then falling back to the local subprocess
    would silently reinstate the exact hole this module replaces.
    """
    cx = settings.codeexec
    key = (cx.e2b_api_key or "").strip()
    if not key:
        raise E2BUnavailableError(
            "codeexec substrate is 'e2b' but HELIXIS_CODEEXEC__E2B_API_KEY is empty"
        )
    kwargs: dict[str, Any] = {"api_key": key}
    if (cx.e2b_template or "").strip():
        kwargs["template"] = cx.e2b_template.strip()
    if cx.e2b_deny_egress:
        # NOT an empty allow_out — that imposes nothing (see module docstring).
        kwargs["allow_internet_access"] = False
    return kwargs


def _create_sandbox(settings: Any) -> Any:
    """Create a locked-down sandbox, or raise ``E2BUnavailableError``."""
    kwargs = _create_sandbox_kwargs(settings)
    try:
        from e2b_code_interpreter import Sandbox
    except ImportError as exc:  # pragma: no cover - the extra isn't installed
        raise E2BUnavailableError("the e2b-code-interpreter package is not installed") from exc
    return Sandbox.create(**kwargs)


def _kill_quietly(sandbox: Any) -> None:
    """Destroy the VM without blocking the caller.

    On the timeout path the answer is already decided, and every second spent
    tearing down is a second the user waits for a result they will not get.
    Billing is per-second, so this must still actually happen — hence a real
    thread rather than a fire-and-forget coroutine.
    """

    def _run() -> None:
        try:
            sandbox.kill()
        except Exception:  # noqa: BLE001 - teardown is best-effort
            logger.warning("codeexec: sandbox teardown failed", exc_info=True)

    threading.Thread(target=_run, name="codeexec-e2b-kill", daemon=False).start()


def _collect_artifacts(sandbox: Any, max_artifact_bytes: int) -> tuple[list[str], dict[str, bytes]]:
    """Read OUTPUT_DIR back out, honouring the byte budget.

    ``files.list`` reports sizes, so the budget is applied BEFORE reading —
    the subprocess path had to read a file to discover it was too big.
    ``files.read(format="bytes")`` returns a ``bytearray``; the contract says
    ``bytes``, and the difference silently breaks equality assertions.
    """
    names: list[str] = []
    contents: dict[str, bytes] = {}
    try:
        entries = sandbox.files.list(_OUTPUT_DIR)
    except Exception:  # noqa: BLE001 - no OUTPUT_DIR is a normal, empty result
        return names, contents

    budget = max_artifact_bytes
    for entry in sorted(entries, key=lambda e: str(getattr(e, "name", e))):
        name = str(getattr(entry, "name", entry))
        if getattr(entry, "type", None) == "dir":
            continue
        names.append(name)
        size = getattr(entry, "size", None)
        if isinstance(size, int) and size > budget:
            continue  # too big for the remaining budget — keep the name
        try:
            raw = sandbox.files.read(str(PurePosixPath(_OUTPUT_DIR) / name), format="bytes")
        except Exception:  # noqa: BLE001
            continue
        data = bytes(raw)
        if len(data) > budget:
            continue
        contents[name] = data
        budget -= len(data)
    return names, contents


def _execute(sandbox: Any, code: str, backstop_s: float, streams: dict[str, list[str]]) -> Any:
    """Run the wrapped code, streaming output so a kill can't erase it."""
    return sandbox.run_code(
        code,
        on_stdout=lambda m: streams["out"].append(str(getattr(m, "line", m))),
        on_stderr=lambda m: streams["err"].append(str(getattr(m, "line", m))),
        timeout=backstop_s,
    )


def run_code_e2b(
    code: str,
    inputs: dict[str, Any] | None = None,
    *,
    input_files: dict[str, bytes] | None = None,
    wall_clock_s: float,
    max_artifact_bytes: int,
    settings: Any,
) -> CodeResult:
    """Execute ``code`` in a fresh microVM and return the same ``CodeResult``.

    Never raises for user-code errors or timeouts — those come back on the
    result, exactly as the subprocess path promised. An infrastructure failure
    (no key, provider down) also comes back as ``ok=False`` rather than
    propagating: the agent loop must be able to read and report it.
    """
    inputs = inputs or {}
    streams: dict[str, list[str]] = {"out": [], "err": []}

    def _streamed(key: str) -> str:
        return "".join(streams[key])[:_MAX_STREAM_CHARS]

    try:
        sandbox = _create_sandbox(settings)
    except E2BUnavailableError as exc:
        logger.error("codeexec: %s", exc)
        return CodeResult(ok=False, error=f"The code sandbox is not available: {exc}")
    except Exception as exc:  # noqa: BLE001 - provider outage / 429 over the cap
        logger.warning("codeexec: sandbox creation failed", exc_info=True)
        return CodeResult(ok=False, error=f"Could not start the code sandbox: {exc}")

    sandbox_id = getattr(sandbox, "sandbox_id", None)
    try:
        for raw_name, data in (input_files or {}).items():
            safe = PurePosixPath(str(raw_name)).name
            if not safe or safe in {".", ".."}:
                continue
            sandbox.files.write(str(PurePosixPath(_INPUT_DIR) / safe), data)
        sandbox.files.write("/home/user/_inputs.json", json.dumps(inputs))

        future = _pool.submit(
            _execute, sandbox, _wrap_code(code), wall_clock_s * _E2B_BACKSTOP_REQUEST, streams
        )
        try:
            execution = future.result(timeout=wall_clock_s)
        except FutureTimeout:
            # OUR deadline, honoured. Artifacts are unreachable (the VM is
            # about to go), but the streamed output survives.
            _kill_quietly(sandbox)
            return CodeResult(
                ok=False,
                error=f"Execution exceeded the {wall_clock_s:g}s time limit.",
                stdout=_streamed("out"),
                stderr=_streamed("err"),
                timed_out=True,
            )
        except Exception as exc:  # noqa: BLE001 - provider error mid-execution
            logger.warning("codeexec: execution failed", exc_info=True)
            return CodeResult(
                ok=False,
                error=f"The code sandbox failed: {exc}",
                stdout=_streamed("out"),
                stderr=_streamed("err"),
            )

        err = getattr(execution, "error", None)
        if err is not None:
            # Structured fields beat scraping the last stderr line, which is
            # what the subprocess path had to do.
            name = getattr(err, "name", "") or "Error"
            value = getattr(err, "value", "") or ""
            return CodeResult(
                ok=False,
                error=f"{name}: {value}".strip(": "),
                stdout=_streamed("out"),
                stderr=(getattr(err, "traceback", "") or _streamed("err"))[:_MAX_STREAM_CHARS],
            )

        result: Any = None
        try:
            raw = sandbox.files.read(_RESULT_PATH)
            result = json.loads(raw if isinstance(raw, str) else bytes(raw).decode())
        except Exception:  # noqa: BLE001 - code that never set `result`
            result = None

        names, contents = _collect_artifacts(sandbox, max_artifact_bytes)
        return CodeResult(
            ok=True,
            result=result,
            stdout=_streamed("out"),
            stderr=_streamed("err"),
            artifacts=names,
            artifact_contents=contents,
        )
    finally:
        if sandbox_id:
            logger.info("codeexec: sandbox %s done", sandbox_id)
        _kill_quietly(sandbox)


__all__ = ["SUBSTRATE", "E2BUnavailableError", "run_code_e2b"]
