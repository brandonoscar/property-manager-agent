"""A deployment that cannot reach Composio must not blame the customer's setup.

Each connector specialist boots into one of four states, and three of them are
facts about the CUSTOMER (tools present / grant lapsed / never connected) while
the fourth is a fact about the DEPLOYMENT (Composio is not configured here at
all). The fourth used to collapse into "never connected", so a customer whose
Gmail was healthy read *"Gmail isn't connected yet. Open Settings → Connectors
→ Gmail and click Connect"* — followed it, and got a 503.

The load-bearing tests here are the two that a one-sided version passes
(gotchas 38/51): the unavailable notice must NOT appear when Composio is fine
and the customer simply has not connected, and the call-site guard must see the
orchestrator actually pass the flag — a behavioural test of the factory cannot
observe a caller that ignores it (gotchas 62/66/69/103).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from helixis.agents.calendar_agent import create_calendar_agent
from helixis.agents.connector_notices import (
    CONNECTOR_PRODUCTS,
    integration_unavailable_notice,
)
from helixis.agents.drive_agent import create_drive_agent
from helixis.agents.gmail_agent import create_gmail_agent
from helixis.agents.outlook_agent import create_outlook_agent
from helixis.settings import Settings

_AGENTS = Path(__file__).resolve().parents[2] / "src" / "helixis" / "agents"
_ORCHESTRATOR = _AGENTS / "orchestrator.py"

#: (factory, module stem, customer-facing product name). The four Composio
#: specialists; Buildium is a manual api_key connector and has no Composio
#: toolkit, so it is correctly absent.
_SPECIALISTS = [
    (create_gmail_agent, "gmail_agent", "Gmail"),
    (create_outlook_agent, "outlook_agent", "Outlook"),
    (create_calendar_agent, "calendar_agent", "Google Calendar"),
    (create_drive_agent, "drive_agent", "Google Drive"),
]

_UNAVAILABLE = "INTEGRATION UNAVAILABLE"
_NEVER_CONNECTED = "NOT CONNECTED"
_EXPIRED = "CONNECTION EXPIRED"


def _settings() -> Settings:
    return Settings()


def _state(instructions: str) -> str:
    """Which of the three zero-tool notices the prompt actually carries."""
    present = [h for h in (_UNAVAILABLE, _EXPIRED, _NEVER_CONNECTED) if h in instructions]
    # ``NOT CONNECTED`` is a substring of nothing else here, but assert the
    # partition rather than trusting it — two notices in one prompt would let
    # every assertion below pass while the model read contradictory orders.
    assert len(present) == 1, f"expected exactly one zero-tool notice, got {present}"
    return present[0]


# ── the four states, per specialist ─────────────────────────────


@pytest.mark.parametrize(("factory", "_mod", "product"), _SPECIALISTS)
def test_an_unreachable_integration_says_so(factory, _mod, product: str) -> None:
    agent = factory(_settings(), integration_unavailable=True)
    assert _state(agent.instructions) == _UNAVAILABLE
    assert product in agent.instructions


@pytest.mark.parametrize(("factory", "_mod", "_product"), _SPECIALISTS)
def test_a_working_deployment_still_says_not_connected(factory, _mod, _product: str) -> None:
    """The direction a one-sided test misses.

    With Composio healthy and the customer simply not connected, the honest
    message IS "connect it" — a version that always claimed the integration was
    down would pass every other test in this file and tell every new customer
    their setup was unfixable.
    """
    agent = factory(_settings())
    assert _state(agent.instructions) == _NEVER_CONNECTED


@pytest.mark.parametrize(("factory", "_mod", "_product"), _SPECIALISTS)
def test_a_lapsed_grant_still_says_reconnect(factory, _mod, _product: str) -> None:
    agent = factory(_settings(), connection_expired=True)
    assert _state(agent.instructions) == _EXPIRED


@pytest.mark.parametrize(("factory", "_mod", "_product"), _SPECIALISTS)
def test_unavailable_outranks_expired(factory, _mod, _product: str) -> None:
    """Precedence, and it is not arbitrary.

    When Composio is unconfigured the degraded-toolkit probe never runs, so
    ``connection_expired`` cannot be trusted either way — and "reconnect" is
    the one instruction guaranteed not to help in this state.
    """
    agent = factory(_settings(), connection_expired=True, integration_unavailable=True)
    assert _state(agent.instructions) == _UNAVAILABLE


# ── what the notice must and must not say ───────────────────────


@pytest.mark.parametrize("product", CONNECTOR_PRODUCTS)
def test_the_notice_gives_no_instruction_that_cannot_work(product: str) -> None:
    """An instruction the reader can follow and that changes nothing is worse
    than none — they do it, nothing improves, and the product reads as broken
    (gotchas 102/106). The notice may NAME those acts in order to forbid them;
    what it must not do is direct the user to perform one."""
    notice = integration_unavailable_notice(product)
    lowered = notice.lower()
    # The customer-facing sentence is the quoted reply shape at the end.
    reply = notice.split("(one short paragraph, no list, no questions):", 1)[1]
    # ⚠ Anchored on the WORDS, never on a glyph. The first cut of this guard
    # forbade "settings →" and a mutation writing "Settings -> Connectors"
    # walked straight past it — gotcha 40's shape, inside the guard written for
    # this defect. "connection" is deliberately NOT forbidden: the reply says
    # "nothing wrong with your connection", which is the whole point.
    for forbidden in ("settings", "connectors", "reconnect", "click connect"):
        assert forbidden not in reply.lower(), (
            f"the reply the user reads tells them to {forbidden!r}, which cannot "
            "change a deployment-side configuration gap"
        )
    # And it must positively say whose problem it is — the whole point.
    assert "our side" in lowered
    assert "administrator" in lowered


def test_the_notice_names_the_product_it_is_for() -> None:
    """A shared builder that ignored its argument would hand every specialist
    the same prompt, and a Drive question would be answered about Gmail."""
    rendered = {p: integration_unavailable_notice(p) for p in CONNECTOR_PRODUCTS}
    assert len(set(rendered.values())) == len(CONNECTOR_PRODUCTS)
    for product, text in rendered.items():
        assert product in text


# ── wiring: the orchestrator must actually pass it ──────────────


def _create_orchestrator_tree() -> ast.AST:
    return ast.parse(_ORCHESTRATOR.read_text(encoding="utf-8"))


def test_the_orchestrator_passes_the_flag_to_every_connector_specialist() -> None:
    """Gotchas 62/66/69/103, fifth instance: a factory can be perfect and a
    caller can quietly never hand it the argument."""
    tree = _create_orchestrator_tree()
    wanted = {f"create_{mod}" for _f, mod, _p in _SPECIALISTS}
    seen: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name in wanted:
            seen[name] = {kw.arg for kw in node.keywords if kw.arg}
    assert set(seen) == wanted, f"call sites not found: {sorted(wanted - set(seen))}"
    for name, kwargs in sorted(seen.items()):
        assert "integration_unavailable" in kwargs, (
            f"{name} is constructed without integration_unavailable — that "
            "specialist will tell a customer their own connection is missing"
        )


def test_the_flag_is_derived_from_the_composio_client_not_hardcoded() -> None:
    """A literal ``False`` would pass the wiring test above and restore the
    defect exactly; a literal ``True`` would break every working deployment."""
    src = _ORCHESTRATOR.read_text(encoding="utf-8")
    tree = ast.parse(src)
    assigns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "integration_unavailable" for t in node.targets)
    ]
    assert len(assigns) == 1, f"expected one definition, found {len(assigns)}"
    value = ast.unparse(assigns[0].value)
    assert "composio_client" in value, (
        f"integration_unavailable is computed as {value!r}, which does not read "
        "the Composio client — the flag would be a constant"
    )


# ── corpus: a fifth connector cannot be silently exempt ─────────


def test_every_connector_specialist_takes_the_flag() -> None:
    """Reads the MODULES, not the four this file happens to import.

    The failure mode is gotcha 45's "silently permits more": a fifth Composio
    specialist added with the usual three-way split inherits the defect and no
    test here would name it. ``NOT_CONNECTED_NOTICE`` is the marker — a module
    that has one is telling a customer about their own connection state.
    """
    found: list[str] = []
    for path in sorted(_AGENTS.glob("*_agent.py")):
        src = path.read_text(encoding="utf-8")
        if "NOT_CONNECTED_NOTICE" not in src:
            continue
        found.append(path.stem)
        tree = ast.parse(src)
        factories = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == f"create_{path.stem}"
        ]
        assert len(factories) == 1, f"{path.stem}: expected one factory"
        args = {a.arg for a in factories[0].args.args + factories[0].args.kwonlyargs}
        assert "integration_unavailable" in args, (
            f"{path.stem} splits on the customer's connection state but cannot "
            "be told the integration itself is unreachable here"
        )
    # A scan that finds zero is a claim to check, not a result (gotchas
    # 49/100/101) — the known four must be present.
    assert set(found) == {mod for _f, mod, _p in _SPECIALISTS}, f"scan found {found}"


def test_the_products_list_matches_what_the_specialists_ask_for() -> None:
    """``CONNECTOR_PRODUCTS`` drives the notice-wording tests above, so a
    product name that drifts from the one a factory passes would leave that
    specialist's real prompt untested."""
    passed: set[str] = set()
    for _factory, mod, _product in _SPECIALISTS:
        tree = ast.parse((_AGENTS / f"{mod}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "integration_unavailable_notice"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                passed.add(node.args[0].value)
    assert passed == set(CONNECTOR_PRODUCTS), (
        f"specialists pass {sorted(passed)}; CONNECTOR_PRODUCTS is {sorted(CONNECTOR_PRODUCTS)}"
    )


def test_the_factory_signature_is_readable_here() -> None:
    """Guards the guard: every assertion above rests on being able to read the
    keyword off a real signature, and a stubbed factory would make them all
    vacuous (gotcha 33 — assert the probe actually ran)."""
    for factory, mod, _product in _SPECIALISTS:
        params = inspect.signature(factory).parameters
        assert "integration_unavailable" in params, mod
        assert params["integration_unavailable"].default is False, (
            f"{mod}: the flag must default False — a deployment that forgets to "
            "pass it should behave exactly as it does today"
        )
