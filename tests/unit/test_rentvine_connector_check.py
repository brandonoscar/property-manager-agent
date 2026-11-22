"""Guards for the connector harness — `scripts/rentvine_connector_check.py`.

⚠ WHAT IS ACTUALLY AT RISK, because it is not the HTTP.
    This harness is what an operator will run to decide whether the Rentvine
    connect surface works, against a deployment nobody here can reach. Its
    failure modes are therefore the two that read as answers:

      1. **A healthy refusal reported as a defect.** The connector ships DARK
         (``render.yaml`` declares the flag false on web and worker), the
         one-PMS trigger answers 409, and every route is admin-floored. All
         three are the product working. A harness that exits 1 on any of them
         sends somebody after a phantom for an afternoon, and the first thing
         they would reach for is the flag — which is the one change that turns
         a dark connector live on a real deployment.

      2. **A pass over a check that was not made.** The store returning 200 is
         not the connector working: the read-back has to come back MASKED, and
         nothing else in this repo asserts that against a real backend. A
         harness that stores and does not read is measuring HTTP.

⚠ And the one it must never do: print the secret. The whole reason the
   credentials come from the environment (never argv) is that a value in argv
   lands in shell history and in ``ps`` — a harness that echoes it into the
   terminal gives that back for free.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "rentvine_connector_check.py"


def _load() -> Any:
    sys.path.insert(0, str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location("rentvine_connector_check", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cc = _load()

ACCOUNT = "sunset-pm"
KEY = "rv_key_abcdefghijkl"
SECRET = "rv_secret_zyxwvutsrqpo"


class _Resp:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    @property
    def text(self) -> str:
        import json

        if isinstance(self._payload, Exception):
            return "not json"
        return json.dumps(self._payload)


class _FakeClient:
    """Answers the four calls the harness makes, and records every one.

    Recording is what lets a test assert a request was NOT sent — which is the
    property ``--check`` and the un-acknowledged ``--disconnect`` both rest on,
    and neither is observable from an exit code alone.
    """

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []
        #: Every body handed to PUT. ``--discover``'s whole claim is about what
        #: is ABSENT from it, and an exit code cannot carry that.
        self.bodies: list[Any] = []

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def _answer(self, verb: str, url: str) -> _Resp:
        self.calls.append((verb, url))
        key = f"{verb} {url.rsplit('/rentvine', 1)[-1]}"
        answer = self.routes[key]
        if isinstance(answer, list):
            return answer.pop(0)
        return answer

    def get(self, url: str, **_: Any) -> _Resp:
        return self._answer("GET", url)

    def put(self, url: str, **kw: Any) -> _Resp:
        self.bodies.append(kw.get("json"))
        return self._answer("PUT", url)

    def post(self, url: str, **_: Any) -> _Resp:
        return self._answer("POST", url)

    def delete(self, url: str, **_: Any) -> _Resp:
        return self._answer("DELETE", url)


class _FakeHttpx:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client

    def Client(self, **_: Any) -> _FakeClient:  # noqa: N802 - mirrors httpx
        return self.client


def _stored(**over: Any) -> dict[str, Any]:
    body = {
        "configured": True,
        "account_code": ACCOUNT,
        "api_key_last4": KEY[-4:],
        "last_tested_at": None,
    }
    body.update(over)
    return body


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cc.BASE_URL_ENV, "https://api.example.test")
    monkeypatch.setenv(cc.TOKEN_ENV, "jwt-token")
    monkeypatch.setenv(cc.ACCOUNT_ENV, ACCOUNT)
    monkeypatch.setenv(cc.KEY_ENV, KEY)
    monkeypatch.setenv(cc.SECRET_ENV, SECRET)


def _run(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], routes: dict[str, Any]
) -> tuple[int, _FakeClient]:
    client = _FakeClient(routes)
    monkeypatch.setattr(cc, "httpx", _FakeHttpx(client))
    return cc.main(argv), client


# ---------------------------------------------------------------- could not run


def test_check_sends_nothing_and_passes_when_the_environment_is_complete(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, client = _run(monkeypatch, ["--check", "--execute"], {})
    assert code == 0
    assert client.calls == [], "--check must send nothing"


def test_check_fails_the_floor_when_a_variable_is_missing(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness that did not run is not one that passed (LED-013)."""
    monkeypatch.delenv(cc.SECRET_ENV)
    code, client = _run(monkeypatch, ["--check", "--execute"], {})
    assert code == 2
    assert client.calls == []


