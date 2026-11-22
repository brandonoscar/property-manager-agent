"""The Sentry helpers against the REAL installed SDK, and the pin that keeps it 2.x.

``test_sentry_setup`` drives the helpers through a fake ``sentry_sdk`` module,
so it cannot notice the SDK moving under them. sentry-sdk 3.0 removes
``push_scope`` and ``Hub``; each helper calls the SDK inside a try that only
logs at DEBUG, so on 3.0 bug-report notices and the shutdown flush would have
stopped with nothing visible. ``pyproject.toml`` said ``>=2.0.0`` and the image
installs fresh on every deploy.

The capture runs in a child process for the same reason as
``test_sentry_sends_no_secrets``: ``sentry_sdk.init`` installs process-wide
hooks that would outlive this test.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

REPO = Path(__file__).resolve().parents[2]

#: Names sentry-sdk 3.0 removes from the top-level module.
REMOVED_IN_3 = {"push_scope", "Hub", "configure_scope"}

_CHILD = textwrap.dedent(
    """
    import json
    import sentry_sdk
    from sentry_sdk.transport import Transport

    events = []

    class Capture(Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.type == "event":
                    events.append(item.payload.json)

    sentry_sdk.init(dsn="https://k@o0.ingest.sentry.io/0", transport=Capture)

    from helixis.observability import sentry_setup

    sentry_setup._active = True
    sentry_setup.capture_notice(
        "a report arrived", op="feedback.submitted", level="warning", tags={"kind": "bug"}
    )
    sentry_setup.capture_swallowed(RuntimeError("degraded"), op="memory.search")
    sentry_setup.capture_unhandled(ValueError("boom"), op="route.x", request_id="r-1")
    sentry_setup.shutdown_sentry()
    print(json.dumps([
        {"level": e.get("level"), "tags": e.get("tags", {}), "message": e.get("message")}
        for e in events
    ]))
    """
)


def _captured() -> list[dict]:
    out = subprocess.run(
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_each_helper_reaches_the_real_sdk_with_its_tags() -> None:
    events = _captured()
    by_op = {e["tags"].get("operation"): e for e in events}
    # Positive control: three captures went in, three events came out.
    assert set(by_op) == {"feedback.submitted", "memory.search", "route.x"}, events

    notice = by_op["feedback.submitted"]
    assert notice["level"] == "warning"
    assert notice["message"] == "a report arrived"
    assert notice["tags"]["notice"] == "true" and notice["tags"]["kind"] == "bug"
    assert by_op["memory.search"]["tags"]["swallowed"] == "true"
    unhandled = by_op["route.x"]["tags"]
    assert unhandled["unhandled"] == "true" and unhandled["request_id"] == "r-1"


def test_scope_tags_do_not_leak_between_captures() -> None:
    """Each helper tags a fresh scope: the notice's tag must not ride along on
    the swallowed exception captured after it."""
    by_op = {e["tags"].get("operation"): e for e in _captured()}
    assert "kind" not in by_op["memory.search"]["tags"]
    assert "notice" not in by_op["route.x"]["tags"]


def _removed_api_uses(paths: list[Path], root: Path = REPO) -> list[str]:
    found = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "sentry_sdk"
                and node.attr in REMOVED_IN_3
            ):
                found.append(f"{path.relative_to(root)}:{node.lineno} sentry_sdk.{node.attr}")
            if isinstance(node, ast.ImportFrom) and node.module == "sentry_sdk":
                for alias in node.names:
                    if alias.name in REMOVED_IN_3:
                        found.append(f"{path.relative_to(root)}:{node.lineno} import {alias.name}")
    return found


def test_no_code_calls_an_api_sentry_3_removes() -> None:
    sources = sorted((REPO / "src/helixis").rglob("*.py"))
    assert len(sources) > 100
    assert _removed_api_uses(sources) == []


def test_the_guard_finds_a_planted_use(tmp_path: Path) -> None:
    """A guard that finds nothing proves nothing: it must be able to fail."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import sentry_sdk\nfrom sentry_sdk import Hub\nwith sentry_sdk.push_scope():\n    pass\n"
    )
    assert _removed_api_uses([planted], tmp_path) == [
        "planted.py:2 import Hub",
        "planted.py:3 sentry_sdk.push_scope",
    ]


def test_pyproject_keeps_sentry_below_3() -> None:
    deps = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["dependencies"]
    (sentry,) = [Requirement(d) for d in deps if Requirement(d).name == "sentry-sdk"]
    assert Version("2.73.0") in sentry.specifier
    assert Version("3.0.0") not in sentry.specifier
    # The 3.0 betas are already on PyPI; the bound must keep them out too.
    assert not list(sentry.specifier.filter(["3.0.0b1"], prereleases=True))
