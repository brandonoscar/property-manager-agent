"""Every `/api/v1/...` path the web app calls resolves to a real backend route.

Found 2026-09-16 by listing what the client asks for and comparing it against
the router table, rather than by any test failing. It is the founder's own
worked example — *"if a 404 happens and it looks like there was no fallback,
that's a problem"* — and until now nothing anywhere compared the two sides
(gotcha 28's rule, at the scale of the API contract).

MEASURED: 62 literal client paths against 137 backend routes. One has no
counterpart at all — `executeWebhookAction` POSTs to `/api/v1/webhooks/{id}/
actions` and the backend has NO route under `/api/v1/webhooks` (the Buildium
receiver is `/webhooks/buildium`, outside the versioned prefix). Its own
comment says *"Production stub. Wire to backend when available."* It is exempt
below ONLY because it has no callers, and the exemption asserts that — a
convenient stub carrying a 404 is what gets wired in later (gotcha 74's
deleted `greeting_or_default`).

⚠ WHAT THIS CANNOT SEE, stated rather than implied: a path assembled from
variables before it reaches `apiFetch`. The scan reads string literals at the
call site, so a client that builds its URL elsewhere is invisible here. That
is the direction that silently permits more (gotcha 45), and it is why the
canary below asserts the scan found a realistic number of call sites — a bad
glob or a renamed helper would otherwise collect zero and report green
(gotchas 49/100/101/114/128).

⚠ A `${...}` SEGMENT MATCHES ANY BACKEND SEGMENT, and that width is a
correction rather than laziness. `reminders.ts::act` builds
`/api/v1/reminders/${id}/${action}` where `action` is only ever
snooze/dismiss/handled — all three real. A stricter match reported that
correct code as a defect, and a guard that fails against correct code is
telling you the property is wrong (gotchas 103/109/111/124). The failure this
exists to catch survives the widening intact: nothing in the backend begins
`/api/v1/webhooks`, at any segment count.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend" / "src"

# The client helpers that actually reach the network. A path passed to
# anything else is not a request.
_CALLERS = r"apiFetch|apiGet|apiPost|apiPut|apiPatch|apiDelete"
_CALL = re.compile(rf"(?:{_CALLERS})\s*[<(][^)]{{0,300}}", re.S)
_LITERAL = re.compile(r"""['"`](/api/v1/[A-Za-z0-9_\-/{}$:.]*)['"`]""")

# Paths with no backend route, exempt for a stated reason that this file
# CHECKS. An entry whose reason stops holding fails its own guard below
# (gotcha 63 — a stale exemption reads as a licence nobody needs).
_EXEMPT = {
    "/api/v1/webhooks/${event.id}/actions": (
        "frontend/src/services/webhooks.ts",
        "executeWebhookAction",
    ),
}


def _routes(router: Any, prefix: str, out: set[str]) -> None:
    """Collect paths, descending through FastAPI's included-router wrappers.

    ⚠ `app.include_router` does NOT flatten in this FastAPI version — it
    appends one `_IncludedRouter` per call, and those carry no `.path`. A walk
    that only reads `.path` finds FIVE routes (the docs pages and the
    websocket) and would report every client path as missing. That is loud
    rather than silent, which is the safe direction, and the count assertion
    below is what names the real cause instead of 62 false findings.
    """
    for route in getattr(router, "routes", []):
        inner = getattr(route, "original_router", None)
        if inner is not None:
            ctx = getattr(route, "include_context", None)
            _routes(inner, prefix + (getattr(ctx, "prefix", "") or ""), out)
            continue
        path = getattr(route, "path", None)
        if path:
            out.add(prefix + path)


def backend_paths() -> set[str]:
    from helixis.main import app

    out: set[str] = set()
    _routes(app, "", out)
    return out


_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
# ⚠ NOT a bare `//` — that eats the rest of any line holding an `https://`
# URL, and the direction it fails in is a MISSED caller, which is the one
# this guard must never do (gotcha 45's "silently permits more").
_LINE_COMMENT = re.compile(r"(?<!:)//[^\n]*")


def _without_comments(text: str) -> str:
    """Prose that MENTIONS a symbol is not a caller.

    The next person to write "executeWebhookAction is still unwired" in a
    neighbouring file would otherwise fail this guard — a scanner that cannot
    tell a defect from the sentence explaining it (gotchas 40/59/91/114/124).
    """
    return _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", text))