def test_the_read_mode_does_not_require_the_credential(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ The required set differs by MODE, and conflating them costs the check.

    Reading the connector's state needs only a way in. Demanding the Rentvine
    secret to run a GET would make the cheapest, safest call the one an
    operator cannot make until they have already found their credentials.
    """
    monkeypatch.delenv(cc.SECRET_ENV)
    monkeypatch.delenv(cc.KEY_ENV)
    code, _ = _run(
        monkeypatch,
        [],
        {"GET /credentials": _Resp(200, {"configured": False})},
    )
    assert code == 0


@pytest.mark.parametrize(
    ("status", "why"),
    [
        (503, "the connector is dark — the shipped state, not a defect"),
        (409, "one PMS per workspace — migration 0100's trigger"),
        (403, "the caller is not an admin"),
    ],
)
def test_a_healthy_refusal_is_could_not_run_and_never_a_finding(
    env: None, monkeypatch: pytest.MonkeyPatch, status: int, why: str
) -> None:
    """⚠ The load-bearing test in this file.

    Each of these is the product working. Exit 1 would read as "the routes are
    broken", and the first thing somebody reaches for after that verdict is
    the kill switch.
    """
    code, _ = _run(
        monkeypatch,
        ["--execute"],
        {"PUT /credentials": _Resp(status, {"detail": why})},
    )
    assert code == 2, f"a {status} is a healthy refusal, not a finding"


def test_the_backends_own_sentence_survives_to_the_operator(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The 409 names the rule AND the remedy; a status line names neither."""
    detail = (
        "This workspace is already connected to Buildium. Occupella supports one "
        "property-management system per workspace — disconnect Buildium first."
    )
    _run(monkeypatch, ["--execute"], {"PUT /credentials": _Resp(409, {"detail": detail})})
    assert "disconnect Buildium first" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["--execute", "--disconnect"],
        # ⚠ THE DISTINGUISHING INPUT, and the first cut did not have it.
        # Without the acknowledgement the purge guard below refuses this pair
        # anyway, so a test that stops there passes against a harness with no
        # mutual-exclusion check at all (gotcha 58 — the mutant DOES differ;
        # the hole was in the test). Acknowledged, the un-guarded version runs
        # the DISCONNECT and silently ignores --execute: the operator asked to
        # store a credential and got a mirror purge.
        ["--execute", "--disconnect", "--i-accept-the-mirror-purge"],
    ],
)
def test_connect_and_disconnect_together_is_refused(
    env: None, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    code, client = _run(monkeypatch, argv, {})
    assert code == 2
    assert client.calls == [], "neither half may run when the two are combined"


# ----------------------------------------------------------------- the findings


def test_a_leaked_secret_on_the_read_back_is_a_finding(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store returning 200 is not the connector working.

    Nothing else in this repo asserts, against a real backend, that what goes
    in encrypted comes back masked.
    """
    code, _ = _run(
        monkeypatch,
        ["--execute"],
        {
            "PUT /credentials": _Resp(200, _stored()),
            "GET /credentials": _Resp(200, _stored(api_secret=SECRET)),
        },
    )
    assert code == 1


def test_a_leaked_key_on_the_read_back_is_a_finding(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _ = _run(
        monkeypatch,
        ["--execute"],
        {
            "PUT /credentials": _Resp(200, _stored()),
            "GET /credentials": _Resp(200, _stored(api_key=KEY)),
        },
    )
    assert code == 1


def test_a_four_character_key_does_not_false_positive_on_its_own_last4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠ `api_key_last4` is returned BY DESIGN and the input model allows 4.

    So the shortest legal key equals its own last4, and a naive containment
    check would report every such read as a leak — a guard that cries wolf is
    one somebody switches off.
    """
    assert cc._leaked_fields('{"api_key_last4": "abcd"}', key="abcd", secret="") == []
    assert cc._leaked_fields('{"api_key": "abcdefgh"}', key="abcdefgh", secret="") == ["api_key"]


def test_a_secret_last4_in_the_response_is_a_finding(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The response model omits it deliberately: a secret has no safe half."""
    code, _ = _run(
        monkeypatch,
        ["--execute"],
        {
            "PUT /credentials": _Resp(200, _stored()),
            "GET /credentials": _Resp(200, _stored(api_secret_last4="rqpo")),
        },
    )
    assert code == 1


def test_a_stored_account_code_that_does_not_read_back_is_a_finding(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _ = _run(
        monkeypatch,
        ["--execute"],
        {
            "PUT /credentials": _Resp(200, _stored()),
            "GET /credentials": _Resp(200, _stored(account_code="somebody-else")),
        },
    )
    assert code == 1


def test_a_200_put_whose_read_back_says_nothing_is_stored_is_a_finding(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _ = _run(
        monkeypatch,
        ["--execute"],
        {
            "PUT /credentials": _Resp(200, _stored()),
            "GET /credentials": _Resp(200, {"configured": False}),
        },
    )
    assert code == 1


def test_rentvine_refusing_the_credential_is_a_finding_that_blames_the_credential(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """⚠ The route WORKED — it classified the failure and returned advice.

    Gotcha 102: telling somebody to reconnect a credential the vendor has
    already accepted sends them to redo something that works, so the harness
    has to say which half failed rather than reporting "connector broken".
    """
    advice = (
        "Rentvine accepted the key and refused this request. The key's ROLE in "
        "Rentvine does not allow reading properties — fix the role there."
    )
    code, _ = _run(
        monkeypatch,
        ["--execute"],
        {
            "PUT /credentials": _Resp(200, _stored()),
            "GET /credentials": _Resp(200, _stored()),
            "POST /test": _Resp(200, {"ok": False, "error": advice}),
        },
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "does not allow reading properties" in out, "the route's advice must survive"
    assert "not the connector" in out


def test_a_404_on_the_read_names_the_stale_build(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The router is mounted unconditionally, so a 404 is not the dark flag."""
    code, _ = _run(monkeypatch, [], {"GET /credentials": _Resp(404, {"detail": "Not Found"})})
    assert code == 1
    assert "before that landed" in capsys.readouterr().out


# ----------------------------------------------------------------- the passes


def test_the_happy_path_stores_reads_back_masked_and_tests(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, client = _run(
        monkeypatch,
        ["--execute"],
        {
            "PUT /credentials": _Resp(200, _stored()),
            "GET /credentials": _Resp(200, _stored()),
            "POST /test": _Resp(200, {"ok": True, "properties_count": 66}),
        },
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "66 properties" in out
    assert [verb for verb, _ in client.calls] == ["PUT", "GET", "POST"]


def test_the_pass_points_at_the_next_step(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A green connector is not a verified mirror — Phase 8 is the next gate."""
    _run(
        monkeypatch,
        ["--execute"],
        {
            "PUT /credentials": _Resp(200, _stored()),
            "GET /credentials": _Resp(200, _stored()),
            "POST /test": _Resp(200, {"ok": True, "properties_count": 66}),
        },
    )
    assert "rentvine_phase8.py" in capsys.readouterr().out


def test_the_secret_is_never_printed(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """⚠ Both directions, because the interesting one is the FAILURE path.

    The leak branch is the one place the harness holds the secret and the
    response text at the same time, so it is where an over-helpful diagnostic
    would print it.
    """
    for routes in (
        {
            "PUT /credentials": _Resp(200, _stored()),
            "GET /credentials": _Resp(200, _stored()),
            "POST /test": _Resp(200, {"ok": True, "properties_count": 66}),
        },
        {
            "PUT /credentials": _Resp(200, _stored()),
            "GET /credentials": _Resp(200, _stored(api_secret=SECRET)),
        },
    ):
        _run(monkeypatch, ["--execute"], routes)
        out = capsys.readouterr().out
        assert SECRET not in out
        assert KEY not in out


# -------------------------------------------------------------- the disconnect


def test_disconnect_without_the_acknowledgement_sends_nothing(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """⚠ It purges ACROSS the whole mirror, not only Rentvine-era rows.

    ``purge_rentvine_mirror`` delegates to the one ``PURGE_TABLES`` list (ADR
    0010 a1), so a workspace holding Buildium-era rows with no Buildium
    credentials row loses them here. Refusing must cost no request at all.
    """
    code, client = _run(monkeypatch, ["--disconnect"], {})
    assert code == 2
    assert client.calls == []
    assert "30 purge tables" in capsys.readouterr().out


def test_disconnect_reports_the_purge_count(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run(
        monkeypatch,
        ["--disconnect", "--i-accept-the-mirror-purge"],
        {"DELETE /credentials": _Resp(200, {"configured": False, "purged_rows": 412})},
    )
    assert code == 0
    assert "412" in capsys.readouterr().out


def test_a_disconnect_that_purged_nothing_measurable_is_a_finding(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0010 e1 requires the mirror to go with the credential.

    A missing count is not zero: it means nothing can say whether it did.
    """
    code, _ = _run(
        monkeypatch,
        ["--disconnect", "--i-accept-the-mirror-purge"],
        {"DELETE /credentials": _Resp(200, {"configured": False})},
    )
    assert code == 1


def test_a_disconnect_that_leaves_the_credential_is_a_finding(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, _ = _run(
        monkeypatch,
        ["--disconnect", "--i-accept-the-mirror-purge"],
        {"DELETE /credentials": _Resp(200, {"configured": True, "purged_rows": 0})},
    )
    assert code == 1


# ------------------------------------------------------------ structural guards


def test_the_credential_is_never_an_argparse_argument() -> None:
    """⚠ A guard a behavioural test cannot make.

    The standing constraint is that a secret goes in the environment and never
    in argv, because argv lands in shell history and in ``ps``. Adding a
    convenient ``--api-secret`` would pass every test above.
    """
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    flags = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    banned = {"--api-secret", "--api-key", "--secret", "--token", "--account-code"}
    assert not (flags & banned), f"credentials must not be argv: {flags & banned}"


def test_the_harness_reads_the_same_variables_the_probe_does() -> None:
    """Gotcha 73: an operator who ran the probe has already done this step.

    A second spelling of the same three facts is a second thing to get wrong
    at 1am, and the failure is a harness that reports COULD NOT RUN against a
    shell that is correctly configured.
    """
    probe = (REPO / "scripts" / "rentvine_probe.py").read_text(encoding="utf-8")
    for name in (cc.ACCOUNT_ENV, cc.KEY_ENV, cc.SECRET_ENV):
        assert name in probe, f"{name} is not what rentvine_probe.py reads"


# ------------------------------------------------------- the customer's path


"""⚠ WHAT `--discover` IS FOR, because the exit code alone hides it.

``account_code`` is OPTIONAL on the PUT — absent, the route discovers the
subdomain from the company name and the admin's email and probes each
candidate with the key pair. That is the path a real customer takes, and the
default ``--execute`` cannot reach it: it always SENDS a code and then asserts
the stored value equals what it sent.

So the properties below are about what is ABSENT from a request, what a
resolved subdomain is CHECKED against, and which outcomes leave the workspace
unconnected. None of the three is observable from a status code.
"""


def _discover_routes(stored_code: str | None = ACCOUNT, **over: Any) -> dict[str, Any]:
    body = _stored(account_code=stored_code)
    body.update(over)
    return {
        "PUT /credentials": _Resp(200, body),
        "GET /credentials": _Resp(200, body),
        "POST /test": _Resp(200, {"ok": True, "properties_count": 12}),
    }


def test_discover_omits_the_account_code_from_the_body(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one input that reaches the route's discovery branch.

    Sending ``account_code: ""`` or the operator's own value takes the SUPPLIED
    branch, so the mode would exercise the path it exists to avoid while
    reporting success — the defect wearing the fix's name.
    """
    code, client = _run(monkeypatch, ["--execute", "--discover"], _discover_routes())
    assert code == 0
    assert client.bodies == [{"api_key": KEY, "api_secret": SECRET}]
    assert "account_code" not in client.bodies[0]


def test_the_supplied_path_still_sends_the_account_code(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction (gotchas 38/51).

    A change that omitted the code in BOTH modes would pass the test above and
    silently delete the supplied path, which is the one an operator uses when
    discovery has already declined.
    """
    code, client = _run(monkeypatch, ["--execute"], _discover_routes())
    assert code == 0
    assert client.bodies[0]["account_code"] == ACCOUNT


def test_discover_without_execute_sends_nothing(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery happens INSIDE the store; there is no read-only form of it.

    A flag that quietly did nothing would read as one that ran — and the run
    it would be mistaken for is the one that answers the question.
    """
    code, client = _run(monkeypatch, ["--discover"], {})
    assert code == 2
    assert client.calls == []


def test_discover_does_not_require_the_account_code(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requiring the subdomain in the mode built to find it deletes the mode."""
    monkeypatch.delenv(cc.ACCOUNT_ENV)
    code, client = _run(monkeypatch, ["--execute", "--discover"], _discover_routes())
    assert code == 0
    assert client.bodies == [{"api_key": KEY, "api_secret": SECRET}]


def test_check_in_discover_mode_does_not_fail_on_a_missing_account_code(
    env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The floor has to match the MODE, or --check refuses a correct shell."""
    monkeypatch.delenv(cc.ACCOUNT_ENV)
    code, client = _run(monkeypatch, ["--check", "--execute", "--discover"], {})
    assert code == 0
    assert client.calls == []


def test_a_declined_discovery_is_not_a_finding_and_is_not_a_pass(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """⚠ Both halves matter, and they pull opposite ways.

    A 422 here is the route keeping its promise — no candidate authenticated,
    so it asked rather than storing a guess. Reporting that as a FINDING sends
    somebody after a connector that is working.

    But it also leaves the workspace UNCONNECTED, and exit 0 on this harness
    means "go and run Phase 8". Exit 0 would be the A2P harness's drift: one
    code for "connected" and "not connected".
    """
    routes = {"PUT /credentials": _Resp(422, {"detail": "Tell us your Rentvine subdomain."})}
    code, client = _run(monkeypatch, ["--execute", "--discover"], routes)
    assert code == 2
    out = capsys.readouterr().out
    assert "DISCOVERY DECLINED" in out
    assert "UNPROVEN" in out
    assert [verb for verb, _ in client.calls] == ["PUT"], "nothing may follow a decline"


def test_a_rejected_supplied_account_code_is_not_a_connector_finding(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A 422 on the supplied path is the operator's input, not a route defect.

    ``normalize_account_code`` refuses anything that is not a bare subdomain —
    pasting the login URL is the obvious way in. Calling that a FINDING points
    the operator at the connector when the thing to change is one word.
    """
    routes = {"PUT /credentials": _Resp(422, {"detail": "not a Rentvine subdomain"})}
    code, _ = _run(monkeypatch, ["--execute"], routes)
    assert code == 2
    out = capsys.readouterr().out
    assert "COULD NOT RUN" in out
    assert "FINDING" not in out


def test_a_discovered_code_that_contradicts_the_control_is_a_finding(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The load-bearing safety assertion in this mode.

    The route promises nothing is stored that was not verified. A stored
    subdomain the operator knows is wrong means either the credential
    authenticated against a host that is not this customer's, or ADR 0010's
    one-credential-one-tenant model does not hold. Either is a finding, and a
    harness that shrugged would leave the worker syncing from a stranger.
    """
    code, _ = _run(
        monkeypatch,
        ["--execute", "--discover"],
        _discover_routes(stored_code="someone-else"),
    )
    assert code == 1
    assert "FINDING" in capsys.readouterr().out


def test_a_discovered_blank_code_is_a_finding(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """200 with no subdomain stored is a blank host, not a success."""
    code, _ = _run(monkeypatch, ["--execute", "--discover"], _discover_routes(stored_code=""))
    assert code == 1


def test_a_resolved_code_with_no_control_says_it_is_unchecked(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """⚠ The control is what makes a hit evidence (gotchas 88/95/98).

    With ``RENTVINE_ACCOUNT_CODE`` unset, a resolved subdomain is the MODEL's
    word — the credential authenticated there, and the model says that
    identifies the tenant. The model is the thing most worth testing, so this
    must not print the same verdict as the checked run.
    """
    monkeypatch.delenv(cc.ACCOUNT_ENV)
    code, _ = _run(monkeypatch, ["--execute", "--discover"], _discover_routes())
    assert code == 0
    out = capsys.readouterr().out
    assert "UNCHECKED" in out


def test_a_resolved_code_with_a_control_says_it_was_checked(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other direction: a run that DID check must not read as unchecked."""
    code, _ = _run(monkeypatch, ["--execute", "--discover"], _discover_routes())
    assert code == 0
    out = capsys.readouterr().out
    assert "UNCHECKED" not in out
    assert cc.ACCOUNT_ENV in out


@pytest.mark.parametrize(
    ("routes", "why"),
    [
        (_discover_routes(), "a resolved run"),
        ({"PUT /credentials": _Resp(422, {"detail": "ask"})}, "a declined run"),
    ],
)
def test_both_discovery_outcomes_report_the_timing(
    env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    routes: dict[str, Any],
    why: str,
) -> None:
    """⚠ The one measurement nobody has taken.

    Candidates are probed concurrently, so the PUT waits for the slowest MISS
    either way — which means both outcomes carry the first observation of what
    a wrong subdomain costs. ``discovery.py``'s largest @unverified is exactly
    that, and a run that did not print the number cannot settle it.
    """
    _run(monkeypatch, ["--execute", "--discover"], routes)
    out = capsys.readouterr().out
    assert "elapsed" in out, why
    assert "PROBE_TIMEOUT_S" in out, "the number needs something to be compared against"


def test_discover_never_prints_the_secret(
    env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The new mode inherits the standing rule rather than being exempt."""
    _run(monkeypatch, ["--execute", "--discover"], _discover_routes())
    out = capsys.readouterr().out
    assert SECRET not in out
    assert KEY not in out


# ---------------------------------------------------------------- the runbook


def _runbook_block() -> str:
    """The 'Check the Rentvine CONNECTOR works' block, verbatim.

    Moved from CLAUDE.md §4 to ``docs/runbooks/rentvine.md`` on 2026-09-24;
    ``test_claude_md_size.py`` pins that CLAUDE.md still links the runbook.
    """
    text = (REPO / "docs" / "runbooks" / "rentvine.md").read_text(encoding="utf-8")
    start = text.index("### Check the Rentvine CONNECTOR works")
    end = text.index("\n### ", start + 1)
    return text[start:end]


def test_every_invocation_the_runbook_prints_is_one_the_script_accepts() -> None:
    """⚠ A RUNBOOK IS PROSE, AND PROSE CANNOT FAIL (gotchas 48/135/154).

    This block is what an operator copy-pastes at 1am against a deployment
    nobody here can reach, so a flag renamed in the script leaves a command
    that argparse refuses — and the operator reads that as the connector being
    broken, which is the one wrong conclusion this whole harness exists to
    prevent.

    ⚠ The check is on the ARGUMENT PARSER rather than on a hand-typed list:
    a second copy of the flag names is a second thing to keep in step, which
    is the drift being guarded against.
    """
    block = _runbook_block()
    known: set[str] = set()
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            known.add(str(node.args[0].value))
    assert known, "found no flags at all — the scan is pointed at the wrong thing"

    invocations = [
        line.strip() for line in block.splitlines() if line.strip().startswith("python scripts/")
    ]
    assert invocations, "the runbook block prints no invocation to check"

    for line in invocations:
        parts = line.split()
        script = REPO / parts[1]
        assert script.exists(), f"{line!r} names a script that does not exist"
        for flag in (p for p in parts[2:] if p.startswith("--")):
            assert flag in known, f"{line!r} passes {flag}, which argparse does not accept"


def test_the_runbook_names_the_discover_mode_and_what_makes_it_evidence() -> None:
    """⚠ The two claims that stop `--discover` being mis-read.

    Without the CONTROL an operator runs it with no ``RENTVINE_ACCOUNT_CODE``
    and reads a resolved subdomain as verified; without DECLINED being named as
    exit 2 they read the fallback working as the connector broken. Both are
    wrong conclusions the block is the only place to head off — a runbook that
    printed the command and neither caveat would be worse than none.
    """
    block = _runbook_block()
    assert "--discover" in block
    assert cc.ACCOUNT_ENV in block, "the control has to be named to be set"
    assert "DISCOVERY DECLINED" in block, "the exit-2 outcome must be named as healthy"
    assert "PROBE_TIMEOUT_S" in block, "the timing number needs something to compare against"
