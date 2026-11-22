"""The containment contract for agent-authored code — and what actually holds.

``executor.py``'s docstring and ADR 0008 §2.1 both state that the scrubbed
child environment is **the load-bearing control**: "even a full sandbox escape
finds nothing to steal, because the process was never handed a secret." The
2026-08-22 audit disproved that empirically (gotcha 33). Every attack below was
executed against the real ``run_code()`` and succeeded.

**VERIFIED ON REAL E2B, 2026-08-23.** All four properties hold, with all six
non-xfail guarantees still green in the same run (`4 failed, 6 passed`, 0
skipped — the "failures" were `[XPASS(strict)]`, which is what a flip looks
like under a strict marker; the word "xpassed" never appears).

So the four markers are now **conditional on the substrate**
(``_leaks_only_on_the_local_substrate``): no marker on e2b, where the test must
PASS outright and a regression is a plain red failure; strict xfail still on
the local subprocess, where the leak is genuinely real and where CI runs. That
is what the old self-retiring marker was for — deleting them outright would
have red-gated every CI run instead, since CI has no E2B key.

Two assertions were RETIRED (see the notes below) because they measured a
mechanism whose underlying property is now measured directly, and the network
one was rewritten: it used to assert `connect()` fails, which gave the SAME
verdict on both substrates and so distinguished nothing. That rewrite is what
made the fourth flip — egress denial — measurable at all.

⚠ **A flip is only evidence if the sandbox actually executed.** Every assertion
here reads ``not (result.ok and result.result)``, which is vacuously true when
nothing ran — a template whose interpreter port never opened produced three
free "flips" on 2026-08-23 and was briefly read as success. The module fixture
below exists to make that impossible; see its docstring for why it cannot be an
in-test assertion.

⚠ **This file only exercises whatever substrate is configured.** Running it
without `HELIXIS_CODEEXEC__SUBSTRATE=e2b` tests the LOCAL path and proves
nothing about E2B. It is also `skipif win32` at module level, so on Windows it
reports `10 skipped` — a run that touched nothing, wearing a plausible costume.
(Measured on Windows 2026-08-23. The count moves whenever an assertion is
added or retired; the SKIP LINE is the thing to read, never the number here.)
Check the skip line before believing a result.

⚠ Do NOT "fix" a failure here by weakening the assertion. The assertions are
the standard `HELIXIS_CODEEXEC__ENABLED` has to meet before it can go true.
The fix is the substrate, not the test. (Retiring an assertion is a different
act, and only legitimate when the property it proxied is measured directly
somewhere else — both retirements below name their replacement.)
"""

from __future__ import annotations

import os
import sys

import pytest

from helixis.codeexec.executor import _e2b_selected, run_code

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX substrate — prod is Linux")

_CONTAINED = "the substrate is now isolated — this must hold on e2b"

#: Which substrate is actually configured. Read from the executor's own
#: selector rather than re-derived, so the test and the code under test can
#: never disagree about which path is live.
_ON_E2B = _e2b_selected()


def _leaks_only_on_the_local_substrate(reason: str):
    """These four properties were VERIFIED to hold on real E2B (2026-08-23):
    all four flipped, with all six non-xfail guarantees still green.

    So the expectation is now conditional on the substrate rather than
    unconditional:

    * on **e2b** no marker applies — the test must PASS. A future regression
      is then a plain red failure, which is what a verified security property
      deserves.
    * on the **local subprocess** the strict xfail stays, because the leak is
      still real there (gotcha 33) and CI has no E2B key.

    This is what "delete the xfail marker" meant. Deleting them outright would
    have red-gated every CI run instead, since CI executes the local path —
    turning a verified property into a permanently broken build.
    """
    return pytest.mark.xfail(not _ON_E2B, strict=True, reason=f"local subprocess: {reason}")


def _run(code: str, *, seconds: float = 15.0):
    return run_code(code, {}, wall_clock_s=seconds, cpu_s=10)


