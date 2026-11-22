"""In-memory sliding-window rate limiter for FastAPI.

Designed for single-process uvicorn. If you scale to multiple workers
behind a load balancer, swap the in-memory dict for Redis while keeping
this same interface — call sites don't change.

Module-level singletons (``agent_run_limiter`` etc.) are created with
defaults; ``apply_settings()`` is called once during app startup to
reconfigure them from YAML / env vars.
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request, status

from helixis.api.auth import AuthenticatedUser

if TYPE_CHECKING:
    from helixis.settings import RateLimitSettings


class SlidingWindowCounter:
    """Thread-safe sliding-window counter.

    Stores a list of timestamps per key. On each check, prunes entries
    older than ``window_seconds`` and compares the remainder against
    ``requests``. Smoother than a fixed window — immune to the
    burst-at-boundary attack.
    """

    def __init__(self, requests: int, window_seconds: float) -> None:
        self.requests = requests
        self.window = window_seconds
        self._log: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def reconfigure(self, requests: int, window_seconds: float) -> None:
        """Swap limits without losing the in-flight counter map."""
        with self._lock:
            self.requests = requests
            self.window = window_seconds

    def is_allowed(self, key: str) -> tuple[bool, int, float]:
        """Record a request attempt.

        Returns ``(allowed, remaining, retry_after_seconds)``. The attempt
        is only recorded if allowed — rejected attempts don't count
        against the window.
        """
        now = time.monotonic()
        cutoff = now - self.window

        with self._lock:
            bucket = [t for t in self._log[key] if t > cutoff]
            current = len(bucket)

            if current >= self.requests:
                oldest = bucket[0] if bucket else now
                retry_after = max(0.0, oldest + self.window - now)
                self._log[key] = bucket
                return False, 0, retry_after

            bucket.append(now)
            self._log[key] = bucket
            return True, self.requests - current - 1, 0.0

    def refund(self, key: str) -> bool:
        """Give back ONE slot this key just consumed. Returns whether one was.

        ⚠ **This exists because a rate limit in front of an inbound webhook is
        a DATA-LOSS switch.** The Buildium receiver has to bound traffic before
        it spends HMAC CPU, so the check runs before the envelope is parsed —
        which means the key is the SENDER'S ip, and every customer's events
        arrive from Buildium's egress. One shared bucket for the whole
        deployment, and crossing it 429s a real event whose loss is silent
        (gotcha 69's shape: a per-IP key that is really a per-DEPLOYMENT one).

        A refund on successful signature verification is what separates the
        two populations without moving the check: a request that proved it
        carries a company's own secret stops consuming the abuse budget, so
        the ceiling bounds FORGED traffic only. The route's unknown-account
        branch already reasons this way — it returns 200 rather than 401
        specifically to protect Buildium's finite retry budget, and a 429
        spends that same budget on an event we DO want.

        ⚠ It pops the NEWEST timestamp, not the oldest. The oldest is what
        the window's expiry is measured from, so removing it would shorten
        the recorded history rather than cancel this request.

        ⚠ **It does NOT make the ceiling unreachable under perfect
        concurrency.** N simultaneous requests are all admitted (or refused)
        before any refund lands, so a burst larger than the ceiling arriving
        all at once still sheds the tail. Senders pipeline rather than fan
        out, so this bounds the realistic case; it is stated rather than
        claimed solved.
        """
        with self._lock:
            bucket = self._log.get(key)
            if not bucket:
                return False
            bucket.pop()
            return True

    def cleanup(self) -> int:
        """Drop empty / expired buckets. Returns number of keys pruned."""
        now = time.monotonic()
        cutoff = now - self.window
        pruned = 0

        with self._lock:
            empty_keys: list[str] = []
            for key in list(self._log.keys()):
                self._log[key] = [t for t in self._log[key] if t > cutoff]
                if not self._log[key]:
                    empty_keys.append(key)

            for key in empty_keys:
                del self._log[key]
                pruned += 1

        return pruned


class RateLimiter:
    """Thin facade that turns a SlidingWindowCounter into FastAPI
    dependency-callable methods. Each call site picks ``check_by_user``
    or ``check_by_ip`` depending on whether the route is authenticated.
    """

    def __init__(self, requests: int, window_seconds: int = 60) -> None:
        self.counter = SlidingWindowCounter(requests, window_seconds)

    @property
    def requests(self) -> int:
        return self.counter.requests

    @property
    def window_seconds(self) -> float:
        return self.counter.window

    def reconfigure(self, requests: int, window_seconds: int = 60) -> None:
        self.counter.reconfigure(requests, window_seconds)

    def _reject(self, remaining: int, retry_after: float) -> None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please slow down.",
            headers={
                "X-RateLimit-Limit": str(self.requests),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(int(retry_after)),
                "Retry-After": str(int(retry_after) + 1),
            },
        )

    async def check_by_user(
        self,
        request: Request,
        user: AuthenticatedUser | None = None,
    ) -> None:
        """Rate-limit by authenticated user_id. Falls back to IP if
        the user object is not yet resolved (shouldn't happen in normal
        route ordering).
        """
        key = f"user:{user.user_id}" if user is not None else f"ip:{_get_client_ip(request)}"
        allowed, remaining, retry_after = self.counter.is_allowed(key)

        if not allowed:
            self._reject(remaining, retry_after)

        # Stash headers for the middleware to attach on the response.
        request.state.rate_limit_headers = {
            "X-RateLimit-Limit": str(self.requests),
            "X-RateLimit-Remaining": str(remaining),
        }

    async def check_by_ip(self, request: Request) -> None:
        """Rate-limit by client IP. For unauthenticated endpoints
        (e.g. the Buildium webhook — signature is the auth, not JWT).
        """
        key = f"ip:{_get_client_ip(request)}"
        allowed, remaining, retry_after = self.counter.is_allowed(key)

        if not allowed:
            self._reject(remaining, retry_after)

        request.state.rate_limit_headers = {
            "X-RateLimit-Limit": str(self.requests),
            "X-RateLimit-Remaining": str(remaining),
        }

    def refund_by_ip(self, request: Request) -> bool:
        """Give back the slot ``check_by_ip`` took for this request.

        Call it ONLY once the request has proven it is authentic — for the
        Buildium receiver that is a verified HMAC signature, which means the
        sender holds a company's own webhook secret. Before that point the
        request is indistinguishable from a forgery and must keep paying.

        Deliberately SYNCHRONOUS: it is not a FastAPI dependency, it is an
        explicit statement made mid-route after a specific fact is
        established. An `async def` would read as another gate to add to a
        route's dependency list, which is exactly where it must not go.
        """
        return self.counter.refund(f"ip:{_get_client_ip(request)}")

    async def check_global(self, request: Request) -> None:
        """Rate-limit the ENDPOINT as a whole, ignoring who is calling.

        ⚠ A per-IP limit bounds ONE caller, not total spend. On an endpoint
        that costs real money per hit and carries no account to bill it to,
        that is the wrong axis: a hundred rotating addresses each stay under
        a 20/min ceiling while together they buy a thousand provider calls a
        minute. Keying on a constant is the point — the question this answers
        is "what can this endpoint cost in a day", which is the only question
        with a bounded answer while the caller is anonymous.

        ⚠ In-memory and therefore PER PROCESS (this module's single-process
        caveat applies here too), so N worker processes means N budgets. It
        bounds the worst case to a known multiple rather than to infinity; one
        shared budget needs the Redis swap the module docstring describes.
        """
        allowed, remaining, retry_after = self.counter.is_allowed("global")
        if not allowed:
            self._reject(remaining, retry_after)


def _get_client_ip(request: Request) -> str:
    """Extract client IP, respecting X-Forwarded-For behind a proxy.

    The first IP in the chain is the original client; each intermediate
    proxy appends its own address, so we split on comma and take [0].
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


# ── Pre-configured limiters ──────────────────────────────────────────
# Module-level singletons so routes can import them directly. Limits
# are placeholders — ``apply_settings()`` resets them from YAML / env
# during app startup.

agent_run_limiter = RateLimiter(requests=20, window_seconds=60)
agent_action_limiter = RateLimiter(requests=60, window_seconds=60)
connectors_limiter = RateLimiter(requests=30, window_seconds=60)
# ⚠ This bounds the SENDER, not a tenant: every company's Buildium events
# arrive from Buildium's egress, so this is one shared bucket for the whole
# deployment. That is tolerable ONLY because the receiver refunds the slot
# once a signature verifies (`refund_by_ip`), which leaves the ceiling
# bounding forged traffic. Do not read the number as a per-customer budget.
webhook_limiter = RateLimiter(requests=1200, window_seconds=60)
# Invites send a Supabase email to an arbitrary address, so this endpoint
# gets a much tighter cap than the generic connectors limiter: enough for a
# real team-invite burst, low enough to defang invite-spam / enumeration
# abuse. Fixed sensible default — deliberately not wired into apply_settings.
invite_limiter = RateLimiter(requests=30, window_seconds=600)
# Branded-browser web search (`GET /search`) is UNAUTHENTICATED — the
# Electron browser chrome has no Helixis JWT — so it's rate-limited by IP.
# Kept tight to defang an open-Tavily-proxy abuse vector while leaving room
# for a human's real search burst. The hard quota this used to defer lives
# below in ``search_daily_limiter``; auth stays off deliberately, because the
# results page must render logged-out.
search_limiter = RateLimiter(requests=20, window_seconds=60)
# ⚠ The per-IP cap above bounds one abuser; it does NOT bound spend. Every hit
# on `GET /search` buys a Tavily call AND a grounded-summary LLM call, and the
# endpoint is anonymous by design — so there is no account to meter and a
# rotating-address caller stays under 20/min forever while the bill does not.
# This is the ceiling on the DAY, checked with `check_global`: a browser user
# searching all day is nowhere near it, so it cannot break the Electron client,
# and the worst case becomes a number instead of an open question.
search_daily_limiter = RateLimiter(requests=2000, window_seconds=86_400)
# Static-map image proxy (`GET /maps/static`) is UNAUTHENTICATED — it's the
# ``src`` of an <img> in a chat bubble, which can't carry a JWT — so it's
# rate-limited by IP. Each hit is a billable Google static-map call, so this
# defangs an open-map-proxy abuse vector; a chat turn renders at most one map,
# and a bubble re-render reuses the browser's image cache. Pre-launch posture.
maps_static_limiter = RateLimiter(requests=30, window_seconds=60)


def apply_settings(rate_limits: RateLimitSettings) -> None:
    """Reconfigure all module-level limiters from a RateLimitSettings.

    Called from ``create_app()`` so the caps come from YAML / env vars
    rather than the hard-coded defaults above.
    """
    agent_run_limiter.reconfigure(rate_limits.agent_run, 60)
    agent_action_limiter.reconfigure(rate_limits.agent_action, 60)
    connectors_limiter.reconfigure(rate_limits.connectors, 60)
    webhook_limiter.reconfigure(rate_limits.webhooks, 60)


__all__ = [
    "RateLimiter",
    "SlidingWindowCounter",
    "agent_action_limiter",
    "agent_run_limiter",
    "apply_settings",
    "connectors_limiter",
    "invite_limiter",
    "maps_static_limiter",
    "search_daily_limiter",
    "search_limiter",
    "webhook_limiter",
]
