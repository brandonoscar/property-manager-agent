"""Which build of the backend is answering this request?

Two deploy systems ship this product from one branch: Render builds the API,
Vercel builds the web app, and neither waits for the other. So for a window
after every merge — and for as long as somebody leaves a tab open — the page
in front of a customer and the server answering it are different builds of
Occupella. When the API contract moved in between, the request fails and the
message they get says the record "may have been deleted" or to "check the
fields", neither of which is true and neither of which reloading-free retrying
can ever fix.

Both halves of the fact already existed and were never put together: the web
app bakes its commit in as ``__APP_VERSION__`` (used for bug reports), and the
backend resolved its own SHA for Sentry's release tag. Nothing carried the
server's build to the client, so nothing could compare them.

⚠ **THE RESOLVER SHELLS OUT TO ``git`` AND THIS IS READ PER REQUEST.**
Sentry called it exactly once at boot, so a 2-second subprocess was free. A
response header is stamped on every response, so the same call unguarded would
fork a process per request in any environment without the env vars set — which
is every local dev machine. ``@lru_cache`` is load-bearing, not tidiness: the
build cannot change while the process is alive.
"""

from __future__ import annotations

import functools

__all__ = ["BUILD_HEADER", "SHORT_LEN", "UNKNOWN_BUILD", "build_sha", "short_build_id"]

#: The response header carrying it. ⚠ A custom response header is invisible to
#: browser JS cross-origin unless it is named in ``Access-Control-Expose-
#: Headers`` — it arrives on the wire and ``res.headers.get(...)`` returns
#: ``null``, with no error anywhere. `middleware.py` exposes it; a test pins
#: that, because the failure is silent in exactly the way a header cannot be.
BUILD_HEADER = "X-Occupella-Build"

#: The client bakes in the first 12 characters of its commit (see
#: ``frontend/vite.config.ts``), so the server sends the same width or the two
#: could never be compared as equals.
SHORT_LEN = 12

#: Stated rather than omitted: "we could not work out which build this is" is a
#: different fact from "the builds differ", and only one of them is worth
#: telling a customer about. Every consumer treats it as UNKNOWN and claims
#: nothing (the decline-to-assert posture the consent ledger and the mirror
#: rollups take).
UNKNOWN_BUILD = "unknown"


@functools.lru_cache(maxsize=1)
def build_sha() -> str:
    """The full SHA of the running backend, or ``"unknown"``.

    Cached for the life of the process — see the module note. The resolution
    order lives in ``sentry_setup`` because that is where it was written and
    where its tests are; this is the one home for *using* it.
    """
    from helixis.observability.sentry_setup import _resolve_release_sha

    return _resolve_release_sha()


def short_build_id(sha: str | None = None) -> str:
    """The comparable form: the first ``SHORT_LEN`` characters, or UNKNOWN.

    ⚠ The sentinel is NOT special-cased here, and that is a decision rather
    than an omission. An explicit ``value == UNKNOWN_BUILD`` arm was written
    first and deleted: it could not change the result for any input, because
    ``UNKNOWN_BUILD`` is shorter than ``SHORT_LEN`` and truncating it returns
    it unchanged. A branch no input can distinguish is dead code wearing a
    safety check's costume — and a mutation battery scores it as covered.

    What the arm was reaching for is real, so it is pinned as the INVARIANT it
    actually depends on instead: ``len(UNKNOWN_BUILD) <= SHORT_LEN``. Lengthen
    the sentinel and a test fails naming this, rather than the client silently
    receiving a truncated word it can no longer match.
    """
    value = (sha if sha is not None else build_sha()).strip()
    if not value:
        return UNKNOWN_BUILD
    return value[:SHORT_LEN]
