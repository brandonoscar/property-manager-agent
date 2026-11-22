"""The Rentvine probe runs against a REAL sandbox, so read-only is a contract.

`scripts/rentvine_probe.py` answers the five questions the vendor's OpenAPI
spec cannot, by asking the server. That means it points at a live account with
real records in it, and `docs/rentvine/vendor/conflicts.md` says in as many
words: run the read-only probes first and do not skip to the writes.

⚠ **"It only contains GETs today" is not the property worth pinning.** A
behavioural test cannot see a later edit adding a `method=` parameter to the one
request function, because every existing test would keep passing while the next
caller hands it "POST". So the load-bearing guards here are AST walks over the
source — the same reason `test_write_confirm_gate.py` scans for confirm gates
rather than exercising them.

The rest pin the two properties that make a verdict evidence rather than a
coincidence: the secret cannot reach the report, and two pages of results can
actually be told apart.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import patch

import pytest

from scripts.rentvine_probe import Creds, _identity, _redact, _rows, main

SOURCE = pathlib.Path("scripts/rentvine_probe.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _calls(name: str) -> list[ast.Call]:
    """Every call whose dotted name ends in `name`."""
    out = []
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == name:
            out.append(node)
        elif isinstance(func, ast.Name) and func.id == name:
            out.append(node)
    return out


# --------------------------------------------------------------------- verb


def test_every_request_is_a_get_written_as_a_literal():
    """The verb must be a constant, not a value something can pass in.

    This is the mutation that matters: `Request(url, method=verb)` reads as a
    harmless refactor, passes every behavioural test, and lets the next caller
    issue a write against a customer's sandbox.
    """
    requests = _calls("Request")
    assert requests, "no urllib Request call found — has the probe been rewritten?"

    for call in requests:
        method = next((kw for kw in call.keywords if kw.arg == "method"), None)
        assert method is not None, (
            f"line {call.lineno}: a Request with no explicit method defaults to GET "
            "by accident rather than by decision — state it."
        )
        assert isinstance(method.value, ast.Constant), (
            f"line {call.lineno}: the verb is not a literal. A parameterised verb "
            "is how this file stops being read-only."
        )
        assert method.value.value == "GET", (
            f"line {call.lineno}: verb is {method.value.value!r}, not 'GET'. This "
            "probe runs against a live account."
        )


def test_no_second_http_client_is_imported():
    """One request path, so one place can be audited.

    `requests`/`httpx` would each bring their own verb handling and their own
    default, and the guard above only walks `Request` calls.
    """
    imported: set[str] = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for banned in ("requests", "httpx", "aiohttp", "urllib3"):
        assert banned not in imported, (
            f"{banned} is imported. The read-only guarantee is checked by walking "
            "urllib Request calls, and a second client is invisible to it."
        )


def test_the_only_sender_is_the_one_function_named_get():
    """`urlopen` must appear once, inside `_get`.

    A second call site is a second thing to audit, and the reason to notice is
    that it would not be covered by the verb guard unless it also went through
    `Request`.
    """
    opens = _calls("urlopen")
    assert len(opens) == 1, (
        f"{len(opens)} urlopen call sites; expected exactly 1 (inside _get). "
        "Every request has to go through the one function whose verb is pinned."
    )

    enclosing = [
        fn.name
        for fn in ast.walk(TREE)
        if isinstance(fn, ast.FunctionDef) and any(n is opens[0] for n in ast.walk(fn))
    ]
    assert "_get" in enclosing, f"urlopen lives in {enclosing}, not _get"


# ------------------------------------------------------------------ secrets


def test_the_secret_is_removed_and_the_account_code_is_kept():
    """Both directions, because a redactor that blanks everything is useless.

    The account code is deliberately NOT redacted: it is in the URL of every
    request, it is not a password, and a report that hides it cannot be read
    against the right tenant.
    """
    creds = Creds.__new__(Creds)
    creds.account = "acme"
    creds.key = "KEYabcdefghij"
    creds.secret = "s3cret-value-do-not-print"

    url = f"https://acme.rentvine.com/api/manager/x?k={creds.key}&s={creds.secret}"
    out = _redact(url, creds)

    assert creds.secret not in out, "the secret survived redaction"
    assert "acme" in out, "the account code was redacted — the report loses its tenant"
    assert "KEYa" in out, "the key was removed entirely; a truncated prefix aids diagnosis"
    assert "KEYabcdefghij" not in out, "the full key survived"


def test_a_missing_credential_sends_nothing_and_exits_two():
    """Exit 2 is 'could not run'. ⚠ It must not be reachable by sending anything.

    The skip-floor rule: a harness that did not run is not one that validated
    anything, and a probe that fired one request before giving up has touched a
    customer's account for no verdict.
    """

    def _explode(*_a, **_k):  # pragma: no cover - the assertion is that this never runs
        raise AssertionError("the probe sent a request without complete credentials")

    with patch.dict(
        "os.environ",
        {"RENTVINE_ACCOUNT_CODE": "", "RENTVINE_API_KEY": "", "RENTVINE_API_SECRET": ""},
        clear=False,
    ):
        with patch("urllib.request.urlopen", _explode):
            assert main(["--out", "/dev/null"]) == 2


# ------------------------------------------------------------------ control


@pytest.mark.parametrize(
    "rows, expect",
    [
        ([{"property": {"propertyID": 7, "name": "A"}}], ["7"]),
        ([{"propertyID": 9}], ["9"]),
        ([{"leaseID": 3}, {"leaseID": 4}], ["3", "4"]),
    ],
)
def test_identity_reads_the_id_through_one_level_of_envelope(rows, expect):
    assert _identity(rows) == expect


def test_identity_distinguishes_two_different_pages():
    """⚠ THIS is what makes the pagination verdict evidence.

    Without a per-row fingerprint the probe cannot tell 'the selector moved us
    to page 2' from 'the server ignored the parameter and returned page 1
    again' — both come back as one row with a 200.
    """
    page_one = _identity([{"propertyID": 1}])
    page_two = _identity([{"propertyID": 2}])
    assert page_one != page_two, "two different rows fingerprint the same"
    assert _identity([{"propertyID": 1}]) == page_one, "the fingerprint is not stable"


def test_an_unrecognised_envelope_returns_no_rows_rather_than_guessing():
    """Two lists means the shape is not the documented one, so decline.

    Picking one would make every count downstream a guess wearing a
    measurement's clothes; an empty list routes the caller to COULD NOT ANSWER.
    """
    assert _rows({"data": [1, 2], "meta": [3]}) == []
    assert _rows({"properties": [1, 2]}) == [1, 2]
    assert _rows([1, 2]) == [1, 2]
    assert _rows("not an envelope") == []


# -------------------------------------------------------------- honest scope


def test_the_docstring_does_not_claim_to_answer_the_write_question():
    """Replace-vs-merge is a WRITE and is out of scope by construction.

    A future reader skimming for 'what is still open' has to find it here, not
    discover the omission by running the probe and finding no verdict.
    """
    doc = ast.get_docstring(TREE) or ""
    assert "REPLACES or MERGES" in doc, (
        "the module no longer states that the write question is out of scope"
    )
    assert "NOT" in doc.split("REPLACES or MERGES")[1][:80], (
        "the write question is mentioned but not excluded"
    )
