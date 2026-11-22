"""A rate limit in front of an inbound webhook is a DATA-LOSS switch.

`buildium_webhook` bounds traffic BEFORE it spends HMAC CPU, which is right —
a forged-signature flood must not be able to burn the receiver. But the check
therefore runs before the envelope is parsed, so its key is the SENDER'S ip,
and every customer's Buildium events arrive from Buildium's own egress. One
bucket for the whole deployment (gotcha 69's shape: a per-IP key that is
really a per-DEPLOYMENT one), and at ~600/min per busy account a THIRD
customer syncing at the same time crosses it.

⚠ What crossing it costs is the part that makes this worth fixing. The
route's own unknown-account branch returns 200 rather than 401 and says why:
*"a 401 would burn Buildium's retry budget for an event we'll never want
anyway"*. So this repo already knows the retry budget is finite — and a 429
spends it on an event we DO want, after which the mirror is silently missing
a row and nothing anywhere says so.

The fix keeps the ordering and refunds the slot the instant a signature
verifies, so the ceiling bounds forged traffic and nothing else.
"""

from __future__ import annotations

import ast
from pathlib import Path

from helixis.api.rate_limit import RateLimiter, SlidingWindowCounter

_ROUTER = Path("src/helixis/webhooks/router.py")


class _Req:
    """The two attributes `_get_client_ip` reads, and nothing else."""

    def __init__(self, ip: str = "203.0.113.7") -> None:
        self.headers: dict[str, str] = {}
        self.client = type("C", (), {"host": ip})()
        self.state = type("S", (), {})()


# ── the counter primitive ────────────────────────────────────────────


def test_a_refund_gives_back_exactly_one_slot() -> None:
    c = SlidingWindowCounter(requests=2, window_seconds=60)
    assert c.is_allowed("k")[0] is True
    assert c.is_allowed("k")[0] is True
    assert c.is_allowed("k")[0] is False  # bucket full

    assert c.refund("k") is True
    assert c.is_allowed("k")[0] is True  # exactly one back
    assert c.is_allowed("k")[0] is False  # and no more


def test_refunding_an_untouched_key_is_a_no_op_rather_than_an_error() -> None:
    """⚠ It must be safe to call unconditionally. A refund that raised on an
    empty bucket would turn a defensive call into a 500 on the receiver —
    the fix becoming a worse version of the defect it replaces."""
    c = SlidingWindowCounter(requests=1, window_seconds=60)
    assert c.refund("never-seen") is False
    assert c.is_allowed("never-seen")[0] is True


def test_a_refund_cannot_manufacture_capacity() -> None:
    """Refunding more times than the key consumed must not push the bucket
    below empty and hand out free slots later."""
    c = SlidingWindowCounter(requests=2, window_seconds=60)
    assert c.is_allowed("k")[0] is True
    for _ in range(5):
        c.refund("k")
    assert c.is_allowed("k")[0] is True
    assert c.is_allowed("k")[0] is True
    assert c.is_allowed("k")[0] is False


def test_the_refund_pops_the_newest_entry_not_the_oldest() -> None:
    """⚠ The oldest timestamp is what the window's expiry is measured from
    (`is_allowed` computes `retry_after` off `bucket[0]`), so popping it
    would shorten the recorded history instead of cancelling THIS request —
    a refund that quietly moves the window is a different bug wearing the
    fix's name."""
    c = SlidingWindowCounter(requests=1, window_seconds=60)
    c.is_allowed("k")
    first = c._log["k"][0]
    c._log["k"].append(first + 30.0)  # a later request in the same window
    c.refund("k")
    assert c._log["k"] == [first]


# ── the verified-traffic property, end to end on the limiter ─────────


def test_verified_traffic_never_exhausts_the_ceiling() -> None:
    """The whole point: a customer's real events stop consuming the abuse
    budget, so the number bounds forgeries rather than portfolios."""
    limiter = RateLimiter(requests=3, window_seconds=60)
    req = _Req()

    async def _drive() -> int:
        admitted = 0
        for _ in range(20):  # far past the ceiling
            try:
                await limiter.check_by_ip(req)
            except Exception:
                break
            admitted += 1
            limiter.refund_by_ip(req)  # signature verified
        return admitted

    import asyncio

    assert asyncio.run(_drive()) == 20


def test_unverified_traffic_still_hits_the_ceiling() -> None:
    """⚠ The other direction, and it is what stops this being a removal of
    the limit. A flood that never verifies must still be shed — that is the
    CPU protection the pre-HMAC ordering exists for (gotchas 38/51: a
    one-sided test passes against a gate that refuses nothing)."""
    limiter = RateLimiter(requests=3, window_seconds=60)
    req = _Req()

    async def _drive() -> int:
        admitted = 0
        for _ in range(20):
            try:
                await limiter.check_by_ip(req)  # no refund — never verified
            except Exception:
                break
            admitted += 1
        return admitted

    import asyncio

    assert asyncio.run(_drive()) == 3


