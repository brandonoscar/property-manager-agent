"""Every connector on the shared layer declares both switches, off, on the web
service AND the worker (render.yaml).

Rentvine's own test (``test_rentvine_kill_switch_is_declared``) gives the
reasons, and they apply to each system ``pms.sync_cycle`` drives: the web
service reads ``ENABLED`` for connect and reads, the worker reads it every
sync tick, and an undeclared or one-sided value is a half-on connector no
session can see (gotchas 29/80). This reads the systems from the registry, so
a system added there fails here until render.yaml declares it.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from helixis.pms import sync_cycle

RENDER = pathlib.Path(__file__).resolve().parents[2] / "render.yaml"
_SERVICES = ("occupella-api", "helixis-webhooks-worker")


def _env(name: str) -> dict[str, str]:
    doc = yaml.safe_load(RENDER.read_text(encoding="utf-8"))
    services = {s["name"]: s for s in doc["services"]}
    assert name in services, f"{name} is not in render.yaml — re-point this test"
    return {
        e["key"]: str(e.get("value", ""))
        for e in services[name].get("envVars", [])
        if isinstance(e, dict) and "key" in e
    }


def test_the_layer_drives_at_least_the_systems_built_on_it() -> None:
    assert {"rentmanager", "propertyware", "doorloop"} <= set(sync_cycle.systems())


@pytest.mark.parametrize("key", sync_cycle.systems())
@pytest.mark.parametrize("switch", ["ENABLED", "WRITES_ENABLED"])
def test_both_services_declare_the_switch_off(key: str, switch: str) -> None:
    flag = f"HELIXIS_{key.upper()}__{switch}"
    for service in _SERVICES:
        env = _env(service)
        assert flag in env, f"{service} does not declare {flag}"
        assert env[flag] == "false", f"{service} ships {flag}={env[flag]!r}"
