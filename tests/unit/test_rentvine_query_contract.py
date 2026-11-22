"""The Rentvine transport rules, and the guard that keeps them sourced.

The load-bearing test here is the CORPUS one: ``_query_index.py`` is generated
from the captured spec, so the only way it can be wrong is by drifting from
that spec — and drift is silent. Everything else tests behaviour a caller
depends on.

Why this matters more than an ordinary parameter check: a Rentvine filter the
server does not recognise is IGNORED, and the request returns the unfiltered
collection with a 200. There is no error to catch. That is CLAUDE.md gotcha
24's silent whole-account read, and it is the failure this module exists to
make unreachable.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from helixis.rentvine import transport  # noqa: E402
from helixis.rentvine._query_index import QUERY_PARAMS, SPEC_SHA256  # noqa: E402


def _generator():
    """Import the generator by path — ``scripts/`` is not an installed package."""
    path = REPO / "scripts" / "rentvine_query_index.py"
    spec = importlib.util.spec_from_file_location("_rv_query_index_gen", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# The corpus guard
# --------------------------------------------------------------------------


def test_the_committed_index_still_matches_the_vendor_spec():
    """Regenerate and compare. Drift here is silent in production."""
    gen = _generator()
    spec, digest = gen.load_spec(REPO / gen.SPEC)
    assert digest == gen.EXPECTED_SHA256, (
        "the captured spec itself changed — re-read it before regenerating"
    )
    assert digest == SPEC_SHA256, "the index was generated from a different document than it claims"

    rebuilt = gen.build_index(spec)
    committed = {op: tuple(sorted(names)) for op, names in QUERY_PARAMS.items()}
    assert rebuilt == committed, "run: python scripts/rentvine_query_index.py --write"


def test_the_index_is_not_vacuously_empty():
    """A scan that finds zero is a claim to check, not a result.

    Without this, a generator bug that produced an empty index would make every
    ``validate_query`` call raise UnknownOperationError — which reads as strictness
    rather than as a broken build.
    """
    assert len(QUERY_PARAMS) == 205, f"expected the spec's 205 operations, got {len(QUERY_PARAMS)}"
    with_filters = [op for op, names in QUERY_PARAMS.items() if names]
    assert len(with_filters) > 50, (
        "almost every operation came back with no query parameters — check the $ref walk"
    )


def test_pagination_is_indexed_on_the_operations_that_declare_it():
    """44 operations paginate, and every one carries BOTH halves.

    Regression guard for the reading that cost an earlier pass: a scan that
    ignored ``$ref`` concluded Rentvine had no page selector at all, because
    ``#/components/parameters/page`` is never declared inline.
    """
    paged = {op for op, names in QUERY_PARAMS.items() if transport.PAGE_PARAM in names}
    sized = {op for op, names in QUERY_PARAMS.items() if transport.PAGE_SIZE_PARAM in names}
    assert len(paged) == 44, f"expected 44 paginated operations, got {len(paged)}"
    assert paged == sized, "an operation offers a page selector without a size, or the reverse"


def test_five_filters_are_declared_both_with_and_without_the_bracket_suffix():
    """The measurement that makes a per-operation index necessary.

    If this ever collapses to zero, a global rule would become possible and
    this module could be much simpler — so the test states the finding rather
    than just guarding it.
    """
    every = {n for names in QUERY_PARAMS.values() for n in names}
    both = sorted(n for n in every if not n.endswith("[]") and f"{n}[]" in every)
    assert both == [
        "assignedToUserIDs",
        "bankAccountIDs",
        "portfolioIDs",
        "propertyIDs",
        "tagIDs",
    ], f"the both-forms set moved: {both}"


# --------------------------------------------------------------------------
# validate_query — the fail-closed half
# --------------------------------------------------------------------------


def test_a_declared_filter_passes_through_unchanged():
    params = {"propertyIDs[]": [1, 2], "page": 1}
    assert transport.validate_query("GET", "/accounting/transactions/search", params) is params


def test_an_undeclared_filter_is_refused_before_anything_is_sent():
    with pytest.raises(transport.UnknownQueryParameterError) as e:
        transport.validate_query("GET", "/accounting/transactions/search", {"propertyIds": [1]})
    # The message has to name what was wrong AND what is right, because the
    # correct spelling is not derivable.
    assert "propertyIds" in str(e.value)
    assert "propertyIDs[]" in str(e.value)


def test_the_wrong_bracket_form_is_refused_on_the_operation_that_wants_the_other_one():
    """The trap, both directions, on two real accounting searches.

    ⚠ These operations were MEASURED, not guessed — the first draft of this
    test asserted ``propertyIDs[]`` on ``GET /properties/search``, which does
    not declare it at all, and the guard caught the author making exactly the
    mistake the module exists to prevent.

    Both endpoints below search money. ``/accounting/payables/search`` wants
    ``propertyIDs``; ``/accounting/transactions/search`` wants
    ``propertyIDs[]``. Send either spelling to the other and Rentvine ignores
    the filter and returns the whole account, 200.
    """
    transport.validate_query("GET", "/accounting/transactions/search", {"propertyIDs[]": [1]})
    with pytest.raises(transport.UnknownQueryParameterError):
        transport.validate_query("GET", "/accounting/transactions/search", {"propertyIDs": [1]})

    transport.validate_query("GET", "/accounting/payables/search", {"propertyIDs": [1]})
    with pytest.raises(transport.UnknownQueryParameterError):
        transport.validate_query("GET", "/accounting/payables/search", {"propertyIDs[]": [1]})


def test_an_undocumented_operation_raises_a_different_error_than_a_bad_parameter():
    """Separating these is deliberate.

    The spec is a ceiling on what is documented, not on what exists — five
    reference-client call sites have no counterpart in it. A caller with a real
    reason to reach one catches UnknownOperationError specifically; a typo must not
    be able to ride through on the same except clause.
    """
    with pytest.raises(transport.UnknownOperationError):
        transport.validate_query("GET", "/leases/{leaseID}/occupants", {})
    assert not issubclass(transport.UnknownQueryParameterError, transport.UnknownOperationError)
    assert not issubclass(transport.UnknownOperationError, transport.UnknownQueryParameterError)


def test_no_parameters_is_fine_on_an_operation_that_declares_none():
    assert transport.validate_query("DELETE", "/leases/{leaseID}", {}) == {}


# --------------------------------------------------------------------------
# paths and URLs
# --------------------------------------------------------------------------


def test_build_path_emits_exactly_one_leading_slash_either_way():
    """15 of the reference client's 60 call sites emit a doubled slash."""
    assert transport.build_path("leases/{leaseID}", leaseID=7) == "/leases/7"
    assert transport.build_path("/leases/{leaseID}", leaseID=7) == "/leases/7"
    assert "//" not in transport.build_path("/leases/{leaseID}", leaseID=7)