def test_one_senders_refunds_do_not_credit_another_sender() -> None:
    """The key still separates addresses — a verified stream from one IP must
    not buy capacity for a flood from a different one."""
    limiter = RateLimiter(requests=2, window_seconds=60)
    good, flood = _Req("203.0.113.7"), _Req("198.51.100.9")

    async def _drive() -> int:
        for _ in range(10):
            await limiter.check_by_ip(good)
            limiter.refund_by_ip(good)
        admitted = 0
        for _ in range(10):
            try:
                await limiter.check_by_ip(flood)
            except Exception:
                break
            admitted += 1
        return admitted

    import asyncio

    assert asyncio.run(_drive()) == 2


# ── it is wired, and wired in the ONE place that is safe ─────────────
#
# ⚠ A behavioural test of the limiter cannot see a route that never refunds
# (gotchas 62/66/69/103, four prior instances) — nor one that refunds too
# EARLY, which is the dangerous direction here: refunding before the
# signature is checked hands the abuse budget straight back to a forgery
# flood and silently deletes the protection the ordering exists for.


def _route() -> ast.FunctionDef:
    tree = ast.parse(_ROUTER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "buildium_webhook":
            return node  # type: ignore[return-value]
    raise AssertionError("buildium_webhook not found")


def _line_of(fn: ast.AST, name: str) -> int:
    """First line calling ``name``, whether it is `x.name(...)` or `name(...)`.

    ⚠ The first cut walked only ``ast.Attribute``, so it found
    `webhook_limiter.check_by_ip` and MISSED `verify_signature`, which is a
    bare function call — and failed against the CORRECT route. Gotcha 103's
    rule from the other side: a scan that matches too little is as useless as
    one that matches too much, and here it fails loudly rather than quietly,
    which is the direction to prefer.
    """
    hits = [
        node.lineno
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == name)
            or (isinstance(node.func, ast.Name) and node.func.id == name)
        )
    ]
    assert hits, f"no call to {name} in the route"
    return min(hits)


def test_the_receiver_refunds_a_verified_request() -> None:
    fn = _route()
    assert _line_of(fn, "refund_by_ip"), (
        "the Buildium receiver no longer refunds the abuse budget on a verified "
        "signature, so one customer's sync can 429 another customer's events"
    )


def test_the_refund_lands_after_the_signature_is_verified() -> None:
    """⚠ ORDER IS THE WHOLE SAFETY PROPERTY AND IS INVISIBLE TO EVERY
    BEHAVIOURAL TEST ABOVE. Moved above `verify_signature` the refund reads
    identically, passes all of them, and hands the budget to unverifiable
    traffic — gotcha 97's "present in the source, absent in effect", pointed
    at a security control instead of a prompt."""
    fn = _route()
    assert _line_of(fn, "refund_by_ip") > _line_of(fn, "verify_signature"), (
        "the refund moved above the signature check — the ceiling now bounds "
        "nothing, because a forgery flood gets its slots back too"
    )


def test_the_check_still_runs_before_the_signature_is_verified() -> None:
    """The pre-HMAC ordering is the CPU protection. Moving the check after
    verification would let a forged flood spend an HMAC per request — the
    cost the comment on that line has always named."""
    fn = _route()
    assert _line_of(fn, "check_by_ip") < _line_of(fn, "verify_signature")


def test_the_configured_limit_agrees_in_every_place_that_states_it() -> None:
    """⚠ REPLACES A GUARD I WROTE THAT FAILED AGAINST THE CORRECT CODE.

    The defect found here was three numbers in three places — the route's
    comment said 120/min, the limiter read 1200 and the YAML said 600. The
    obvious guard scans the route for a restated figure, and it fires on the
    corrected comment, which has to QUOTE the stale numbers in order to
    explain them (gotchas 40/59: scope to statements, never to prose, and a
    guard that cannot tell a defect from its own explanation is the wrong
    guard).

    What IS checkable is the property the drift actually broke: the three
    places that carry the number must agree (gotcha 28's both-sides rule).
    """
    import re

    from helixis.api.rate_limit import webhook_limiter
    from helixis.settings import RateLimitSettings

    yaml_text = Path("config/default.yaml").read_text(encoding="utf-8")
    match = re.search(r"^\s*webhooks:\s*(\d+)", yaml_text, re.M)
    assert match, "config/default.yaml no longer states the webhook rate limit"

    assert int(match.group(1)) == RateLimitSettings().webhooks == webhook_limiter.requests, (
        "the webhook rate limit disagrees across config/default.yaml, the "
        "RateLimitSettings default and the module singleton — the exact drift "
        "that left the route's comment quoting a number nothing used"
    )
