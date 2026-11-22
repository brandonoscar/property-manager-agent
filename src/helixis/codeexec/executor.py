"""Locked-down execution substrate for agent-authored Python (Increment 1).

Containment that is enforceable and testable IN CODE — the same posture as the
analyst SQL executor (``analysis/executor.py``), but for general compute:

1. **No ambient authority (the load-bearing control).** The child runs with a
   SCRUBBED environment — none of the parent's ``HELIXIS_*`` / ``ANTHROPIC_*``
   / DB / Fernet secrets are inherited. Even a full sandbox escape finds
   nothing to steal, because the process was never handed a secret or a
   credentialed handle. This is why the security model does NOT rest on the
   Python layer being unescapable.
2. **Accident guards, NOT a boundary.** The runner preamble replaces
   ``socket.socket`` with a raising stub and neutralises ``os.system`` /
   ``subprocess`` / ``fork`` / ``exec`` before user code imports anything, so a
   stray ``requests`` call or an accidental spawn fails closed. ⚠ These are
   monkeypatches on module namespaces and ``importlib.reload`` defeats every
   one of them (gotcha 33, proven in ``test_codeexec_containment``). Treat them
   as protection against MISTAKES, never against intent.
3. **Isolated interpreter.** ``python -I`` ignores ``PYTHON*`` env, user
   site-packages, and the CWD on ``sys.path`` — the child can't be steered by
   inherited interpreter config.
4. **Resource ceilings (real — these are kernel controls).** ``RLIMIT_CPU``,
   ``RLIMIT_AS`` and ``RLIMIT_FSIZE`` via ``preexec_fn`` on POSIX, plus a
   wall-clock timeout. The child runs in its own session, and the timeout path
   kills the whole process GROUP, so a detached grandchild can no longer
   outlive the run that spawned it.
5. **Scratch-only filesystem.** The child runs in a fresh temp dir (its CWD)
   and writes artifacts to a provided ``OUTPUT_DIR`` inside it; the whole tree
   is deleted when the call returns.

Contract: user code runs in a namespace where ``inputs`` (a JSON-decoded dict
passed by the caller) and ``OUTPUT_DIR`` (a str path) are bound. Whatever it
assigns to ``result`` is JSON-serialized back to the caller. Failures never
raise into the agent loop — they come back as a structured ``CodeResult`` the
model can read and self-correct from.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Defaults; the caller (settings-driven) overrides. Deliberately tight — a
# reporting/compute task is short and small.
DEFAULT_WALL_CLOCK_S = 10.0
DEFAULT_CPU_S = 8
DEFAULT_MEMORY_MB = 512
DEFAULT_FSIZE_MB = 32
DEFAULT_MAX_ARTIFACT_BYTES = 10 * 1024 * 1024  # total artifact bytes read back out
_MAX_STREAM_CHARS = 20_000  # cap stdout/stderr returned to the model


# The runner executes INSIDE the child. It fails the network closed, binds the
# contract variables, runs the user code, and writes ``result`` out. Kept as a
# text preamble (not an import) so the child needs nothing from our package —
# it runs under ``-I`` with a scrubbed env and an empty sys.path entry.
_RUNNER_PREAMBLE = """\
import json as _json, sys as _sys

# ── Fail the network closed BEFORE user code can import a client ──────────
try:
    import socket as _socket

    def _blocked(*_a, **_k):
        raise PermissionError("network access is disabled in the sandbox")

    _socket.socket = _blocked  # type: ignore[assignment]
    _socket.create_connection = _blocked  # type: ignore[assignment]
    if hasattr(_socket, "create_server"):
        _socket.create_server = _blocked  # type: ignore[assignment]
except Exception:
    pass

# ── Block process spawning too: the socket stub only covers THIS process, so a
# spawned child (os.system("curl ..."), subprocess) could otherwise egress or
# fork-bomb around the caps. Neutralise the common vectors. ──────────────────
try:
    import os as _os
    import subprocess as _sp

    def _no_spawn(*_a, **_k):
        raise PermissionError("spawning processes is disabled in the sandbox")

    for _n in (
        "system", "popen", "fork", "forkpty",
        "exec", "execv", "execve", "execvp", "execvpe",
        "execl", "execle", "execlp", "execlpe",
        "spawnv", "spawnve", "spawnl", "spawnle", "posix_spawn",
    ):
        if hasattr(_os, _n):
            setattr(_os, _n, _no_spawn)
    _sp_names = (
        "Popen", "run", "call", "check_call",
        "check_output", "getoutput", "getstatusoutput",
    )
    for _n in _sp_names:
        if hasattr(_sp, _n):
            setattr(_sp, _n, _no_spawn)
except Exception:
    pass

_inputs_path, _input_dir, _output_dir, _code_path, _result_path = _sys.argv[1:6]
with open(_inputs_path) as _f:
    inputs = _json.load(_f)
INPUT_DIR = _input_dir
OUTPUT_DIR = _output_dir
with open(_code_path) as _f:
    _user_code = _f.read()

