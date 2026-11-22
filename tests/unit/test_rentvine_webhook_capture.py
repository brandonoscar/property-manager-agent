"""Can the capture harness actually DERIVE a signature scheme from one delivery?

Phase 5 is blocked on a fact no document supplies: how Rentvine signs a
webhook. The harness claims one real delivery plus the account's signing key
settles it. That claim is testable without a network — synthesise a delivery
signed a known way, and check the deriver reports exactly that way.

The failure that would matter most is a FALSE NEGATIVE. "No scheme found"
reads as *this delivery was not signed*, which would send someone to argue
with Rentvine support about a feature that works. So the tests below pin the
recall cases hard: unconventional header names, base64 digests, prefixed
values, timestamp-mixed payloads, and encoded keys.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

SECRET = "rv_whsec_2f8c41a9e7b34d5f"
BODY = b'{"event":"property.updated","propertyID":4471,"at":"2026-09-17T12:00:00Z"}'


def _module():
    path = REPO / "scripts" / "rentvine_webhook_capture.py"
    spec = importlib.util.spec_from_file_location("_rv_capture", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rv_capture"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cap():
    return _module()


def _sign(body: bytes, algo: str = "sha256", key: bytes | None = None) -> str:
    return hmac.new(key or SECRET.encode(), body, getattr(hashlib, algo)).hexdigest()


# --------------------------------------------------------------------------
# the happy path, and the variants a real sender might use
# --------------------------------------------------------------------------


def test_it_derives_a_plain_sha256_hex_body_signature(cap):
    headers = {"X-Rentvine-Signature": _sign(BODY), "Content-Type": "application/json"}
    out = cap.derive_signature_scheme(headers, BODY, SECRET)
    assert out["found"] is True
    assert out["header"] == "X-Rentvine-Signature"
    assert out["algorithm"] == "sha256"
    assert out["payload"] == "body"
    assert out["digest_encoding"] == "hex"


def test_it_finds_a_signature_under_an_unconventional_header_name(cap):
    """The header name is unknown, so the search must not depend on it.

    A deriver that only looked at ``*signature*`` headers would report NOT
    FOUND on a delivery it could have explained.
    """
    headers = {"X-RV-Verify": _sign(BODY)}
    out = cap.derive_signature_scheme(headers, BODY, SECRET)
    assert out["found"] is True
    assert out["header"] == "X-RV-Verify"


def test_it_derives_a_base64_digest(cap):
    mac = hmac.new(SECRET.encode(), BODY, hashlib.sha256)
    headers = {"X-Signature": base64.b64encode(mac.digest()).decode()}
    out = cap.derive_signature_scheme(headers, BODY, SECRET)
    assert out["found"] is True
    assert out["digest_encoding"] == "base64"


def test_it_derives_a_prefixed_value(cap):
    """``sha256=<hex>`` is a common shape; the digest is a token inside it."""
    headers = {"X-Signature": f"sha256={_sign(BODY)}"}
    out = cap.derive_signature_scheme(headers, BODY, SECRET)
    assert out["found"] is True
    assert out["algorithm"] == "sha256"


def test_it_derives_a_timestamped_payload(cap):
    ts = "1789598400"
    signed = hmac.new(SECRET.encode(), ts.encode() + b"." + BODY, hashlib.sha256).hexdigest()
    headers = {"X-Signature": signed, "X-Timestamp": ts}
    out = cap.derive_signature_scheme(headers, BODY, SECRET)
    assert out["found"] is True
    assert out["payload"] == "timestamp.body"
    assert out["timestamp_header_used"] is True


def test_it_derives_when_the_key_itself_is_hex_encoded(cap):
    """A key shown as hex is sometimes meant to be DECODED before use.

    Trying only the utf-8 bytes would report no match — indistinguishable from
    "not an HMAC", which is the false negative that costs the most.
    """
    hex_key = "a3f1" * 8
    signed = hmac.new(bytes.fromhex(hex_key), BODY, hashlib.sha256).hexdigest()
    out = cap.derive_signature_scheme({"X-Signature": signed}, BODY, hex_key)
    assert out["found"] is True
    assert out["key_encoding"] == "hex_decoded"


def test_it_derives_sha1_and_sha512_too(cap):
    for algo in ("sha1", "sha512"):
        out = cap.derive_signature_scheme({"X-Sig": _sign(BODY, algo)}, BODY, SECRET)
        assert out["found"] is True, algo
        assert out["algorithm"] == algo


# --------------------------------------------------------------------------
# the trap the whole design turns on
# --------------------------------------------------------------------------


def test_a_reserialised_body_breaks_every_candidate(cap):
    """THIS is why the capture stores raw bytes and never parses them.

    Re-serialising the JSON changes whitespace, so the digest stops matching.
    The analysis would then report "no scheme found" — reading as *Rentvine
    does not sign* rather than *we destroyed the input*. The test exists to
    show the failure is real, not theoretical.
    """
    headers = {"X-Signature": _sign(BODY)}
    assert cap.derive_signature_scheme(headers, BODY, SECRET)["found"] is True

    round_tripped = json.dumps(json.loads(BODY)).encode()
    assert round_tripped != BODY, "pick a body whose re-serialisation actually differs"
    assert cap.derive_signature_scheme(headers, round_tripped, SECRET)["found"] is False


# --------------------------------------------------------------------------
# declining honestly
# --------------------------------------------------------------------------


def test_a_wrong_key_reports_not_found_without_claiming_it_was_unsigned(cap):
    out = cap.derive_signature_scheme({"X-Signature": _sign(BODY)}, BODY, "the-wrong-key")
    assert out["found"] is False
    # The message must not let a reader conclude the delivery was unsigned.
    assert "NOT proof" in out["note"]
    assert out["digest_shaped_headers"] == ["X-Signature"]


def test_no_secret_and_no_headers_decline_rather_than_search(cap):
    assert cap.derive_signature_scheme({"X-Signature": "abc"}, BODY, "")["found"] is False
    assert cap.derive_signature_scheme({}, BODY, SECRET)["found"] is False


def test_ordinary_headers_alone_find_nothing_and_say_so(cap):
    headers = {"Content-Type": "application/json", "User-Agent": "Rentvine"}
    out = cap.derive_signature_scheme(headers, BODY, SECRET)
    assert out["found"] is False
    assert out["digest_shaped_headers"] == []


def test_a_digest_shaped_header_that_is_not_ours_is_not_claimed(cap):
    """A guard against reporting a match that is really a coincidence."""
    headers = {"X-Request-Id": "f" * 64}
    assert cap.derive_signature_scheme(headers, BODY, SECRET)["found"] is False


# --------------------------------------------------------------------------
# the capture file and the whole-report path
# --------------------------------------------------------------------------


def test_summarise_reads_a_capture_file_and_derives_per_delivery(cap):
    capture = {
        "deliveries": [
            {
                "received_at": "2026-09-17T12:00:00Z",
                "method": "POST",
                "path": "/rentvine",
                "body_base64": base64.b64encode(BODY).decode(),
                "headers": {"X-Signature": _sign(BODY)},
            }
        ]
    }
    report = cap.summarise(capture, SECRET)
    assert report["analysed"] == 1
    assert report["deliveries"][0]["scheme"]["found"] is True
    assert report["deliveries"][0]["body_bytes"] == len(BODY)


def test_record_appends_rather_than_replacing(cap, tmp_path):
    """A second delivery must not destroy the first — captures are expensive."""
    out = tmp_path / "capture.json"
    cap._record(out, {"received_at": "1", "headers": {}, "body_base64": ""})
    cap._record(out, {"received_at": "2", "headers": {}, "body_base64": ""})
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert [d["received_at"] for d in saved["deliveries"]] == ["1", "2"]


def test_a_corrupt_capture_file_does_not_lose_the_delivery_being_recorded(cap, tmp_path):
    out = tmp_path / "capture.json"
    out.write_text("{not json", encoding="utf-8")
    cap._record(out, {"received_at": "1", "headers": {}, "body_base64": ""})
    assert len(json.loads(out.read_text(encoding="utf-8"))["deliveries"]) == 1


# --------------------------------------------------------------------------
# the listener's one hard rule
# --------------------------------------------------------------------------


def test_the_listener_always_answers_200_and_never_parses_the_body(cap):
    """Two properties a behavioural test cannot reach without a socket.

    A non-2xx makes a sender retry and eventually disable the endpoint, and
    parsing the body is what would destroy the bytes the analysis needs.
    """
    import ast

    src = (REPO / "scripts" / "rentvine_webhook_capture.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    capture_fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_capture"
    )
    codes = [
        n.args[0].value
        for n in ast.walk(capture_fn)
        if isinstance(n, ast.Call)
        and getattr(n.func, "attr", "") == "send_response"
        and n.args
        and isinstance(n.args[0], ast.Constant)
    ]
    assert codes == [200], f"the listener must answer 200 unconditionally, got {codes}"

    called = {getattr(n.func, "attr", "") for n in ast.walk(capture_fn) if isinstance(n, ast.Call)}
    assert "loads" not in called, "the body must not be parsed — an HMAC is over exact bytes"
    assert "b64encode" in called, "the body must be stored verbatim as base64"


# --------------------------------------------------------------------------
# handing the answer over — the half that was printed and never written
# --------------------------------------------------------------------------
#
# The deriver above was complete and well covered; `main()` had no test at
# all, which is why the five derived values lived in terminal scrollback and
# had to be hand-transcribed into a deployment. The gap was not in the
# analysis — it was in what happens to the analysis afterwards.


def _capture_file(tmp_path: Path, deliveries: list[dict]) -> Path:
    path = tmp_path / "capture.json"
    path.write_text(json.dumps({"deliveries": deliveries}), encoding="utf-8")
    return path


def _delivery(body: bytes, headers: dict[str, str], at: str = "T1") -> dict:
    return {
        "received_at": at,
        "method": "POST",
        "path": "/rv",
        "body_base64": base64.b64encode(body).decode(),
        "headers": headers,
    }


def test_analyse_writes_the_derived_scheme_to_a_file(cap, tmp_path, monkeypatch, capsys):
    """The whole point: the answer survives the terminal.

    A run that only PRINTS leaves an operator retyping six values into a
    deployment, and `settings.py` says beside those very fields that a value
    typed from memory is the guess the seam exists to avoid.
    """
    capture = _capture_file(tmp_path, [_delivery(BODY, {"X-Rentvine-Signature": _sign(BODY)})])
    out = tmp_path / "nested" / "scheme.json"
    monkeypatch.setenv("RENTVINE_WEBHOOK_SECRET", SECRET)

    code = cap.main(["--analyse", str(capture), "--scheme-out", str(out)])

    assert code == 0
    assert out.exists(), "the derived scheme must be written, not only printed"
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["consensus"]["agreed"] is True
    assert written["consensus"]["scheme"]["algorithm"] == "sha256"


def test_analyse_prints_every_env_line_ready_to_paste(cap, tmp_path, monkeypatch, capsys):
    """All SIX, spelled exactly as a deployment sets them.

    The blank timestamp header is included deliberately — omitting it leaves
    the reader unable to tell "not needed" from "the tool forgot it".
    """
    capture = _capture_file(tmp_path, [_delivery(BODY, {"X-Rentvine-Signature": _sign(BODY)})])
    monkeypatch.setenv("RENTVINE_WEBHOOK_SECRET", SECRET)

    cap.main(["--analyse", str(capture), "--scheme-out", str(tmp_path / "s.json")])
    printed = capsys.readouterr().out

    for suffix, value in [
        ("HEADER", "X-Rentvine-Signature"),
        ("ALGORITHM", "sha256"),
        ("PAYLOAD", "body"),
        ("KEY_ENCODING", "utf8"),
        ("DIGEST_ENCODING", "hex"),
        ("TIMESTAMP_HEADER", ""),
    ]:
        assert f"HELIXIS_RENTVINE__WEBHOOK_SIGNATURE_{suffix}={value}" in printed, suffix


def test_the_env_names_are_the_settings_fields(cap):
    """Both sides of the copy, compared (gotcha 28).

    The tool's job is that settling the unknown is a COPY rather than a
    translation. That only holds while every name it prints is a field
    `RentvineSettings` actually declares — and a drifted name reads as a
    correct configuration while the receiver refuses every delivery.
    """
    from helixis.settings import RentvineSettings

    declared = set(RentvineSettings.model_fields)
    for field, _ in cap._SCHEME_TO_SETTINGS:
        assert field in declared, f"{field} is not a RentvineSettings field"

    # ...and the other direction: every signature field the settings declare
    # must be one this tool emits, or a deployment is configured by hand for
    # the one it left out.
    signature_fields = {f for f in declared if f.startswith("webhook_signature_")}
    assert signature_fields == {field for field, _ in cap._SCHEME_TO_SETTINGS}


def test_it_refuses_to_write_the_analysis_over_the_capture(cap, tmp_path, monkeypatch):
    """The capture is irreplaceable; the analysis is derived and repeatable.

    Both are JSON in the same folder, so pointing `--scheme-out` at the
    capture is one tab-completion away — and it destroys the only copy of a
    real delivery's verbatim bytes.
    """
    capture = _capture_file(tmp_path, [_delivery(BODY, {"X-Rentvine-Signature": _sign(BODY)})])
    before = capture.read_bytes()
    monkeypatch.setenv("RENTVINE_WEBHOOK_SECRET", SECRET)

    code = cap.main(["--analyse", str(capture), "--scheme-out", str(capture)])

    assert code == 2, "aiming the output wrongly is COULD NOT RUN, not a failed derivation"
    assert capture.read_bytes() == before, "the capture must survive byte for byte"


def test_it_refuses_to_write_over_the_listen_destination_too(cap, tmp_path, monkeypatch):
    """A second capture file is just as irreplaceable as the one being read."""
    capture = _capture_file(tmp_path, [_delivery(BODY, {"X-Rentvine-Signature": _sign(BODY)})])
    other = tmp_path / "other-capture.json"
    other.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("RENTVINE_WEBHOOK_SECRET", SECRET)

    code = cap.main(["--analyse", str(capture), "--out", str(other), "--scheme-out", str(other)])

    assert code == 2
    assert other.read_text(encoding="utf-8") == "{}"


def test_taking_every_default_produces_a_working_run(cap, tmp_path, monkeypatch):
    """The commonest invocation there is: `--listen`, then `--analyse <it>`.

    ⚠ This drives the REAL defaults through argparse rather than comparing
    the two constants. A first cut did compare the constants and a mutation
    walked straight past it: pointing `--scheme-out`'s `default=` at
    `DEFAULT_OUT` leaves both constants untouched and still aims the report at
    the capture, at which point the guard above refuses and the default
    invocation stops working for everybody.
    """
    monkeypatch.chdir(tmp_path)
    capture = Path(cap.DEFAULT_OUT)
    capture.parent.mkdir(parents=True, exist_ok=True)
    capture.write_text(
        json.dumps({"deliveries": [_delivery(BODY, {"X-Sig": _sign(BODY)})]}),
        encoding="utf-8",
    )
    before = capture.read_bytes()
    monkeypatch.setenv("RENTVINE_WEBHOOK_SECRET", SECRET)

    code = cap.main(["--analyse", str(capture)])

    assert code == 0, "taking every default must derive, not refuse"
    assert Path(cap.DEFAULT_SCHEME_OUT).exists(), "the default report path must be written"
    assert capture.read_bytes() == before, "the default run must not touch the capture"


def test_a_failed_derivation_still_writes_its_diagnostics(cap, tmp_path, monkeypatch):
    """An absent file must mean "never ran", never "nothing matched".

    The failure branch carries the most re-usable material there is — which
    headers looked like digests, and how many constructions were tried — and
    losing that to scrollback is the defect this change exists to fix.
    """
    capture = _capture_file(
        tmp_path, [_delivery(BODY, {"X-Rentvine-Signature": _sign(BODY, key=b"a-different-key")})]
    )
    out = tmp_path / "scheme.json"
    monkeypatch.setenv("RENTVINE_WEBHOOK_SECRET", SECRET)

    code = cap.main(["--analyse", str(capture), "--scheme-out", str(out)])

    assert code == 1
    assert out.exists(), "a failed derivation must still write its diagnostics"
    scheme = json.loads(out.read_text(encoding="utf-8"))["deliveries"][0]["scheme"]
    assert scheme["found"] is False
    assert scheme["digest_shaped_headers"] == ["X-Rentvine-Signature"]
    assert scheme["attempts"] > 0


def test_two_deliveries_deriving_different_schemes_emit_nothing(cap, tmp_path, monkeypatch, capsys):
    """Disagreement declines rather than picking the first.

    One of the two is wrong and nothing here can say which; a wrong scheme
    refuses every delivery, which is indistinguishable from the unconfigured
    state it replaced.
    """
    b1, b2 = b'{"a":1}', b'{"b":2}'
    capture = _capture_file(
        tmp_path,
        [
            _delivery(b1, {"X-Sig": _sign(b1)}, at="T1"),
            _delivery(
                b2,
                {
                    "X-Sig": base64.b64encode(
                        hmac.new(SECRET.encode(), b2, hashlib.sha512).digest()
                    ).decode()
                },
                at="T2",
            ),
        ],
    )
    monkeypatch.setenv("RENTVINE_WEBHOOK_SECRET", SECRET)

    code = cap.main(["--analyse", str(capture), "--scheme-out", str(tmp_path / "s.json")])
    printed = capsys.readouterr().out

    assert code == 1, "a contradiction is not a scheme you can set"
    assert "HELIXIS_RENTVINE__" not in printed, "no env block may be emitted on disagreement"
    assert "DIFFERENT schemes" in printed


def test_two_deliveries_agreeing_do_emit(cap, tmp_path, monkeypatch, capsys):
    """The other direction — a refusal that fires on agreement is useless.

    Without this, a `consensus_scheme` that never agreed would pass the test
    above perfectly while making the tool incapable of ever answering.
    """
    b1, b2 = b'{"a":1}', b'{"b":2}'
    capture = _capture_file(
        tmp_path,
        [_delivery(b1, {"X-Sig": _sign(b1)}, "T1"), _delivery(b2, {"X-Sig": _sign(b2)}, "T2")],
    )
    monkeypatch.setenv("RENTVINE_WEBHOOK_SECRET", SECRET)

    code = cap.main(["--analyse", str(capture), "--scheme-out", str(tmp_path / "s.json")])
    printed = capsys.readouterr().out

    assert code == 0
    assert "HELIXIS_RENTVINE__WEBHOOK_SIGNATURE_ALGORITHM=sha256" in printed


def test_a_partial_scheme_raises_rather_than_emitting_what_it_has(cap):
    """A half-configured verifier reads as configured and refuses everything.

    `webhook-setup.md` singles that state out as worse than the unconfigured
    one, so five of six lines is not a partial success.
    """
    with pytest.raises(ValueError, match="missing"):
        cap.scheme_env_lines({"found": True, "header": "X-Sig", "algorithm": "sha256"})

    with pytest.raises(ValueError, match="nothing to set"):
        cap.scheme_env_lines({"found": False})


# --------------------------------------------------------------------------
# signatures in the URL (measured 2026-10-04: ?timestamp=…&signature=…&token=…)
# --------------------------------------------------------------------------


def _base36(number: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while number:
        number, rem = divmod(number, 36)
        out = digits[rem] + out
    return out or "0"


def _dashed(value: str) -> str:
    """Dashes where the measured signature had them: 16 and 8 from the end."""
    return f"{value[:-16]}-{value[-16:-8]}-{value[-8:]}"


def test_it_derives_a_signature_carried_in_the_query_string(cap):
    """Rentvine sent no signature header at all; a header-only search reports
    NOT FOUND on a delivery it could have explained."""
    ts, token = "1789598400", "aaa"
    signed = hmac.new(SECRET.encode(), (ts + token).encode(), hashlib.sha256).hexdigest()
    query = f"timestamp={ts}&signature={signed}&token={token}"
    out = cap.derive_signature_scheme({}, BODY, SECRET, path="/rv", query=query)
    assert out["found"] is True, out
    assert out["header"] == "query:signature"
    # Mailgun's construction: the timestamp, then the token BESIDE the
    # signature. It is the narrower description of these bytes, so it wins
    # over `query_values`, which would also break if a parameter were added.
    assert out["payload"] == "timestamp+token"
    assert out["timestamp_header"] == "query:timestamp"


def test_it_derives_a_signature_over_every_query_value(cap):
    """When the values signed are not just timestamp and token, only
    ``query_values`` reproduces them — the case that keeps it in the search."""
    ts, token = "1789598400", "aaa"
    signed = hmac.new(SECRET.encode(), f"1{ts}{token}".encode(), hashlib.sha256).hexdigest()
    query = f"a=1&timestamp={ts}&token={token}&signature={signed}"
    out = cap.derive_signature_scheme({}, BODY, SECRET, path="/rv", query=query)
    assert out["found"] is True, out
    assert (out["header"], out["payload"]) == ("query:signature", "query_values")
    assert out["timestamp_header"] == ""


def test_it_derives_a_dashed_base36_digest_and_names_the_query_timestamp(cap):
    ts = "1789598400"
    digest = hmac.new(SECRET.encode(), ts.encode() + b"." + BODY, hashlib.sha256).digest()
    query = f"timestamp={ts}&signature={_dashed(_base36(int.from_bytes(digest, 'big')))}&token=a"
    out = cap.derive_signature_scheme(
        {"Content-Type": "application/json"}, BODY, SECRET, query=query
    )
    assert out["found"] is True, out
    assert out["digest_encoding"] == "base36"
    assert out["payload"] == "timestamp.body"
    assert out["timestamp_header"] == "query:timestamp", (
        "the receiver must be told WHERE the timestamp is, or it cannot run the construction"
    )


def test_every_timestamp_candidate_is_tried_not_just_the_first(cap):
    """A decoy that merely LOOKS like a timestamp must not hide the real one."""
    ts = "1789598400"
    signed = hmac.new(SECRET.encode(), ts.encode() + b"." + BODY, hashlib.sha256).hexdigest()
    out = cap.derive_signature_scheme(
        {"X-Signature": signed, "X-Timestamp": ts}, BODY, SECRET, query="ts=42"
    )
    assert out["found"] is True, out
    assert out["timestamp_header"] == "X-Timestamp"


def test_it_derives_a_signature_over_the_full_url(cap):
    """The URL as Occupella received it, minus the signature parameter."""
    path, query_rest = "/webhooks/rentvine/acme", "timestamp=1&token=aaa"
    url = f"https://your-api.onrender.com{path}?{query_rest}".encode()
    signed = base64.b64encode(hmac.new(SECRET.encode(), url + BODY, hashlib.sha256).digest())
    query = f"timestamp=1&signature={signed.decode().replace('+', '%2B')}&token=aaa"
    headers = {"Host": "your-api.onrender.com"}
    out = cap.derive_signature_scheme(headers, BODY, SECRET, path=path, query=query)
    assert out["found"] is True, out
    assert (out["header"], out["payload"]) == ("query:signature", "url+body")


def test_the_measured_shape_is_digest_shaped_and_ordinary_values_are_not(cap):
    """A 48-digit base36 value with two dashes, the shape Rentvine sent. A
    filter that rejected it would skip the only candidate there was."""
    shaped = _dashed(_base36(int.from_bytes(hashlib.sha256(b"x").digest(), "big"))[:48])
    assert cap._looks_like_a_digest(shaped)
    for ordinary in ("aaa", "1791137042", "axios/0.28.1", "application/json", "keep-alive"):
        assert not cap._looks_like_a_digest(ordinary), ordinary


def test_summarise_splits_the_query_off_the_recorded_target(cap):
    """`--listen` records the request target, query included. The receiver's
    path has no query, so a `path+body` derived over the whole target would be
    a scheme the receiver can never reproduce."""
    target = "/webhooks/rentvine/acme?timestamp=1&signature=x&token=aaa"
    signed = hmac.new(SECRET.encode(), b"/webhooks/rentvine/acme" + BODY, hashlib.sha256)
    capture = {
        "deliveries": [
            {
                "received_at": "T1",
                "method": "POST",
                "path": target,
                "body_base64": base64.b64encode(BODY).decode(),
                "headers": {"X-Sig": signed.hexdigest()},
            }
        ]
    }
    d = cap.summarise(capture, SECRET)["deliveries"][0]
    assert d["path"] == "/webhooks/rentvine/acme"
    assert d["query"] == "timestamp=1&signature=x&token=aaa"
    assert d["scheme"]["found"] is True
    assert d["scheme"]["payload"] == "path+body"


# --------------------------------------------------------------------------
# --analyse-db: the deliveries Occupella refused and kept (migration 0106)
# --------------------------------------------------------------------------


class _Conn:
    def __init__(self, companies: list[dict], rows: list[dict]) -> None:
        self.companies, self.rows, self.calls = companies, rows, []

    async def fetch(self, sql: str, *args):
        self.calls.append((sql, args))
        return self.companies if "company_rentvine_credentials" in sql else self.rows


def _kept_row(query: str, body: bytes = BODY, headers=None) -> dict:
    from datetime import UTC, datetime

    return {
        "received_at": datetime(2026, 10, 4, 18, 4, 2, tzinfo=UTC),
        "method": "POST",
        "path": "/webhooks/rentvine/acme",
        "query": query,
        # asyncpg hands jsonb back as TEXT and bytea as bytes
        "headers": json.dumps(headers or {"content-type": "application/json"}),
        "body": body,
        "refused_because": "Rentvine's signature scheme is not configured.",
    }


def _query_signed(ts: str = "1789598400") -> str:
    digest = hmac.new(SECRET.encode(), ts.encode() + b"." + BODY, hashlib.sha256).digest()
    return f"timestamp={ts}&signature={_dashed(_base36(int.from_bytes(digest, 'big')))}&token=a"


def test_fetch_db_capture_reads_one_company_newest_first(cap):
    import asyncio

    conn = _Conn([{"company_id": "co-1"}], [_kept_row(_query_signed())])
    capture = asyncio.run(cap.fetch_db_capture(conn, "acme"))
    (company_sql, company_args), (rows_sql, rows_args) = conn.calls
    assert company_args == ("acme",)
    assert rows_args == ("co-1", cap.DB_LIMIT), "scoped to the one company, bounded"
    assert "ORDER BY id DESC" in rows_sql
    delivery = capture["deliveries"][0]
    assert delivery["path"].startswith("/webhooks/rentvine/acme?timestamp=")
    assert base64.b64decode(delivery["body_base64"]) == BODY
    assert delivery["headers"] == {"content-type": "application/json"}


def test_fetch_db_capture_declines_an_unknown_or_ambiguous_account(cap):
    import asyncio

    with pytest.raises(LookupError, match="no Rentvine connection"):
        asyncio.run(cap.fetch_db_capture(_Conn([], []), "acme"))
    two = [{"company_id": "co-1"}, {"company_id": "co-2"}]
    with pytest.raises(LookupError, match="refusing to guess"):
        asyncio.run(cap.fetch_db_capture(_Conn(two, []), "acme"))


def test_delivery_from_row_accepts_jsonb_as_text_or_object_and_a_memoryview(cap):
    row = _kept_row("a=1")
    row["headers"] = {"x": "y"}
    row["body"] = memoryview(BODY)
    d = cap.delivery_from_row(row)
    assert d["headers"] == {"x": "y"}
    assert base64.b64decode(d["body_base64"]) == BODY
    assert d["path"] == "/webhooks/rentvine/acme?a=1"
    assert cap.delivery_from_row(_kept_row(""))["path"] == "/webhooks/rentvine/acme"


def _fake_db(monkeypatch, cap, capture=None, exc: Exception | None = None):
    async def read(dsn: str, account: str):
        if exc is not None:
            raise exc
        return capture

    monkeypatch.setattr(cap, "_read_db_capture", read)


def test_analyse_db_derives_and_prints_the_six_lines(cap, tmp_path, monkeypatch, capsys):
    capture = {"deliveries": [cap.delivery_from_row(_kept_row(_query_signed()))]}
    _fake_db(monkeypatch, cap, capture)
    monkeypatch.setenv(cap.DB_URL_VAR, "postgresql://u:p@h/db")
    monkeypatch.setenv("RENTVINE_WEBHOOK_SECRET", SECRET)
    out = tmp_path / "scheme.json"

    code = cap.main(["--analyse-db", "--account", "acme", "--scheme-out", str(out)])
    printed = capsys.readouterr().out

    assert code == 0, printed
    assert "HELIXIS_RENTVINE__WEBHOOK_SIGNATURE_HEADER=query:signature" in printed
    assert "HELIXIS_RENTVINE__WEBHOOK_SIGNATURE_DIGEST_ENCODING=base36" in printed
    assert "HELIXIS_RENTVINE__WEBHOOK_SIGNATURE_TIMESTAMP_HEADER=query:timestamp" in printed
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["consensus"]["agreed"] is True
    assert "body_base64" not in json.dumps(report), "the report carries no delivery body"


def test_analyse_db_without_its_inputs_could_not_run(cap, monkeypatch, capsys):
    _fake_db(monkeypatch, cap, {"deliveries": []})
    monkeypatch.delenv("RENTVINE_ACCOUNT_CODE", raising=False)
    monkeypatch.delenv(cap.DB_URL_VAR, raising=False)
    monkeypatch.setenv("RENTVINE_WEBHOOK_SECRET", SECRET)
    assert cap.main(["--analyse-db"]) == 2
    assert cap.main(["--analyse-db", "--account", "acme"]) == 2
    assert cap.DB_URL_VAR in capsys.readouterr().out

    monkeypatch.setenv(cap.DB_URL_VAR, "postgresql://u:p@h/db")
    monkeypatch.delenv("RENTVINE_WEBHOOK_SECRET")
    assert cap.main(["--analyse-db", "--account", "acme"]) == 2

    monkeypatch.setenv("RENTVINE_WEBHOOK_SECRET", SECRET)
    assert cap.main(["--analyse-db", "--account", "acme"]) == 2
    assert "no refused delivery" in capsys.readouterr().out


def test_analyse_db_never_prints_the_connection_string(cap, monkeypatch, capsys):
    """A connection error can quote its connection details; CLAUDE.md rule 3."""
    dsn = "postgresql://postgres.ref:hunter2-not-real@pooler.example:5432/postgres"
    _fake_db(monkeypatch, cap, exc=OSError(f"could not connect to {dsn}"))
    monkeypatch.setenv(cap.DB_URL_VAR, dsn)
    monkeypatch.setenv("RENTVINE_WEBHOOK_SECRET", SECRET)

    code = cap.main(["--analyse-db", "--account", "acme"])
    printed = capsys.readouterr().out

    assert code == 2
    assert "hunter2" not in printed and "pooler.example" not in printed
    assert "OSError" in printed and "0106" in printed


def test_analyse_db_reports_an_unknown_account_plainly(cap, monkeypatch, capsys):
    _fake_db(monkeypatch, cap, exc=LookupError("no Rentvine connection uses the account code 'x'"))
    monkeypatch.setenv(cap.DB_URL_VAR, "postgresql://u:p@h/db")
    monkeypatch.setenv("RENTVINE_WEBHOOK_SECRET", SECRET)
    assert cap.main(["--analyse-db", "--account", "x"]) == 2
    assert "no Rentvine connection" in capsys.readouterr().out


# --------------------------------------------------------------------------
# Rentvine's real scheme (measured 2026-10-04): Mailgun's, inside the body
# --------------------------------------------------------------------------


def _rentvine_shaped(key: str, ts: int = 1789598400, token: str = "Tk" * 25) -> tuple[str, bytes]:
    """The key itself in the URL's ``signature``; the real signature in the
    body's ``auth`` block over timestamp + token. Synthetic values only."""
    signed = hmac.new(key.encode(), f"{ts}{token}".encode(), hashlib.sha256).hexdigest()
    body = json.dumps(
        {
            "data": {"workOrderID": "9", "dateTimeModified": "2026-10-04 15:14:54"},
            "event": {"eventType": "maintenance::workOrder::updated"},
            "auth": {"signature": signed, "timestamp": ts, "token": token},
        }
    ).encode()
    return f"timestamp={ts}&signature={key}&token=aaa", body


