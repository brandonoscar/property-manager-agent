"""Capture one real Rentvine webhook and DERIVE its signature scheme from it.

WHY THIS EXISTS
    Phase 5 is the only phase blocked on something no document can supply.
    Rentvine's own OpenAPI spec contains **zero** occurrences of ``webhook``,
    ``signature`` or ``hmac`` (measured over 2.6 MB), and there is no top-level
    ``webhooks`` key. Third-party prose says deliveries are signed with a
    per-account key, viewable and resettable from the Rentvine Actions menu —
    but **the algorithm and the header name are unknown**.

    That is not a gap a careful reading closes. A receiver that cannot compute
    a signature must refuse EVERYTHING rather than accept everything, so Phase
    5 stays blocked until somebody watches one real delivery arrive.

    One delivery is enough. Given the raw bytes, the headers, and the signing
    key from the Actions menu, ``derive_signature_scheme`` brute-forces the
    usual constructions and reports which one reproduces the header value.
    That turns an unknowable into a measured fact.

THREE MODES

    1. Listen. Point a Rentvine webhook at a public URL that reaches this
       process (a tunnel is fine — it only has to survive one delivery):

           python scripts/rentvine_webhook_capture.py --listen --port 8787

       Every request is recorded VERBATIM and answered ``200`` immediately.

    2. Analyse. With the signing key in the environment:

           $s = Read-Host "Rentvine webhook signing key (Actions -> signing key)" -AsSecureString
           $env:RENTVINE_WEBHOOK_SECRET = [Net.NetworkCredential]::new("", $s).Password
           python scripts/rentvine_webhook_capture.py --analyse docs/rentvine/webhook-capture.json

       It WRITES the full analysis to ``--scheme-out`` (default
       ``docs/rentvine/webhook-scheme.json``) and prints the six
       ``HELIXIS_RENTVINE__WEBHOOK_SIGNATURE_*`` lines ready to paste.
       ⚠ Printing alone was the first cut, and it is the defect this tool
       exists to avoid one level up: the five derived values lived in terminal
       scrollback and had to be HAND-TRANSCRIBED into a deployment, which is
       exactly the "a value typed from memory is the guess the seam exists to
       avoid" that ``settings.py`` warns about beside those fields.

    3. Analyse what Occupella already received. The receiver keeps every
       delivery it refuses (migration 0106), so no tunnel is needed: point the
       Rentvine webhooks at Occupella's own URL, trigger one, then

           $s = Read-Host "the helixis-test pooler URL" -AsSecureString
           $env:HELIXIS_SUPABASE__DB_URL = [Net.NetworkCredential]::new("", $s).Password
           $s = Read-Host "Rentvine webhook signing key (Actions -> signing key)" -AsSecureString
           $env:RENTVINE_WEBHOOK_SECRET  = [Net.NetworkCredential]::new("", $s).Password
           python scripts/rentvine_webhook_capture.py --analyse-db --account <subdomain>

       Same analysis, same report, same six lines. Needs ``asyncpg`` (the
       helixis venv has it); the other two modes need nothing but Python.

⚠ WHERE RENTVINE PUTS THINGS (measured 2026-10-04)
    A delivery arrives as ``POST …/<account>?timestamp=…&signature=…&token=…``
    with no signature header. The URL's ``signature`` is NOT a signature: it
    is the signing key ITSELF, identical on two deliveries 71 minutes apart.
    The real one is in the body: ``auth: {timestamp, token, signature}``, the
    lowercase-hex HMAC-SHA256 of timestamp + token (Mailgun's scheme). So a
    location is a header name, ``query:<name>`` or ``body:<dotted.path>``;
    every header, query parameter and body field is a candidate; the token
    beside a candidate is found as its sibling; and a run says where the key
    itself was sent (``key_sent_at``). ``base36`` is searched because the
    URL value's alphabet fit nothing else before it turned out to be the key.

⚠ THE RAW BODY IS THE WHOLE POINT, AND IT IS EASY TO DESTROY
    An HMAC is computed over exact bytes. Parsing the body to JSON and
    re-serialising it changes whitespace and key order, so the digest stops
    matching and the analysis reports "no scheme found" — which reads as *this
    is not an HMAC* rather than *we broke the input*. The capture therefore
    stores the body base64-encoded and never round-trips it through a parser.

    Same reason the analysis REFUSES to write over the capture file: the
    capture holds the only copy of one real delivery's bytes and the analysis
    is derived from it, so the two are not interchangeable destinations even
    though both are JSON in the same folder.

⚠ IT ALWAYS ANSWERS 200
    A non-2xx makes a sender retry and eventually disable the endpoint. This
    product already carries that scar for Stripe (``stripe_webhook`` swallows
    its raise for exactly this reason). The capture is a listener, not a
    validator — it has no opinion to express in a status code.

⚠ NOT A RECEIVER, AND DELIBERATELY NOT IN ``src/``
    This verifies nothing and authorises nothing. The real receiver fails
    closed on a signature it can compute; this exists only to find out what
    that computation is. It is operator tooling, like ``rentvine_probe.py``.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote_plus

DEFAULT_OUT = "docs/rentvine/webhook-capture.json"
DEFAULT_PORT = 8787

#: Where ``--analyse`` writes its result. DELIBERATELY NOT ``DEFAULT_OUT``.
#: That file is the ``--listen`` destination and holds the verbatim bytes of a
#: real delivery — irreplaceable without another live webhook — while this one
#: is derived and can be regenerated any number of times.
DEFAULT_SCHEME_OUT = "docs/rentvine/webhook-scheme.json"

#: (settings field, derived-scheme key) — the ONE mapping between what this
#: tool derives and what a deployment sets. ``RentvineSettings`` carries the
#: same six and ``rentvine/signature.py`` executes them, so settling the
#: unknown is a COPY rather than a translation (gotcha 28's both-sides rule,
#: with a deployment env var as the second side).
_SCHEME_TO_SETTINGS: tuple[tuple[str, str], ...] = (
    ("webhook_signature_header", "header"),
    ("webhook_signature_algorithm", "algorithm"),
    ("webhook_signature_payload", "payload"),
    ("webhook_signature_key_encoding", "key_encoding"),
    ("webhook_signature_digest_encoding", "digest_encoding"),
    ("webhook_signature_timestamp_header", "timestamp_header"),
)

#: Header names worth trying first. The list is a HINT for ordering only —
#: every header is tried regardless, because the real name is unknown and a
#: hardcoded guess is what gotcha 79 records the cost of.
_LIKELY_SIGNATURE_HINTS = ("signature", "sign", "hmac", "digest", "hash", "token", "checksum")

#: Header names that carry a timestamp some schemes mix into the signed
#: payload (Stripe's ``t=`` construction is the familiar one).
_LIKELY_TIMESTAMP_HINTS = ("timestamp", "time", "date", "sent", "ts")

_ALGORITHMS = ("sha256", "sha1", "sha512", "md5")

#: A location naming a QUERY-STRING parameter rather than a header, e.g.
#: ``query:signature`` — the syntax ``rentvine/signature.py`` reads as
#: ``QUERY_PREFIX``. A guard asserts the two are equal.
_QUERY_PREFIX = "query:"

#: The constructions that mix in a timestamp. They are tried once per
#: timestamp candidate; every other construction is tried once.
_TIMESTAMP_PAYLOADS = frozenset(
    {"timestamp.body", "timestamp+body", "body+timestamp", "timestamp", "timestamp+token"}
)

#: A location naming a field of the JSON body by dotted path, e.g.
#: ``body:auth.signature`` — ``rentvine/signature.py``'s ``BODY_PREFIX``.
_BODY_PREFIX = "body:"

#: How deep, and how many leaves, the body walk reads. A delivery body is at
#: most 256 KB at the receiver; these only bound a hand-made capture.
_BODY_MAX_DEPTH = 8
_BODY_MAX_LEAVES = 10_000

#: Where ``--analyse-db`` reads from: the deliveries the receiver refused.
DB_URL_VAR = "HELIXIS_SUPABASE__DB_URL"
#: Newest first, as many as the receiver keeps per company.
DB_LIMIT = 20
_DB_COMPANY_SQL = (
    "SELECT company_id::text AS company_id FROM public.company_rentvine_credentials "
    "WHERE account_code = $1"
)
_DB_DELIVERIES_SQL = """
SELECT received_at, method, path, query, headers, body, refused_because
  FROM public.helixis_rentvine_webhook_deliveries
 WHERE company_id = $1::uuid
 ORDER BY id DESC
 LIMIT $2
