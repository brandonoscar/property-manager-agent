"""A credential typed into a connect form never leaves in an error report or a
422 body.

⚠ MEASURED 2026-10-05, sentry-sdk 2.71.0, with ``configure_sentry`` as it was:

- a Buildium or Rentvine credentials save that SUCCEEDED put the client secret
  (or the API secret) in the sampled transaction's request body;
- one that FAILED with a 500 put it in the error event's frame variables, twice:
  FastAPI's raw ``body_bytes`` and the parsed model's repr. ``SecretStr`` did
  not help, because ``body_bytes`` is the raw request;
- a save missing a field answered 422 with the other fields echoed back, the
  secret among them (FastAPI's default handler returns each error's ``input``);
- an outbound call's query string went out whole in spans and breadcrumbs (the
  Google geocoder's ``key=``), and an inbound one in the request
  (the websocket's ``?token=``).

The Sentry half runs in a child process: ``sentry_sdk.init`` installs
process-wide hooks (logging, ``http.client``, threads) that would outlive this
test and run under every later one. Each check carries a positive control (the
transaction was captured, the frame was there, the query was recorded and
filtered), so "nothing leaked" cannot pass because nothing was captured. The
unprotected run proves the harness sees each leak the protected run must not
show (``test_codeexec_containment`` is the precedent: a helper that did not run
must not read as "contained").
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import helixis
from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.middleware import setup_middleware
from helixis.api.routes import buildium as buildium_routes
from helixis.api.routes import rentvine as rentvine_routes
from helixis.settings import Settings

_SRC = str(Path(helixis.__file__).resolve().parents[1])


def _plant() -> str:
    """A value that cannot occur by chance and is not in any source file, so a
    hit in a stack frame's source context cannot be mistaken for a leak."""
    return "PLANT" + secrets.token_hex(12)


def _settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": "test-secret"},
        memory={"enabled": False},
        api={"host": "127.0.0.1", "port": 8000},
    )


# ── the 422 body ─────────────────────────────────────────────────────


def _app(*, with_middleware: bool) -> TestClient:
    """The real credential routes, signed in, with or without our handlers."""
    app = FastAPI()
    if with_middleware:
        setup_middleware(app, _settings())
    app.include_router(buildium_routes.router, prefix="/api/v1")
    app.include_router(rentvine_routes.router, prefix="/api/v1")
    user = AuthenticatedUser(user_id="u", email="u@example.com", company_ids=["co"])
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


# Each one fails validation before the route body runs, so no database is
# reached: a missing field, and a value past a length limit.
_REJECTED: list[tuple[str, str, dict[str, str], list[str]]] = [
    (
        "buildium credentials, client_id missing",
        "/api/v1/buildium/credentials",
        {"client_secret": _plant(), "environment": "sandbox"},
        ["body", "client_id"],
    ),
    (
        "rentvine credentials, api_key missing",
        "/api/v1/rentvine/credentials",
        {"api_secret": _plant()},
        ["body", "api_key"],
    ),
    (
        "buildium webhook secret, too long",
        "/api/v1/buildium/webhook-secret",
        {"secret": "PLANT" + secrets.token_hex(300)},
        ["body", "secret"],
    ),
]


@pytest.mark.parametrize(
    ("path", "body", "loc"),
    [case[1:] for case in _REJECTED],
    ids=[case[0] for case in _REJECTED],
)
def test_a_rejected_credential_save_does_not_echo_the_secret(
    path: str, body: dict[str, str], loc: list[str]
) -> None:
    resp = _app(with_middleware=True).put(path, json=body)

    assert resp.status_code == 422
    for value in body.values():
        if value.startswith("PLANT"):
            assert value not in resp.text, "the 422 echoed a submitted secret"
    # What the web app reads (``validationFields`` takes ``loc``) is still there.
    detail = resp.json()["detail"]
    assert [entry["loc"] for entry in detail] == [loc]
    assert all(set(entry) <= {"loc", "msg", "type"} for entry in detail)


@pytest.mark.parametrize(
    ("path", "body", "loc"),
    [case[1:] for case in _REJECTED],
    ids=[case[0] for case in _REJECTED],
)
def test_fastapis_own_422_echoes_the_secret(
    path: str, body: dict[str, str], loc: list[str]
) -> None:
    """The control: without our handler the same request DOES echo it, so the
    test above is reaching the code path it claims to guard."""
    resp = _app(with_middleware=False).put(path, json=body)

    assert resp.status_code == 422
    planted = [value for value in body.values() if value.startswith("PLANT")]
    assert any(value in resp.text for value in planted)


# ── what reaches Sentry ──────────────────────────────────────────────

