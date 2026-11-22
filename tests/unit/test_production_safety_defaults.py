"""Production fails closed on write dedup, and a weakened default says so at boot.

Andrew's audit (2026-09-29), gotcha 180: the only thing making guarded writes fail closed
in production was ``HELIXIS_IDEMPOTENCY__REQUIRED`` in ``render.yaml``. That
file may not reach the live services (gotcha 9), and its worker block never
set the var. Without it, a dedup store outage let a Buildium write run with no
dedup record. ``config/production.yaml`` now carries the default, and both
services were measured booting with ``env=production`` (Render logs,
2026-10-02), so it is the file they load.

Code execution's sandbox egress can be opened deliberately (a test in
``test_codeexec_e2b_substrate`` keeps that path), so it doesn't refuse to boot.
It logs a warning instead, as does a production process with dedup turned off.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from helixis import main
from helixis.settings import Settings, weakened_safety_defaults
from helixis.webhooks import worker

_LIVE_CODEEXEC = {"enabled": True, "substrate": "e2b", "e2b_api_key": "k"}


@pytest.fixture(autouse=True)
def _no_ambient_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HELIXIS_IDEMPOTENCY__REQUIRED", raising=False)
    monkeypatch.delenv("HELIXIS_CODEEXEC__E2B_DENY_EGRESS", raising=False)


def test_production_requires_write_dedup_without_the_env_var() -> None:
    settings = Settings(env="production")
    assert settings.idempotency.required is True
    assert weakened_safety_defaults(settings) == []


def test_other_environments_keep_the_best_effort_default() -> None:
    settings = Settings(env="test")
    assert settings.idempotency.required is False
    assert weakened_safety_defaults(settings) == []


def test_the_env_var_still_outranks_production_yaml_and_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HELIXIS_IDEMPOTENCY__REQUIRED", "false")
    settings = Settings(env="production")
    assert settings.idempotency.required is False
    [weakened] = weakened_safety_defaults(settings)
    assert "not fail-closed" in weakened


def test_live_code_execution_with_open_egress_is_reported() -> None:
    sealed = Settings(env="test", codeexec=_LIVE_CODEEXEC)
    assert weakened_safety_defaults(sealed) == []
    opened = Settings(env="test", codeexec={**_LIVE_CODEEXEC, "e2b_deny_egress": False})
    [weakened] = weakened_safety_defaults(opened)
    assert "egress open" in weakened


def test_open_egress_on_dark_code_execution_is_not_noise() -> None:
    dark = Settings(env="test", codeexec={"e2b_deny_egress": False})
    assert weakened_safety_defaults(dark) == []


@pytest.mark.parametrize("entry", [main.lifespan, worker.run_worker], ids=["api", "worker"])
def test_both_processes_log_the_weakened_defaults_at_boot(entry: object) -> None:
    """The list is read by nobody unless each process's startup calls it."""
    tree = ast.parse(inspect.getsource(inspect.unwrap(entry)))  # type: ignore[arg-type]
    calls = {
        getattr(node.func, "id", None) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "weakened_safety_defaults" in calls
