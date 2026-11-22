"""An invited address is normalised once, in one place — and the pattern that
validates it is compiled by Rust, not by Python.

TWO THINGS THIS PINS, AND THE SECOND IS THE IMPORTANT ONE
---------------------------------------------------------
1. **Normalisation.** Pasting `"  Person@Example.com "` out of a spreadsheet
   used to return a 422 whose body is a raw regex — which tells an admin
   nothing about a space they cannot see. It now normalises to
   `person@example.com` and the invite works. Both invite models share ONE
   field declaration (`EmailAddress`), so the single-address and bulk routes
   cannot drift: they previously did, with `role` getting `.strip().lower()`
   and `email` only `.lower()` two lines apart.

2. **The anchor must stay `$`.** This is the guard that matters, because
   getting it wrong is an import-time crash rather than a wrong answer.

   `_EMAIL_SHAPE` is never run by Python's `re`. Pydantic 2 compiles
   `pattern=` with the Rust `regex` crate, and the two engines DISAGREE about
   this exact pattern:

   * Python's `re` lets `$` match before a trailing newline, so
     `re.match(_EMAIL_SHAPE, "a@b.co\\n")` is a match. Test the constant in a
     REPL and you will conclude the route accepts a pasted spreadsheet cell.
     That conclusion is drawn from an engine this code does not use.
   * Rust's `$` matches end-of-haystack only, so the real model rejects it.
   * Rust's regex has **no `\\Z`**. Reaching for the Python-correct anchor
     raises `SchemaError: unrecognized escape sequence` while BUILDING the
     model — the API fails to boot.

   That sequence actually happened while writing this file: a "fix" for a bug
   that could not occur, which would have taken the whole API down. The tests
   below fail in both directions so nobody repeats it — and the reason they can
   is that they exercise the MODEL, never the constant through `re`.
"""

from __future__ import annotations

import re

import pytest
from pydantic import BaseModel, Field, ValidationError

from helixis.api.routes.company import _EMAIL_SHAPE, InviteEntry, InviteRequest


@pytest.mark.parametrize("model", [InviteRequest, InviteEntry])
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Brandon@X.com  ", "brandon@x.com"),  # the pasted-cell case
        ("A@B.CO", "a@b.co"),  # case only
        ("a@b.co\n", "a@b.co"),  # trailing newline, the CSV column shape
        ("\tperson@example.com", "person@example.com"),  # a tab from a paste
        ("person@example.com", "person@example.com"),  # already clean
    ],
)
def test_both_invite_models_normalise_identically(model: type, raw: str, expected: str) -> None:
    """One declaration, so single-address and bulk cannot answer differently."""
    assert model(email=raw).email == expected


@pytest.mark.parametrize("model", [InviteRequest, InviteEntry])
@pytest.mark.parametrize("bad", ["no-at-sign", "a@b", "", "   ", "two@at@signs.com", "a b@c.co"])
def test_a_genuinely_bad_address_is_still_refused(model: type, bad: str) -> None:
    """The other direction. Normalising must not turn the validator off —
    stripping whitespace is not the same as accepting anything."""
    with pytest.raises(ValidationError):
        model(email=bad)


def test_normalisation_happens_in_the_model_not_at_the_call_site() -> None:
    """The property that makes a THIRD caller safe.

    `_invite_one` looks the invitee up with `WHERE lower(email) = $1` and the
    bulk route dedups on the address, so a caller that forgets to normalise
    silently fails to match an existing teammate. Asserting the model already
    did it is what lets the call sites stop caring.
    """
    assert InviteRequest(email="  MiXeD@Case.COM  ").email == "mixed@case.com"
    assert InviteEntry(email="  MiXeD@Case.COM  ").email == "mixed@case.com"


def test_the_pattern_compiles_under_pydantics_engine() -> None:
    """A `\\Z` here is an import-time crash, not a failing request.

    Building a model with the pattern is the whole assertion — Pydantic
    compiles `pattern=` eagerly, so an anchor the Rust engine does not know
    raises `SchemaError` here exactly as it would at application import.
    """

    class Probe(BaseModel):
        email: str = Field(pattern=_EMAIL_SHAPE, max_length=254)

    assert Probe(email="a@b.co").email == "a@b.co"


def test_the_anchor_is_the_one_the_rust_engine_understands() -> None:
    """Stated as a property of the string, so the failure NAMES the cause.

    Without this, reintroducing `\\Z` fails the test above with a SchemaError
    traceback about escape sequences, and the next reader has to rediscover why
    a perfectly reasonable-looking anchor is forbidden.
    """
    assert _EMAIL_SHAPE.endswith("$"), (
        "_EMAIL_SHAPE must end with `$`. Pydantic compiles it with the Rust regex "
        "crate, which has no `\\Z` and raises SchemaError at model-build time — "
        "the API would fail to boot. Rust's `$` already means end-of-haystack, so "
        "Python's trailing-newline quirk does not apply to this code path."
    )
    assert "\\Z" not in _EMAIL_SHAPE and r"\Z" not in _EMAIL_SHAPE


def test_python_re_and_the_real_validator_disagree_and_that_is_the_trap() -> None:
    """The measurement that started this, kept executable.

    If a future Pydantic switches to Python's `re`, this test breaks — and that
    IS the signal: the anchor reasoning above would need revisiting, because
    `$` would start admitting a trailing newline into the address that reaches
    GoTrue. Pinning the disagreement is how the assumption announces itself
    when it stops holding.
    """
    assert re.match(_EMAIL_SHAPE, "a@b.co\n"), (
        "python's re is expected to MATCH the trailing newline"
    )

    class Probe(BaseModel):
        email: str = Field(pattern=_EMAIL_SHAPE, max_length=254)

    with pytest.raises(ValidationError):
        Probe(email="a@b.co\n")  # the real engine refuses it
