"""The Rentvine kill switch must be declared, and both services must agree.

`rentvine.enabled` is a DEPLOYMENT switch — is the connector live here at all
— and it is read in six places across two processes that do not share one:

    agents/rentvine_tools.py     the confirm-gated write tool  (web)
    api/routes/rentvine.py       refuses to STORE credentials  (web)
    webhooks/rentvine_router.py  the delivery receiver         (web)
    rentvine/sync.py             `sync_company`'s own refusal  (WORKER)
    rentvine/sync_cycle.py       the nightly tick              (WORKER)
    webhooks/worker.py           gates the tick's IMPORT       (WORKER)

⚠ The routes reader is ASYMMETRIC inside its own module and that is the point:
the PUT refuses while the flag is false, the GET and the DELETE do not. A dark
deployment that stored a credential would sync nothing AND block that company's
Buildium connection through migration 0100's exclusivity trigger, so the write
is strictly worse than not connecting — while somebody who connected before the
flag went off must still be able to see and undo it.

⚠ The three worker readers are not redundant and the ORDER is the point: the
worker declines to import a dark connector at all, the cycle declines to query
for companies, and `sync_company` refuses per company for a caller that reached
it another way (`scripts/rentvine_phase8.py` names the variable rather than
flipping it, so that last refusal is what makes a forged flag impossible).

⚠ SO A HALF-ON STATE IS EXPRESSIBLE AND SILENT. Web-on with worker-off answers
chat questions from a mirror nothing is filling — which is this product's worst
failure mode (gotcha 26: a confident answer over data that is not there), and
nothing anywhere raises. Worker-on with web-off syncs a connector the product
will not surface: wasted calls against a customer's API budget with no symptom.

⚠ AND WHY IT IS DECLARED AT ALL RATHER THAN LEFT TO THE CODE DEFAULT. Gotchas
29/80: a flag's live value is not what the repo says, env vars outrank both
`settings.py` and the YAML, and no session here can read Render's environment.
An undeclared flag is one nobody can answer a question about — which is exactly
how `reasoning_loop` came to be read as dark code while it was on in production.

⚠ What this does NOT assert: that Render is actually serving these values. The
Blueprint may not be linked (gotcha 9), which is why the comments beside both
entries name the dashboard fallback. The file is the reviewable record; the
dashboard is the deployment.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
import yaml

RENDER = pathlib.Path("render.yaml")
FLAG = "HELIXIS_RENTVINE__ENABLED"


def _services() -> dict[str, dict]:
    doc = yaml.safe_load(RENDER.read_text(encoding="utf-8"))
    return {s["name"]: s for s in doc["services"]}


def _env(service: dict) -> dict[str, str]:
    return {
        e["key"]: str(e.get("value", ""))
        for e in service.get("envVars", [])
        if isinstance(e, dict) and "key" in e
    }


#: The two services that read the flag. The cron does not — it runs one
#: Buildium backfill script and never touches the Rentvine sync.
_MUST_DECLARE = ("occupella-api", "helixis-webhooks-worker")


@pytest.mark.parametrize("name", _MUST_DECLARE)
def test_both_services_declare_the_kill_switch(name: str) -> None:
    services = _services()
    assert name in services, f"{name} is not in render.yaml — re-point this test, do not delete it"
    env = _env(services[name])
    assert FLAG in env, (
        f"{name} does not declare {FLAG}. It reads the flag at runtime, so leaving it "
        f"undeclared means the only answer to 'is Rentvine live on this service?' is in "
        f"a dashboard no session can read (gotchas 29/80)."
    )


def test_the_web_and_worker_values_agree() -> None:
    """⚠ The half-on state, which is silent in both directions.

    This is the whole reason the flag is declared twice rather than once.
    """
    services = _services()
    values = {name: _env(services[name])[FLAG] for name in _MUST_DECLARE}
    assert len(set(values.values())) == 1, (
        f"the kill switch disagrees across services: {values}. Web-on/worker-off answers "
        f"chat from a mirror nothing is filling; worker-on/web-off spends a customer's API "
        f"budget on a connector the product will not surface. Neither raises anything."
    )


def test_it_ships_dark() -> None:
    """The connector ships off and a deploy turns it on.

    Not style: `rentvine/sync.py` and the write tool both gate on this, and a
    file that shipped it `true` would enable a connector no customer has
    consented to on the next Blueprint sync.
    """
    services = _services()
    for name in _MUST_DECLARE:
        assert _env(services[name])[FLAG] == "false", f"{name} ships the connector ON"


def test_the_declared_value_matches_the_code_default() -> None:
    """Both sides of the claim (gotcha 28).

    The YAML says false and `RentvineSettings.enabled` says false; if the code
    default ever moved, a deployment whose Blueprint is not linked would boot
    on the new one while this file went on describing the old.
    """
    from helixis.settings import RentvineSettings

    assert RentvineSettings().enabled is False


def test_the_flag_is_read_by_the_worker_and_the_web_process() -> None:
    """⚠ The premise the parametrize list rests on, checked rather than assumed.

    If every reader moved out of the worker, requiring it to declare the flag
    would be cargo-culted; if a THIRD process started reading it, this list
    would be missing one.

    ⚠ Re-pointed TWICE on 2026-09-22 — once when the nightly tick was wired
    into the worker (`rentvine/sync_cycle.py` + its gated import in
    `webhooks/worker.py`), and again when the connect routes landed
    (`api/routes/rentvine.py`). Both times the full-suite SET diff named this
    test alone, which is the guard doing its job rather than a regression.
    A third time on 2026-10-03: `api/routes/connectors.py` lists the Rentvine
    card on Settings → Connectors only while the connector is on (or keys are
    stored). It is a web-process route like `api/routes/rentvine.py`, so no new
    process needs to declare the flag.
    Re-point, never relax to a subset check: an exact set is what makes a
    reader appearing in a THIRD process loud, and that is the whole premise
    `_MUST_DECLARE` rests on.

    ⚠ IT WALKS THE AST RATHER THAN GREPPING, and the substring form it replaces
    had already started failing against correct code. `main.py` mounts the
    routes with a comment saying they are mounted *regardless of*
    `rentvine.enabled` — so the scan matched the sentence explaining the rule
    and reported a seventh reader that reads nothing (gotchas 40/59/91/114/150,
    the recorded trap). Every module in this list now discusses the flag in its
    own prose too, so the substring scan was one docstring away from being
    useless in the other direction as well. A comment is never an
    `ast.Attribute`; this asks for the actual attribute access.
    """
    src = pathlib.Path("src/helixis")

    def _reads_the_flag(path: pathlib.Path) -> bool:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        return any(
            isinstance(node, ast.Attribute)
            and node.attr == "enabled"
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "rentvine"
            for node in ast.walk(tree)
        )

    readers = sorted(p.relative_to(src).as_posix() for p in src.rglob("*.py") if _reads_the_flag(p))
    assert readers == [
        "agents/rentvine_tools.py",
        "api/routes/connectors.py",
        "api/routes/rentvine.py",
        "rentvine/sync.py",
        "rentvine/sync_cycle.py",
        "webhooks/rentvine_router.py",
        "webhooks/worker.py",
    ], f"the set of readers moved: {readers}. Re-check which services need the flag."