_ns = {
    "inputs": inputs,
    "INPUT_DIR": INPUT_DIR,
    "OUTPUT_DIR": OUTPUT_DIR,
    "__name__": "__sandbox__",
}
result = None
exec(compile(_user_code, "<agent_code>", "exec"), _ns)
result = _ns.get("result")

# Only JSON-serializable results cross the boundary; anything else is stringified
# so a print-style task still returns something rather than crashing the runner.
try:
    _payload = _json.dumps(result)
except (TypeError, ValueError):
    _payload = _json.dumps({"_repr": repr(result)[:5000]})
with open(_result_path, "w") as _f:
    _f.write(_payload)
"""


@dataclass
class CodeResult:
    """Outcome of one sandboxed run. ``ok`` is the only success signal."""

    ok: bool
    result: Any = None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    #: Names of files the code wrote under OUTPUT_DIR (all of them, even if
    #: their content was too large to read back — so the caller can see a file
    #: was produced).
    artifacts: list[str] = field(default_factory=list)
    #: Name → bytes for artifacts whose content was captured before the temp
    #: tree was deleted, in name order, up to ``max_artifact_bytes`` total. A
    #: name present in ``artifacts`` but absent here was skipped for size.
    artifact_contents: dict[str, bytes] = field(default_factory=dict)
    timed_out: bool = False


def _e2b_selected() -> bool:
    """Is the isolated substrate configured?

    Read at call time rather than import time: settings are env-driven and a
    module-level constant would freeze whatever the first import saw (the
    ``config/production.yaml`` class of bug, gotcha 6).

    Unreadable settings fall through to the LOCAL path — which is the unsafe
    one, so that is not a fail-closed and must not be described as one. What
    makes it safe is upstream: ``CodeExecSettings`` REFUSES to validate with
    ``enabled=True`` while ``substrate="subprocess"``, so a deployment that
    could reach this fallback with the capability live cannot boot at all.
    Here the fallback only serves unit tests, which call ``run_code`` directly
    and want the local path.
    """
    try:
        from helixis.settings import get_settings

        return (get_settings().codeexec.substrate or "").strip().lower() == "e2b"
    except Exception:  # noqa: BLE001 - settings unavailable (scripts/tests)
        return False


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """Kill the timed-out run AND anything it spawned.

    ``subprocess.run(timeout=…)`` kills only the direct child. The runner
    preamble neutralises ``os.system``/``subprocess``, but that is a
    monkeypatch on a module namespace — ``importlib.reload`` restores the real
    thing (gotcha 33) — so a run CAN detach a grandchild that survives the
    wall clock and keeps burning the single vCPU the API process shares.
    Verified: a ``Popen(..., start_new_session=True)`` from inside the sandbox
    outlived its own kill.

    The child is started in its own session, so the whole tree can be signalled
    at once.

    ⚠ The pgid comparison is not paranoia. If ``start_new_session`` did not
    take effect, ``getpgid(child)`` returns OUR group — and ``killpg`` would
    then SIGKILL the web process, the worker, or the test runner. Falling back
    to killing just the child is strictly better than that.
    """
    try:
        child_pgid = os.getpgid(proc.pid)
        own_pgid = os.getpgid(0)
    except (AttributeError, OSError):  # pragma: no cover - non-POSIX
        proc.kill()
        return

    if child_pgid == own_pgid:
        logger.warning("codeexec: child shares our process group — killing the child only")
        proc.kill()
        return

    try:
        os.killpg(child_pgid, signal.SIGKILL)
    except (AttributeError, OSError, ProcessLookupError):
        proc.kill()


def _rlimit_preexec(cpu_s: int, memory_mb: int, fsize_mb: int) -> Any:
    """Build a preexec_fn that caps the child's CPU, memory, and file size.

    POSIX-only (prod is Linux). Returns None where ``resource`` is absent so
    the wall-clock timeout remains the backstop.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - non-POSIX
        return None

    def _apply() -> None:  # pragma: no cover - runs in the child
        mem = memory_mb * 1024 * 1024
        fsize = fsize_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))

    return _apply


