"""The CORS regex admits every Vercel preview URL our own deploys produce.

⚠ **It refused BRANCH previews for as long as it has existed, and its own
comment said the opposite.** The pattern was ``[a-z0-9]+`` before the team
segment, which matches a per-commit slug (``helixis-onboarding-abc123-…``) and
not a per-BRANCH one (``helixis-onboarding-git-claude-sen-ebeb4f-…``), because
a branch slug contains hyphens. Branch previews are the only way to look at a
wizard change before it reaches main, so every one of them failed with a bare
``Failed to fetch`` in the browser — no server log, nothing naming CORS
(founder report, 2026-09-09).

⚠ **A test that feeds this regex hand-written strings is the wrong test.**
That is gotcha 45's shape: the pattern was self-consistent and correct on its
own terms, and wrong about the URLs Vercel actually mints. So the fixtures
below are the REAL shapes — including the exact URL that was refused — and the
file reads the pattern out of ``config/production.yaml`` rather than restating
it, because a copy here would agree with itself forever while the deployed
value drifted.

⚠ **Both directions.** A pattern that admits everything passes a
"preview URLs work" test just as happily, and would let any Vercel subdomain
on earth call this API with a browser's credentials.

⚠ **TWO FILES SET THIS ONE VALUE AND THEY DISAGREED.** `render.yaml` declares
``HELIXIS_API__CORS_ORIGIN_REGEX`` as an env var, and an env var SHADOWS the
YAML leaf (``_drop_env_shadowed``, gotcha 6) — so whichever is live depends on
whether the var is actually set on the service, which the Render API does not
expose (gotcha 80). Its value was
``(-[a-z0-9-]+)?`` — the hyphen present (so it already admitted the branch
preview) but the ``-your-team`` team segment MISSING, which is the
entire security boundary: Vercel subdomains are globally unique and anyone can
create a project, so ``your-app-evil.vercel.app`` was allowed to call
this API from a browser carrying the user's credentials. The last test here
reads BOTH files and fails if they drift (gotcha 28's both-sides rule applied
to a deployment setting).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / "config" / "production.yaml"
_RENDER = _ROOT / "render.yaml"

_ENV_KEY = "HELIXIS_API__CORS_ORIGIN_REGEX"


def _pattern() -> re.Pattern[str]:
    return re.compile(_yaml_expr())


def _yaml_expr() -> str:
    raw = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    expr = raw["api"]["cors_origin_regex"]
    assert expr, "production.yaml has no cors_origin_regex — the guard is measuring nothing"
    return expr


def _render_exprs() -> list[str]:
    """Every value render.yaml gives the CORS regex env var, across all services."""
    raw = yaml.safe_load(_RENDER.read_text(encoding="utf-8"))
    return [
        env["value"]
        for service in raw["services"]
        for env in service.get("envVars") or []
        if env.get("key") == _ENV_KEY and "value" in env
    ]


# Real shapes, not invented ones.
ALLOWED = [
    # Production.
    "https://your-app.vercel.app",
    "https://your-onboarding.vercel.app",
    # Per-commit preview — the shape that always worked.
    "https://your-onboarding-9f3a2c1-your-team.vercel.app",
    # ⚠ THE ONE THAT WAS REFUSED. Verbatim from the founder's report.
    "https://your-onboarding-git-claude-sen-ebeb4f-your-team.vercel.app",
    "https://your-app-git-main-your-team.vercel.app",
]

REFUSED = [
    # A different team's Vercel project. The team segment is the whole
    # security boundary — widening the slug class must not reach past it.
    "https://your-onboarding-git-evil-someoneelses-projects.vercel.app",
    "https://helixis-onboarding.evil.com",
    # ⚠ A subdomain UNDER an allowed host. The trailing `$` is what stops
    # this, and it is the reason the anchor is not decorative.
    "https://your-onboarding.vercel.app.evil.com",
    # A project this deployment does not own.
    "https://someone-else-your-team.vercel.app",
    # http, not https.
    "http://your-onboarding.vercel.app",
]


@pytest.mark.parametrize("origin", ALLOWED)
def test_our_own_preview_deploys_are_allowed(origin: str) -> None:
    assert _pattern().match(origin), (
        f"{origin} is refused — a browser calling the API from it gets a bare "
        "'Failed to fetch' with nothing naming CORS"
    )


@pytest.mark.parametrize("origin", REFUSED)
def test_everything_else_is_refused(origin: str) -> None:
    assert not _pattern().match(origin), f"{origin} should not be able to call this API"


def test_the_hyphen_is_what_admits_a_branch_preview() -> None:
    """The specific character, pinned so a 'tidy-up' cannot remove it.

    ⚠ Written as a MUTATION of the live pattern rather than as a second copy
    of it: narrowing the slug class back to ``[a-z0-9]`` must break exactly the
    branch-preview case and nothing else. A test asserting the string contains
    ``[a-z0-9-]`` would pass against a pattern that had been rewritten to some
    other broken shape carrying that substring.
    """
    live = _pattern().pattern
    narrowed = re.compile(live.replace("[a-z0-9-]", "[a-z0-9]"))
    branch = "https://your-onboarding-git-claude-sen-ebeb4f-your-team.vercel.app"
    commit = "https://your-onboarding-9f3a2c1-your-team.vercel.app"

    assert narrowed.pattern != live, "the hyphen is gone from the slug class"
    assert not narrowed.match(branch), "narrowing no longer breaks the branch preview"
    assert narrowed.match(commit), (
        "narrowing broke the per-commit preview too — the fixtures no longer "
        "isolate the hyphen, so this test is measuring something else"
    )


def test_render_yaml_sets_the_env_var_that_shadows_the_yaml() -> None:
    """The var must exist — an empty list makes the identity test below vacuous.

    Asserting two things are equal proves nothing when one of them is absent,
    and absence here is not hypothetical: deleting the env var is a plausible
    "tidy-up" that would silently hand the decision back to production.yaml.
    """
    exprs = _render_exprs()
    assert exprs, (
        f"render.yaml declares no {_ENV_KEY} — either restore it, or delete this "
        "guard deliberately because the deployment now reads production.yaml alone"
    )


@pytest.mark.parametrize("expr", _render_exprs())
def test_render_yaml_declares_the_same_pattern(expr: str) -> None:
    """One setting, two files — an env var wins, and nobody can read Render's env.

    ⚠ This is not tidiness. The two values were DIFFERENT and only one carried
    the ``-your-team`` team segment; the other admitted any Vercel
    project on earth. While they disagree, "what does the deployment allow" has
    two answers and the honest one is *unknown* (gotcha 80: the Render API here
    does not expose env vars, so it cannot be settled from a session).
    """
    assert expr == _yaml_expr(), (
        "render.yaml and config/production.yaml set the same CORS regex to "
        "different values. An env var shadows the YAML leaf (gotcha 6), so the "
        "deployment enforces whichever is set on the service — which cannot be "
        "read from here. Make them byte-identical.\n"
        f"  render.yaml       : {expr}\n"
        f"  production.yaml   : {_yaml_expr()}"
    )


# ---------------------------------------------------------------------------
# The origin LIST — the regex's sibling setting, same three files, no guard.
#
# ⚠ The regex above is only half the CORS config, and the half below is the one
# that carries the named hosts customers actually browse from. It has the
# identical shadowing problem (`HELIXIS_API__CORS_ORIGINS` in render.yaml vs
# `api.cors_origins` in production.yaml, gotcha 6) and, until 2026-09-15,
# nothing compared any of the three places that spell it out.
#
# ⚠ These do NOT assert that a drift breaks the app — it does not, and saying
# so would overstate the control (gotcha 33's ADR-0008 lesson). `setup_
# middleware` unions every CONTRACT_ORIGIN back in whatever config resolved to,
# and logs a warning. What a drift breaks is the ability to answer "what does
# the deployment allow" from the repo, which is exactly what #477 cost days to.
# ---------------------------------------------------------------------------

_ORIGINS_ENV_KEY = "HELIXIS_API__CORS_ORIGINS"


def _yaml_origins() -> list[str]:
    raw = yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))
    origins = raw["api"]["cors_origins"]
    assert origins, "production.yaml lists no cors_origins — the guard is measuring nothing"
    return list(origins)


def _render_origins() -> list[list[str]]:
    """Every value render.yaml gives the origin-list env var, across all services."""
    import json

    raw = yaml.safe_load(_RENDER.read_text(encoding="utf-8"))
    return [
        json.loads(env["value"])
        for service in raw["services"]
        for env in service.get("envVars") or []
        if env.get("key") == _ORIGINS_ENV_KEY and "value" in env
    ]


def test_production_yaml_lists_every_contract_origin() -> None:
    from helixis.api.middleware import CONTRACT_ORIGINS

    configured = _yaml_origins()
    missing = [o for o in CONTRACT_ORIGINS if o not in configured]
    assert not missing, (
        f"config/production.yaml is missing ADR-0003 contract origin(s) {missing}. "
        "setup_middleware adds them back at boot with a warning, so the app still "
        "works — but the repo now says something different from what ships."
    )


@pytest.mark.parametrize("origins", _render_origins())
def test_render_yaml_lists_every_contract_origin(origins: list[str]) -> None:
    from helixis.api.middleware import CONTRACT_ORIGINS

    missing = [o for o in CONTRACT_ORIGINS if o not in origins]
    assert not missing, (
        f"render.yaml's {_ORIGINS_ENV_KEY} is missing {missing}. This env var "
        "SHADOWS production.yaml (gotcha 6), so it is the likelier of the two to "
        "be live — and gotcha 80 records that the Blueprint may not even be "
        "linked, in which case the value on the service was set by hand and this "
        "file is decorative. Keep it honest either way."
    )


def test_the_conformance_probe_only_tests_origins_the_backend_promises() -> None:
    """Both-sides rule (gotcha 28), pointed at a check and the thing it checks.

    ⚠ The failure this catches is a conformance run going red for a reason no
    config change can fix: the probe asserting preflight for an origin the
    backend never pinned. A 6-hourly routine that cries wolf is one people stop
    reading (gotcha 52/84), and it would bury the next real CORS break.

    ⚠ Deliberately ONE-directional. Not every contract origin is probed — the
    apex and www are the same deployment as `app.*` and add round trips without
    adding information — so demanding the reverse would fail against correct
    code (gotchas 103/109/111).
    """
    from helixis.api.middleware import CONTRACT_ORIGINS
    from helixis.conformance.checks import CONTRACT_CLIENT_ORIGINS

    assert CONTRACT_CLIENT_ORIGINS, "the probe tests no origins at all"
    unpinned = [o for o in CONTRACT_CLIENT_ORIGINS if o not in CONTRACT_ORIGINS]
    assert not unpinned, (
        f"conformance probes {unpinned}, which api/middleware.py does not pin. "
        "Either add it to CONTRACT_ORIGINS or stop probing it — as it stands the "
        "6-hourly conformance run fails on something no config can fix."
    )


# ---------------------------------------------------------------------------
# What a production origin may look like (the founder's auth hardening,
# 2026-11-11): an exact https host, or one Chrome extension's exact origin.
#
# ⚠ `http://localhost`, `http://localhost:80` and `chrome-extension://*` sat in
# production.yaml, and render.yaml's env var carried `http://localhost:5173` and
# `:3000`. Starlette's CORSMiddleware globs only a bare "*" and compares every
# other entry to the Origin header as a literal, so the extension "wildcard"
# never admitted an extension, while the localhost entries admitted exactly what
# they said. Both directions are pinned below against the REAL middleware.
# ---------------------------------------------------------------------------

#: A Chrome extension's origin: its id is 32 letters a to p.
_EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")
_HTTPS_HOST = re.compile(r"^https://[a-z0-9.-]+$")


def _bad_production_origins(origins: list[str]) -> list[str]:
    return [
        o
        for o in origins
        if not (_HTTPS_HOST.fullmatch(o) or _EXTENSION_ORIGIN.fullmatch(o)) or "localhost" in o
    ]


def test_production_yaml_names_only_exact_https_origins() -> None:
    bad = _bad_production_origins(_yaml_origins())
    assert not bad, (
        f"config/production.yaml allows {bad}. A production origin is an exact "
        "https host, or `chrome-extension://<32-letter id>` for one extension: no "
        "localhost, no http, and no `*` (Starlette treats `chrome-extension://*` "
        "as a literal that matches nothing)."
    )


@pytest.mark.parametrize("origins", _render_origins())
def test_render_yaml_names_only_exact_https_origins(origins: list[str]) -> None:
    """The env var shadows the YAML (gotcha 6), so it is held to the same rule."""
    bad = _bad_production_origins(origins)
    assert not bad, f"render.yaml's {_ORIGINS_ENV_KEY} allows {bad}"


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost",
        "http://localhost:80",
        "http://localhost:5173",
        "chrome-extension://*",
        "https://*.occupella.com",
        "https://app.occupella.com/",
        "chrome-extension://abc",
    ],
)
def test_the_origin_rule_can_say_no(origin: str) -> None:
    assert _bad_production_origins([origin]) == [origin]


def test_the_origin_rule_admits_what_production_needs() -> None:
    """The control: today's hosts and the shape the sidebar's origin will take."""
    from helixis.api.middleware import CONTRACT_ORIGINS

    fine = [*CONTRACT_ORIGINS, "chrome-extension://" + "a" * 32]
    assert _bad_production_origins(fine) == []


def _preflight_allows(allow_origins: list[str], origin: str) -> bool:
    """A preflight through Starlette's own CORSMiddleware, configured as
    setup_middleware configures it."""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    resp = TestClient(app).options(
        "/anything",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    return resp.headers.get("access-control-allow-origin") == origin


def test_a_star_inside_an_origin_matches_no_extension() -> None:
    """Why `chrome-extension://*` was removed rather than kept "to be safe": it
    never admitted an extension, so the sidebar cannot have depended on it."""
    sidebar_shaped = "chrome-extension://" + "a" * 32
    assert _preflight_allows(["chrome-extension://*"], sidebar_shaped) is False
    # ...while the exact origin, the shape the sidebar's entry will take, does.
    assert _preflight_allows([sidebar_shaped], sidebar_shaped) is True


def test_production_config_refuses_a_localhost_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behaviour, not just the file: the origins production.yaml resolves to
    (env overrides removed, as the YAML-only reading) refuse localhost."""
    from helixis.api.middleware import CONTRACT_ORIGINS
    from helixis.settings import Settings

    monkeypatch.setenv("HELIXIS_ENV", "production")
    monkeypatch.delenv("HELIXIS_API__CORS_ORIGINS", raising=False)
    monkeypatch.delenv("HELIXIS_API__CORS_ORIGIN_REGEX", raising=False)
    configured = list(Settings().api.cors_origins)
    allow = configured + [o for o in CONTRACT_ORIGINS if o not in configured]
    for origin in ("http://localhost", "http://localhost:80", "http://localhost:5173"):
        assert _preflight_allows(allow, origin) is False, origin
    assert _preflight_allows(allow, "https://app.occupella.com") is True
