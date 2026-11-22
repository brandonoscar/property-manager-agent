"""Phase 5 guards: the fail-closed receiver, the seam, and the normaliser.

The headline property is a NEGATIVE one — with no signature scheme configured,
which is the state this connector ships in, every delivery is refused and no
mirror row is written. A receiver that failed open on an unknown algorithm
would accept forged events into a landlord's books, and it would look exactly
like a working one.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.api.rate_limit import SlidingWindowCounter
from helixis.rentvine import client as rvclient
from helixis.rentvine import events, webhook_deliveries
from helixis.rentvine import signature as sig
from helixis.settings import RentvineSettings, Settings
from helixis.webhooks import rentvine_router

REPO = Path(__file__).resolve().parents[2]
_COMPANY = "11111111-1111-1111-1111-111111111111"
_ACCOUNT = "acme"
_SECRET = "the-signing-key"


def _capture_tool():
    """The derivation tool, loaded the way its own tests load it."""
    path = REPO / "scripts" / "rentvine_webhook_capture.py"
    spec = importlib.util.spec_from_file_location("_rv_capture", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rv_capture"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tool():
    return _capture_tool()


# ── the seam: unconfigured means REFUSE ──────────────────────────────────────


def test_no_scheme_refuses_every_delivery() -> None:
    """The state this connector ships in. It is a refusal, not a pass."""
    with pytest.raises(sig.SignatureUnverifiable, match="not configured"):
        sig.verify_signature(scheme=None, secret=_SECRET, headers={"x": "y"}, body=b"{}")


def test_a_partially_configured_scheme_also_refuses() -> None:
    """The dangerous middle state — somebody believes they turned it on."""
    half = RentvineSettings(
        webhook_signature_header="x-rentvine-signature",
        webhook_signature_algorithm="sha256",
        # payload / key_encoding / digest_encoding left blank
    )
    assert sig.scheme_from_settings(half) is None
    assert sig.scheme_from_settings(RentvineSettings()) is None


def test_a_complete_scheme_is_returned_and_an_invalid_value_is_not() -> None:
    good = RentvineSettings(
        webhook_signature_header="x-rentvine-signature",
        webhook_signature_algorithm="sha256",
        webhook_signature_payload="body",
        webhook_signature_key_encoding="utf8",
        webhook_signature_digest_encoding="base64",
    )
    scheme = sig.scheme_from_settings(good)
    assert scheme is not None and scheme.algorithm == "sha256"
    bad = good.model_copy(update={"webhook_signature_algorithm": "sha3_512"})
    assert sig.scheme_from_settings(bad) is None, "an invalid value must not build a scheme"


def test_a_timestamp_construction_without_its_header_is_rejected() -> None:
    """A construction whose input cannot be sourced is not a scheme."""
    with pytest.raises(ValueError, match="must name the header"):
        sig.SignatureScheme(
            header="x-sig",
            algorithm="sha256",
            payload="timestamp.body",
            key_encoding="utf8",
            digest_encoding="hex",
        )


# ── the seam and the deriver must agree (gotcha 28) ──────────────────────────


def test_every_scheme_the_tool_can_emit_is_one_this_module_can_execute(tool) -> None:
    """The failure this prevents is the worst outcome of the whole design: the
    probe settles the unknown, reports a scheme, and the receiver cannot run
    it — so deliveries stay refused and it looks like the probe was wrong."""
    assert set(tool._ALGORITHMS) == set(sig.ALGORITHMS)
    key_names = {name for name, _ in tool._key_variants("deadbeef")}
    assert key_names <= set(sig.KEY_ENCODINGS), key_names
    # Every input present (timestamp, path, query, host), so every construction
    # the tool knows is produced; a missing input only ever removes names.
    payload_names = {
        n for n, _ in tool._payload_variants(b"{}", "123", "/p", "a=1", "h.test", "tok")
    }
    assert payload_names == set(sig.PAYLOAD_CONSTRUCTIONS), payload_names
    # The location syntax and the timestamp set are vocabulary too: a tool
    # that wrote `qs:signature` would report a scheme the receiver reads as
    # a header name, and refuse forever.
    assert tool._QUERY_PREFIX == sig.QUERY_PREFIX
    assert tool._BODY_PREFIX == sig.BODY_PREFIX
    assert tool._TIMESTAMP_PAYLOADS == sig._NEEDS_TIMESTAMP
    import hashlib
    import hmac as _hmac

    enc_names = set(tool._encodings(_hmac.new(b"k", b"b", hashlib.sha256)))
    assert enc_names == set(sig.DIGEST_ENCODINGS), enc_names


def test_the_two_implementations_produce_identical_bytes(tool) -> None:
    """Not just the same NAMES — the same digest for every combination.

    ⚠ Agreement on vocabulary is not agreement on construction. Two modules
    could both offer ``timestamp.body`` and disagree about the separator, and
    the symptom would be a correct receiver refusing a correctly derived
    scheme.
    """
    import hashlib
    import hmac as _hmac

    body = b'{"leaseID":991,"objectType":"lease","action":"updated"}'
    timestamp, path = "1789000000", "/webhooks/rentvine/acme"
    # Percent-escapes and a plus, so `query_values` decoding is exercised.
    query, host = "timestamp=1789000000&token=a%20b+c", "your-api.onrender.com"
    token = "Zx9-mailgun-style-token"
    checked = 0
    for secret in ("plain-secret", "deadbeef", "aGVsbG8gd29ybGQ="):
        for key_name, key in tool._key_variants(secret):
            for payload_name, signed in tool._payload_variants(
                body, timestamp, path, query, host, token
            ):
                for algo in tool._ALGORITHMS:
                    theirs = tool._encodings(_hmac.new(key, signed, getattr(hashlib, algo)))
                    for enc_name, expected in theirs.items():
                        scheme = sig.SignatureScheme(
                            header="x-sig",
                            algorithm=algo,
                            payload=payload_name,
                            key_encoding=key_name,
                            digest_encoding=enc_name,
                            timestamp_header="x-ts" if "timestamp" in payload_name else "",
                        )
                        mine = sig.compute_digest(
                            scheme,
                            secret,
                            body,
                            timestamp=timestamp,
                            path=path,
                            query=query,
                            host=host,
                            token=token,
                        )
                        assert mine == expected, (scheme, secret)
                        checked += 1
    # ⚠ The count is DERIVED from the tool's own vocabularies, not typed. A
    # hardcoded number goes stale the day the tool gains an algorithm — and a
    # loop that silently exercised nothing would pass a bare "no assertion
    # failed" check identically (gotchas 49/100/101).
    expected_total = sum(
        len(tool._key_variants(secret))
        * len(tool._payload_variants(b"x", "1", "/p", "q=1", "h", "t"))
        * len(tool._ALGORITHMS)
        * len(sig.DIGEST_ENCODINGS)
        for secret in ("plain-secret", "deadbeef", "aGVsbG8gd29ybGQ=")
    )
    assert checked == expected_total, f"exercised {checked}, expected {expected_total}"
    assert checked > 100, "the cross-check space collapsed — check the vocabularies"


def test_the_tokenisers_agree(tool) -> None:
    """A receiver that tokenises differently refuses the scheme the deriver
    reported — and base64 PADDING is where the two most easily diverge."""
    for value in (
        "abc123",
        "sha256=deadbeef",
        "t=1789000000,v1=abcdef",
        "YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXoxMjM0NTY3OA==",
        "  spaced  ",
    ):
        assert sig.header_tokens(value) == tool._tokens(value), value


def test_the_query_constructions_agree_on_awkward_queries(tool) -> None:
    """Empty, blank values, repeats, escapes, a bare flag: where two copies of
    a query rule most easily disagree."""
    body, path, host = b'{"a":1}', "/webhooks/rentvine/acme", "h.test"
    for query in ("", "a=", "a=1&a=2", "x=%41&y=b+c", "flag", "&&t=1&"):
        produced = dict(tool._payload_variants(body, None, path, query, host))
        for name, signed in produced.items():
            assert sig.payload_bytes(name, body, path=path, query=query, host=host) == signed, (
                name,
                query,
            )
        assert {n for n in produced if n not in ("body", "path+body")}, query


def test_the_query_rules_agree(tool) -> None:
    """Which parameter is the signature, and what is left once it is removed.

    ⚠ The receiver removes it before computing and so must the tool, or a
    scheme derived over the signed query is refused by the receiver.
    """
    for query, location in (
        ("timestamp=1&signature=abc&token=aaa", "query:signature"),
        ("signature=x&a=1&signature=y", "query:signature"),
        ("sig%6Eature=x&b=2", "query:signature"),
        ("a=1&b=2", "x-rentvine-signature"),
        ("&&a=1&", "query:a"),
        ("", "query:signature"),
        ("Signature=x&b=2", "QUERY:Signature"),
    ):
        assert sig.unsigned_query(query, location) == tool._unsigned_query(query, location), (
            query,
            location,
        )
        assert sig.query_param_name(location) == tool._query_param_name(location), location


def test_the_body_and_sibling_rules_agree(tool) -> None:
    """Where a body field is, what sits beside a signature, and what a location
    reads: the receiver and the deriver must answer all three the same way."""
    body = json.dumps(
        {
            "auth": {"signature": "ab", "timestamp": 1791141294, "token": "T0k"},
            "data": {"workOrderID": "9", "items": [1, 2], "none": None},
        }
    ).encode()
    for path in ("auth.signature", "auth.timestamp", "data.items", "data.none", "x", ""):
        assert sig.body_field(body, path) == tool._body_field(body, path), path
    assert sig.body_field(b"not json", "a") is None and tool._body_field(b"not json", "a") is None
    for location in ("body:auth.signature", "body:sig", "query:signature", "x-rv-signature", "sig"):
        assert sig.sibling_location(location, "token") == tool._sibling(location, "token"), location
    headers, query = {"X-Sig": "h"}, "signature=q&token=aaa"
    for location in ("x-sig", "query:signature", "query:token", "body:auth.token", "body:nope"):
        assert sig.read_location(location, headers, query, body) == tool._read_location(
            location, headers, query, body
        ), location


def test_the_token_normalisers_agree(tool) -> None:
    """base36 has no fixed width or case, and Rentvine's value carries dashes."""
    for token in ("47TJD-33kl-0w", "000abc", "0", "00", "", "---", "abc", "AbC=="):
        for encoding in sig.DIGEST_ENCODINGS:
            assert sig.normalise_token(token, encoding) == tool._normalise_token(token, encoding), (
                token,
                encoding,
            )