def run_code(
    code: str,
    inputs: dict[str, Any] | None = None,
    *,
    input_files: dict[str, bytes] | None = None,
    wall_clock_s: float = DEFAULT_WALL_CLOCK_S,
    cpu_s: int = DEFAULT_CPU_S,
    memory_mb: int = DEFAULT_MEMORY_MB,
    fsize_mb: int = DEFAULT_FSIZE_MB,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> CodeResult:
    """Run ``code`` in the locked-down substrate and return a structured result.

    Never raises for user-code errors, timeouts, or resource kills — those come
    back on ``CodeResult``. Inside the sandbox the code sees:

    * ``inputs`` — the JSON-decoded ``inputs`` dict;
    * ``INPUT_DIR`` — a str path holding any ``input_files`` (e.g. an uploaded
      invoice PDF), keyed by their (basename-sanitised) names;
    * ``OUTPUT_DIR`` — a str path to write artifacts into;

    and returns via a ``result`` variable. Files the code writes under
    ``OUTPUT_DIR`` come back on ``CodeResult.artifacts`` (names) and, up to
    ``max_artifact_bytes`` total, ``CodeResult.artifact_contents`` (bytes) —
    captured before the temp tree is deleted.

    **Substrate dispatch.** With ``codeexec.substrate == "e2b"`` this delegates
    to a Firecracker microVM (``e2b_substrate``) and the local path below is
    never taken. The signature and ``CodeResult`` are identical either way,
    which is what lets the swap happen without touching a caller or a test.
    """
    if _e2b_selected():
        from helixis.codeexec.e2b_substrate import run_code_e2b
        from helixis.settings import get_settings

        return run_code_e2b(
            code,
            inputs,
            input_files=input_files,
            wall_clock_s=wall_clock_s,
            max_artifact_bytes=max_artifact_bytes,
            settings=get_settings(),
        )

    inputs = inputs or {}
    with tempfile.TemporaryDirectory(prefix="helixis-codeexec-") as tmp:
        root = Path(tmp)
        input_dir = root / "in"
        output_dir = root / "out"
        input_dir.mkdir()
        output_dir.mkdir()
        runner_path = root / "_runner.py"
        code_path = root / "_agent_code.py"
        inputs_path = root / "_inputs.json"
        result_path = root / "_result.json"

        runner_path.write_text(_RUNNER_PREAMBLE)
        code_path.write_text(code)
        inputs_path.write_text(json.dumps(inputs))

        # Materialise caller-supplied files into INPUT_DIR. Names are reduced to
        # a basename so a malicious/clumsy name can't escape the sandbox dir.
        for raw_name, data in (input_files or {}).items():
            safe = Path(str(raw_name)).name
            if not safe or safe in {".", ".."}:
                continue
            (input_dir / safe).write_bytes(data)

        argv = [
            sys.executable,
            "-I",  # isolated: ignore PYTHON* env, user site, and CWD on sys.path
            str(runner_path),
            str(inputs_path),
            str(input_dir),
            str(output_dir),
            str(code_path),
            str(result_path),
        ]

        # Streams go to FILES, not pipes. A pipe is held open by anything the
        # run spawned, so ``communicate()`` after a kill blocks until those
        # grandchildren exit — a 2s run that spawned a 20s sleeper took 7s to
        # return, i.e. the timeout stopped bounding the call. Files have no
        # such reader/writer coupling: we kill, then read whatever landed.
        out_path = root / "_stdout.txt"
        err_path = root / "_stderr.txt"

        def _read(p: Path) -> str:
            try:
                return p.read_text(errors="replace")[:_MAX_STREAM_CHARS]
            except OSError:
                return ""

        with out_path.open("w") as out_f, err_path.open("w") as err_f:
            # Popen rather than ``run`` so the timeout path can kill the whole
            # process GROUP — see ``_kill_process_group``. ``start_new_session``
            # is what makes that group exist and stay separate from ours.
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                argv,
                cwd=str(root),
                env={"PATH": "/usr/bin:/bin"},  # SCRUBBED — no HELIXIS_*/secrets inherited
                stdout=out_f,
                stderr=err_f,
                text=True,
                preexec_fn=_rlimit_preexec(cpu_s, memory_mb, fsize_mb),
                start_new_session=True,
                close_fds=True,
            )
            try:
                proc.wait(timeout=wall_clock_s)
            except subprocess.TimeoutExpired:
                logger.info(
                    "codeexec: wall-clock timeout after %ss — killing the group", wall_clock_s
                )
                _kill_process_group(proc)
                try:
                    proc.wait(timeout=5)  # reap; SIGKILL is not refusable
                except subprocess.TimeoutExpired:  # pragma: no cover - unreachable in practice
                    logger.warning("codeexec: child survived SIGKILL")
                return CodeResult(
                    ok=False,
                    error=f"Execution exceeded the {wall_clock_s:g}s time limit.",
                    stdout=_read(out_path),
                    stderr=_read(err_path),
                    timed_out=True,
                )

        stdout = _read(out_path)
        stderr = _read(err_path)

        if proc.returncode != 0:
            # Non-zero covers user exceptions AND resource kills (SIGKILL/-9 on
            # RLIMIT_AS, SIGXCPU on RLIMIT_CPU). Report honestly; never raise.
            return CodeResult(
                ok=False,
                stdout=stdout,
                stderr=stderr,
                error=(
                    stderr.strip().splitlines()[-1]
                    if stderr.strip()
                    else f"Code exited with status {proc.returncode}."
                ),
            )

        result: Any = None
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text() or "null")
            except (ValueError, OSError):
                result = None

        artifact_files = sorted(
            (str(p.relative_to(output_dir)), p) for p in output_dir.rglob("*") if p.is_file()
        )
        artifacts = [name for name, _ in artifact_files]
        # Read content out BEFORE the temp tree is deleted, honouring the total
        # byte budget so a giant artifact can't blow up the caller's memory.
        contents: dict[str, bytes] = {}
        budget = max_artifact_bytes
        for name, path in artifact_files:
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if len(data) > budget:
                continue  # too big for the remaining budget — skip, keep the name
            contents[name] = data
            budget -= len(data)
        return CodeResult(
            ok=True,
            result=result,
            stdout=stdout,
            stderr=stderr,
            artifacts=artifacts,
            artifact_contents=contents,
        )
