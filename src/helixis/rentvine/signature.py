"""Rentvine webhook signature verification — a seam, not a scheme. Phase 5.

⚠ **NOBODY KNOWS HOW RENTVINE SIGNS A WEBHOOK.** The captured vendor spec
contains **zero** occurrences of ``webhook``, ``signature`` or ``hmac``, and no
document we can obtain supplies the header name or the algorithm (api-notes §7,
ADR 0010 Decision (c)). So this module does not implement a scheme. It
implements the SHAPE of one and refuses to run without the five facts that
define it.

WHY A SEAM RATHER THAN A BEST GUESS
    A receiver that fails OPEN on an unknown signature algorithm is strictly
    worse than no receiver at all: it accepts forged events into a landlord's
    books, with a 200 and nothing anywhere saying the payload was unverified.
    Refusing costs a customer some freshness — and on Rentvine polling is
    PRIMARY and webhooks optimise four object types (Decision (c)), so a
    refused delivery is a slower mirror, never a missing one.

HOW IT GETS SETTLED, AND BY WHAT
    ``scripts/rentvine_webhook_capture.py`` takes ONE real delivery plus the
    account's signing key and derives the scheme by brute force over the
    algorithms, payload constructions, key encodings and digest encodings
    below, with every header and every query parameter as a candidate
    location. Its result dict is exactly :class:`SignatureScheme`'s fields, so
    its output drops straight in — nothing here is invented and nothing there
    has to be re-typed.

⚠ **THE CONSTRUCTIONS ARE DUPLICATED FROM THAT TOOL ON PURPOSE, AND GUARDED.**
The tool must stay standalone: it runs on a machine with egress and no
``helixis`` install, which is the whole reason it can capture anything. So the
key/payload/digest constructions exist in both places, and
``tests/unit/test_rentvine_webhooks.py`` imports BOTH and asserts that every
combination the tool can emit is one this module can execute, byte for byte
(gotcha 28). A scheme the tool reports and the receiver cannot run would be the
worst possible outcome of this design: a settled unknown that stays refused.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote_plus

logger = logging.getLogger(__name__)

#: Algorithms the capture tool searches. Kept in the same order for readability
#: when the two lists are diffed by eye; the guard compares them as SETS.
ALGORITHMS: tuple[str, ...] = ("sha256", "sha1", "sha512", "md5")

#: How the signing key's characters become bytes.
KEY_ENCODINGS: tuple[str, ...] = ("utf8", "hex_decoded", "base64_decoded")

#: How the digest bytes become the header's characters. ``base36`` (lowercase
#: letters and digits) is the only common encoding that fits the signature
#: Rentvine sent on 2026-10-04; it is compared as a number, so a dash or a
#: leading zero cannot turn a correct signature into a mismatch.
DIGEST_ENCODINGS: tuple[str, ...] = ("hex", "hex_upper", "base64", "base64url", "base36")

#: What gets signed. ``body`` first because it is by far the commonest.
#:
#: The query-string constructions exist because Rentvine signs in the URL
#: (measured 2026-10-04: ``…/demoaccount41?timestamp=…&signature=…&token=…``).
#: ``query`` is the RAW query string as it arrived, minus the signature
#: parameter; ``query_values`` is that string's decoded values, joined in order;
#: ``url`` is ``https://`` + the Host header + path + ``?`` + ``query``.
PAYLOAD_CONSTRUCTIONS: tuple[str, ...] = (
    "body",
    "timestamp.body",
    "timestamp+body",
    "body+timestamp",
    "path+body",
    "timestamp",
    "query",
    "query+body",
    "path?query",
    "path?query+body",
    "url",
    "url+body",
    "query_values",
    "query_values+body",
    "timestamp+token",
)

#: A scheme location that names a QUERY-STRING parameter rather than a header,
#: e.g. ``query:signature``. The six settings keep their names; a value with
#: this prefix reads the parameter instead.
QUERY_PREFIX = "query:"

#: A scheme location that names a field of the JSON BODY by dotted path, e.g.
#: ``body:auth.signature``. Numbers are read as the text that arrived, so a
#: timestamp signs exactly as sent.
BODY_PREFIX = "body:"

# SETTLED 2026-10-04 on a real sandbox delivery, and it is Mailgun's scheme:
# the body carries ``auth: {timestamp, token, signature}`` and ``signature`` is
# the lowercase-hex HMAC-SHA256 of ``timestamp + token`` under the account's
# signing key as Rentvine sends it, dashes included. Measured as one boolean
# per variant inside Postgres (the key is never printed): that construction
# matched, and the controls did not (token+timestamp, timestamp.token, either
# part alone, the key without its dashes). So:
#     header=body:auth.signature  algorithm=sha256  payload=timestamp+token
#     key_encoding=utf8  digest_encoding=hex  timestamp_header=body:auth.timestamp
#
# ⚠ The URL's own ``?signature=`` is NOT a signature: it is the signing key
# itself, identical on two deliveries 71 minutes apart (Render request log).
# It is in every request log the URL reaches, so a stored refusal redacts it
# (``rentvine/webhook_deliveries.stored_query``) — gotcha 203.
#
# ⚠ What the scheme does NOT cover: the ``data`` and ``event`` blocks. Mailgun's
# construction signs the timestamp and token only, so a replayed ``auth`` block
# authenticates any body. Bounded here because an accepted delivery only
# chooses which record to RE-READ from Rentvine; nothing in it is written.

#: Constructions that cannot be computed without a timestamp from the request.
_NEEDS_TIMESTAMP = frozenset(
    {"timestamp.body", "timestamp+body", "body+timestamp", "timestamp", "timestamp+token"}
)
#: Constructions that also need the ``token`` beside the signature
#: (:func:`sibling_location`), Mailgun's construction.
_NEEDS_TOKEN = frozenset({"timestamp+token"})
_NEEDS_PATH = frozenset({"path+body", "path?query", "path?query+body", "url", "url+body"})
_NEEDS_HOST = frozenset({"url", "url+body"})
_NEEDS_QUERY = frozenset(
    {
        "query",
        "query+body",
        "path?query",
        "path?query+body",
        "url",
        "url+body",
        "query_values",
        "query_values+body",
    }
)


class SignatureUnverifiable(Exception):  # noqa: N818 — "unverifiable" is the semantics
    """The delivery was not accepted, and the message says which kind of "not".

    Deliberately ONE exception for "we cannot check" and "it did not match".
    The receiver's answer is the same either way — refuse — and a caller that
    could tell them apart would eventually be tempted to treat the first as
    benign, which is the fail-open this module exists to prevent.
    """


@dataclass(frozen=True)
class SignatureScheme:
    """The five facts that turn "signed somehow" into something checkable.

    Field-for-field what ``scripts/rentvine_webhook_capture.py`` emits on a
    successful derivation, so settling the unknown is a copy, not a
    translation.
    """

    #: Where the digest arrives: a request header, e.g. ``x-rentvine-signature``,
    #: or ``query:<name>`` for a query-string parameter.
    header: str
    #: One of :data:`ALGORITHMS`.
    algorithm: str
    #: One of :data:`PAYLOAD_CONSTRUCTIONS`.
    payload: str
    #: One of :data:`KEY_ENCODINGS`.
    key_encoding: str
    #: One of :data:`DIGEST_ENCODINGS`.
    digest_encoding: str
    #: Where the timestamp arrives (a header, or ``query:<name>``), when the
    #: construction mixes one in.
    timestamp_header: str = ""

    def __post_init__(self) -> None:
        # ⚠ Validated at CONSTRUCTION, so a typo in a config value fails at
        # boot rather than on the first real delivery — where the symptom
        # would be "every webhook is refused", indistinguishable from the
        # unconfigured state this module ships in.
        for value, allowed, what in (
            (self.algorithm, ALGORITHMS, "algorithm"),
            (self.payload, PAYLOAD_CONSTRUCTIONS, "payload"),
            (self.key_encoding, KEY_ENCODINGS, "key_encoding"),
            (self.digest_encoding, DIGEST_ENCODINGS, "digest_encoding"),
        ):
            if value not in allowed:
                raise ValueError(f"unknown {what} {value!r}; expected one of {list(allowed)}")
        if not self.header.strip() or self.header.strip().lower() in (QUERY_PREFIX, BODY_PREFIX):
            raise ValueError("a scheme must name the header carrying the digest")
        if self.payload in _NEEDS_TIMESTAMP and not self.timestamp_header.strip():
            raise ValueError(
                f"payload construction {self.payload!r} mixes in a timestamp, so the "
                f"scheme must name the header it comes from"
            )


def scheme_from_capture(result: Mapping[str, object]) -> SignatureScheme:
    """Build a scheme from ``derive_signature_scheme``'s output.

    ⚠ Refuses a result whose ``found`` is false rather than building a
    half-populated scheme. The tool's own docstring says a failed derivation is
    NOT proof the delivery was unsigned — it means none of the tried
    constructions matched — so a scheme built from it would be a guess wearing
    the tool's authority.
    """
    if not result.get("found"):
        raise ValueError(
            "the capture tool did not derive a scheme "
            f"({result.get('reason', 'no reason given')}) — a failed derivation is "
            f"not evidence the delivery was unsigned, so there is nothing to install"
        )
    return SignatureScheme(
        header=str(result["header"]),
        algorithm=str(result["algorithm"]),
        payload=str(result["payload"]),
        key_encoding=str(result["key_encoding"]),
        digest_encoding=str(result["digest_encoding"]),
        timestamp_header=str(result.get("timestamp_header") or ""),
    )


def scheme_from_settings(rentvine: Any) -> SignatureScheme | None:
    """The configured scheme, or ``None`` when the deployment has not set one.

    ⚠ **THREE STATES, AND TWO OF THEM MUST BOTH REFUSE.** All fields blank is
    the state this connector ships in and returns ``None``. A PARTIALLY filled
    scheme is the dangerous one — somebody believes they turned it on — so it
    is logged at ERROR and also returns ``None``, because a half-configured
    verifier that accepted anything would be the fail-open this whole module
    exists to prevent. Only a complete, valid scheme is returned.

    It does not raise: a bad config value must not take the API down at boot,
    and refusing every delivery is already the safe answer.
    """
    fields = {
        "header": getattr(rentvine, "webhook_signature_header", "") or "",
        "algorithm": getattr(rentvine, "webhook_signature_algorithm", "") or "",
        "payload": getattr(rentvine, "webhook_signature_payload", "") or "",
        "key_encoding": getattr(rentvine, "webhook_signature_key_encoding", "") or "",
        "digest_encoding": getattr(rentvine, "webhook_signature_digest_encoding", "") or "",
    }
    timestamp_header = getattr(rentvine, "webhook_signature_timestamp_header", "") or ""
    if not any(v.strip() for v in fields.values()):
        return None
    try:
        return SignatureScheme(**fields, timestamp_header=timestamp_header)
    except ValueError as exc:
        logger.error(
            "Rentvine webhook signature scheme is configured but INVALID (%s) — every "
            "delivery will be refused. Re-run scripts/rentvine_webhook_capture.py and "
            "copy its five fields verbatim.",
            exc,
        )
        return None


def key_bytes(secret: str, key_encoding: str) -> bytes:
    """The signing key as bytes.

    ⚠ A key DISPLAYED as hex or base64 is sometimes meant to be decoded before
    use, and trying only the utf-8 bytes reports no match — indistinguishable
    from "not an HMAC". That is why the capture tool searches three encodings
    and why this takes the answer rather than assuming one.
    """
    if key_encoding == "utf8":
        return secret.encode()
    raw = secret.strip()
    if key_encoding == "hex_decoded":
        try:
            return binascii.unhexlify(raw)
        except binascii.Error as exc:
            raise SignatureUnverifiable(f"signing key is not valid hex: {exc}") from None
    if key_encoding == "base64_decoded":
        try:
            return base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SignatureUnverifiable(f"signing key is not valid base64: {exc}") from None
    raise SignatureUnverifiable(f"unknown key encoding {key_encoding!r}")


def query_param_name(location: str) -> str | None:
    """The parameter a ``query:<name>`` location names, or ``None`` for a header."""
    if location[: len(QUERY_PREFIX)].lower() == QUERY_PREFIX:
        return location[len(QUERY_PREFIX) :]
    return None


def body_field_path(location: str) -> str | None:
    """The dotted path a ``body:<path>`` location names, or ``None`` otherwise."""
    if location[: len(BODY_PREFIX)].lower() == BODY_PREFIX:
        return location[len(BODY_PREFIX) :]
    return None


def body_field(body: bytes, path: str) -> str | None:
    """A string or number at ``path`` in a JSON body, as the text that arrived.

    ⚠ Numbers stay TEXT (``parse_int=str``): a timestamp is signed as the
    characters sent, and ``float`` would turn ``1791141294`` into something
    else for a large enough value. Anything that is not a string or a number,
    and any body that is not JSON, is ``None``.
    """
    try:
        node: Any = json.loads(body, parse_int=str, parse_float=str)
    except (ValueError, UnicodeDecodeError):
        return None
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


def sibling_location(location: str, name: str) -> str:
    """The location called ``name`` beside ``location``.

    ``body:auth.signature`` -> ``body:auth.token``; ``query:signature`` ->
    ``query:token``; a header ``x-rv-signature`` -> ``x-rv-token``. Mailgun's
    construction signs the ``token`` that travels with the signature, so this
    is how a scheme finds it without a seventh setting.
    """
    path = body_field_path(location)
    if path is not None:
        parent, _, _ = path.rpartition(".")
        return BODY_PREFIX + (f"{parent}.{name}" if parent else name)
    if query_param_name(location) is not None:
        return QUERY_PREFIX + name
    head, dash, _ = location.rpartition("-")
    return f"{head}{dash}{name}" if dash else name


def read_location(
    location: str, headers: Mapping[str, str], query: str, body: bytes = b""
) -> str | None:
    """The value at a scheme location: a header (any case), a query parameter,
    or a field of the JSON body."""
    path = body_field_path(location)
    if path is not None:
        return body_field(body, path)
    name = query_param_name(location)
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


def unsigned_query(query: str, signature_location: str) -> str:
    """The raw query string without the signature parameter.

    ⚠ RAW, in arrival order and encoding. A signature over the URL covers the
    characters as sent; a decoded or re-sorted copy would never match it.
    """
    name = query_param_name(signature_location)
    if name is None:
        return query
    return "&".join(
        part for part in query.split("&") if part and unquote_plus(part.split("=", 1)[0]) != name
    )


def payload_bytes(
    construction: str,
    body: bytes,
    *,
    timestamp: str | None = None,
    path: str | None = None,
    query: str | None = None,
    host: str | None = None,
    token: str | None = None,
) -> bytes:
    """The exact bytes the HMAC is taken over.

    ``query`` is the UNSIGNED query string (:func:`unsigned_query`); ``token``
    is the value beside the signature (:func:`sibling_location`).

    ⚠ ``body`` must be the bytes that ARRIVED. A JSON round trip changes
    whitespace and key order, and the resulting digest is wrong in a way that
    reads as a forgery — the one failure mode that would make a correct
    receiver look like a broken one.
    """
    if construction not in PAYLOAD_CONSTRUCTIONS:
        raise SignatureUnverifiable(f"unknown payload construction {construction!r}")
    if construction == "body":
        return body
    if construction in _NEEDS_TIMESTAMP:
        if not timestamp:
            raise SignatureUnverifiable(
                f"construction {construction!r} needs a timestamp and the request carried none"
            )
        ts = timestamp.encode()
        if construction == "timestamp.body":
            return ts + b"." + body
        if construction == "timestamp+body":
            return ts + body
        if construction == "timestamp":
            return ts
        if construction == "timestamp+token":
            if not token:
                raise SignatureUnverifiable(
                    f"construction {construction!r} needs the token beside the signature "
                    "and the request carried none"
                )
            return ts + token.encode()
        return body + ts
    if construction in _NEEDS_PATH and not path:
        raise SignatureUnverifiable(
            f"construction {construction!r} needs the request path and none was supplied"
        )
    if construction == "path+body":
        return str(path).encode() + body
    if construction in _NEEDS_QUERY and query is None:
        raise SignatureUnverifiable(
            f"construction {construction!r} needs the query string and none was supplied"
        )
    if construction in _NEEDS_HOST and not host:
        raise SignatureUnverifiable(
            f"construction {construction!r} needs the Host header and the request carried none"
        )
    q = query or ""
    tail = body if construction.endswith("+body") else b""
    if construction.startswith("query_values"):
        values = "".join(v for _, v in parse_qsl(q, keep_blank_values=True))
        return values.encode() + tail
    if construction.startswith("query"):
        return q.encode() + tail
    target = str(path) + (f"?{q}" if q else "")
    if construction.startswith("url"):
        target = f"https://{host}{target}"
    return target.encode() + tail


def encode_digest(mac: hmac.HMAC, digest_encoding: str) -> str:
    """The digest rendered the way the header carries it."""
    if digest_encoding == "hex":
        return mac.hexdigest()
    if digest_encoding == "hex_upper":
        return mac.hexdigest().upper()
    digest = mac.digest()
    if digest_encoding == "base64":
        return base64.b64encode(digest).decode()
    if digest_encoding == "base64url":
        return base64.urlsafe_b64encode(digest).decode()
    if digest_encoding == "base36":
        return _base36(int.from_bytes(digest, "big"))
    raise SignatureUnverifiable(f"unknown digest encoding {digest_encoding!r}")


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


def normalise_token(token: str, digest_encoding: str) -> str:
    """A received digest token in the form :func:`encode_digest` produces.

    Only ``base36`` is rewritten: it has no fixed width or case, and Rentvine's
    value carries dashes, so dashes and leading zeros are dropped and letters
    lowered. Every other encoding is compared exactly as it arrived. Copied by
    the capture tool as ``_normalise_token``; a guard asserts the two agree.
    """
    if digest_encoding != "base36":
        return token
    cleaned = token.replace("-", "").lower()
    return cleaned.lstrip("0") or ("0" if cleaned else "")


def compute_digest(
    scheme: SignatureScheme,
    secret: str,
    body: bytes,
    *,
    timestamp: str | None = None,
    path: str | None = None,
    query: str | None = None,
    host: str | None = None,
    token: str | None = None,
) -> str:
    """What the signature should say, under ``scheme``. ``query`` is unsigned."""
    if not secret:
        raise SignatureUnverifiable("no signing key for this company")
    key = key_bytes(secret, scheme.key_encoding)
    signed = payload_bytes(
        scheme.payload, body, timestamp=timestamp, path=path, query=query, host=host, token=token
    )
    mac = hmac.new(key, signed, getattr(hashlib, scheme.algorithm))
    return encode_digest(mac, scheme.digest_encoding)


def header_tokens(value: str) -> list[str]:
    """The whole header value, then its parts.

    ⚠ **The WHOLE value first, and that is not cosmetic.** Splitting on ``=``
    strips a ``sha256=<hex>`` prefix — and it also eats base64 PADDING, so a
    44-character sha256 digest arrives as 43 and matches nothing. Checking the
    intact value first keeps both shapes working. Copied deliberately from the
    capture tool's ``_tokens``; the guard asserts the two agree, because a
    receiver that tokenises differently from the deriver would refuse the very
    scheme the deriver reported.
    """
    whole = value.strip()
    out = [whole]
    out.extend(p.strip() for p in re.split(r"[=,;\s]", whole) if p.strip() and p.strip() != whole)
    return out


def verify_signature(
    *,
    scheme: SignatureScheme | None,
    secret: str | None,
    headers: Mapping[str, str],
    body: bytes,
    path: str | None = None,
    query: str = "",
) -> None:
    """Refuse unless this delivery was signed with this company's key.

    ``query`` is the RAW query string as it arrived (Rentvine signs in the URL).

    Returns ``None`` on success and raises :class:`SignatureUnverifiable` on
    every other outcome — including the outcome this connector ships in, where
    no scheme has been derived yet.

    ⚠ **THE UNCONFIGURED CASE IS A REFUSAL, NOT A PASS, AND IT IS THE FIRST
    THING CHECKED.** Ordering it first means a deployment that has not settled
    the scheme cannot accept a delivery even if every other input happens to
    line up. The scheme is measured (the comment beside :data:`BODY_PREFIX`)
    and still comes from settings, so a deployment that has not set the six
    values refuses.
    """
    if scheme is None:
        raise SignatureUnverifiable(
            "Rentvine's signature scheme is not configured. The vendor spec "
            "documents none and this connector refuses rather than accepting "
            "unverified events — run scripts/rentvine_webhook_capture.py "
            "against one real delivery to derive it."
        )
    if not secret:
        raise SignatureUnverifiable("this company has no Rentvine webhook signing key on file")

    provided = read_location(scheme.header, headers, query, body)
    if not provided:
        raise SignatureUnverifiable(f"request carried no {scheme.header}")

    timestamp = (
        read_location(scheme.timestamp_header, headers, query, body)
        if scheme.timestamp_header
        else None
    )
    token = (
        read_location(sibling_location(scheme.header, "token"), headers, query, body)
        if scheme.payload in _NEEDS_TOKEN
        else None
    )
    expected = compute_digest(
        scheme,
        secret,
        body,
        timestamp=timestamp,
        path=path,
        query=unsigned_query(query, scheme.header),
        host=read_location("host", headers, ""),
        token=token,
    )

    # ⚠ Compared against EVERY token, the same set the deriver matched on. A
    # verifier that only compared the whole value would refuse a scheme the
    # tool legitimately reported from a `sha256=<hex>` header.
    expected_bytes = expected.encode()
    for token in header_tokens(provided):
        candidate = normalise_token(token, scheme.digest_encoding).encode()
        # compare_digest is only timing-safe on equal lengths; the length
        # check leaks nothing a digest's own encoding does not already.
        if len(candidate) == len(expected_bytes) and hmac.compare_digest(candidate, expected_bytes):
            return
    raise SignatureUnverifiable("signature does not match")


__all__ = [
    "ALGORITHMS",
    "BODY_PREFIX",
    "DIGEST_ENCODINGS",
    "KEY_ENCODINGS",
    "PAYLOAD_CONSTRUCTIONS",
    "QUERY_PREFIX",
    "SignatureScheme",
    "SignatureUnverifiable",
    "body_field",
    "body_field_path",
    "compute_digest",
    "encode_digest",
    "header_tokens",
    "key_bytes",
    "normalise_token",
    "payload_bytes",
    "query_param_name",
    "read_location",
    "scheme_from_capture",
    "scheme_from_settings",
    "sibling_location",
    "unsigned_query",
    "verify_signature",
]