_CHILD = textwrap.dedent(
    """
    import json
    import os
    import secrets

    import httpx
    import sentry_sdk
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sentry_sdk.transport import Transport

    from helixis.api.middleware import setup_middleware
    from helixis.api.routes.buildium import BuildiumCredentialsInput, SetWebhookSecretRequest
    from helixis.api.routes.rentvine import RentvineCredentialsInput, RentvineWebhookSecretInput
    from helixis.observability import sentry_setup
    from helixis.settings import SentrySettings, Settings

    events = []

    class Capture(Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.payload.json is not None:
                    events.append(item.payload.json)

    settings = Settings(
        env="test", memory={"enabled": False}, api={"host": "127.0.0.1", "port": 8000}
    )
    settings.sentry = SentrySettings(
        enabled=True,
        dsn="https://public@example.invalid/1",
        traces_sample_rate=1.0,
        send_default_pii=False,
    )

    if os.environ["SENTRY_LEAK_MODE"] == "unprotected":
        # The SDK's defaults, as configure_sentry called it before 2026-10-05.
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=settings.sentry.dsn,
            transport=Capture,
            traces_sample_rate=1.0,
            profiles_sample_rate=0.0,
            send_default_pii=False,
            integrations=[FastApiIntegration(), StarletteIntegration(), AsyncioIntegration()],
        )
        sentry_setup._active = True
        configured = True
    else:
        real_init = sentry_sdk.init

        def init_with_capture(*args, **kwargs):
            kwargs["transport"] = Capture
            return real_init(*args, **kwargs)

        sentry_sdk.init = init_with_capture
        configured = sentry_setup.configure_sentry(settings)

    app = FastAPI()
    setup_middleware(app, settings)

    MODELS = {
        "buildium": BuildiumCredentialsInput,
        "buildium-webhook": SetWebhookSecretRequest,
        "rentvine": RentvineCredentialsInput,
        "rentvine-webhook": RentvineWebhookSecretInput,
    }

    def mount(name, model):
        async def saved(payload: model):
            return {"ok": True}

        async def failed(payload: model):
            raise RuntimeError("database unreachable")

        app.put("/" + name)(saved)
        app.put("/" + name + "/fails")(failed)

    for name, model in MODELS.items():
        mount(name, model)

    OUTBOUND = {}

    @app.get("/socket")
    async def socket(token: str = ""):
        raise RuntimeError("session check failed")

    @app.get("/geocode")
    async def geocode():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get(
                "https://maps.example.test/geocode/json",
                params={"address": "1 Main St", "key": OUTBOUND["key"]},
            )
        raise RuntimeError("geocoder returned nothing")

    def plant():
        return "PLANT" + secrets.token_hex(12)

    def body_for(name):
        if name == "buildium":
            return {"client_id": plant(), "client_secret": plant(), "environment": "sandbox"}
        if name == "rentvine":
            return {"api_key": plant(), "api_secret": plant()}
        return {"secret": plant()}

    client = TestClient(app, raise_server_exceptions=False)
    report = {"configured": configured, "cases": {}}

    def run(case, planted, method, path, **kwargs):
        events.clear()
        response = client.request(method, path, **kwargs)
        sentry_sdk.flush()
        report["cases"][case] = {
            "status": response.status_code,
            "response": response.text,
            "planted": planted,
            "events": list(events),
        }

    for name in MODELS:
        body = body_for(name)
        planted = [v for v in body.values() if v.startswith("PLANT")]
        run(name + " saved", planted, "PUT", "/" + name, json=body)
        body = body_for(name)
        planted = [v for v in body.values() if v.startswith("PLANT")]
        run(name + " fails", planted, "PUT", "/" + name + "/fails", json=body)

    token = plant()
    run("inbound query", [token], "GET", "/socket?token=" + token)
    OUTBOUND["key"] = plant()
    run("outbound query", [OUTBOUND["key"]], "GET", "/geocode")

    print("REPORT " + json.dumps(report))
    """
)

_MODELS = ("buildium", "buildium-webhook", "rentvine", "rentvine-webhook")