"""


# --------------------------------------------------------------------------
# analysis — the part worth testing, and it needs no network
# --------------------------------------------------------------------------


#: Digest sizes of the algorithms this tool can actually compute, as the
#: LENGTH of their hex and base64 renderings. md5/sha1/sha256/sha384/sha512.
_HEX_LENGTHS = frozenset({32, 40, 64, 96, 128})
_B64_LENGTHS = frozenset({24, 28, 44, 64, 88})

_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(number: int) -> str:
    """``number`` in lowercase base 36, with no leading zeros."""
    if number == 0:
        return "0"
    out = []
    while number:
        number, rem = divmod(number, 36)
        out.append(_BASE36[rem])
    return "".join(reversed(out))


#: md5/sha1/sha256/sha384/sha512 in bits — the digests the lengths above cover.
_DIGEST_BITS = (128, 160, 256, 384, 512)
#: base36 has no fixed width: every leading zero digit makes it one shorter.
#: Eight short of the longest covers all but about one digest in 10^12.
_BASE36_SLACK = 8
_BASE36_LENGTHS = frozenset(
    n
    for bits in _DIGEST_BITS
    for n in range(len(_base36((1 << bits) - 1)) - _BASE36_SLACK, len(_base36((1 << bits) - 1)) + 1)
)


def _looks_like_a_digest(value: str) -> bool:
    """Whether a header value could be a digest this tool can reproduce.

    ⚠ **Skipping a header here can only lose a match one time in about 10^12,
    and that is what makes the strictness free.** ``_encodings`` produces hex
    and base64 of an md5/sha1/sha256/sha384/sha512 digest, whose lengths are
    fixed, and base36, whose length is within ``_BASE36_SLACK`` of the longest
    except for a digest with that many leading zero digits. A token of any
    other length could not have compared equal to a candidate no matter how
    long the search ran. The filter removes work and report noise, not recall.

    The looser "≥16 chars of a plausible alphabet" rule this replaces flagged
    ``application/json`` — exactly 16 characters, and ``/`` is in the base64url
    alphabet — so ``Content-Type`` was reported as digest-shaped. A reader
    chasing that is being sent somewhere by noise.
    """
    return any(_is_digest_token(p) for p in _tokens(value))


def _tokens(value: str) -> list[str]:
    """The whole value, then its parts.

    ⚠ The WHOLE value has to be tried first. Splitting on ``=`` is what lets a
    ``sha256=<hex>`` prefix be stripped — and it also eats base64 PADDING, so a
    44-character sha256 digest arrives here as 43 characters and matches no
    known length. Checking the intact value first keeps both shapes working.
    """
    whole = value.strip()
    out = [whole]
    out.extend(p.strip() for p in re.split(r"[=,;\s]", whole) if p.strip() and p.strip() != whole)
    return out


def _is_digest_token(part: str) -> bool:
    if len(part) in _HEX_LENGTHS and re.fullmatch(r"[A-Fa-f0-9]+", part):
        return True
    if len(part) in _B64_LENGTHS and re.fullmatch(r"[A-Za-z0-9+/_-]+={0,2}", part):
        return True
    # base36: the shape Rentvine sent on 2026-10-04 (letters, digits, dashes).
    number = _normalise_token(part, "base36")
    return len(number) in _BASE36_LENGTHS and re.fullmatch(r"[0-9a-z]+", number) is not None


def _normalise_token(token: str, digest_encoding: str) -> str:
    """A received token in the form ``_encodings`` produces for that encoding.

    Copied from ``rentvine/signature.normalise_token``; a guard asserts the two
    agree. Only base36 is rewritten: dashes and leading zeros dropped, letters
    lowered, because it has no fixed width or case.
    """
    if digest_encoding != "base36":
        return token
    cleaned = token.replace("-", "").lower()
    return cleaned.lstrip("0") or ("0" if cleaned else "")


def _candidate_digest_values(value: str) -> list[str]:
    """Every token in a header that could itself be the digest.

    Shares ``_tokens`` with the shape check so the two cannot disagree about
    what a token is — the padding bug above lived in exactly that gap.
    """
    return _tokens(value)


def _encodings(mac: hmac.HMAC) -> dict[str, str]:
    digest = mac.digest()
    return {
        "hex": mac.hexdigest(),
        "hex_upper": mac.hexdigest().upper(),
        "base64": base64.b64encode(digest).decode(),
        "base64url": base64.urlsafe_b64encode(digest).decode(),
        "base36": _base36(int.from_bytes(digest, "big")),
    }


def _key_variants(secret: str) -> list[tuple[str, bytes]]:
    """The secret as bytes, plus a decoded form if it is itself encoded.

    A signing key displayed as hex or base64 is sometimes meant to be DECODED
    before use. Trying only the utf-8 bytes would miss that and report no
    match — indistinguishable from "not an HMAC".
    """
    variants: list[tuple[str, bytes]] = [("utf8", secret.encode())]
    raw = secret.strip()
    if re.fullmatch(r"[A-Fa-f0-9]+", raw) and len(raw) % 2 == 0:
        try:
            variants.append(("hex_decoded", binascii.unhexlify(raw)))
        except binascii.Error:
            pass
    if re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", raw) and len(raw) % 4 == 0:
        try:
            variants.append(("base64_decoded", base64.b64decode(raw, validate=True)))
        except (binascii.Error, ValueError):
            pass
    return variants


def _query_param_name(location: str) -> str | None:
    """The parameter a ``query:<name>`` location names, or ``None`` for a header."""
    if location[: len(_QUERY_PREFIX)].lower() == _QUERY_PREFIX:
        return location[len(_QUERY_PREFIX) :]
    return None


def _body_field_path(location: str) -> str | None:
    """The dotted path a ``body:<path>`` location names, or ``None`` otherwise."""
    if location[: len(_BODY_PREFIX)].lower() == _BODY_PREFIX:
        return location[len(_BODY_PREFIX) :]
    return None


def _parse_body(body: bytes) -> Any:
    """The body as JSON with every number kept as the TEXT that arrived, or None."""
    try:
        return json.loads(body, parse_int=str, parse_float=str)
    except (ValueError, UnicodeDecodeError):
        return None


def _body_field(body: bytes, path: str) -> str | None:
    """Copied from ``rentvine/signature.body_field``; a guard asserts they agree."""
    node = _parse_body(body)
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


def _sibling(location: str, name: str) -> str:
    """The location called ``name`` beside ``location`` (Mailgun's ``token``).

    Copied from ``rentvine/signature.sibling_location``; a guard asserts they agree.
    """
    path = _body_field_path(location)
    if path is not None:
        parent, _, _ = path.rpartition(".")
        return _BODY_PREFIX + (f"{parent}.{name}" if parent else name)
    if _query_param_name(location) is not None:
        return _QUERY_PREFIX + name
    head, dash, _ = location.rpartition("-")
    return f"{head}{dash}{name}" if dash else name


def _read_location(location: str, headers: dict[str, str], query: str, body: bytes) -> str | None:
    """Copied from ``rentvine/signature.read_location``; a guard asserts they agree."""
    path = _body_field_path(location)
    if path is not None:
        return _body_field(body, path)
    name = _query_param_name(location)
    if name is not None:
        for key, value in parse_qsl(query, keep_blank_values=True):
            if key == name:
                return value
        return None
    wanted = location.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def _body_leaves(body: bytes) -> list[tuple[str, str]]:
    """Every string (or number, as text) in a JSON body, as ``(dotted path, value)``.

    Arrays are skipped: a dotted path cannot name an element, so a signature
    inside one is not a location a scheme can state.
    """
    out: list[tuple[str, str]] = []

    def walk(node: Any, prefix: str, depth: int) -> None:
        if depth > _BODY_MAX_DEPTH or len(out) >= _BODY_MAX_LEAVES:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{prefix}.{key}" if prefix else str(key), depth + 1)
        elif isinstance(node, str) and prefix:
            out.append((prefix, node))

    walk(_parse_body(body), "", 0)
    return out


def _unsigned_query(query: str, signature_location: str) -> str:
    """The RAW query string without the signature parameter, in arrival order.

    Copied from ``rentvine/signature.unsigned_query``; a guard asserts the two
    agree. A decoded or re-sorted copy would never match a signature over the
    characters as sent.
    """
    name = _query_param_name(signature_location)
    if name is None:
        return query
    return "&".join(
        part for part in query.split("&") if part and unquote_plus(part.split("=", 1)[0]) != name
    )


def _payload_variants(
    body: bytes,
    timestamp: str | None,
    path: str | None,
    query: str | None = None,
    host: str | None = None,
    token: str | None = None,
) -> list[tuple[str, bytes]]:
    """Constructions seen in the wild, body-only first.

    ``query`` is the UNSIGNED query string (``_unsigned_query``); ``None``
    means the request had none, so no query construction is tried. ``token``
    is the value beside the candidate signature (``_sibling``), for Mailgun's
    ``timestamp+token``.
    """
    out: list[tuple[str, bytes]] = [("body", body)]
    if timestamp:
        ts = timestamp.encode()
        out += [
            ("timestamp.body", ts + b"." + body),
            ("timestamp+body", ts + body),
            ("body+timestamp", body + ts),
            ("timestamp", ts),
        ]
        if token:
            out.append(("timestamp+token", ts + token.encode()))
    if path:
        out.append(("path+body", path.encode() + body))
    if query is not None:
        raw = query.encode()
        values = "".join(v for _, v in parse_qsl(query, keep_blank_values=True)).encode()
        out += [
            ("query", raw),
            ("query+body", raw + body),
            ("query_values", values),
            ("query_values+body", values + body),
        ]
        if path:
            target = (path + (f"?{query}" if query else "")).encode()
            out += [("path?query", target), ("path?query+body", target + body)]
            if host:
                url = f"https://{host}".encode() + target
                out += [("url", url), ("url+body", url + body)]
    return out


def derive_signature_scheme(
    headers: dict[str, str],
    body: bytes,
    secret: str,
    *,
    path: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """Find the (location, algorithm, payload, key, encoding) that reproduces a digest.

    Returns a dict with ``found`` and, when found, every part of the scheme —
    enough for the real receiver to implement it without guessing. A location
    is a header name, ``query:<name>`` or ``body:<dotted.path>``; ``query`` is
    the RAW query string.

    Both outcomes carry ``key_sent_at``: every location whose value IS the
    signing key. Rentvine sends its key as ``?signature=`` (measured
    2026-10-04), which puts it in every request log the URL reaches.

    ⚠ ``body`` must be the EXACT bytes that arrived. A JSON round trip changes
    whitespace and key order and silently breaks every candidate.
    """
    if not secret:
        return {"found": False, "reason": "no signing key supplied"}
    if not headers and not query and not body:
        return {"found": False, "reason": "no headers captured"}

    params = parse_qsl(query or "", keep_blank_values=True)
    leaves = _body_leaves(body)
    host = next((v for k, v in headers.items() if k.lower() == "host"), None)

    # Ordered so a conventionally-named location is reported first, but every
    # header, query parameter and body field is still tried — the real name is
    # unknown.
    locations = (
        list(headers.items())
        + [(_QUERY_PREFIX + k, v) for k, v in params]
        + [(_BODY_PREFIX + p, v) for p, v in leaves]
    )
    key_sent_at = [
        loc
        for loc, v in locations
        if v.isascii() and hmac.compare_digest(v.strip().encode(), secret.strip().encode())
    ]

    # ⚠ The location NAME is kept, not just its value. An earlier cut returned
    # only the value, so a derived scheme that mixes a timestamp into the
    # signed payload named no header to read it from — and a receiver cannot
    # execute a construction it cannot source an input for. Found by building
    # the consumer (`src/helixis/rentvine/signature.py`), which is the only
    # thing that could have found it: the tool's own output looked complete.
    def is_time(name: str) -> bool:
        return any(h in name.lower() for h in _LIKELY_TIMESTAMP_HINTS)

    all_timestamps: list[tuple[str, str | None]] = (
        [(_QUERY_PREFIX + k, v) for k, v in params if v and is_time(k)]
        + [(_BODY_PREFIX + p, v) for p, v in leaves if v and is_time(p.rsplit(".", 1)[-1])]
        + [(k, v) for k, v in headers.items() if v and is_time(k)]
    )

    def rank(item: tuple[str, str]) -> int:
        return 0 if any(h in item[0].lower() for h in _LIKELY_SIGNATURE_HINTS) else 1

    attempts = 0
    for location, value in sorted(locations, key=rank):
        if not _looks_like_a_digest(value):
            continue
        # compare_digest refuses a str with non-ASCII characters outright.
        targets = [t for t in _candidate_digest_values(value) if t.isascii()]
        unsigned = None if query is None else _unsigned_query(query, location)
        token = _read_location(_sibling(location, "token"), headers, query or "", body)
        # The timestamp beside the signature first (``body:auth.timestamp``
        # for ``body:auth.signature``), then every other candidate.
        beside = _sibling(location, "timestamp")
        timestamps = [c for c in all_timestamps if c[0] == beside] + [
            c for c in all_timestamps if c[0] != beside
        ]
        if not timestamps:
            timestamps = [("", None)]
        for i, (timestamp_location, timestamp) in enumerate(timestamps):
            for algo in _ALGORITHMS:
                for payload_name, payload in _payload_variants(
                    body, timestamp, path, unsigned, host, token
                ):
                    if i and payload_name not in _TIMESTAMP_PAYLOADS:
                        continue  # tried with the first timestamp candidate
                    for key_name, key in _key_variants(secret):
                        mac = hmac.new(key, payload, getattr(hashlib, algo))
                        for enc_name, computed in _encodings(mac).items():
                            attempts += 1
                            for target in targets:
                                if not hmac.compare_digest(
                                    computed, _normalise_token(target, enc_name)
                                ):
                                    continue
                                uses_timestamp = payload_name in _TIMESTAMP_PAYLOADS
                                return {
                                    "found": True,
                                    "header": location,
                                    "algorithm": algo,
                                    "payload": payload_name,
                                    "key_encoding": key_name,
                                    "digest_encoding": enc_name,
                                    "timestamp_header_used": uses_timestamp,
                                    # The receiver needs the NAME to read the
                                    # value back off a live request; "" when
                                    # the construction does not use one.
                                    "timestamp_header": (
                                        timestamp_location if uses_timestamp else ""
                                    ),
                                    "attempts": attempts,
                                    "key_sent_at": key_sent_at,
                                }
    return {
        "found": False,
        "reason": "no tried construction reproduced any header, query or body value",
        "attempts": attempts,
        "digest_shaped_headers": [k for k, v in locations if _looks_like_a_digest(v)],
        "key_sent_at": key_sent_at,
        "note": (
            "This is NOT proof the delivery was unsigned. It means none of "
            f"{len(_ALGORITHMS)} algorithms x the tried payload/key/encoding "
            "combinations matched. Check the key was copied from the same "
            "account, and that the captured body is verbatim."
        ),
    }


def summarise(capture: dict[str, Any], secret: str) -> dict[str, Any]:
    """Analyse every captured delivery, newest first."""
    results = []
    for delivery in capture.get("deliveries", []):
        body = base64.b64decode(delivery.get("body_base64") or "")
        headers = delivery.get("headers") or {}
        # The recorded target carries the query string, and Rentvine signs
        # there: split it off RAW, so `path` is what the receiver sees as the
        # path and the query reaches the deriver untouched.
        path, has_query, raw_query = str(delivery.get("path") or "").partition("?")
        query = raw_query if has_query else None
        results.append(
            {
                "received_at": delivery.get("received_at"),
                "method": delivery.get("method"),
                "path": path or None,
                "query": query,
                "body_bytes": len(body),
                # Only a delivery read with --analyse-db carries one.
                "refused_because": delivery.get("refused_because"),
                "scheme": derive_signature_scheme(
                    headers, body, secret, path=path or None, query=query
                ),
            }
        )
    return {"analysed": len(results), "deliveries": results}


# --------------------------------------------------------------------------
# reading what the receiver kept (migration 0106)
# --------------------------------------------------------------------------


def delivery_from_row(row: Any) -> dict[str, Any]:
    """One stored refusal in the capture format ``summarise`` reads.

    The query goes back onto the path exactly as it arrived, so a stored
    delivery and a ``--listen`` capture are analysed by the same code.
    """
    headers = row["headers"]
    if isinstance(headers, str):  # asyncpg hands jsonb back as text
        headers = json.loads(headers)
    body = bytes(row["body"])
    query = row["query"] or ""
    received = row["received_at"]
    return {
        "received_at": received.isoformat() if hasattr(received, "isoformat") else str(received),
        "method": row["method"],
        "path": row["path"] + (f"?{query}" if query else ""),
        "body_base64": base64.b64encode(body).decode(),
        "body_bytes": len(body),
        "headers": headers or {},
        "refused_because": row["refused_because"],
    }


async def fetch_db_capture(conn: Any, account_code: str) -> dict[str, Any]:
    """The refused deliveries Occupella kept for one Rentvine account.

    ⚠ Declines an ambiguous account code rather than reading either company,
    the same posture as the receiver's own lookup: two companies sharing one
    account code is a cross-tenant problem, not a tie to break.
    """
    companies = await conn.fetch(_DB_COMPANY_SQL, account_code)
    if not companies:
        raise LookupError(f"no Rentvine connection uses the account code {account_code!r}")
    if len(companies) > 1:
        raise LookupError(
            f"{len(companies)} companies use the account code {account_code!r}; "
            "refusing to guess which one's deliveries to read"
        )
    rows = await conn.fetch(_DB_DELIVERIES_SQL, companies[0]["company_id"], DB_LIMIT)
    return {
        "tool": "scripts/rentvine_webhook_capture.py",
        "source": f"helixis_rentvine_webhook_deliveries for {account_code}",
        "deliveries": [delivery_from_row(r) for r in rows],
    }


async def _read_db_capture(dsn: str, account_code: str) -> dict[str, Any]:
    import asyncpg  # only this mode needs it; the other two stay stdlib-only

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        return await fetch_db_capture(conn, account_code)
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# listening
# --------------------------------------------------------------------------


def _record(out_path: Path, delivery: dict[str, Any]) -> None:
    """Append one delivery, keeping earlier ones."""
    capture: dict[str, Any] = {"tool": "scripts/rentvine_webhook_capture.py", "deliveries": []}
    if out_path.exists():
        try:
            capture = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a corrupt file must not lose this delivery
            capture = {"tool": "scripts/rentvine_webhook_capture.py", "deliveries": []}
    capture.setdefault("deliveries", []).append(delivery)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(capture, indent=2), encoding="utf-8")


def _serve(port: int, out_path: Path) -> int:
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _capture(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            delivery = {
                "received_at": datetime.now(UTC).isoformat(),
                "method": self.command,
                "path": self.path,
                # Verbatim. Never parsed, never re-serialised — an HMAC is over
                # exact bytes and a round trip destroys it.
                "body_base64": base64.b64encode(body).decode(),
                "body_bytes": len(body),
                "headers": dict(self.headers.items()),
            }
            _record(out_path, delivery)
            # 200 first and always: a non-2xx makes a sender retry and then
            # disable the endpoint.
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            print(
                f"captured {self.command} {self.path} — {len(body)} bytes, "
                f"{len(delivery['headers'])} headers -> {out_path}"
            )

        do_POST = _capture  # noqa: N815 - BaseHTTPRequestHandler's naming
        do_GET = _capture  # noqa: N815
        do_PUT = _capture  # noqa: N815

        def log_message(self, *_args: Any) -> None:
            pass  # the capture line above is the log

    print(f"listening on 0.0.0.0:{port} — every request is answered 200 and recorded")
    print(f"writing to {out_path}")
    print("point a Rentvine webhook here (a tunnel is fine), then Ctrl-C\n")
    server = HTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - a tunnel must reach it
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


# --------------------------------------------------------------------------
# handing the answer over
# --------------------------------------------------------------------------


def scheme_env_lines(scheme: dict[str, Any]) -> list[str]:
    """Render a derived scheme as the six env lines a deployment sets.

    ⚠ ALL SIX, INCLUDING A BLANK TIMESTAMP HEADER. The blank is a FACT — this
    construction does not mix a timestamp in — and dropping the line leaves an
    operator unable to tell "not needed" from "the tool forgot it".
    ``RentvineSettings`` defaults it to the same blank, so pasting the whole
    block can only ever agree with the code.

    ⚠ RAISES on a partial scheme rather than emitting what it has. A
    half-configured verifier refuses every delivery while reading as
    configured, which is the one state ``webhook-setup.md`` singles out as
    worse than the unconfigured one.
    """
    if not scheme.get("found"):
        raise ValueError("no scheme was derived — there is nothing to set")
    missing = [key for _, key in _SCHEME_TO_SETTINGS if key not in scheme]
    if missing:
        raise ValueError(
            f"the derived scheme is missing {missing}; emitting the rest would "
            "produce a half-configured verifier that refuses every delivery"
        )
    return [
        f"HELIXIS_RENTVINE__{field.upper()}={scheme[key] or ''}"
        for field, key in _SCHEME_TO_SETTINGS
    ]


def consensus_scheme(report: dict[str, Any]) -> dict[str, Any]:
    """What the analysed deliveries AGREE the signing scheme is.

    Returns ``{agreed, scheme, derived, distinct, reason}``.

    ⚠ TWO DELIVERIES DERIVING DIFFERENT SCHEMES IS A FINDING, NOT A TIE TO
    BREAK. Taking the first would emit an env block that is right for one
    delivery and wrong for the other — and a wrong scheme refuses everything,
    which is indistinguishable from the unconfigured state it replaced. So
    disagreement declines and names both (gotchas 55/57).
    """
    derived = [
        d["scheme"] for d in report.get("deliveries", []) if (d.get("scheme") or {}).get("found")
    ]
    if not derived:
        return {
            "agreed": False,
            "scheme": None,
            "derived": 0,
            "distinct": 0,
            "reason": "no delivery yielded a scheme",
        }
    distinct = {tuple(s.get(key) or "" for _, key in _SCHEME_TO_SETTINGS) for s in derived}
    if len(distinct) > 1:
        return {
            "agreed": False,
            "scheme": None,
            "derived": len(derived),
            "distinct": len(distinct),
            "reason": (
                f"{len(derived)} deliveries derived {len(distinct)} DIFFERENT schemes. "
                "One of them is wrong and nothing here can say which, so no "
                "configuration is emitted. Re-capture from a single account."
            ),
        }
    return {
        "agreed": True,
        "scheme": derived[0],
        "derived": len(derived),
        "distinct": 1,
        "reason": "",
    }


def write_scheme_report(
    path: Path,
    report: dict[str, Any],
    *,
    protect: tuple[Path, ...] = (),
) -> Path:
    """Write the analysis to ``path``, refusing to land on a CAPTURE file.

    ⚠ THE CAPTURE IS THE IRREPLACEABLE HALF and this report is derived from
    it, so a ``--scheme-out`` that resolves onto one is refused outright
    rather than backed up or renamed. By the time anything could notice, the
    delivery's verbatim bytes are gone and only another live webhook replaces
    them.

    ⚠ Written on a FAILED derivation too. An absent file would then mean
    either "nothing matched" or "the analysis never ran", and the failure
    branch carries the most re-usable material there is — the digest-shaped
    headers it saw and how many constructions it tried.
    """
    target = path.resolve()
    for guarded in protect:
        if target == guarded.resolve():
            raise ValueError(
                f"refusing to write the analysis over {guarded} — that file holds "
                "the only copy of a delivery's verbatim bytes. Pass a different "
                "--scheme-out."
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    # encoding pinned: the report carries the capture's own header values, and
    # a non-cp1252 byte in one of them must not kill the write on Windows
    # AFTER the analysis has already run (gotcha 49).
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------


def _analyse_and_report(
    capture: dict[str, Any], secret: str, scheme_out: Path, protect: tuple[Path, ...]
) -> int:
    """Derive, write the report, print the verdict. Shared by both analyse modes."""
    report = summarise(capture, secret)
    if not report["analysed"]:
        print("VERDICT: COULD NOT RUN — the capture holds no deliveries")
        return 2
    agreement = consensus_scheme(report)
    report["consensus"] = agreement

    for d in report["deliveries"]:
        s = d["scheme"]
        if s.get("found"):
            print(
                f"{d['received_at']}  SCHEME FOUND: header={s['header']} "
                f"algo={s['algorithm']} payload={s['payload']} "
                f"key={s['key_encoding']} digest={s['digest_encoding']}"
            )
        else:
            print(f"{d['received_at']}  no match — {s.get('reason')}")
        if s.get("key_sent_at"):
            print(
                f"  ⚠ the signing key ITSELF was sent at {', '.join(s['key_sent_at'])}: "
                "every request log that URL reaches holds the key"
            )

    # The write comes BEFORE the env block and happens on every outcome:
    # a failed derivation's digest-shaped-header list is what a second
    # attempt starts from, and losing it to scrollback is this whole
    # change's defect.
    try:
        written = write_scheme_report(scheme_out, report, protect=protect)
    except (OSError, ValueError) as exc:
        # Exit 2, not 1. The derivation may well have SUCCEEDED — the
        # operator aimed the output wrongly, or the directory is
        # unwritable. "1" in this tool means "no scheme"; saying that here
        # would report a finding the run did not make. And a traceback
        # where the docstring promises a verdict is its own defect
        # (gotcha 57's probe made exactly this mistake).
        print(f"\nVERDICT: COULD NOT RUN — {exc}")
        print("Nothing was written. The capture is untouched; re-run with --scheme-out.")
        return 2
    print(f"\nfull analysis written to {written}")

    if agreement["agreed"]:
        print("\nSet these SIX in the deployment's environment — copy, never retype:\n")
        for line in scheme_env_lines(agreement["scheme"]):
            print(f"  {line}")
        print(
            "\n  (a blank TIMESTAMP_HEADER is a FACT, not an omission: this\n"
            "   construction does not mix a timestamp into what is signed.)"
        )
    elif agreement["derived"]:
        print(f"\nNOT EMITTING a configuration — {agreement['reason']}")

    # ⚠ Exit 1 on DISAGREEMENT even though deliveries were derived. Exit 0
    # has to keep meaning "there is a scheme you can set"; letting a
    # contradiction exit 0 is the meaning-drift the A2P harness records in
    # §4, where "every POST was accepted" and "the registration is valid"
    # printed identically.
    verdict = "DERIVED" if agreement["agreed"] else "NOT DERIVED"
    print(f"\nVERDICT: {verdict} ({agreement['derived']}/{report['analysed']})")
    return 0 if agreement["agreed"] else 1


def _secret_or_none() -> str | None:
    secret = os.environ.get("RENTVINE_WEBHOOK_SECRET", "").strip()
    if not secret:
        print("VERDICT: COULD NOT RUN — set RENTVINE_WEBHOOK_SECRET (Actions -> signing key)")
        print("Exit 2 is not a pass; nothing was derived.")
        return None
    return secret


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument("--listen", action="store_true", help="record incoming deliveries")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--analyse", metavar="CAPTURE", help="derive the scheme from a capture file")
    ap.add_argument(
        "--analyse-db",
        action="store_true",
        help="derive the scheme from the deliveries Occupella refused and kept",
    )
    ap.add_argument(
        "--account",
        default=os.environ.get("RENTVINE_ACCOUNT_CODE", "").strip(),
        help="the Rentvine subdomain whose kept deliveries --analyse-db reads",
    )
    ap.add_argument("--out", default=DEFAULT_OUT, help="where --listen records deliveries")
    ap.add_argument(
        "--scheme-out",
        default=DEFAULT_SCHEME_OUT,
        help="where --analyse writes its result (never the capture file)",
    )
    args = ap.parse_args(argv)

    try:  # Windows console (CLAUDE.md gotcha 49)
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    if args.listen:
        return _serve(args.port, Path(args.out))

    if args.analyse:
        path = Path(args.analyse)
        if not path.exists():
            print(f"VERDICT: COULD NOT RUN — no capture at {path}")
            return 2
        secret = _secret_or_none()
        if secret is None:
            return 2
        capture = json.loads(path.read_text(encoding="utf-8"))
        return _analyse_and_report(
            capture, secret, Path(args.scheme_out), protect=(path, Path(args.out))
        )

    if args.analyse_db:
        if not args.account:
            print("VERDICT: COULD NOT RUN — pass --account <your Rentvine subdomain>")
            return 2
        dsn = os.environ.get(DB_URL_VAR, "").strip()
        if not dsn:
            print(f"VERDICT: COULD NOT RUN — set {DB_URL_VAR} (the helixis-test pooler URL)")
            return 2
        secret = _secret_or_none()
        if secret is None:
            return 2
        import asyncio

        try:
            capture = asyncio.run(_read_db_capture(dsn, args.account))
        except LookupError as exc:
            print(f"VERDICT: COULD NOT RUN — {exc}")
            return 2
        except Exception as exc:  # noqa: BLE001 — a verdict, never a traceback
            # The exception TYPE only: a connection error can quote the
            # connection details, and those never reach a terminal.
            print(
                f"VERDICT: COULD NOT RUN — reading the kept deliveries failed: {type(exc).__name__}"
            )
            print(f"Check {DB_URL_VAR}, and that migration 0106 is applied.")
            return 2
        if not capture["deliveries"]:
            print(
                f"VERDICT: COULD NOT RUN — Occupella has kept no refused delivery for "
                f"{args.account} yet. Change a work order in Rentvine, wait a minute, "
                "and run this again."
            )
            return 2
        return _analyse_and_report(
            capture, secret, Path(args.scheme_out), protect=(Path(args.out),)
        )

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
