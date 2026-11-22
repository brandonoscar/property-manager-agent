"""A failed Buildium read must never become "you have no records".

THE DEFECT
----------
`_sanitize_buildium_error` turns a `BuildiumApiError` into the sentence the
model reads. Five outcomes were reachable — 5xx, the shared 401/403 auth
branch, 404, 400 and a generic fallback — and **only that auth branch told the
model not to report the failure as an empty portfolio.**

⚠ 401 and 403 have since been SPLIT (they are different answers and the shared
branch gave 401's), so there are six. The anti-absence rule below is unchanged
and covers both.

So a 500, a rejected request, or anything unclassified handed back
*"I couldn't list open work orders in Buildium. (logged for review)"* with no
instruction attached, and the reply a model writes from that is "you have no
open work orders". That is this product's documented worst failure mode
(gotcha 26) and precisely what golden `099_buildium_auth_failed_honesty`
exists to catch — caught for ONE status class and not the others.

A 429 had no branch at all and fell to the generic fallback, which is also the
one status where the client has already retried: `_RETRY_STATUSES` in
`buildium/client.py` covers 429 with exponential backoff and honours
`Retry-After`, so a 429 reaching this function means the retries were spent.

WHAT IS PINNED
--------------
Behaviour, on the real function, for every status class — plus the deliberate
EXCEPTION, because "add the clause everywhere" is the wrong rule. On a 404
absence is the correct reading, and forbidding the model from saying so would
make it hedge about a fact it actually holds. A guard that cannot express that
difference would push someone to weaken it.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

import pytest

from helixis.agents.buildium_tools import _NOT_AN_ABSENCE, _sanitize_buildium_error
from helixis.buildium.errors import BuildiumApiError


def _err(status: int) -> BuildiumApiError:
    """A BuildiumApiError carrying `status`.

    ⚠ `status` is a REQUIRED POSITIONAL on this class — read from
    `buildium/errors.py` rather than guessed. My first draft constructed it
    with a message only and set the attribute afterwards, which raised. That
    is the same "check the signature, do not infer it" lesson the function's
    own comment records about an earlier brief guessing `status_code`.
    """
    return BuildiumApiError(status, f"raw buildium text for {status}")


#: Every status where the read FAILED. Absence is a false claim for all of them.
_FAILURES = [500, 502, 503, 400, 429, 401, 403, 418, 451]


@pytest.mark.parametrize("status", _FAILURES)
def test_a_failed_read_forbids_reporting_an_absence(status: int) -> None:
    message = _sanitize_buildium_error(_err(status), "list open work orders")
    assert "no records" in message or "having no records" in message, (
        f"status {status} produces a message with no instruction against reporting "
        "an absence. A model handed a bare failure writes 'you have no open work "
        "orders' — the false-absence failure gotcha 26 calls this product's worst."
    )


def test_the_404_branch_deliberately_does_not_forbid_it() -> None:
    """The exception that makes the rule meaningful.

    A 404 IS an absence — the record is not in Buildium. Attaching the clause
    here would make the model hedge about something it actually knows, which is
    a different kind of bad answer.
    """
    message = _sanitize_buildium_error(_err(404), "find that lease")
    assert _NOT_AN_ABSENCE not in message
    assert "couldn't find" in message


def test_an_unclassified_status_still_carries_the_clause() -> None:
    """The fallback is where a NEW Buildium status lands.

    That is the branch most likely to be reached by something nobody
    anticipated, so it is the one that must not be silent.
    """
    message = _sanitize_buildium_error(_err(418), "do the thing")
    assert _NOT_AN_ABSENCE in message


def test_a_non_integer_status_still_carries_the_clause() -> None:
    """The defensive `isinstance(status, int)` branch.

    ⚠ **Currently unreachable through `BuildiumApiError` itself** — `status` is
    a required positional int on that class, so a real one always has one. This
    covers the branch the function actually wrote (it guards on `isinstance`
    rather than assuming), reached here with a stand-in.

    Saying it is unreachable rather than dressing it up as a live path is
    gotcha 58's rule: pinning a distinction that does not exist teaches the
    next reader that it matters. It becomes live the day anything else with a
    `status` attribute is passed in.
    """

    class _NoStatus:
        status = None

        def __str__(self) -> str:  # what the logger would print
            return "transport failure"

    message = _sanitize_buildium_error(_NoStatus(), "read the ledger")  # type: ignore[arg-type]
    assert _NOT_AN_ABSENCE in message


# ── 401 and 403 are different answers ─────────────────────────────────────
#
# They shared one branch, and the branch said 401's sentence: "the saved
# credentials were rejected … reconnect on the Connectors page". A 403 means
# Buildium READ the credentials, ACCEPTED them, and refused this request — a
# permission on the API key, or a lapsed plan. So the message sent that user to
# redo a connection that works, and its blast-radius claim ("every other live
# Buildium call will fail the same way") is false for a per-endpoint denial.
#
# Three other places in this repo already treated a Buildium 403 as scope —
# `buildium/sync.py` names the `Administration > Users — View` toggle,
# `buildium/endpoints/administration.py`'s docstring says it is per-endpoint,
# and `agent/handlers/send_tenant_message` reads it the same way. The
# agent-facing message was the one that disagreed (gotcha 67's shape).


def _golden_099_mock_returns() -> list[str]:
    """The 401 strings golden 099 hard-codes in its `tool_mocks`."""
    case = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "src"
            / "helixis"
            / "evals"
            / "golden"
            / "099_buildium_auth_failed_honesty.json"
        ).read_text(encoding="utf-8")
    )
    mocks = case["input"]["tool_mocks"]
    return [
        entry["return"]
        for tool in ("buildium_list_open_work_orders", "buildium_list_work_orders_for_unit")
        for entry in mocks[tool]
    ]


def test_the_401_text_is_what_golden_099_mocks() -> None:
    """BOTH SIDES, and the side nobody was reading (gotcha 28).

    099's mocks quote this function's 401 output verbatim and **nothing
    compared them**. So a reworded 401 branch would leave the case exercising a
    string the tool no longer produces — a mock drifted from its tool, passing
    the whole time (gotcha 41). Splitting 403 out is exactly the kind of edit
    that could have done it.

    The expected values are READ FROM THE JSON, never retyped: a hand-copied
    expectation tests the transcription, not the agreement.
    """
    live = {
        _sanitize_buildium_error(_err(401), "list open work orders"),
        _sanitize_buildium_error(_err(401), "list work orders for that unit"),
    }
    mocked = set(_golden_099_mock_returns())
    assert mocked, "099's mocks are empty — the case would exercise nothing"
    assert mocked == live, (
        "golden 099 mocks a 401 string this function no longer produces.\n"
        f"  only in the mock: {mocked - live}\n"
        f"  only in the code: {live - mocked}"
    )


def test_401_and_403_do_not_give_the_same_instruction() -> None:
    """The defect, and the thing the fix must not overshoot into.

    Guarded in BOTH directions (gotchas 38/51): 401 must still say
    authentication and still claim the wide blast radius, because for a
    rejected credential both are TRUE and the agent prompt keys its
    stop-the-cascade rule on that phrase. 403 must say neither.
    """
    unauthorized = _sanitize_buildium_error(_err(401), "list open work orders").lower()
    forbidden = _sanitize_buildium_error(_err(403), "list open work orders").lower()

    assert "failed authentication" in unauthorized
    assert "every other live buildium call" in unauthorized
    assert "reconnected on the connectors page" in unauthorized

    assert "failed authentication" not in forbidden, (
        "a 403 is not an authentication failure — the credentials were accepted"
    )
    assert "every other live buildium call" not in forbidden, (
        "a permission denial is per-endpoint; claiming it kills every call is false"
    )
    assert "permission" in forbidden


def test_the_403_names_the_permission_path_before_reconnecting() -> None:
    """⚠ Reconnecting SURVIVES as the fallback, deliberately.

    Nothing in this repo has probed what Buildium returns for a disabled key or
    a lapsed Open API entitlement, and those plausibly DO need a new
    credential. Encoding "it is always permissions" would be a guess dressed as
    fact (gotcha 79). The order is the claim: permissions first, reconnect only
    if the rest fails too.
    """
    message = _sanitize_buildium_error(_err(403), "list open work orders")
    lowered = message.lower()
    assert "api keys" in lowered, "name where the permission is set"
    assert "subscription" in lowered, "a lapsed plan looks identical — say so"
    assert lowered.index("permission") < lowered.index("reconnect"), (
        "reconnecting must be offered as the fallback, not the headline"
    )


def test_the_403_wording_is_something_the_agent_prompt_actually_reads() -> None:
    """A branch whose phrasing no prompt rule consumes is decoration.

    Gotcha 28's both-sides rule, and the reason the prompt is part of this
    change rather than a follow-up: the 401 message works because
    `buildium_agent`'s prompt keys a rule on "failed authentication". The 403
    message needs its own trigger phrase, and the prompt has to recognise it.
    """
    from helixis.agents import buildium_agent

    trigger = "accepted the credentials and refused this request"
    assert trigger in _sanitize_buildium_error(_err(403), "list things")
    prompt = inspect.getsource(buildium_agent)
    assert trigger in prompt, (
        "the 403 message's trigger phrase appears in no prompt rule, so nothing "
        "tells the agent to keep going instead of reporting a broken connection"
    )


def test_the_473_mock_is_the_live_403_string() -> None:
    """The same drift guard as 099's, applied to the case being added with it.

    Writing a mock by hand is how 099's copy of the 401 text became a thing
    nobody compared to the tool. A case that mocks a string the tool no longer
    produces measures fiction and passes while doing it (gotchas 35/41).
    """
    case = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "src"
            / "helixis"
            / "evals"
            / "golden"
            / "473_buildium_permission_denied_is_not_a_broken_connection.json"
        ).read_text(encoding="utf-8")
    )
    mocked = {
        entry["return"] for entry in case["input"]["tool_mocks"]["buildium_list_open_work_orders"]
    }
    assert mocked == {_sanitize_buildium_error(_err(403), "list open work orders")}


def test_each_auth_branch_records_its_own_reason_code() -> None:
    """An AST walk, because a behavioural test cannot see a deleted call.

    Both codes must exist in the registry: `record_reason_code` warns and drops
    an unlisted slug, so a typo here is silent in exactly the way that makes an
    operator dashboard wrong rather than empty (gotcha 35 — grep every reader
    when adding a value).
    """
    from helixis.observability.reason_codes import REASON_CODES

    src = Path(inspect.getsourcefile(_sanitize_buildium_error) or "").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_sanitize_buildium_error"
    )
    recorded = [
        node.args[0].value
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "record_reason_code"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert recorded == ["buildium_auth_error", "buildium_permission_denied"], recorded
    for code in recorded:
        assert code in REASON_CODES, f"{code} is not in the stable vocabulary"


def test_the_rate_limit_says_the_retries_are_already_spent() -> None:
    """429 had no branch and fell to the generic fallback.

    The wording matters: the client already retried with backoff and honoured
    Retry-After, so "try again" on its own invites an immediate retry into the
    same limit. The message asks for a narrower slice.
    """
    message = _sanitize_buildium_error(_err(429), "list every work order")
    assert "rate-limit" in message.lower()
    assert "retried" in message.lower(), "say the retries are spent, not just 'try again'"
    assert "narrower" in message.lower()
    assert _NOT_AN_ABSENCE in message


def test_no_branch_leaks_buildiums_raw_text() -> None:
    """The property this function existed for in the first place.

    Adding branches must not reopen it — Buildium's raw English carries field
    names and API-key paths.
    """
    for status in [*_FAILURES, 404]:
        message = _sanitize_buildium_error(_err(status), "list things")
        assert "raw buildium text" not in message, f"status {status} leaked the raw error"


def test_the_action_phrase_reaches_every_message() -> None:
    """Each branch splices the caller's verb phrase, so the model knows WHAT
    failed. A branch that drops it reports a failure with no subject."""
    for status in [*_FAILURES, 404]:
        message = _sanitize_buildium_error(_err(status), "list overdue balances")
        assert "overdue balances" in message or "find that" in message, (
            f"status {status} dropped the action phrase"
        )