def test_a_scheme_the_tool_derived_verifies_through_the_receiver(tool) -> None:
    """End to end on a SYNTHETIC signed delivery: sign it the way a vendor
    might, let the tool work out how, then check the receiver accepts it."""
    body = b'{"objectType":"work_order","action":"created","workOrderID":11316}'
    import hashlib
    import hmac as _hmac

    digest = _hmac.new(_SECRET.encode(), b"1789000000." + body, hashlib.sha256).hexdigest()
    headers = {
        "X-Rentvine-Signature": f"sha256={digest}",
        "X-Rentvine-Timestamp": "1789000000",
        "Content-Type": "application/json",
    }
    derived = tool.derive_signature_scheme(headers, body, _SECRET)
    assert derived["found"], derived
    scheme = sig.scheme_from_capture(derived)
    assert scheme.payload == "timestamp.body"
    assert scheme.timestamp_header == "X-Rentvine-Timestamp", (
        "the tool must report WHICH header the timestamp came from — a "
        "construction whose input cannot be sourced is unusable"
    )
    sig.verify_signature(scheme=scheme, secret=_SECRET, headers=headers, body=body)

    # and the negatives, in the same run — a one-sided check passes against a
    # verifier that accepts everything (gotchas 38/51).
    with pytest.raises(sig.SignatureUnverifiable):
        sig.verify_signature(scheme=scheme, secret="wrong", headers=headers, body=body)
    with pytest.raises(sig.SignatureUnverifiable):
        sig.verify_signature(scheme=scheme, secret=_SECRET, headers=headers, body=body + b" ")
    with pytest.raises(sig.SignatureUnverifiable, match="no Rentvine webhook signing key"):
        sig.verify_signature(scheme=scheme, secret=None, headers=headers, body=body)
    with pytest.raises(sig.SignatureUnverifiable, match="carried no"):
        sig.verify_signature(scheme=scheme, secret=_SECRET, headers={}, body=body)


def test_a_failed_derivation_does_not_become_a_scheme() -> None:
    """The tool's own docstring: a failed derivation is NOT proof the delivery
    was unsigned. Building a scheme from one would be a guess wearing the
    tool's authority."""
    with pytest.raises(ValueError, match="did not derive"):
        sig.scheme_from_capture({"found": False, "reason": "nothing matched"})


# ── the normaliser ───────────────────────────────────────────────────────────


