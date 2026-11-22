"""The headers a BROWSER gets are not the headers the API sets.

`api/middleware.py` puts a full security-header set on every response — and
every one of those responses is an `/api/v1/...` JSON body. The page a person
actually loads is served by Vercel from `frontend/vercel.json`, which until
2026-09-08 declared no headers at all. So `X-Frame-Options: DENY` protected the
JSON and not the web app itself, which is the document somebody would frame.

That asymmetry is invisible from either side on its own: the backend tests pass,
the frontend has nothing to test, and the gap only shows up if you go and ask
what the two configs say TOGETHER. Hence this file, which reads both.

⚠ **The CSP here is deliberately NARROW, and that is a decision rather than an
omission.** It carries only directives that cannot break rendering:
`frame-ancestors` (no iframe embeds anywhere — measured across all four repos:
the Electron shell docks a `WebContentsView`, which is a top-level frame and
ignores framing headers; the Chrome sidebar is a side panel; nothing else
embeds the app), `base-uri`, `object-src` (no `<object>`/`<embed>` in the tree)
and `form-action` (no form carries an `action=`).

A full `script-src` / `connect-src` / `style-src` policy is the real prize and
is NOT here, because getting one wrong is a white screen for every user with no
error anyone sees. It needs a report endpoint and a Report-Only observation
window first — a Report-Only header with nowhere to report is the "silently
does nothing" shape this repo keeps rediscovering (gotchas 45/54), so it was
not shipped as a placeholder.

**Update 2026-11-11: `script-src` (and the `worker-src` it forces) is here,
by the founder's call, without the Report-Only window.** What stood in for it:
an inventory of every script the build and the DEPLOYED bundle run (one
same-origin module; posthog-js's lazy scripts from its assets host, web vitals
among them since the project has it on; Sentry Replay's blob: worker; the
JSON-LD data block, which never runs), the built app loaded in Chrome under the
policy with no violation, and `frontend/src/csp.test.ts`, which asks posthog-js
itself which host it loads from. `connect-src`, `style-src` and the rest stay
out until someone does the same for them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_VERCEL = _ROOT / "frontend" / "vercel.json"
_MIDDLEWARE = _ROOT / "src" / "helixis" / "api" / "middleware.py"

#: Set by the API and NOT expected on the static app, with the reason.
_BACKEND_ONLY = {
    # Correlates one API request with one log line. A static asset has no
    # request to correlate, and Vercel issues its own `x-vercel-id`.
    "X-Request-ID",
}


def _vercel_headers() -> dict[str, str]:
    config = json.loads(_VERCEL.read_text(encoding="utf-8"))
    rules = config.get("headers") or []
    assert rules, "frontend/vercel.json declares no headers block at all"
    out: dict[str, str] = {}
    for rule in rules:
        for header in rule.get("headers", []):
            out[header["key"]] = header["value"]
    return out


def _middleware_headers() -> dict[str, str]:
    """Every `response.headers["X"] = "literal"` the middleware assigns.

    Read from the SOURCE rather than by booting the app: this container cannot
    import the app (broken crypto stack), and the point of the guard is a
    config comparison, not a runtime one. A non-literal value is skipped —
    HSTS is assembled across two lines and is handled by name below.
    """
    src = _MIDDLEWARE.read_text(encoding="utf-8")
    pattern = re.compile(
        r"""response\.headers\[\s*["'](?P<key>[\w-]+)["']\s*\]\s*=\s*["'](?P<value>[^"']*)["']"""
    )
    return {m.group("key"): m.group("value") for m in pattern.finditer(src)}


def test_the_static_app_sends_every_security_header_the_api_does() -> None:
    api = _middleware_headers()
    assert api, "found no header assignments in middleware.py — the scan is broken"

    app = _vercel_headers()
    missing = sorted(k for k in api if k not in _BACKEND_ONLY and k not in app)
    assert not missing, (
        "the API sets these and the page a browser loads does not: "
        f"{missing}. Add them to frontend/vercel.json, or add an entry to "
        "_BACKEND_ONLY saying why the static app is exempt."
    )


def test_the_shared_headers_carry_the_same_value_on_both_surfaces() -> None:
    """Two configs that disagree are worse than one — a reader cannot tell
    which is intended, and the weaker one is the one that ships."""
    api = _middleware_headers()
    app = _vercel_headers()
    drift = {
        key: (value, app[key]) for key, value in api.items() if key in app and app[key] != value
    }
    assert not drift, f"same header, different value on API vs app: {drift}"


def test_hsts_is_unconditional_on_the_static_app() -> None:
    """The backend gates HSTS on `settings.env == "production"` because it also
    runs over plain HTTP in development. Vercel only ever serves HTTPS, so the
    same gate there would be a header nobody gets."""
    value = _vercel_headers().get("Strict-Transport-Security", "")
    assert "max-age=" in value, "no HSTS on the static app"
    seconds = int(re.search(r"max-age=(\d+)", value).group(1))
    assert seconds >= 31_536_000, f"HSTS max-age is under a year: {seconds}"
    assert "includeSubDomains" in value


def _csp_directives() -> dict[str, list[str]]:
    csp = _vercel_headers().get("Content-Security-Policy", "")
    out: dict[str, list[str]] = {}
    for part in csp.split(";"):
        words = part.split()
        if words:
            out[words[0]] = words[1:]
    return out


def test_the_csp_denies_framing_and_names_only_the_safe_directives() -> None:
    """Re-pointed 2026-11-11: `script-src` and `worker-src` left the risky set by
    the founder's call (see the module note for what stood in for the Report-Only
    window, and the next test for what they may say). The checkpoint still
    holds for every other fetch directive."""
    csp = _vercel_headers().get("Content-Security-Policy")
    assert csp, "no Content-Security-Policy on the static app"

    directives = {part.strip().split()[0] for part in csp.split(";") if part.strip()}
    assert "frame-ancestors 'none'" in csp, (
        "frame-ancestors is the modern half of X-Frame-Options and the one "
        "directive here with a measured basis — nothing embeds this app"
    )

    # A `connect-src` arriving without the inventory work is the change that
    # white-screens everybody. Requiring a deliberate edit here is the point:
    # this assertion is a checkpoint, not a ceiling.
    risky = directives & {
        "connect-src",
        "style-src",
        "img-src",
        "font-src",
        "default-src",
    }
    assert not risky, (
        f"{sorted(risky)} added to the enforced CSP. That is the right "
        "destination, but it breaks the whole app if one origin is missed — "
        "inventory what the deployed bundle loads (as script-src's was, "
        "frontend/src/csp.test.ts), then widen this test in the same change."
    )


def test_script_src_names_exactly_what_the_app_runs() -> None:
    """The founder's item (2026-11-11): a script-src on the chat's CSP that
    allows the scripts the built app loads, and nothing else. No inline, no
    eval, no wildcard: an injected script tag finds nothing it may run."""
    directives = _csp_directives()
    assert directives.get("script-src") == ["'self'", "https://us-assets.i.posthog.com"], (
        directives.get("script-src")
    )
    # With a script-src and no worker-src, script-src would govern workers and
    # refuse Sentry Replay's compression worker, which starts from a blob: URL.
    assert directives.get("worker-src") == ["blob:"], directives.get("worker-src")


def test_the_csp_covers_the_app_pages() -> None:
    config = json.loads(_VERCEL.read_text(encoding="utf-8"))
    rules = [
        rule
        for rule in config.get("headers") or []
        if "content-security-policy"
        in {header["key"].lower() for header in rule.get("headers", [])}
    ]
    assert rules, "vercel.json sets no Content-Security-Policy"
    for rule in rules:
        pattern = re.compile(f"^{rule['source']}$")
        assert pattern.match("/settings"), rule["source"]