@pytest.fixture(scope="module", autouse=True)
def _the_substrate_must_actually_execute() -> None:
    """A sandbox that never ran anything is not a sandbox that contained
    something — and without this, it reads exactly like one.

    Live-found 2026-08-23. A custom E2B template built without a start command
    boots a VM whose code-interpreter server never listens, so every execution
    returns ``502 the sandbox is running but port is not open`` and
    ``result.ok`` is False. Every containment assertion in this file is of the
    shape ``assert not (result.ok and result.result)`` — all of which are
    vacuously TRUE when nothing executed. Three tests XPASSed and were read as
    the substrate working. *An attack that never runs is an attack that never
    succeeds.*

    ⚠ Why this is a FIXTURE and not an assertion inside each test: those tests
    are ``xfail(strict=True)``, so a failure raised in the call phase is
    swallowed as a perfectly ordinary expected-failure and CI stays green —
    "substrate is broken" would be indistinguishable from "substrate is leaky".
    A fixture raising in SETUP is reported as an ERROR, which no xfail marker
    can absorb. The liveness check has to sit outside xfail semantics or it
    does not exist.
    """
    probe = _run("result = 21 * 2", seconds=20.0)
    if not (probe.ok and probe.result == 42):
        raise RuntimeError(
            "the configured substrate cannot execute code, so NOTHING in this "
            "file measures containment. Every xfail here would pass vacuously. "
            f"ok={probe.ok!r} result={probe.result!r} error={probe.error!r}\n"
            "If this is E2B: a custom template needs a start command AND a "
            "ready command, or the interpreter port never opens (502)."
        )


#: A DNS query for example.com — the smallest complete round trip there is.
#: ``socket`` is reloaded first because the runner preamble stubs it out, and
#: the whole point is that a monkeypatch is not a boundary.
_DNS_PROBE = (
    "import importlib, socket\n"
    "socket = importlib.reload(socket)\n"
    "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(6)\n"
    "q = b'\\xaa\\xbb\\x01\\x00\\x00\\x01\\x00\\x00\\x00\\x00\\x00\\x00'\n"
    "q += b'\\x07example\\x03com\\x00\\x00\\x01\\x00\\x01'\n"
    "s.sendto(q, ('1.1.1.1', 53))\n"
    "result = len(s.recv(512))\n"
)


def _host_can_reach_dns() -> bool:
    """Can the HOST do the probe at all?

    Without this the test is a false-negative generator: on a runner with no
    outbound UDP/53, the sandbox fails for an environmental reason, the
    assertion passes, and a `strict` xfail turns that into a CI failure
    announcing containment we never demonstrated. Same lesson as the
    ``.decode('replace')`` bug — confirm the probe can run before trusting
    its verdict.
    """
    import socket as _socket

    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.settimeout(6)
        q = b"\xaa\xbb\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        q += b"\x07example\x03com\x00\x00\x01\x00\x01"
        s.sendto(q, ("1.1.1.1", 53))
        return len(s.recv(512)) > 0
    except OSError:
        return False
    finally:
        try:
            s.close()
        except (OSError, UnboundLocalError):  # pragma: no cover
            pass


# ── secrets ───────────────────────────────────────────────────────────────


