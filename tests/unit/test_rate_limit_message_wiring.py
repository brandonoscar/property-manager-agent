"""Every branch that reports a rate limit asks the ONE predicate.

WHAT BROKE
----------
`LoginForm.tsx` carries a helper whose own docstring says, verbatim, that
`/rate limit|too many/` does NOT match what Supabase sends — the real message
is "For security purposes, you can only request this after 60 seconds". Two
email branches used the helper. The SIGN-IN branch carried
`/rate limit|too many requests/`: the exact pattern documented as not working.

So somebody who mistyped their password a few times fell to the generic `else`
and read *"Sign in didn't go through. Try again, or contact
team@occupella.com"* — the two worst instructions available, because retrying
is what extends the limit and there is nothing for support to do. That is the
checklist's "account locked after failed attempts, no unlock path", and the
lock was never real: GoTrue's limiter is time-based, so the unlock path IS the
wait, and naming its length is what turns a dead end into an action.

⚠ **A behavioural test cannot see the NEXT branch.** `LoginForm.rateLimit.
test.tsx` proves the three current paths behave; it is structurally blind to a
fourth added later with its own inline pattern — which is precisely how this
one arrived. Gotcha 62's rule (pair a behavioural test with a call-site guard),
and gotchas 66/69 are two more instances of a correct helper a caller ignored.

⚠ **The stripper is load-bearing and is tested BOTH ways.** The helper's
docstring now QUOTES the broken pattern in order to explain it, so a raw scan
fires on the sentence documenting the guard — measured True against the
corrected file. That is gotcha 40/59's trap, and stripping comments without
proving the stripper can still say no is how a guard goes quietly blind.

⚠ Source-level rather than AST: `typescript` at this version is the Go-native
port and exports only `version`, and no other parser is installed here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_LOGIN = Path("frontend/src/components/LoginForm.tsx")

#: Any inline rate-limit regex in this file.
#:
#: ⚠ The test is not "does a pattern exist" — `isRateLimited` IS one, and the
#: first cut of this guard flagged the correct implementation. What separates a
#: working pattern from the broken one is whether it carries the string Supabase
#: ACTUALLY sends: "you can only request this after N seconds". A pattern that
#: matches on `rate limit|too many` alone misses every real limit.
_RATE_PATTERN = re.compile(r"/[^/\n]*(?:rate limit|too many)[^/\n]*/i")
_REAL_PROVIDER_STRING = "only request this after"

#: The predicate every branch must ask instead.
_PREDICATE = "isRateLimited("


def _strip_comments(src: str) -> str:
    """Remove `/* … */` and `// …`, so prose explaining the guard is not read
    as code that trips it."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def _source() -> str:
    return _strip_comments(_LOGIN.read_text(encoding="utf-8"))


def test_the_stripper_can_still_say_no() -> None:
    """Guard-the-guard, in both directions. A stripper that removed everything
    would make every assertion below vacuous; one that removed nothing would
    fire on the docstring."""
    sample = "const a = /rate limit|too many/i;\n// mentions /rate limit|too many/i\n"
    stripped = _strip_comments(sample)
    assert "const a" in stripped, "the stripper is eating real code"
    assert stripped.count("rate limit") == 1, "the stripper left the comment behind"
    block = _strip_comments("/* quotes /rate limit|too many/i */\nconst b = 1;")
    assert "rate limit" not in block and "const b" in block


def test_the_scan_finds_the_file_and_the_predicate() -> None:
    """A renamed file or a renamed helper collects nothing and every check
    below passes against anything (gotcha 45's shape, arriving at the guard)."""
    src = _source()
    assert len(src) > 2000, f"{_LOGIN} is missing or unexpectedly small"
    assert src.count(_PREDICATE) >= 3, (
        f"expected the shared predicate at all three rate-limit branches, "
        f"found {src.count(_PREDICATE)}"
    )


def test_no_branch_re_implements_the_pattern_that_does_not_work() -> None:
    blind = [p for p in _RATE_PATTERN.findall(_source()) if _REAL_PROVIDER_STRING not in p]
    assert not blind, (
        f"a branch tests {blind} inline, and none of them carries "
        f"{_REAL_PROVIDER_STRING!r} — so a real Supabase rate limit falls "
        "straight past it. That is how the sign-in path reported one as an "
        "unknown failure and sent the user to support. Call isRateLimited()."
    )


def test_the_guard_would_catch_the_original_defect() -> None:
    """⚠ Proof this can say YES, not just no. A guard verified only against
    the fixed file is one that might be matching nothing at all (gotcha 45).
    This is the literal branch that shipped."""
    blind = [
        p
        for p in _RATE_PATTERN.findall("} else if (/rate limit|too many requests/i.test(raw)) {")
        if _REAL_PROVIDER_STRING not in p
    ]
    assert blind == ["/rate limit|too many requests/i"]


def test_the_guard_spares_the_real_predicate() -> None:
    """And the other direction: the working pattern must NOT be flagged, or
    the only way to go green is to delete the implementation."""
    real = "return /rate limit|too many|only request this after|over_email_send_rate_limit/i.test("
    assert [p for p in _RATE_PATTERN.findall(real) if _REAL_PROVIDER_STRING not in p] == []


def test_the_sign_in_branch_specifically_asks_the_predicate() -> None:
    """The branch the defect was actually in, named so a future edit to it
    fails here rather than silently reverting.

    Anchored on the message it produces, because that is the user-visible
    contract: the wait, and the fact that nothing is locked.
    """
    src = _source()
    assert "Too many sign-in attempts" in src, (
        "the sign-in rate-limit branch has lost its message — a generic "
        "'try again, or contact support' is the failure this closed"
    )
    assert "nothing has been locked" in src, (
        "the honest half. There is no lock to unlock; saying so is what stops "
        "somebody resetting a password that was already correct"
    )


@pytest.mark.parametrize("guessed", ["wait a minute and try again.'", "wait a minute and try"])
def test_no_branch_hardcodes_the_wait(guessed: str) -> None:
    """The number is in the message and every branch used to guess it.

    "Wait a minute" is right for the 60-second limiter and wrong for any other —
    wrong in the direction that sends somebody back too early to be limited
    again. `waitPhrase` reads what the server said and falls back to the
    generic only when it said nothing (gotchas 55/57).
    """
    assert guessed not in _source(), (
        "a branch states the wait as a constant rather than reading it from the "
        "provider's message — use waitPhrase(raw)"
    )