def test_every_name_it_can_emit_already_exists_in_the_vocabulary() -> None:
    """Decision (c)'s whole claim, read on BOTH sides (gotcha 28). A name the
    existing table does not carry would route to ``other`` and land in a lane
    nobody watches."""
    from helixis.webhooks.categorization import EVENT_CATEGORY

    emitted = {
        f"{spec['event_prefix']}.{verb}"
        for spec in events.OBJECTS.values()
        for verb in ("Created", "Updated")
    }
    assert len(emitted) == 8, emitted
    missing = emitted - set(EVENT_CATEGORY)
    assert not missing, f"the normaliser emits names categorization.py does not know: {missing}"


def test_no_delete_verb_is_reachable() -> None:
    """``Rental.Deleted``'s handler DELETES the mirror row. T3 says the UI
    offers create and update; putting a destructive branch behind a verb we
    have not seen Rentvine send is gotcha 79's shape."""
    for spelling in ("delete", "deleted", "remove", "destroy", "archived"):
        with pytest.raises(events.UnmappableEvent):
            events.identify({"objectType": "property", "action": spelling})


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"objectType": "property", "action": "created"}, "Rental.Created"),
        ({"object": "Units", "verb": "UPDATE"}, "RentalUnit.Updated"),
        ({"eventName": "lease.updated"}, "Lease.Updated"),
        ({"eventName": "created.work_order"}, "WorkOrder.Created"),
        ({"entityType": "workOrder", "operation": "Changed"}, "WorkOrder.Updated"),
    ],
)
def test_plausible_spellings_all_resolve(payload: dict, expected: str) -> None:
    """⚠ @unverified — which spelling Rentvine actually sends. Accepting only
    one would drop real deliveries while looking like a working mapper."""
    obj, verb = events.identify(payload)
    assert f"{events.OBJECTS[obj]['event_prefix']}.{verb}" == expected


def test_an_unidentifiable_delivery_is_refused_not_named() -> None:
    with pytest.raises(events.UnmappableEvent, match="refusing to guess"):
        events.identify({"hello": "world", "amount": 12})


def test_a_delivery_with_no_record_id_is_refused() -> None:
    """An envelope with no entity id would enqueue work for no record."""
    with pytest.raises(events.UnmappableEvent, match="nothing to refresh"):
        events.to_envelope(
            {"objectType": "lease", "action": "updated"},
            company_id=_COMPANY,
            raw_body=b"{}",
        )


def test_the_record_id_is_found_at_the_root_or_one_level_down() -> None:
    for payload in (
        {"objectType": "lease", "action": "updated", "leaseID": 991},
        {"objectType": "lease", "action": "updated", "data": {"leaseID": 991}},
        {"objectType": "lease", "action": "updated", "record": {"leaseid": "991"}},
    ):
        assert events.entity_id(payload, "lease") == 991


def test_a_unit_delivery_without_its_property_is_refused() -> None:
    """Rentvine's only unit GET is nested under a property. Guessing the parent
    would refresh somebody else's unit."""
    payload = {"objectType": "unit", "action": "updated", "unitID": 77}
    envelope = events.to_envelope(payload, company_id=_COMPANY, raw_body=b"{}")
    with pytest.raises(events.UnmappableEvent, match="named no property"):
        events.refresh_target(payload, envelope)
    with_parent = {**payload, "propertyID": 4610}
    env2 = events.to_envelope(with_parent, company_id=_COMPANY, raw_body=b"{}")
    table, path, params = events.refresh_target(with_parent, env2)
    assert table == "helixis_units"
    assert params == {"unitID": 77, "propertyID": 4610}


def test_the_message_id_separates_companies_and_survives_a_redelivery() -> None:
    """Every Rentvine envelope carries the same synthetic ``AccountId: 0``, so
    without this two companies' identical events in one second would collide on
    the queue's unique index and the second would be silently dropped."""
    body = b'{"objectType":"lease","action":"updated","leaseID":991}'
    assert events.message_id("company-a", body) == events.message_id("company-a", body)
    assert events.message_id("company-a", body) != events.message_id("company-b", body)
    assert events.message_id("company-a", body) != events.message_id("company-a", body + b" ")


def test_the_synthetic_account_id_is_not_mistaken_for_an_identifier() -> None:
    envelope = events.to_envelope(
        {"objectType": "property", "action": "created", "propertyID": 4610},
        company_id=_COMPANY,
        raw_body=b"{}",
    )
    assert envelope["AccountId"] == 0
    assert envelope["PropertyId"] == 4610
    assert envelope["Pms"] == "rentvine"
    assert envelope["MessageId"].startswith("rentvine:")


# ── the receiver ─────────────────────────────────────────────────────────────


def _settings(**rentvine: Any) -> Settings:
    base = Settings()
    return base.model_copy(
        update={"rentvine": base.rentvine.model_copy(update={"enabled": True, **rentvine})}
    )


_LIVE_SCHEME = {
    "webhook_signature_header": "x-rentvine-signature",
    "webhook_signature_algorithm": "sha256",
    "webhook_signature_payload": "body",
    "webhook_signature_key_encoding": "utf8",
    "webhook_signature_digest_encoding": "base64",
}


def _row(secret_blob: str | None = "enc:key") -> dict[str, Any]:
    return {
        "company_id": _COMPANY,
        "account_code": _ACCOUNT,
        "encrypted_api_key": "enc:k",
        "api_key_last4": "1234",
        "encrypted_api_secret": "enc:s",
        "encrypted_webhook_secret": secret_blob,
        "base_url": "https://acme.rentvine.com/api/manager",
        "last_tested_at": None,
    }