def test_base_url_is_per_customer_and_refuses_an_empty_subdomain():
    assert transport.base_url("acme") == "https://acme.rentvine.com/api/manager"
    assert transport.base_url(" /acme/ ") == "https://acme.rentvine.com/api/manager"
    with pytest.raises(transport.RentvineTransportError):
        transport.base_url("   ")


def test_operation_key_normalises_verb_and_slash():
    assert transport.operation_key("get", "leases") == transport.operation_key("GET", "/leases")


# --------------------------------------------------------------------------
# pagination
# --------------------------------------------------------------------------


def test_page_params_always_sends_both_and_is_one_based():
    assert transport.page_params(1, 25) == {"page": 1, "pageSize": 25}
    with pytest.raises(transport.RentvineTransportError):
        transport.page_params(0, 25)
    with pytest.raises(transport.RentvineTransportError):
        transport.page_params(1, 0)


def test_a_short_page_ends_the_loop_and_a_full_one_does_not_prove_more():
    assert transport.page_looked_complete(25, 25) is True
    assert transport.page_looked_complete(24, 25) is False
    assert transport.page_looked_complete(0, 25) is False
    # Over-full is still "keep going" rather than an assertion about totals.
    assert transport.page_looked_complete(26, 25) is True


# --------------------------------------------------------------------------
# error meaning
# --------------------------------------------------------------------------


def test_401_and_403_do_not_collapse_into_one_meaning():
    """Conflating them is a documented product bug on the Buildium side."""
    creds, _ = transport.classify_status(401)
    perm, _ = transport.classify_status(403)
    assert creds is transport.Failure.CREDENTIALS
    assert perm is transport.Failure.PERMISSION
    assert creds is not perm


def test_an_undocumented_status_backs_off_rather_than_failing_hard():
    """No rate limit is documented anywhere; absence is not a promise."""
    for status in (429, 500, 502, 503):
        meaning, retryable = transport.classify_status(status)
        assert meaning is transport.Failure.UNDOCUMENTED, status
        assert retryable is True, status


def test_the_documented_statuses_are_not_retried():
    for status in (400, 401, 403, 404):
        _, retryable = transport.classify_status(status)
        assert retryable is False, status
