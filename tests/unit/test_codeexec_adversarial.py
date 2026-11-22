"""Adversarial battery for code-execution (Inc.5c) — the red-team pass.

Beyond the happy-path containment tests, this actively tries to BREAK the
guarantees a determined (or prompt-injected) run would attack: exfiltrate a
secret, reach the network by any vector, spawn a process, reach another
tenant's data, or slip a forbidden action past the guard. Each must fail
closed. These are the code-exec analogue of the browser 06x safety sweep.
"""

from __future__ import annotations

import sys

import pytest

from helixis.codeexec.action_guard import review_proposed_actions
from helixis.codeexec.executor import run_code

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sandbox — prod is Linux")


# ── exfiltration: no secret is reachable by any obvious vector ────────────


def test_no_secret_via_environ_or_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELIXIS_DATA_KEY", "fernet-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    res = run_code(
        "import os\n"
        "hits = [k for k in os.environ if 'HELIXIS' in k or 'ANTHROPIC' in k or 'KEY' in k]\n"
        "proc = ''\n"
        "try:\n"
        "    proc = open('/proc/self/environ','rb').read().decode('utf-8','ignore')\n"
        "except Exception:\n"
        "    pass\n"
        "result = {'env_hits': hits, 'secret_in_proc': 'fernet-secret' in proc}"
    )
    assert res.ok
    assert res.result["env_hits"] == []
    assert res.result["secret_in_proc"] is False


# ── network: blocked by every vector, including a spawned process ─────────


@pytest.mark.parametrize(
    "code",
    [
        "import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
        "import urllib.request as u; u.urlopen('http://169.254.169.254/', timeout=1)",
        "import http.client as h; h.HTTPConnection('example.com', timeout=1).connect()",
        "import socket; socket.create_connection(('example.com', 80), timeout=1)",
    ],
)
def test_network_blocked_every_vector(code: str) -> None:
    res = run_code(code + "\nresult = 'REACHED_NETWORK'")
    # Either the call raised (run not ok) or it never reached the marker.
    assert res.result != "REACHED_NETWORK"


@pytest.mark.parametrize(
    "code",
    [
        "import os; os.system('curl http://example.com')",
        "import subprocess; subprocess.Popen(['curl', 'http://example.com'])",
        "import subprocess; subprocess.run(['echo', 'hi'])",
        "import os; os.popen('id').read()",
    ],
)
def test_process_spawning_blocked(code: str) -> None:
    res = run_code(code + "\nresult = 'SPAWNED'")
    assert res.result != "SPAWNED"


def test_fork_bomb_is_bounded() -> None:
    # os.fork is neutralised; even if it weren't, the wall-clock caps it.
    res = run_code(
        "import os\n"
        "try:\n"
        "    os.fork()\n"
        "    result = 'FORKED'\n"
        "except Exception:\n"
        "    result = 'BLOCKED'",
        wall_clock_s=3.0,
    )
    assert res.result != "FORKED"


# ── the guard can't be slipped ───────────────────────────────────────────


@pytest.mark.parametrize(
    "sneaky",
    [
        "buildium_record_payment",
        " buildium_record_payment ",  # whitespace padding
        "buildium_delete_lease",  # a delete that doesn't even exist as a tool
        "buildium_create_property",  # bulk-create class
        "BUILDIUM_RECORD_PAYMENT",  # case variant (allow-list is exact)
    ],
)
def test_guard_refuses_sneaky_money_or_destructive(sneaky: str) -> None:
    review = review_proposed_actions([{"action": sneaky, "args": {"amount": 999999}}])
    assert review.allowed == []
    assert len(review.rejected) == 1