def _fake_pool() -> Any:
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    pool = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    pool._conn = conn
    return pool


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch):
    """A one-route app plus every seam the route reaches, patched.

    ⚠ Builds its own FastAPI app rather than importing ``helixis.main`` — the
    real app pulls in the agents SDK, and a test that cannot run is not a test
    that passes (gotcha 60).
    """
    pool = _fake_pool()
    state: dict[str, Any] = {"pool": pool, "rows": [], "row": _row(), "upstream": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        # ⚠ Suffix, not equality: the client prefixes the account base URL,
        # so the real path is /api/manager/properties/4610.
        path = str(request.url.path)
        body = next((v for k, v in state["upstream"].items() if path.endswith(k)), None)
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, json=body)

    monkeypatch.setattr(rentvine_router, "get_pool", lambda: pool)
    monkeypatch.setattr(
        rentvine_router,
        "fetch_rentvine_row_by_account_code",
        AsyncMock(side_effect=lambda code: state["row"] if code == _ACCOUNT else None),
    )
    monkeypatch.setattr(rentvine_router, "decrypt_webhook_secret", lambda row, s: state["secret"])
    monkeypatch.setattr(
        rentvine_router,
        "decrypt_rentvine_settings_from",
        lambda row, s: RentvineSettings(account_code=_ACCOUNT, api_key="k", api_secret="s"),
    )
    monkeypatch.setattr(
        rentvine_router,
        "RentvineClient",
        lambda creds, **_kw: rvclient.RentvineClient(
            creds, client=httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=("k", "s"))
        ),
    )

    async def _capture_activity(_pool: Any, entry: Any) -> None:
        state["rows"].append(entry)

    monkeypatch.setattr(rentvine_router, "write_activity", _capture_activity)
    monkeypatch.setattr(rentvine_router.webhook_limiter, "check_by_ip", AsyncMock())
    monkeypatch.setattr(rentvine_router.webhook_limiter, "refund_by_ip", MagicMock())

    # The refused-delivery recorder is its own seam, so `pool._conn.execute`
    # below still means "a MIRROR write" and every "refused and writes
    # nothing" assertion keeps its meaning. Its SQL is tested on its own, and
    # once through this route with the real recorder.
    state["recorded"] = []

    async def _record(_pool: Any, **kw: Any) -> bool:
        state["recorded"].append(kw)
        return True

    monkeypatch.setattr(rentvine_router, "record_refused_delivery", _record)
    # A fresh budget per test: it is module state, and refusals in earlier
    # tests would otherwise use up this test's recordings.
    monkeypatch.setattr(
        rentvine_router,
        "_record_budget",
        SlidingWindowCounter(requests=webhook_deliveries.RECORDS_PER_HOUR, window_seconds=3600),
    )

    state["secret"] = _SECRET
    app = FastAPI()
    app.include_router(rentvine_router.router)
    state["client"] = TestClient(app, raise_server_exceptions=False)
    return state


def _sign(body: bytes, secret: str = _SECRET) -> dict[str, str]:
    scheme = sig.scheme_from_settings(RentvineSettings(**_LIVE_SCHEME))
    assert scheme is not None
    return {
        "x-rentvine-signature": sig.compute_digest(scheme, secret, body),
        "content-type": "application/json",
    }


_DELIVERY = json.dumps({"objectType": "property", "action": "updated", "propertyID": 4610}).encode()

_PROPERTY = {
    "propertyID": 4610,
    "name": "Redbud",
    "address": "500 Woodlane St",
    "city": "Denver",
    "stateID": "CO",
    "postalCode": "80202",
    "isActive": True,
    "propertyTypeID": 1,
}


def test_a_signed_delivery_lands_a_mirror_row(receiver, monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 5's pass criterion, positive half."""
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings(**_LIVE_SCHEME))
    receiver["upstream"]["/properties/4610"] = _PROPERTY

    resp = receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}", content=_DELIVERY, headers=_sign(_DELIVERY)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"event_name": "Rental.Updated", "refreshed": True}

    statements = [c.args[0] for c in receiver["pool"]._conn.execute.await_args_list]
    assert statements, "no mirror row was written"
    assert any("INSERT INTO helixis_properties" in s for s in statements)
    assert all("ON CONFLICT (company_id, buildium_id)" in s for s in statements)
    assert receiver["rows"], "the delivery was not recorded in the activity ledger"
    assert receiver["rows"][0].source_label == "Rentvine · Rental.Updated"


def test_an_unsigned_delivery_is_refused_and_writes_nothing(
    receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase 5's pass criterion, negative half — and the one that matters."""
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings(**_LIVE_SCHEME))
    receiver["upstream"]["/properties/4610"] = _PROPERTY

    resp = receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}",
        content=_DELIVERY,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 401
    assert receiver["pool"]._conn.execute.await_args_list == []
    assert receiver["rows"] == []


def test_a_forged_signature_is_refused_and_writes_nothing(
    receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings(**_LIVE_SCHEME))
    receiver["upstream"]["/properties/4610"] = _PROPERTY
    resp = receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}",
        content=_DELIVERY,
        headers=_sign(_DELIVERY, secret="attacker-key"),
    )
    assert resp.status_code == 401
    assert receiver["pool"]._conn.execute.await_args_list == []


def test_with_no_scheme_configured_even_a_correctly_signed_delivery_is_refused(
    receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE STATE THIS SHIPS IN, and the whole argument for the phase.

    A receiver that accepted here — because the bytes happen to line up under
    some scheme nobody derived — would be accepting on a guess.
    """
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings())
    receiver["upstream"]["/properties/4610"] = _PROPERTY
    resp = receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}", content=_DELIVERY, headers=_sign(_DELIVERY)
    )
    assert resp.status_code == 401
    assert receiver["pool"]._conn.execute.await_args_list == []


def test_a_company_with_no_signing_key_is_refused(
    receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NULL is the ordinary state for a customer who has not wired webhooks —
    and it still refuses, because there is nothing to verify against."""
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings(**_LIVE_SCHEME))
    receiver["secret"] = None
    resp = receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}", content=_DELIVERY, headers=_sign(_DELIVERY)
    )
    assert resp.status_code == 401
    assert receiver["pool"]._conn.execute.await_args_list == []


def test_an_unknown_or_ambiguous_account_is_acknowledged_not_challenged(
    receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loader returns None for BOTH, deliberately — routing a delivery to
    one of two companies is a cross-tenant leak. 200 so the sender's retry
    budget is not spent on something we will never want."""
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings(**_LIVE_SCHEME))
    resp = receiver["client"].post(
        "/webhooks/rentvine/somebody-else", content=_DELIVERY, headers=_sign(_DELIVERY)
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "unknown_account"}
    assert receiver["pool"]._conn.execute.await_args_list == []


def test_the_deployment_flag_is_checked_before_anything_else(
    receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = Settings()
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: base)
    resp = receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}", content=_DELIVERY, headers=_sign(_DELIVERY)
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "connector_disabled"}
    assert receiver["pool"]._conn.execute.await_args_list == []


def test_an_unmappable_delivery_is_acknowledged_and_dropped(
    receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mapping something unidentifiable onto a Buildium name would hand a
    handler a payload it will mis-read."""
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings(**_LIVE_SCHEME))
    body = json.dumps({"something": "else"}).encode()
    resp = receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}", content=body, headers=_sign(body)
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "unmapped"}
    assert receiver["pool"]._conn.execute.await_args_list == []