def _segments(path: str) -> list[str]:
    return [s for s in path.strip("/").split("/") if s]


def _wild(seg: str) -> bool:
    return seg.startswith("${") or (seg.startswith("{") and seg.endswith("}"))


def _matches(client: str, server: str) -> bool:
    a, b = _segments(client), _segments(server)
    if len(a) != len(b):
        return False
    return all(_wild(x) or _wild(y) or x == y for x, y in zip(a, b))


def client_paths() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(FRONTEND.rglob("*.ts*")):
        if ".test." in path.name or ".spec." in path.name:
            continue
        text = path.read_text(encoding="utf-8")
        for call in _CALL.finditer(text):
            for lit in _LITERAL.finditer(call.group(0)):
                found.setdefault(lit.group(1), set()).add(str(path.relative_to(REPO)))
    return found


def test_the_scan_reaches_both_sides() -> None:
    """A scan that finds nothing is a claim to check, not a result."""
    server = backend_paths()
    assert len(server) > 100, (
        f"only {len(server)} backend routes collected — FastAPI's include shape "
        "probably moved; see _routes()"
    )
    assert "/api/v1/health" in server, "the router walk is not reaching mounted routers"
    client = client_paths()
    assert len(client) > 40, f"only {len(client)} client paths found — wrong root or helper name?"


def test_every_client_path_has_a_backend_route() -> None:
    server = backend_paths()
    missing = []
    for path, sites in sorted(client_paths().items()):
        if path in _EXEMPT:
            continue
        if not any(_matches(path, s) for s in server):
            missing.append(f"{path}  called from {', '.join(sorted(sites))}")
    assert not missing, (
        "the web app calls a path the backend does not serve — a 404 the user "
        "cannot act on:\n" + "\n".join(missing)
    )


def test_the_exempt_stub_still_has_no_callers() -> None:
    """The exemption is only safe while nothing invokes it.

    `executeWebhookAction` would 404 the moment a button is wired to it, and
    the 404 now also carries build-skew advice ("reload in a moment") that
    cannot help — an instruction the reader cannot carry out (gotchas
    102/106/118). So the licence is conditional on the caller count, which is
    a fact this test can check rather than a promise in a comment.
    """
    for path, (owner, symbol) in _EXEMPT.items():
        assert symbol in (REPO / owner).read_text(encoding="utf-8"), (
            f"{symbol} is gone from {owner} — drop the {path} exemption"
        )
        callers = [
            p
            for p in FRONTEND.rglob("*.ts*")
            if str(p.relative_to(REPO)) != owner
            and ".test." not in p.name
            and re.search(rf"\b{symbol}\b", _without_comments(p.read_text(encoding="utf-8")))
        ]
        assert not callers, (
            f"{symbol} now has callers ({[str(p.relative_to(REPO)) for p in callers]}) "
            f"and {path} has no backend route — build the route or remove the call; "
            "do not widen this exemption"
        )


def test_a_mention_is_not_a_caller_and_a_url_is_not_a_comment() -> None:
    assert "executeWebhookAction" not in _without_comments(
        "// executeWebhookAction is still unwired\n"
    )
    assert "executeWebhookAction" not in _without_comments(
        "/* TODO: wire executeWebhookAction */\n"
    )
    # …and a real call on a line that also carries a URL survives.
    kept = _without_comments("const u = 'https://x/y'; executeWebhookAction(e, 'o');\n")
    assert "executeWebhookAction" in kept


def test_a_path_the_backend_does_not_serve_is_caught() -> None:
    """The distinguishing input, supplied directly (gotcha 122's rule).

    Both directions, because a matcher that rejected everything would pass the
    positive check perfectly while flagging all 62 real paths (gotchas 38/51).
    """
    server = backend_paths()
    assert not any(_matches("/api/v1/webhooks/${id}/actions", s) for s in server)
    assert any(_matches("/api/v1/reminders/${id}/${action}", s) for s in server)
    assert any(_matches("/api/v1/billing/checkout", s) for s in server)
    # A segment count that does not line up is still a miss.
    assert not any(_matches("/api/v1/billing/checkout/extra", s) for s in server)
