"""Containment tests for the code-execution substrate (Increment 1).

These are the security contract, exercised in code: the sandbox must run useful
compute AND must fail closed on network, secrets, runaway time, and runaway
memory. If any of these regress, the "compute never act" guarantee is hollow.
"""

from __future__ import annotations

import sys

import pytest

from helixis.codeexec.executor import run_code

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX rlimits / preexec_fn — prod is Linux"
)


# ── it actually computes ─────────────────────────────────────────────────


def test_happy_path_returns_result() -> None:
    res = run_code("result = sum(range(11))")
    assert res.ok
    assert res.result == 55


def test_inputs_are_passed_in() -> None:
    res = run_code(
        "result = sum(x['amount'] for x in inputs['bills'])",
        {"bills": [{"amount": 100.0}, {"amount": 250.5}]},
    )
    assert res.ok
    assert res.result == 350.5


def test_artifacts_written_to_output_dir_are_reported() -> None:
    res = run_code(
        "import os\n"
        "open(os.path.join(OUTPUT_DIR, 'report.csv'), 'w').write('a,b\\n1,2\\n')\n"
        "result = 'wrote'"
    )
    assert res.ok
    assert "report.csv" in res.artifacts


# ── it fails closed on the dangerous stuff ───────────────────────────────


def test_network_is_blocked() -> None:
    res = run_code(
        "import socket\n"
        "try:\n"
        "    socket.socket()\n"
        "    result = 'NETWORK_OPEN'\n"
        "except Exception:\n"
        "    result = 'BLOCKED'"
    )
    assert res.ok
    assert res.result == "BLOCKED"


def test_secrets_are_not_inherited(monkeypatch: pytest.MonkeyPatch) -> None:
    # A real secret in the PARENT env must be invisible to the child.
    monkeypatch.setenv("HELIXIS_DATA_KEY", "super-secret-fernet-key")
    monkeypatch.setenv("HELIXIS_LLM__ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    res = run_code(
        "import os\n"
        "leaked = [k for k in os.environ if k.startswith('HELIXIS') or 'ANTHROPIC' in k]\n"
        "result = leaked"
    )
    assert res.ok
    assert res.result == []  # nothing HELIXIS_* / ANTHROPIC reached the child


def test_wall_clock_timeout_fires_and_never_raises() -> None:
    res = run_code("while True:\n    pass", wall_clock_s=1.0)
    assert not res.ok
    assert res.timed_out


def test_memory_ceiling_is_enforced() -> None:
    # Allocate well past the cap; the child must die (non-zero exit / kill),
    # surfaced as ok=False rather than taking down the parent.
    res = run_code("result = len(bytes(600 * 1024 * 1024))", memory_mb=256)
    assert not res.ok


def test_user_exception_comes_back_as_structured_error() -> None:
    res = run_code("result = 1 / 0")
    assert not res.ok
    assert res.error
    assert "ZeroDivisionError" in res.stderr


def test_non_json_result_is_stringified_not_crashed() -> None:
    res = run_code("result = object()")
    assert res.ok
    assert isinstance(res.result, dict)
    assert "_repr" in res.result


# ── files in / artifacts out (Increment 3) ───────────────────────────────


def test_input_files_are_readable_in_sandbox() -> None:
    res = run_code(
        "import os\nwith open(os.path.join(INPUT_DIR, 'data.csv')) as f:\n    result = f.read()",
        input_files={"data.csv": b"a,b\n1,2\n"},
    )
    assert res.ok
    assert res.result == "a,b\n1,2\n"


def test_artifact_content_is_returned() -> None:
    res = run_code(
        "import os\n"
        "with open(os.path.join(OUTPUT_DIR, 'report.csv'), 'wb') as f:\n"
        "    f.write(b'x,y\\n3,4\\n')\n"
        "result = 'done'"
    )
    assert res.ok
    assert res.artifacts == ["report.csv"]
    assert res.artifact_contents["report.csv"] == b"x,y\n3,4\n"


def test_input_filename_traversal_is_sanitised() -> None:
    # A traversal-y name must be reduced to its basename inside the sandbox —
    # it can't escape INPUT_DIR to write elsewhere.
    res = run_code(
        "import os\nresult = sorted(os.listdir(INPUT_DIR))",
        input_files={"../../etc/evil.txt": b"nope"},
    )
    assert res.ok
    assert res.result == ["evil.txt"]


def test_large_artifact_is_named_but_content_skipped_by_budget() -> None:
    res = run_code(
        "import os\n"
        "open(os.path.join(OUTPUT_DIR, 'big.bin'), 'wb').write(b'0' * (2 * 1024 * 1024))\n"
        "result = 'done'",
        max_artifact_bytes=1024 * 1024,  # 1 MiB budget, artifact is 2 MiB
    )
    assert res.ok
    assert "big.bin" in res.artifacts  # the caller still sees a file was made
    assert "big.bin" not in res.artifact_contents  # but its content was skipped