def test_a_failed_upstream_read_answers_200_and_leaves_the_mirror_alone(
    receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 500 would spend the sender's retry budget on work the next poll does
    anyway — polling is primary on Rentvine (Decision (c))."""
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings(**_LIVE_SCHEME))
    # no upstream body registered → the mock transport 404s
    resp = receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}", content=_DELIVERY, headers=_sign(_DELIVERY)
    )
    assert resp.status_code == 200
    assert resp.json()["refreshed"] is False
    assert receiver["pool"]._conn.execute.await_args_list == []
    assert receiver["rows"], "a failed refresh must still be recorded"
    assert "refresh" in receiver["rows"][0].description


def test_an_oversized_body_is_refused_before_the_hmac(
    receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings(**_LIVE_SCHEME))
    body = b'{"objectType":"property","action":"updated","propertyID":4610,"pad":"'
    body += b"x" * rentvine_router.MAX_BODY_BYTES + b'"}'
    resp = receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}", content=body, headers=_sign(body)
    )
    assert resp.status_code == 413


# ── signatures in the URL (measured 2026-10-04) ──────────────────────────────


def _base36(number: int) -> str:
    """Written out here, not imported: the test signs independently."""
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while number:
        number, rem = divmod(number, 36)
        out = digits[rem] + out
    return out or "0"


def _rentvine_style_target(body: bytes, secret: str = _SECRET, ts: str = "1789000000") -> str:
    """A delivery URL shaped like the one measured on Render: the signature
    in the query, base36 with dashes, between a timestamp and a token."""
    import hashlib
    import hmac as _hmac

    digest = _hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).digest()
    b36 = _base36(int.from_bytes(digest, "big"))
    dashed = f"{b36[:-16]}-{b36[-16:-8]}-{b36[-8:]}"
    return f"/webhooks/rentvine/{_ACCOUNT}?timestamp={ts}&signature={dashed}&token=aaa"


def test_a_query_signed_delivery_derives_and_then_verifies_through_the_receiver(
    tool, receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole loop on a SYNTHETIC delivery of the measured shape: the tool
    derives from the recorded target, its six lines configure the receiver,
    and the receiver accepts the same request.

    ⚠ This is the test the header-only design could never have passed: the
    tool looked at headers alone, and the receiver read no query string.
    """
    import base64

    target = _rentvine_style_target(_DELIVERY)
    capture = {
        "deliveries": [
            {
                "received_at": "T1",
                "method": "POST",
                "path": target,
                "body_base64": base64.b64encode(_DELIVERY).decode(),
                "headers": {"host": "testserver", "content-type": "application/json"},
            }
        ]
    }
    report = tool.summarise(capture, _SECRET)
    derived = report["deliveries"][0]["scheme"]
    assert derived["found"], derived
    assert derived["header"] == "query:signature"
    assert derived["digest_encoding"] == "base36"
    assert derived["timestamp_header"] == "query:timestamp"

    configured = {}
    for line in tool.scheme_env_lines(derived):
        name, _, value = line.partition("=")
        configured[name.removeprefix("HELIXIS_RENTVINE__").lower()] = value
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings(**configured))
    receiver["upstream"]["/properties/4610"] = _PROPERTY

    resp = receiver["client"].post(
        target, content=_DELIVERY, headers={"content-type": "application/json"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"event_name": "Rental.Updated", "refreshed": True}
    assert receiver["recorded"] == [], "an accepted delivery is not a refusal to keep"

    # ...and the negatives, through the same configured receiver.
    forged = _rentvine_style_target(_DELIVERY, secret="attacker-key")
    assert receiver["client"].post(forged, content=_DELIVERY).status_code == 401
    replayed = target.replace("timestamp=1789000000", "timestamp=1789000001")
    assert receiver["client"].post(replayed, content=_DELIVERY).status_code == 401
    unsigned = target.split("&signature=")[0]
    assert receiver["client"].post(unsigned, content=_DELIVERY).status_code == 401


def test_query_and_url_schemes_verify_over_the_unsigned_query() -> None:
    """The receiver must drop the signature parameter before computing, and
    hand the Host header to a URL construction; a tampered parameter fails."""
    import hashlib
    import hmac as _hmac

    body, path, host = b'{"a":1}', "/webhooks/rentvine/acme", "your-api.onrender.com"
    unsigned = "timestamp=1789000000&token=aaa"
    for payload, signed in (
        ("query_values", b"1789000000aaa"),
        ("url+body", f"https://{host}{path}?{unsigned}".encode() + body),
    ):
        digest = _hmac.new(_SECRET.encode(), signed, hashlib.sha256).hexdigest()
        query = f"timestamp=1789000000&signature={digest}&token=aaa"
        scheme = sig.SignatureScheme(
            header="query:signature",
            algorithm="sha256",
            payload=payload,
            key_encoding="utf8",
            digest_encoding="hex",
        )
        headers = {"Host": host}
        sig.verify_signature(
            scheme=scheme, secret=_SECRET, headers=headers, body=body, path=path, query=query
        )
        with pytest.raises(sig.SignatureUnverifiable, match="does not match"):
            sig.verify_signature(
                scheme=scheme,
                secret=_SECRET,
                headers=headers,
                body=body,
                path=path,
                query=query.replace("token=aaa", "token=aab"),
            )


def test_read_location_reads_a_header_in_any_case_or_a_query_parameter() -> None:
    headers = {"X-Rentvine-Signature": "h"}
    query = "timestamp=1&signature=q%2Bv&token="
    assert sig.read_location("x-rentvine-signature", headers, query) == "h"
    assert sig.read_location("query:signature", headers, query) == "q+v"
    assert sig.read_location("query:token", headers, query) == ""
    assert sig.read_location("query:missing", headers, query) is None
    assert sig.read_location("x-missing", headers, query) is None


def test_base36_tokens_compare_as_numbers() -> None:
    """Dashes, case and leading zeros cannot turn a right signature wrong."""
    assert sig.normalise_token("00Ab-c1-2", "base36") == "abc12"
    assert sig.normalise_token("000", "base36") == "0"
    assert sig.normalise_token("Ab-C", "hex") == "Ab-C", "only base36 is rewritten"


def test_a_query_construction_without_its_input_is_unverifiable() -> None:
    body = b"{}"
    for name, kwargs in (
        ("query", {}),
        ("query_values+body", {}),
        ("path?query", {"query": "a=1"}),
        ("url", {"query": "a=1", "path": "/p"}),
        ("timestamp", {}),
    ):
        with pytest.raises(sig.SignatureUnverifiable, match="needs"):
            sig.payload_bytes(name, body, **kwargs)
    with pytest.raises(sig.SignatureUnverifiable, match="unknown payload"):
        sig.payload_bytes("query+path", body, query="a=1", path="/p")


def test_a_query_location_must_name_its_parameter() -> None:
    with pytest.raises(ValueError, match="must name the header"):
        sig.SignatureScheme(
            header="query:",
            algorithm="sha256",
            payload="query",
            key_encoding="utf8",
            digest_encoding="base36",
        )


def test_a_query_scheme_refuses_a_request_without_the_parameter() -> None:
    scheme = sig.SignatureScheme(
        header="query:signature",
        algorithm="sha256",
        payload="query",
        key_encoding="utf8",
        digest_encoding="base36",
    )
    with pytest.raises(sig.SignatureUnverifiable, match="carried no query:signature"):
        sig.verify_signature(
            scheme=scheme, secret=_SECRET, headers={}, body=b"{}", query="timestamp=1"
        )


# ── Rentvine's real scheme: Mailgun's, inside the body (measured 2026-10-04) ──


def _mailgun_delivery(
    secret: str = _SECRET,
    *,
    ts: int = 1789000000,
    token: str = "x8Kq2mZt7Lw0pRv5nB3yJ6cH1dF9gS4aE2uT8iO0kM5eW7rQ1z",
    work_order_id: str = "9",
) -> tuple[str, bytes]:
    """A delivery shaped like the measured one: the key itself in the URL's
    ``signature``, a ``token=aaa`` beside it, and the real signature inside the
    body's ``auth`` block. Every value here is synthetic."""
    import hashlib
    import hmac as _hmac

    signature = _hmac.new(secret.encode(), f"{ts}{token}".encode(), hashlib.sha256).hexdigest()
    body = json.dumps(
        {
            "data": {"workOrderID": work_order_id, "propertyID": "4610"},
            "event": {
                "eventType": "maintenance::workOrder::updated",
                "objectTypeID": "16",
                "objectID": work_order_id,
            },
            "auth": {"signature": signature, "timestamp": ts, "token": token},
        }
    ).encode()
    target = f"/webhooks/rentvine/{_ACCOUNT}?timestamp={ts}&signature={secret}&token=aaa"
    return target, body


_WORK_ORDER = {
    "workOrderID": 9,
    "workOrderNumber": "9",
    "propertyID": 4610,
    "description": "Synthetic",
    "workOrderStatusID": 1,
}


def test_rentvines_real_scheme_derives_and_then_verifies_through_the_receiver(
    tool, receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured delivery, synthesised: the tool finds the body signature
    (not the URL's key), says where the key itself was sent, and its six lines
    let the receiver accept the delivery and re-read the work order."""
    import base64

    target, body = _mailgun_delivery()
    capture = {
        "deliveries": [
            {
                "received_at": "T1",
                "method": "POST",
                "path": target,
                "body_base64": base64.b64encode(body).decode(),
                "headers": {"host": "testserver", "content-type": "application/json"},
            }
        ]
    }
    derived = tool.summarise(capture, _SECRET)["deliveries"][0]["scheme"]
    assert derived["found"], derived
    assert (derived["header"], derived["payload"], derived["digest_encoding"]) == (
        "body:auth.signature",
        "timestamp+token",
        "hex",
    )
    assert derived["timestamp_header"] == "body:auth.timestamp"
    assert derived["key_sent_at"] == ["query:signature"], "the run must say the key is in the URL"

    configured = {}
    for line in tool.scheme_env_lines(derived):
        name, _, value = line.partition("=")
        configured[name.removeprefix("HELIXIS_RENTVINE__").lower()] = value
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings(**configured))
    receiver["upstream"]["/maintenance/work-orders/9"] = _WORK_ORDER

    resp = receiver["client"].post(target, content=body)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"event_name": "WorkOrder.Updated", "refreshed": True}
    assert receiver["recorded"] == []

    # The negatives, through the same receiver: another key, a token that is
    # not the one signed, and a body with no auth block at all.
    _, forged = _mailgun_delivery("attacker-key")
    assert receiver["client"].post(target, content=forged).status_code == 401
    tampered = body.replace(b"x8Kq2mZt7Lw0", b"x8Kq2mZt7Lw1")
    assert tampered != body
    assert receiver["client"].post(target, content=tampered).status_code == 401
    stripped = json.dumps({"data": {"workOrderID": "9"}}).encode()
    assert receiver["client"].post(target, content=stripped).status_code == 401


def test_a_body_field_reads_numbers_as_the_text_that_arrived() -> None:
    body = b'{"auth": {"timestamp": 1791141294, "token": "t"}, "f": 1.10, "l": [1], "b": true}'
    assert sig.body_field(body, "auth.timestamp") == "1791141294"
    assert sig.body_field(body, "f") == "1.10", "a float signs as sent, not as 1.1"
    assert sig.body_field(body, "l") is None
    assert sig.body_field(body, "b") is None
    assert sig.body_field(body, "auth.missing") is None
    assert sig.body_field(b"\xff\xfe not json", "auth") is None


def test_the_token_is_found_beside_the_signature() -> None:
    assert sig.sibling_location("body:auth.signature", "token") == "body:auth.token"
    assert sig.sibling_location("body:signature", "token") == "body:token"
    assert sig.sibling_location("query:signature", "token") == "query:token"
    assert sig.sibling_location("x-rv-signature", "token") == "x-rv-token"


def test_timestamp_plus_token_needs_both_and_an_empty_body_location_is_refused() -> None:
    with pytest.raises(sig.SignatureUnverifiable, match="needs the token"):
        sig.payload_bytes("timestamp+token", b"{}", timestamp="1")
    with pytest.raises(sig.SignatureUnverifiable, match="needs a timestamp"):
        sig.payload_bytes("timestamp+token", b"{}", token="t")
    assert sig.payload_bytes("timestamp+token", b"{}", timestamp="17", token="ab") == b"17ab"
    with pytest.raises(ValueError, match="must name the header"):
        sig.SignatureScheme(
            header="body:",
            algorithm="sha256",
            payload="body",
            key_encoding="utf8",
            digest_encoding="hex",
        )


def test_rentvines_measured_event_name_resolves() -> None:
    """``event.eventType = maintenance::workOrder::updated``: nested, and the
    object between the ends. Either alone left every real delivery unmapped."""
    payload = {
        "data": {"workOrderID": "9"},
        "event": {"eventType": "maintenance::workOrder::updated", "objectTypeID": "16"},
        "auth": {"signature": "s", "timestamp": 1, "token": "t"},
    }
    assert events.identify(payload) == ("work_order", "Updated")
    assert events.entity_id(payload, "work_order") == 9
    assert events.identify({"eventType": "maintenance::workOrder::created"}) == (
        "work_order",
        "Created",
    )
    # Two objects between the ends is ambiguous, and a verb in the middle is
    # not where Rentvine puts one.
    for name in ("maintenance::workOrder::lease::updated", "workOrder::updated::maintenance"):
        with pytest.raises(events.UnmappableEvent):
            events.identify({"event": {"eventType": name}})
    # An OBJECT under a combined key is not a name, however its text reads.
    with pytest.raises(events.UnmappableEvent):
        events.identify({"event": {"note": "workOrder.updated"}, "name": {"x": 1}})


# ── refused deliveries are kept (migration 0106) ─────────────────────────────


def test_a_refused_delivery_is_kept_verbatim_for_the_analysis(
    receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state this ships in refuses everything — and now keeps it, so the
    scheme can be derived from what Rentvine really sent."""
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings())
    query = "timestamp=1789000000&signature=ab-c%2Bd&token=aaa"
    resp = receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}?{query}",
        content=_DELIVERY,
        headers={"content-type": "application/json", "x-extra": "kept"},
    )
    assert resp.status_code == 401
    assert receiver["pool"]._conn.execute.await_args_list == [], "no mirror write"
    assert len(receiver["recorded"]) == 1
    kept = receiver["recorded"][0]
    assert kept["company_id"] == _COMPANY
    assert kept["method"] == "POST"
    assert kept["path"] == f"/webhooks/rentvine/{_ACCOUNT}"
    assert kept["query"] == query, "the query must be kept RAW, escapes and all"
    assert kept["body"] == _DELIVERY
    assert kept["headers"]["x-extra"] == "kept"
    assert "not configured" in kept["reason"]
    assert kept["secret"] == _SECRET, "the recorder needs the key to withhold copies of it"


