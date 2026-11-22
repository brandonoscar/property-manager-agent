"""The server says which build answered, and the browser can actually read it.

Render deploys the API and Vercel deploys the web app from one branch,
independently, so the page in front of a customer and the server answering it
are routinely different builds of Occupella. Until this shipped neither side
could know: the bundle baked in its commit for bug reports, the backend
resolved its own SHA for Sentry's release tag, and nothing carried one to the
other. A stale tab's 404 read as "that record may have been deleted".

Three separate properties, and each fails in its own silent way:

  * the header is STAMPED  — a behavioural test can see this;
  * the header is EXPOSED  — a behavioural test CANNOT. `TestClient` reads
    response headers whatever CORS says, so the one failure mode that matters
    in a real browser is invisible to it (see the test's own note);
  * the resolver is CACHED — Sentry called it once at boot, this is called on
    every response, and it shells out to `git`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from helixis.observability.build_id import (
    BUILD_HEADER,
    SHORT_LEN,
    UNKNOWN_BUILD,
    build_sha,
    short_build_id,
)

_REPO = Path(__file__).resolve().parents[2]
_MIDDLEWARE = _REPO / "src/helixis/api/middleware.py"
_CLIENT = _REPO / "frontend/src/services/buildVersion.ts"


# ---------------------------------------------------------------------------
# What the id itself is allowed to be.
# ---------------------------------------------------------------------------


def test_it_truncates_to_the_width_the_client_bakes_in() -> None:
    # `vite.config.ts` slices the Vercel commit to 12. A server sending 40
    # would never compare equal to anything, so the check would silently
    # report "different builds" on every single request — the cry-wolf
    # direction, which is how a control gets switched off.
    assert short_build_id("0123456789abcdef0123456789abcdef01234567") == "0123456789ab"
    assert len(short_build_id("0123456789abcdef0123456789abcdef01234567")) == SHORT_LEN


@pytest.mark.parametrize("value", ["", "   ", UNKNOWN_BUILD])
def test_an_unresolvable_build_comes_out_as_the_sentinel(value: str) -> None:
    # The client matches this exactly to mean "claim nothing", so an empty or
    # blank SHA must arrive as the word rather than as "".
    assert short_build_id(value) == UNKNOWN_BUILD


def test_the_sentinel_is_short_enough_to_survive_the_truncation() -> None:
    """⚠ The invariant that let a dead branch be deleted, kept as a test.

    `short_build_id` used to special-case the sentinel so the truncation could
    not reshape it. That arm could not change the result for ANY input —
    `unknown` is 7 characters against a 12-character cut — so the mutation
    battery scored it as covered while it did nothing (gotcha 58).

    The concern behind it is real, and this is where it lives now: lengthen
    the sentinel past `SHORT_LEN` and the client starts receiving a truncated
    word it can never match, which reads as a real build id and so reports a
    mismatch against every page forever. That fails HERE, naming the reason,
    instead of silently in a browser.
    """
    assert len(UNKNOWN_BUILD) <= SHORT_LEN, (
        f"{UNKNOWN_BUILD!r} is longer than the {SHORT_LEN}-character cut, so "
        "short_build_id would truncate it and the client's 'claim nothing' "
        "sentinel would stop matching"
    )


def test_the_resolver_is_cached_because_it_shells_out_to_git() -> None:
    """⚠ Load-bearing, not tidiness.

    `_resolve_release_sha` runs `git rev-parse HEAD` with a 2s timeout when
    neither env var is set — which is every local dev machine and every test
    run. Sentry called it ONCE at boot, so that cost nothing. This is stamped
    on every response, so an uncached read forks a process per request.
    """
    build_sha.cache_clear()
    build_sha()
    build_sha()
    build_sha()
    info = build_sha.cache_info()
    assert info.misses == 1, f"resolved {info.misses} times — the cache is not holding"
    assert info.hits == 2


# ---------------------------------------------------------------------------
# The two wiring halves. One is behavioural; the other structurally cannot be.
# ---------------------------------------------------------------------------


def test_every_response_carries_the_build_header() -> None:
    from fastapi.testclient import TestClient

    from helixis.main import app

    with TestClient(app) as client:
        res = client.get("/api/v1/health")

    assert res.headers.get(BUILD_HEADER), (
        f"{BUILD_HEADER} missing — the client has nothing to compare against "
        "and every stale-page failure goes back to reading as a deleted record"
    )


def _exposed_headers() -> list[str]:
    """What CORSMiddleware was ACTUALLY constructed with.

    ⚠ Read off the real middleware entry, not the source. The first cut of
    this walked the `cors_kwargs` dict literal and collected `ast.Constant`
    values only — so it failed against the CORRECT code, because the middleware
    passes the imported `BUILD_HEADER` constant rather than retyping the
    string, which is the better spelling. An AST scan grades how a value was
    written; this grades what it resolved to.
    """
    from fastapi.middleware.cors import CORSMiddleware

    from helixis.main import app

    for m in app.user_middleware:
        if m.cls is CORSMiddleware:
            return list(m.kwargs.get("expose_headers") or [])
    raise AssertionError("CORSMiddleware is not installed on the app")


def test_the_header_is_exposed_to_browser_javascript() -> None:
    """⚠ THE HALF A BEHAVIOURAL TEST CANNOT SEE, and the one that fails silently.

    `TestClient` hands back every response header regardless of CORS, so the
    test above passes whether or not a real browser can read the value. Cross
    origin, `res.headers.get(...)` returns `null` unless the name is in
    `Access-Control-Expose-Headers` — no error, no warning, nothing in the
    console. The header arrives, DevTools shows it, and the comparison is
    permanently unknown.

    `allow_headers` is NOT this: that governs the REQUEST. And the wildcard is
    not a substitute — `Access-Control-Expose-Headers: *` is ignored on any
    credentialed request, which every authenticated call here is.
    """
    exposed = _exposed_headers()

    assert BUILD_HEADER in exposed, (
        f"{BUILD_HEADER} is stamped but not in expose_headers — browser JS "
        "reads null for it cross-origin, with no error to say why"
    )
    # Stamped since this middleware shipped and unreadable by the client the
    # whole time. It is the id a support conversation turns on.
    assert "X-Request-ID" in exposed


def test_the_middleware_uses_the_shared_resolver() -> None:
    """A behavioural test cannot see a second copy of the rule.

    If the middleware went back to calling `_resolve_release_sha` directly, the
    header would still appear and every test above would pass — while forking
    `git` per request and sending a 40-character value the client can never
    match. Gotchas 62/66/69/103: pair the behaviour with its call site.
    """
    tree = ast.parse(_MIDDLEWARE.read_text(encoding="utf-8"))
    called = {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "short_build_id" in called
    assert "_resolve_release_sha" not in called


# ---------------------------------------------------------------------------
# Both sides of the contract, in two languages.
# ---------------------------------------------------------------------------


def _ts_const(name: str) -> str:
    """Read `export const <name> = '<value>';` out of the client module."""
    src = _CLIENT.read_text(encoding="utf-8")
    m = re.search(rf"export const {name} = '([^']*)'", src)
    assert m, f"{name} not found in {_CLIENT.name} — renamed, or no longer a literal"
    return m.group(1)


def test_the_two_languages_agree_on_the_header_name() -> None:
    # Gotcha 28: a string that crosses a producer/consumer boundary needs a
    # test that reads BOTH sides. Each half is individually correct and a
    # one-character drift makes the comparison permanently unknown — which
    # looks exactly like "the builds always match".
    assert _ts_const("BUILD_HEADER") == BUILD_HEADER


def test_the_two_languages_agree_on_the_unknown_sentinel() -> None:
    # The client matches this exactly to decide "claim nothing". If the server
    # started sending a different word for the same state, the client would
    # read it as a real build id and report a mismatch against every page.
    assert _ts_const("UNKNOWN_BUILD") == UNKNOWN_BUILD