@cache
def _reports() -> dict[str, dict[str, Any]]:
    """Run the child once per mode, both at once, and return what each captured."""
    children = {}
    for mode in ("protected", "unprotected"):
        env = {**os.environ, "SENTRY_LEAK_MODE": mode}
        env["PYTHONPATH"] = os.pathsep.join(p for p in (_SRC, env.get("PYTHONPATH")) if p)
        children[mode] = subprocess.Popen(
            [sys.executable, "-c", _CHILD],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    reports = {}
    for mode, child in children.items():
        stdout, stderr = child.communicate(timeout=180)
        lines = [line for line in stdout.splitlines() if line.startswith("REPORT ")]
        # A child that died before reporting must fail here, never read as "no leak".
        assert lines, f"the {mode} child did not report (exit {child.returncode}): {stderr[-2000:]}"
        report: dict[str, Any] = json.loads(lines[-1].removeprefix("REPORT "))
        assert report["configured"] is True, f"Sentry did not come on in the {mode} child"
        reports[mode] = report
    return reports


def _leaks(node: Any, needles: list[str], at: str = "") -> Iterator[str]:
    """Where in an event any needle appears, as a path, in a key or a value."""
    if isinstance(node, dict):
        for key, value in node.items():
            if any(needle in str(key) for needle in needles):
                yield f"{at}.{key} (key)"
            yield from _leaks(value, needles, f"{at}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _leaks(value, needles, f"{at}[{index}]")
    elif isinstance(node, str) and any(needle in node for needle in needles):
        yield at


def _case(mode: str, name: str) -> dict[str, Any]:
    case: dict[str, Any] = _reports()[mode]["cases"][name]
    return case


def _typed(case: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [event for event in case["events"] if event.get("type") == kind]


def _errors(case: dict[str, Any], message: str) -> list[dict[str, Any]]:
    return [
        event
        for event in case["events"]
        if any(
            value.get("value") == message
            for value in (event.get("exception") or {}).get("values", [])
        )
    ]


def _values_of(node: Any, wanted: str) -> Iterator[Any]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == wanted:
                yield value
            yield from _values_of(value, wanted)
    elif isinstance(node, list):
        for value in node:
            yield from _values_of(value, wanted)


_CASES = [
    *(f"{name} saved" for name in _MODELS),
    *(f"{name} fails" for name in _MODELS),
    "inbound query",
    "outbound query",
]


@pytest.mark.parametrize("name", _CASES)
def test_no_planted_secret_reaches_any_sentry_event(name: str) -> None:
    case = _case("protected", name)
    leaked = sorted({path for event in case["events"] for path in _leaks(event, case["planted"])})
    assert not leaked, f"{name}: a secret reached Sentry at {leaked}"
    assert not any(value in case["response"] for value in case["planted"])


@pytest.mark.parametrize("model", _MODELS)
def test_a_saved_credential_was_traced_without_its_body(model: str) -> None:
    """The control for the 200s: the transaction WAS captured, and its request
    carries no body (the SDK leaves an empty string, annotated "removed by
    config")."""
    case = _case("protected", f"{model} saved")
    assert case["status"] == 200
    transactions = _typed(case, "transaction")
    assert transactions, "no transaction captured: the check above proved nothing"
    assert all(event["request"]["method"] == "PUT" for event in transactions)
    assert not [event["request"]["data"] for event in transactions if event["request"].get("data")]


@pytest.mark.parametrize("model", _MODELS)
def test_a_failed_save_was_reported_without_frame_variables(model: str) -> None:
    """The control for the 500s: the error WAS reported, with its stack, and no
    frame carries its variables."""
    case = _case("protected", f"{model} fails")
    assert case["status"] == 500
    errors = _errors(case, "database unreachable")
    assert errors, "no error event captured: the check above proved nothing"
    frames = list(_values_of(errors, "frames"))
    assert frames and all(frame_list for frame_list in frames)
    assert not [frame for frame_list in frames for frame in frame_list if "vars" in frame]


def test_query_strings_keep_their_names_and_lose_their_values() -> None:
    """The control for the query strings: each WAS recorded, filtered."""
    inbound = _errors(_case("protected", "inbound query"), "session check failed")
    assert inbound and inbound[0]["request"]["query_string"] == "token=[Filtered]"

    outbound = _case("protected", "outbound query")
    recorded = list(_values_of(outbound["events"], "http.query"))
    assert recorded, "the outbound call was not recorded: nothing was checked"
    assert set(recorded) == {"address=[Filtered]&key=[Filtered]"}


@pytest.mark.parametrize(
    ("name", "where"),
    [
        ("buildium saved", "request.data"),
        ("buildium fails", "vars"),
        ("rentvine-webhook fails", "vars.body_bytes"),
        ("outbound query", "http.query"),
    ],
)
def test_the_sdk_defaults_do_leak_so_the_harness_can_see_it(name: str, where: str) -> None:
    """⚠ The same requests under the SDK's defaults leak each secret where the
    fix stops it. If this fails after an SDK upgrade, the SDK's defaults
    changed: read what it now sends before retiring a row."""
    case = _case("unprotected", name)
    leaked = [path for event in case["events"] for path in _leaks(event, case["planted"])]
    assert any(where in path for path in leaked), f"{name}: expected a leak at {where}: {leaked}"