def test_only_refusals_for_a_known_account_are_kept(
    receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown account has no company to file it under, the flag-off branch
    reads nothing, and an accepted delivery is not a refusal."""
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings(**_LIVE_SCHEME))
    receiver["upstream"]["/properties/4610"] = _PROPERTY
    receiver["client"].post(
        "/webhooks/rentvine/somebody-else", content=_DELIVERY, headers=_sign(_DELIVERY)
    )
    ok = receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}", content=_DELIVERY, headers=_sign(_DELIVERY)
    )
    assert ok.status_code == 200
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: Settings())
    receiver["client"].post(f"/webhooks/rentvine/{_ACCOUNT}", content=_DELIVERY)
    assert receiver["recorded"] == []

    # ...and a forgery for a known account IS kept: once a scheme is set, a
    # refusal is either a forgery or a scheme that is wrong for some events.
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings(**_LIVE_SCHEME))
    forged = receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}",
        content=_DELIVERY,
        headers=_sign(_DELIVERY, secret="attacker-key"),
    )
    assert forged.status_code == 401
    assert [r["reason"] for r in receiver["recorded"]] == ["signature does not match"]


def test_recording_is_capped_per_company(receiver, monkeypatch: pytest.MonkeyPatch) -> None:
    """The IP limit admits 1,200 a minute; the table must not take that."""
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        rentvine_router, "_record_budget", SlidingWindowCounter(requests=2, window_seconds=3600)
    )
    for _ in range(3):
        resp = receiver["client"].post(f"/webhooks/rentvine/{_ACCOUNT}", content=_DELIVERY)
        assert resp.status_code == 401, "over the cap is still a refusal"
    assert len(receiver["recorded"]) == 2


def test_the_real_recorder_writes_only_its_own_table_and_never_breaks_the_401(
    receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once through the route with the REAL recorder: its two statements touch
    the deliveries table and nothing else, and a database failure while
    recording still answers 401, not 500."""
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        rentvine_router, "record_refused_delivery", webhook_deliveries.record_refused_delivery
    )
    resp = receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}?timestamp=1&signature={_SECRET}&token=aaa",
        content=_DELIVERY,
        headers={"x-copy": _SECRET},
    )
    assert resp.status_code == 401
    calls = receiver["pool"]._conn.execute.await_args_list
    statements = [c.args[0] for c in calls]
    assert len(statements) == 2, statements
    stored_query, stored_headers = calls[0].args[4], json.loads(calls[0].args[5])
    assert _SECRET not in stored_query and _SECRET not in json.dumps(stored_headers), (
        "the signing key must never be stored, wherever it was sent"
    )
    assert stored_query == "timestamp=1&signature=[redacted]&token=aaa"
    assert all("public.helixis_rentvine_webhook_deliveries" in s for s in statements)
    assert statements[0].lstrip().startswith("INSERT")
    assert statements[1].lstrip().startswith("DELETE")

    receiver["pool"]._conn.execute = AsyncMock(side_effect=RuntimeError("db down"))
    resp = receiver["client"].post(f"/webhooks/rentvine/{_ACCOUNT}?signature=x", content=_DELIVERY)
    assert resp.status_code == 401