@_leaks_only_on_the_local_substrate("/proc/<ppid>/environ is readable same-uid")
def test_the_parent_environment_is_unreachable() -> None:
    """THE critical one. The child gets ``env={"PATH": ...}`` with no secrets,
    but the scrub only covers what is HANDED to it — the parent's environment
    block is still on disk in /proc, readable by any same-uid process, and the
    runner neutralises ``os.system``/``subprocess`` while leaving ``open()``
    untouched.

    On Render the parent is uvicorn holding ``HELIXIS_DATA_KEY`` — the single
    Fernet key wrapping every company's Buildium credentials (gotcha 11). One
    read is a cross-tenant compromise, not a single-tenant one.

    The canary has to be in the environment AT EXEC TIME to appear in /proc —
    assigning ``os.environ`` in-process does not rewrite the block. So this
    re-execs a helper holding the canary, exactly as Render execs uvicorn
    holding the real key, and asks the sandbox to go and read it.
    """
    import subprocess
    import textwrap

    src = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "src")
    helper = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {src!r})
        from helixis.codeexec.executor import run_code
        ATTACK = (
            "import os\\n"
            "blob = open(f'/proc/{{os.getppid()}}/environ','rb').read()"
            ".decode('utf-8', 'replace')\\n"
            "result = [k for k in blob.split(chr(0)) if 'HELIXIS_CANARY' in k]\\n"
        )
        r = run_code(ATTACK, {{}}, wall_clock_s=20.0)
        # DEAD is its own answer. A sandbox that never executed the attack has
        # not contained it, and collapsing that into CONTAINED is how three of
        # these XPASSed against a template whose interpreter port never opened.
        print("DEAD" if not r.ok else ("STOLE" if r.result else "CONTAINED"))
    """)

    env = dict(os.environ)
    env["HELIXIS_CANARY_DATA_KEY"] = "fernet-master-that-must-not-escape"
    proc = subprocess.run(
        [sys.executable, "-c", helper], capture_output=True, text=True, timeout=90, env=env
    )
    # A helper that failed to run at all must NOT read as "contained" — that is
    # exactly the false negative this assertion existed to avoid.
    assert proc.stdout.strip() in {"STOLE", "CONTAINED"}, (
        f"canary helper did not run, or the sandbox did not execute the attack "
        f"(DEAD): {proc.stdout!r} {proc.stderr[-400:]!r}"
    )
    assert "CONTAINED" in proc.stdout, _CONTAINED


# RETIRED 2026-08-22 — ``test_reading_proc_at_all_is_refused``.
#
# It asserted that ``open('/proc/<ppid>/environ')`` must FAIL. Against real
# E2B it succeeds, and reads 18 bytes: ``HOME=/`` and ``TERM=linux``. Every
# Linux process can read /proc; that is only a security failure when there is
# something in it to find, and the test that checks THAT —
# ``test_the_parent_environment_is_unreachable`` — now XPASSes.
#
# So this was asserting a MECHANISM as a proxy for a PROPERTY, and the
# property is directly measured one test above. Retiring a proxy once you
# hold the real measurement is not the same as weakening an assertion to go
# green; keeping it would have meant carrying a permanent xfail that says
# nothing about whether secrets are reachable.


# ── network ───────────────────────────────────────────────────────────────


@_leaks_only_on_the_local_substrate("importlib.reload restores the stubbed socket, and data flows")
def test_no_data_can_leave_the_sandbox() -> None:
    """Egress means DATA CROSSING, not ``connect()`` returning an error.

    The previous version asserted that ``connect()`` must fail — and that
    assertion could not tell the two substrates apart, which is the only job
    it had:

    ==================  ===========  =============  =======
    substrate           connect()    data flows?    verdict
    ==================  ===========  =============  =======
    local subprocess    succeeds     YES            xfail
    E2B, egress denied  succeeds     no             xfail
    ==================  ===========  =============  =======

    E2B's denial accepts the connection and then drops it, so a connect-only
    probe reports "reachable" on a substrate where nothing can actually leave.
    Measuring a real transfer separates them.

    A DNS query over UDP is the probe: it is the smallest complete round trip
    (bytes out, bytes back) with no TLS stack to break on the module reload —
    reloading ``socket`` desynchronises ``ssl``'s cached reference to it and
    an HTTPS attempt dies with an unrelated ``TypeError``, which would have
    made this test pass for entirely the wrong reason.
    """
    if not _host_can_reach_dns():
        pytest.skip("this host has no outbound DNS — the probe cannot distinguish anything")

    result = _run(_DNS_PROBE, seconds=25.0)
    leaked = bool(result.ok and result.result)
    assert not leaked, _CONTAINED


# RETIRED 2026-08-22 — ``test_process_spawning_stays_blocked``.
#
# It asserted that spawning a process must fail. On E2B it succeeds — you are
# root inside a disposable microVM. But blocking spawn was only ever a proxy
# for "cannot persist" and "cannot egress", and both are now measured
# directly: ``test_a_setsid_grandchild_cannot_outlive_the_timeout`` XPASSes
# (the VM dies and takes every descendant with it) and
# ``test_no_data_can_leave_the_sandbox`` covers the other. A process spawned
# inside a VM you own, which cannot outlive the run or reach the network, is
# not a threat worth a permanent xfail.


def _grandchild_survives(popen_args: str, *, label: str, sleep_s: int = 6) -> bool:
    """Spawn a grandchild from inside the sandbox, time the run out, and report
    whether the grandchild lived long enough to touch its marker."""
    import tempfile
    import time

    marker = os.path.join(tempfile.gettempdir(), f"helixis_containment_{label}")
    if os.path.exists(marker):
        os.remove(marker)
    result = _run(
        "import importlib, subprocess\n"
        "subprocess = importlib.reload(subprocess)\n"
        f"subprocess.Popen(['/bin/sh','-c','sleep {sleep_s}; touch {marker}'], {popen_args})\n"
        "while True: pass\n",
        seconds=2.0,
    )
    assert result.timed_out, "the run under test must actually hit its timeout"
    time.sleep(sleep_s + 3)
    survived = os.path.exists(marker)
    if survived:
        os.remove(marker)
    return survived


def test_a_spawned_grandchild_dies_with_the_run() -> None:
    """FIXED. The wall clock used to kill only the direct child, so anything a
    run spawned kept burning the single vCPU the API process shares. The
    executor now starts the child in its own session and SIGKILLs the whole
    process GROUP on timeout, which reaches every ordinary descendant."""
    assert not _grandchild_survives("", label="group")


@_leaks_only_on_the_local_substrate(
    "a process can call setsid itself; only a PID namespace contains this"
)
def test_a_setsid_grandchild_cannot_outlive_the_timeout() -> None:
    """The residue, and the honest limit of the group kill.

    A grandchild that calls ``setsid`` for itself lands in a NEW session with a
    new pgid, so signalling the sandbox's group cannot reach it — measured
    directly: sandbox ``pgid=3345``, grandchild ``pgid=3346``, and it completed
    a 20s sleep after its parent was killed.

    This is the same shape as the reload-defeats-the-monkeypatch finding: a
    control the sandboxed process can simply step outside of is not a boundary.
    Only a PID namespace (i.e. a real isolated substrate) closes it — which is
    why this stays xfail while the group kill above does not.
    """
    assert not _grandchild_survives("start_new_session=True", label="setsid")


# ── filesystem ────────────────────────────────────────────────────────────


@_leaks_only_on_the_local_substrate("the child shares the container filesystem")
def test_the_application_source_is_unreadable() -> None:
    """A run should see its scratch dir and nothing else. Today it can read the
    whole image, including the modules that handle credentials — which is how
    an attacker learns exactly which env var to go after."""
    # Derived, never hardcoded: the CI runner checks out under a different root
    # than a dev box, and a path that simply doesn't exist would read as
    # "unreadable" — an XPASS that red-gates CI for the wrong reason.
    import helixis.security.fernet as fernet_mod

    target = fernet_mod.__file__
    assert os.path.exists(target)  # the premise of the test, on any host
    result = _run(
        "import pathlib\n"
        f"p = pathlib.Path({target!r})\n"
        "result = p.exists() and len(p.read_text()) > 0\n"
    )
    assert not (result.ok and result.result is True), _CONTAINED


# ── what DOES hold today (no xfail — these are real guarantees) ───────────


def test_resource_ceilings_are_enforced() -> None:
    """RLIMIT_AS is a kernel control, not a monkeypatch, so unlike everything
    above it actually holds. A memory balloon is killed."""
    result = _run("x = bytearray(2_000_000_000)\nresult = len(x)\n")
    assert not result.ok


def test_the_wall_clock_bounds_the_run_itself() -> None:
    """The direct child is genuinely killed on timeout — the gap is only the
    grandchild case above."""
    result = _run("while True: pass\n", seconds=2.0)
    assert result.timed_out is True
    assert result.ok is False


def test_a_user_error_comes_back_structured_never_raises() -> None:
    """Contract the agent loop depends on: the model must be able to read its
    own failure and self-correct rather than the run blowing up."""
    result = _run("result = 1 / 0\n")
    assert result.ok is False
    assert result.error
    assert "ZeroDivisionError" in result.stderr


def test_artifacts_survive_the_temp_tree_teardown() -> None:
    """Files are read out BEFORE the TemporaryDirectory is deleted — the whole
    reason 'generate an owner statement' can work at all."""
    result = _run("open(OUTPUT_DIR + '/out.csv','w').write('a,b\\n1,2\\n')\nresult = 'done'\n")
    assert result.ok
    assert "out.csv" in result.artifacts
    assert result.artifact_contents["out.csv"] == b"a,b\n1,2\n"


def test_an_input_filename_cannot_escape_the_input_dir() -> None:
    """Path traversal on a caller-supplied name is reduced to a basename —
    a real guard, and one worth keeping pinned."""
    result = _run("import os\nresult = sorted(os.listdir(INPUT_DIR))\n")
    assert result.ok
    result2 = run_code(
        "import os\nresult = sorted(os.listdir(INPUT_DIR))\n",
        {},
        input_files={"../../etc/passwd": b"nope", "fine.csv": b"a,b\n"},
        wall_clock_s=15.0,
    )
    assert result2.ok
    assert result2.result == ["fine.csv", "passwd"]  # flattened, never escaped