def test_it_derives_the_body_signature_not_the_key_in_the_url(cap):
    query, body = _rentvine_shaped(SECRET)
    out = cap.derive_signature_scheme({"Host": "h.test"}, body, SECRET, path="/rv", query=query)
    assert out["found"] is True, out
    assert out["header"] == "body:auth.signature"
    assert (out["algorithm"], out["payload"], out["digest_encoding"]) == (
        "sha256",
        "timestamp+token",
        "hex",
    )
    assert out["timestamp_header"] == "body:auth.timestamp", (
        "the timestamp BESIDE the signature, not the URL's copy of it"
    )
    assert out["key_sent_at"] == ["query:signature"]


def test_a_failed_run_still_says_where_the_key_was_sent(cap):
    query, body = _rentvine_shaped("a-different-key")
    out = cap.derive_signature_scheme(
        {}, body, SECRET, query=query.replace("a-different-key", SECRET)
    )
    assert out["found"] is False
    assert out["key_sent_at"] == ["query:signature"]


def test_the_body_walk_reads_strings_and_numbers_as_text_and_skips_arrays(cap):
    leaves = dict(cap._body_leaves(b'{"a": {"b": "x", "n": 1.50, "l": ["y"], "z": null}}'))
    assert leaves == {"a.b": "x", "a.n": "1.50"}
    assert cap._body_leaves(b"not json") == []


def test_analyse_warns_when_the_key_itself_was_sent(cap, tmp_path, monkeypatch, capsys):
    query, body = _rentvine_shaped(SECRET)
    capture = _capture_file(tmp_path, [_delivery(body, {}, "T1") | {"path": f"/rv?{query}"}])
    monkeypatch.setenv("RENTVINE_WEBHOOK_SECRET", SECRET)

    code = cap.main(["--analyse", str(capture), "--scheme-out", str(tmp_path / "s.json")])
    printed = capsys.readouterr().out

    assert code == 0, printed
    assert "the signing key ITSELF was sent at query:signature" in printed
    assert "HELIXIS_RENTVINE__WEBHOOK_SIGNATURE_HEADER=body:auth.signature" in printed
    assert SECRET not in printed, "the warning names WHERE, never the key"