# ── the Buildium path is untouched ───────────────────────────────────────────


def test_the_rentvine_receiver_touches_no_buildium_routing() -> None:
    """ "No Buildium event's routing changes" — read as a property of the SOURCE
    rather than trusted. A behavioural test of the Rentvine route cannot see
    it reaching into the Buildium dispatch chain."""
    import ast

    tree = ast.parse(Path(rentvine_router.__file__).read_text())
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    forbidden = {
        "helixis.webhooks.categorization",
        "helixis.webhooks.dispatch",
        "helixis.webhooks.routing",
        "helixis.webhooks.queue",
        "helixis.webhooks.handlers.enrichment_registry",
    }
    assert not (imported & forbidden), (
        f"the Rentvine receiver reaches into the Buildium pipeline: {imported & forbidden}. "
        f"Those handlers fetch from BUILDIUM, which a Rentvine company has no "
        f"credentials for — see this module's docstring."
    )


def test_the_categorization_table_gained_no_rentvine_entries() -> None:
    """Decision (c) reuses the existing names; a new entry would mean it didn't."""
    from helixis.webhooks.categorization import EVENT_CATEGORY

    assert not [k for k in EVENT_CATEGORY if "rentvine" in k.lower()]


def test_the_route_is_mounted_without_the_api_prefix() -> None:
    """The customer pastes this URL into the Rentvine UI by hand. A route
    nothing mounts is the ``comms_triage`` failure (gotcha 62) — and a
    behavioural test of the router object cannot see that main.py skipped it."""
    import ast

    tree = ast.parse((REPO / "src/helixis/main.py").read_text())
    included = {
        node.args[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "include_router"
        and node.args
        and isinstance(node.args[0], ast.Name)
    }
    assert "rentvine_webhooks_router" in included
    # and with no prefix — the URL is ${PUBLIC_BASE_URL}/webhooks/rentvine/...
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "include_router"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "rentvine_webhooks_router"
        ):
            assert not node.keywords, "the Rentvine webhook route takes no prefix"


