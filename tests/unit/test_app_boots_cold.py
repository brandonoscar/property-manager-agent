"""The app must import in a FRESH interpreter — the way uvicorn starts it.

WHAT HAPPENED (2026-08-26)
--------------------------
Every Render deploy from 2026-08-24 onward failed. Twelve merged PRs never
reached production and the app served ~three-day-old code the whole time,
because Render keeps the previous instance alive when a new one fails its
health check — so the site stayed up and nothing looked wrong.

The deploy log:

    File "/app/src/helixis/webhooks/queue.py", line 23
      from helixis.agent.planner.id_hygiene import scrub_surface_dict
    ...
    File "/app/src/helixis/detectors/inbox_bridge.py", line 38
      from helixis.webhooks.queue import enqueue, store_enrichment
    ImportError: cannot import name 'enqueue' from partially initialized
    module 'helixis.webhooks.queue' (most likely due to a circular import)

``queue`` imported the planner package, whose ``__init__`` pulls planner →
enrichment → detectors → ``inbox_bridge``, which imports ``enqueue`` back out
of ``queue`` — still half-initialized. Introduced by #892 (2026-08-24).

WHY 5,036 TESTS DID NOT CATCH IT
--------------------------------
⚠ **A warmed module graph cannot reproduce this failure.** By the time any
test imports ``helixis.main``, some earlier import has already fully
initialized ``helixis.webhooks.queue``, so the cycle resolves and everything
passes. Several modules in this repo even carry
``import helixis.agent.enrichment.models  # noqa`` at the top as an explicit
import-order workaround — the bug was *known*, written down, and read as a
test-collection quirk rather than what it was: the production entry point
being unable to start.

That is gotcha 36's rule in a new costume — a test that cannot observe the
failure is not covering it — and the fix is the same one: run the real thing
in its own process. Each test here spawns a **subprocess** with a clean
interpreter and does exactly what the container does. Nothing else can see it.

⚠ Do NOT "simplify" these into plain in-process imports. They will pass, and
they will be worthless.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

#: What each deployment target actually runs.
#:   web    — Dockerfile: uvicorn helixis.main:app
#:   worker — render.yaml: python -m helixis.webhooks.worker
ENTRY_POINTS: dict[str, str] = {
    "web (uvicorn helixis.main:app)": "helixis.main",
    "worker (python -m helixis.webhooks.worker)": "helixis.webhooks.worker",
}


def _import_in_fresh_interpreter(module: str) -> subprocess.CompletedProcess[str]:
    """Import ``module`` first thing in a clean process.

    ``-I`` isolates it: no site-packages user dir, no PYTHONPATH inherited,
    no ``sitecustomize`` — so nothing can pre-warm the graph and hide a cycle.
    """
    return subprocess.run(
        [sys.executable, "-I", "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.mark.parametrize("label,module", list(ENTRY_POINTS.items()))
def test_the_entry_point_imports_from_cold(label: str, module: str) -> None:
    """⚠ The guard that would have caught a three-day production outage on the
    commit that caused it.

    An entry point that cannot be imported cannot be started. There is no
    partial credit and no degraded mode: the container exits, the health check
    fails, and the deploy silently rolls back to whatever was already running.
    """
    proc = _import_in_fresh_interpreter(module)
    assert proc.returncode == 0, (
        f"{label} CANNOT START — `import {module}` fails in a fresh interpreter, "
        f"so the container will exit on boot and the deploy will roll back to the "
        f"previous version WITHOUT looking broken.\n\n"
        f"This is invisible to the rest of the suite: once any test has imported "
        f"part of the graph, the cycle resolves and every in-process import "
        f"succeeds.\n\n--- stderr ---\n{proc.stderr[-3000:]}"
    )


def test_import_order_does_not_decide_whether_the_app_starts() -> None:
    """⚠ The subtler half, and the reason the bug survived being SEEN.

    The cycle was known — modules carry ``import
    helixis.agent.enrichment.models  # noqa`` to work around it, and it was
    diagnosed more than once as a test-collection quirk. What nobody checked
    was whether the workaround was load-bearing in PRODUCTION.

    It was. ``import helixis.main`` alone failed while
    ``import helixis.agent.enrichment.models; import helixis.main`` succeeded,
    and only the first is what uvicorn does.

    So: importing the app FIRST must work, not merely importing it after
    something else has warmed the graph.
    """
    warmed = _import_in_fresh_interpreter(
        "helixis.agent.enrichment.models, helixis.main"  # the workaround order
    )
    cold = _import_in_fresh_interpreter("helixis.main")  # what uvicorn does

    assert cold.returncode == 0, (
        "the app imports only when something else is imported first — the "
        "import-order workaround is load-bearing, and production does not use "
        f"it.\n\n--- cold stderr ---\n{cold.stderr[-2000:]}"
    )
    # Both orders must work. If the warmed one fails too, something is broken
    # beyond ordering and the message above would be misleading.
    assert warmed.returncode == 0, (
        f"even the warmed import order fails — this is not a cycle.\n{warmed.stderr[-2000:]}"
    )


def test_the_guard_can_actually_fail() -> None:
    """⚠ Assert the probe RUNS (gotcha 33). If the subprocess machinery were
    broken — wrong interpreter, import path missing — every test above would
    report a false GREEN by failing to fail. A module that certainly does not
    exist must certainly fail."""
    proc = _import_in_fresh_interpreter("helixis.this_module_does_not_exist")
    assert proc.returncode != 0, (
        "the subprocess import probe cannot detect a missing module, so its "
        "verdict on the real entry points means nothing"
    )
    assert "ModuleNotFoundError" in proc.stderr or "ImportError" in proc.stderr