# ── holes the mutation battery found, not review ─────────────────────────────


def test_an_authentic_delivery_gives_the_abuse_budget_its_slot_back(
    receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gotcha 114's shape: the limiter's key is the SENDER's ip and every
    company's deliveries arrive from Rentvine's egress, so it is one shared
    bucket for the whole deployment. Without the refund, a third customer's
    real events get shed by a ceiling meant for forgeries."""
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings(**_LIVE_SCHEME))
    receiver["upstream"]["/properties/4610"] = _PROPERTY
    receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}", content=_DELIVERY, headers=_sign(_DELIVERY)
    )
    assert rentvine_router.webhook_limiter.refund_by_ip.called


def test_a_refused_delivery_does_not_get_its_slot_back(
    receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction, and the one that matters: refunding traffic we
    could not verify would hand the abuse budget straight to an attacker."""
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings(**_LIVE_SCHEME))
    receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}",
        content=_DELIVERY,
        headers=_sign(_DELIVERY, secret="attacker-key"),
    )
    assert not rentvine_router.webhook_limiter.refund_by_ip.called
    # ...and neither does an unknown account, which has verified nothing either.
    receiver["client"].post(
        "/webhooks/rentvine/somebody-else", content=_DELIVERY, headers=_sign(_DELIVERY)
    )
    assert not rentvine_router.webhook_limiter.refund_by_ip.called


def test_a_record_with_no_usable_id_is_not_reported_as_refreshed(
    receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``refresh_one`` returns whether a row LANDED, not whether a request was
    made. A record whose id does not coerce is skipped by the same rule the
    full sync uses — reporting it as refreshed would be a fabricated success
    on the one signal an operator reads."""
    monkeypatch.setattr(rentvine_router, "get_settings", lambda: _settings(**_LIVE_SCHEME))
    receiver["upstream"]["/properties/4610"] = {**_PROPERTY, "propertyID": "not-a-number"}
    resp = receiver["client"].post(
        f"/webhooks/rentvine/{_ACCOUNT}", content=_DELIVERY, headers=_sign(_DELIVERY)
    )
    assert resp.status_code == 200
    assert resp.json()["refreshed"] is False
    assert receiver["pool"]._conn.execute.await_args_list == []


@pytest.mark.asyncio
async def test_an_ambiguous_single_object_envelope_is_refused_not_guessed() -> None:
    """The success envelope is undocumented, so BOTH plausible shapes are
    accepted — and a third RAISES rather than picking one, because a record
    read from the wrong wrapper would blank a mirror row that had real values."""
    shapes = {
        "/bare": {"propertyID": 4610, "name": "Redbud"},
        "/wrapped": {"property": {"propertyID": 4610, "name": "Redbud"}},
        "/ambiguous": {
            "property": {"propertyID": 4610},
            "previous": {"propertyID": 4609},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        for key, body in shapes.items():
            if str(request.url.path).endswith(key):
                return httpx.Response(200, json=body)
        return httpx.Response(404)

    client = rvclient.RentvineClient(
        RentvineSettings(account_code=_ACCOUNT, api_key="k", api_secret="s"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=("k", "s")),
    )
    monkeypatched = {
        "GET /bare": frozenset(),
        "GET /wrapped": frozenset(),
        "GET /ambiguous": frozenset(),
    }
    from helixis.rentvine import transport

    transport.QUERY_PARAMS.update(monkeypatched)  # type: ignore[attr-defined]
    try:
        assert (await client.get_object("/bare", id_field="propertyID"))["name"] == "Redbud"
        assert (await client.get_object("/wrapped", id_field="propertyID"))["name"] == "Redbud"
        with pytest.raises(rvclient.RentvineAPIError, match="refusing to guess"):
            await client.get_object("/ambiguous", id_field="propertyID")
    finally:
        for key in monkeypatched:
            transport.QUERY_PARAMS.pop(key, None)
        await client.aclose()


# ── the webhook secret's three states ────────────────────────────────────────


def test_the_webhook_secret_has_three_states_and_two_of_them_are_none() -> None:
    """No column value is the ORDINARY state (polling is primary, so a customer
    who never wired webhooks has a working product). A value that will not
    decrypt is gotcha 11's shape. Both answer None — the receiver's response is
    the same either way — and the log is what tells a human them apart.

    ⚠ ``None`` rather than ``""``: the annotation says ``str | None``, and a
    caller written later as ``if secret is None`` must not read an empty string
    as a key.
    """
    from cryptography.fernet import Fernet

    from helixis.rentvine import credentials as creds

    # A real Fernet key so the cipher constructs — the same shape
    # test_rentvine_credentials.py uses. Test-only, generated here.
    base = Settings().model_copy(update={"data_key": Fernet.generate_key().decode()})
    assert creds.decrypt_webhook_secret({"company_id": _COMPANY}, base) is None
    assert creds.decrypt_webhook_secret({"encrypted_webhook_secret": None}, base) is None
    assert creds.decrypt_webhook_secret({"encrypted_webhook_secret": ""}, base) is None
    assert creds.decrypt_webhook_secret({"encrypted_webhook_secret": "not-fernet"}, base) is None, (
        "an undecryptable value must not surface as a key"
    )

    from helixis.security import CredentialCipher

    blob = CredentialCipher(base).encrypt(_SECRET)
    assert creds.decrypt_webhook_secret({"encrypted_webhook_secret": blob}, base) == _SECRET
