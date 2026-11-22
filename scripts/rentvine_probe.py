"""Answer the Rentvine questions a document cannot, against a real sandbox.

WHY THIS EXISTS
    `docs/rentvine/vendor/docs.rentvine.com/openapi.json` is Rentvine's own
    OpenAPI 3.1.0 spec, so endpoint inventory, field shapes, auth and the base
    URL are settled. Eight questions survive it, and every one of them is a
    property of the SERVER rather than of the document:

      A. Does auth work at all?          (the control — see below)
      B. How do you ask for page 2?      ⚠ CORRECTED — the spec DOES document
                                         pagination: `#/components/parameters/
                                         page` is referenced by 44 operations,
                                         each with a page size. The earlier
                                         "no selector anywhere" reading came
                                         from a scan that never followed
                                         `$ref`. What is still open is the STOP
                                         CONDITION: no total, no hasMore, no
                                         Link header, so nothing distinguishes
                                         a last page from a short one
      C. What is in an error body?       401 carries a content schema on 33 of
                                         175 declarations, 403 on 35 of 168
      D. Does a doubled slash normalise? 15 of the reference client's 61 call
                                         sites emit `/api/manager//...`
      E. Do the 5 undocumented client paths exist? A 404 says the client
                                         invented them; a 200 says the spec is
                                         incomplete, which is a fact about
                                         every other absence in it
      F. Does a bare collection differ from its /search twin?
      G. Are record ids numeric, and in what range? ⚠ **The assumption ADR
                                         0010 rev 3 rests on, and the spec
                                         CONTRADICTS ITSELF on it** — 126 of
                                         127 `*ID` path params are integer
                                         while most leaseID/propertyID schema
                                         declarations are string-typed. Both
                                         PMSs' ids now share one bigint column,
                                         so the range matters too: an overlap
                                         makes the one-PMS-per-company rule the
                                         only thing preventing a silent
                                         overwrite
      H. Which path reads a single bill? T1 says `/accounting/{billID}` (which
                                         also carries DELETE Void Bill); the
                                         reference client says
                                         `/accounting/bills/{billID}`. That is
                                         the ONLY documented undo of a posted
                                         bill, so picking the wrong one loses
                                         the most consequential reversal in the
                                         money surface

    None of these can be reasoned out. CLAUDE.md gotcha 79 is the standing
    record of what guessing a wire detail costs here.

READ-ONLY BY CONSTRUCTION, NOT BY CARE
    `_get` hardcodes the verb and there is no parameter to change it, so this
    file cannot issue a write even if a later edit asks it to. That is
    deliberate: `docs/rentvine/vendor/conflicts.md` says to run the read-only
    probes first, and the Buildium equivalent exists because a write harness
    that has not proved its reads is measuring its own fixtures.

    Whether a POST to a record path REPLACES or MERGES is therefore NOT
    answered here. It is the sixth open question and it needs its own gated
    harness against a disposable record.

CREDENTIALS
    Environment only — never arguments, because argv reaches shell history and
    process listings. The secret is never printed; `--check` reports set/unset.

      $env:RENTVINE_ACCOUNT_CODE = "<your subdomain>"
      $s = Read-Host "RENTVINE_API_KEY" -AsSecureString
      $env:RENTVINE_API_KEY      = [Net.NetworkCredential]::new("", $s).Password
      $s = Read-Host "RENTVINE_API_SECRET" -AsSecureString
      $env:RENTVINE_API_SECRET   = [Net.NetworkCredential]::new("", $s).Password
      python scripts/rentvine_probe.py

EXIT CODES — and 0 is narrow on purpose
    0  auth worked AND every probe returned a verdict
    1  auth worked, at least one probe could not be answered (said so)
    2  could not run: missing credentials, or auth refused

    ⚠ Exit 2 is NOT a pass. A harness that did not run is not one that
    validated anything (the LED-013 skip-floor rule).

OUTPUT
    A verbatim JSON report next to this script's `--out` path. Send THAT file
    rather than retyping the findings: a hand-copied result tests the
    transcription (CLAUDE.md gotcha 88).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# The spec's own server template, with {account} substituted.
# servers[0].url = https://{account}.rentvine.com/api/manager
_BASE = "https://{account}.rentvine.com/api/manager"

# Paths the SPEC documents, used as probe targets so a failure means the
# server disagreed rather than that we guessed a URL.
_COLLECTION = "/properties/search"  # declares pageSize (default 25) + search
_BARE_VS_SEARCH = [("/owners", "/owners/search"), ("/tenants", "/tenants/search")]

# Candidate page selectors. None of these appears in the spec; that absence is
# the question. Ordered by how conventional they are.
_PAGE_SELECTORS = ["page", "pageNumber", "offset", "start", "skip", "cursor"]

# Call sites the reference client has and the spec does not document.
# See docs/rentvine/vendor/conflicts.md.
_UNDOCUMENTED = [
    "/accounting/bills/{id}",
    "/files/{id}",
    "/leases/statuses/{id}",
    "/leases/{id}/occupants",
]

_UNKNOWN = "COULD NOT ANSWER"


class Creds:
    def __init__(self) -> None:
        self.account = os.environ.get("RENTVINE_ACCOUNT_CODE", "").strip()
        self.key = os.environ.get("RENTVINE_API_KEY", "").strip()
        self.secret = os.environ.get("RENTVINE_API_SECRET", "").strip()

    @property
    def complete(self) -> bool:
        return bool(self.account and self.key and self.secret)

    @property
    def missing(self) -> list[str]:
        names = {
            "RENTVINE_ACCOUNT_CODE": self.account,
            "RENTVINE_API_KEY": self.key,
            "RENTVINE_API_SECRET": self.secret,
        }
        return [n for n, v in names.items() if not v]

    def base(self) -> str:
        return _BASE.format(account=self.account)

    def header(self) -> str:
        raw = f"{self.key}:{self.secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()


def _get(
    creds: Creds,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    raw_path: bool = False,
    timeout: int = 45,
) -> dict[str, Any]:
    """The ONLY request function, and it cannot issue anything but a GET.

    `raw_path` skips base-URL joining so the doubled-slash probe can send a URL
    a well-behaved joiner would normalise away before it left the process — the
    question is what the SERVER does with it, so the client must not answer it
    first.
    """
    url = path if raw_path else creds.base() + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)

    req = urllib.request.Request(url, method="GET")  # the verb is not a variable
    req.add_header("Authorization", creds.header())
    req.add_header("Accept", "application/json")
    # transport.md records the reference client sending `charset` as its own
    # header, which is unusual. Not reproduced: this is a GET with no body, so
    # a request charset has nothing to describe.

    out: dict[str, Any] = {
        "url": _redact(url, creds),
        "status": None,
        "content_type": None,
        "body": None,
        "error": None,
    }
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out["status"] = resp.status
            out["content_type"] = resp.headers.get("Content-Type")
            out["body"] = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        out["status"] = exc.code
        out["content_type"] = exc.headers.get("Content-Type") if exc.headers else None
        try:
            out["body"] = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - the body is diagnostics, never load-bearing
            out["body"] = None
    except Exception as exc:  # noqa: BLE001 - a transport failure is a finding
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _redact(url: str, creds: Creds) -> str:
    """The secret never reaches the report, and the key is truncated.

    The account code stays: it is not a password, it is in the URL of every
    request, and a report that hides it cannot be read against the right
    tenant.
    """
    out = url.replace(creds.secret, "<secret>") if creds.secret else url
    if creds.key and len(creds.key) > 6:
        out = out.replace(creds.key, creds.key[:4] + "...")
    return out


def _json(body: str | None) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return None


def _rows(payload: Any) -> list[Any]:
    """Pull the row list out of whatever envelope came back.

    ⚠ The envelope shape is itself a T2 claim (transport.md), so this declines
    rather than guessing: an unrecognised shape returns [] and the caller
    reports COULD NOT ANSWER instead of treating zero rows as an answer.
    """
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    # A single-key envelope whose value is a list is the documented shape.
    lists = [v for v in payload.values() if isinstance(v, list)]
    return lists[0] if len(lists) == 1 else []


def _identity(rows: list[Any]) -> list[str]:
    """A stable fingerprint per row, so two pages can be COMPARED.

    Without this the pagination probe cannot tell "the selector worked" from
    "the server ignored it and returned the same page" — the two look
    identical if you only count rows.
    """
    out = []
    for r in rows:
        if isinstance(r, dict):
            inner = r
            # Rows often nest one level: {"property": {...}}
            if len(r) == 1:
                only = next(iter(r.values()))
                if isinstance(only, dict):
                    inner = only
            ident = next(
                (
                    str(v)
                    for k, v in inner.items()
                    if k.endswith("ID") and isinstance(v, (int, str))
                ),
                None,
            )
            out.append(ident if ident is not None else json.dumps(r, sort_keys=True)[:80])
        else:
            out.append(str(r)[:80])
    return out


# --------------------------------------------------------------------------
# A. auth — the control


def probe_auth(creds: Creds) -> dict[str, Any]:
    r = _get(creds, _COLLECTION, {"pageSize": 1})
    ok = r["status"] == 200
    verdict = "auth OK" if ok else f"auth FAILED (status {r['status']})"
    if r["error"]:
        verdict = f"could not reach the host — {r['error']}"
    return {
        "question": "A. Does auth work, and is the account code right?",
        "verdict": verdict,
        "ok": ok,
        "status": r["status"],
        "content_type": r["content_type"],
        "envelope_keys": sorted(_json(r["body"]).keys())
        if isinstance(_json(r["body"]), dict)
        else None,
        "body_head": (r["body"] or "")[:400],
        "url": r["url"],
        "note": (
            "⚠ A wrong account_code does not produce an auth error — it produces "
            "a different tenant or a dead host (transport.md). A 200 here means "
            "the credentials and the subdomain are BOTH right."
        ),
    }


# --------------------------------------------------------------------------
# B. pagination


def probe_pagination(creds: Creds) -> dict[str, Any]:
    out: dict[str, Any] = {
        "question": "B. How do you ask for page 2?",
        "spec_says": "pageSize on 13 of 205 operations; no page selector anywhere",
    }

    two = _get(creds, _COLLECTION, {"pageSize": 2})
    one = _get(creds, _COLLECTION, {"pageSize": 1})
    rows_two, rows_one = _rows(_json(two["body"])), _rows(_json(one["body"]))
    out["rows_at_pageSize_2"] = len(rows_two)
    out["rows_at_pageSize_1"] = len(rows_one)
    one_body = _json(one["body"])
    out["envelope_keys"] = sorted(one_body.keys()) if isinstance(one_body, dict) else None

    # THE CONTROL. If pageSize itself does nothing, no selector result below
    # can be trusted — a selector that "changes nothing" would be
    # indistinguishable from a server ignoring every query parameter.
    size_works = len(rows_two) == 2 and len(rows_one) == 1
    out["pageSize_is_honoured"] = size_works
    if not size_works:
        out["verdict"] = _UNKNOWN
        out["why"] = (
            f"pageSize=2 returned {len(rows_two)} rows and pageSize=1 returned "
            f"{len(rows_one)}. Without a working pageSize there is no control, so "
            "a selector that appears to do nothing proves nothing. Fewer rows than "
            "asked for can also just mean a small sandbox — seed more properties "
            "and re-run."
        )
        return out

    baseline = _identity(rows_one)
    found: list[dict[str, Any]] = []
    tried: list[dict[str, Any]] = []
    for sel in _PAGE_SELECTORS:
        for value in (2, 1):  # 1-indexed and 0-indexed both plausible
            r = _get(creds, _COLLECTION, {"pageSize": 1, sel: value})
            ident = _identity(_rows(_json(r["body"])))
            moved = bool(ident) and ident != baseline
            tried.append(
                {
                    "selector": sel,
                    "value": value,
                    "status": r["status"],
                    "rows": len(ident),
                    "moved_off_page_one": moved,
                }
            )
            if moved:
                found.append({"selector": sel, "value": value})
                break

    out["baseline_page_one"] = baseline
    out["attempts"] = tried
    if found:
        first = found[0]
        out["verdict"] = (
            f"page selector is `{first['selector']}` (first page at value {first['value']})"
        )
        out["all_that_worked"] = found
    else:
        out["verdict"] = "NO page selector found among " + ", ".join(_PAGE_SELECTORS)
        out["why"] = (
            "pageSize is honoured, so query parameters ARE read — which makes this "
            "a real negative rather than a broken probe. ⚠ It does not prove "
            "pagination is impossible: the cursor may live in the response "
            "envelope, or a Link header, or the API may cap rather than page. "
            "Read `envelope_keys` above and the headers before concluding."
        )
    return out


# --------------------------------------------------------------------------
# C. error bodies


def probe_errors(creds: Creds) -> dict[str, Any]:
    out: dict[str, Any] = {
        "question": "C. What is in an error body?",
        "spec_says": "401 has a content schema on 33/175 declarations, 403 on 35/168",
        "cases": [],
    }

    # A bad parameter value on a documented endpoint — a 4xx we caused
    # deliberately, on a path we know exists.
    bad = _get(creds, _COLLECTION, {"pageSize": "not-a-number"})
    out["cases"].append(
        {
            "case": "pageSize=not-a-number on a documented collection",
            "status": bad["status"],
            "content_type": bad["content_type"],
            "parses_as_json": _json(bad["body"]) is not None,
            "top_level_keys": sorted(_json(bad["body"]).keys())
            if isinstance(_json(bad["body"]), dict)
            else None,
            "body_head": (bad["body"] or "")[:400],
        }
    )

    # A path that is definitely not in the spec — the 404 shape.
    missing = _get(creds, "/this-endpoint-does-not-exist-probe")
    out["cases"].append(
        {
            "case": "a path that does not exist",
            "status": missing["status"],
            "content_type": missing["content_type"],
            "parses_as_json": _json(missing["body"]) is not None,
            "body_head": (missing["body"] or "")[:400],
        }
    )

    kinds = {c["content_type"] for c in out["cases"] if c["status"] and c["status"] >= 400}
    if not kinds:
        out["verdict"] = _UNKNOWN
        out["why"] = "neither case produced a 4xx, so no error body was observed"
    else:
        json_ok = all(
            c["parses_as_json"] for c in out["cases"] if c["status"] and c["status"] >= 400
        )
        out["verdict"] = (
            "every observed error body is JSON"
            if json_ok
            else "⚠ at least one error body is NOT JSON — "
            "rentvine_error.rb's both-shapes handling is needed"
        )
    return out


# --------------------------------------------------------------------------
# D. doubled slash


def probe_double_slash(creds: Creds) -> dict[str, Any]:
    clean = _get(creds, _COLLECTION, {"pageSize": 1})
    doubled = _get(creds, creds.base() + "/" + _COLLECTION, {"pageSize": 1}, raw_path=True)
    out = {
        "question": "D. Does a doubled slash normalise?",
        "why_it_matters": "15 of the client's 61 call sites emit /api/manager//...",
        "clean_status": clean["status"],
        "doubled_status": doubled["status"],
        "doubled_url": doubled["url"],
    }
    if clean["status"] != 200:
        out["verdict"] = _UNKNOWN
        out["why"] = "the clean control did not return 200, so the comparison means nothing"
    elif doubled["status"] == 200:
        out["verdict"] = "normalises — both spellings return 200"
        out["note"] = (
            "Write ours WITHOUT the doubled slash anyway. That it is "
            "tolerated today is not a contract."
        )
    else:
        out["verdict"] = (
            f"⚠ does NOT normalise — doubled slash returns "
            f"{doubled['status']} while the clean path returns 200"
        )
        out["note"] = "Any client copying the reference client's leading slash is broken."
    return out


# --------------------------------------------------------------------------
# E. the undocumented paths


def probe_undocumented(creds: Creds, sample_ids: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "question": "E. Do the 5 client call sites the spec omits exist?",
        "reading": (
            "404 → the client invented it. 200 → the SPEC is incomplete, "
            "which is a fact about every other absence in it."
        ),
        "cases": [],
    }
    for template in _UNDOCUMENTED:
        ident = sample_ids.get(template)
        if not ident:
            out["cases"].append(
                {
                    "path": template,
                    "verdict": _UNKNOWN,
                    "why": "no live id was found to substitute, so a 404 would be "
                    "ambiguous between 'no such route' and 'no such record'",
                }
            )
            continue
        path = template.replace("{id}", str(ident))
        r = _get(creds, path)
        out["cases"].append(
            {
                "path": path,
                "status": r["status"],
                "verdict": (
                    "route exists — THE SPEC IS INCOMPLETE"
                    if r["status"] == 200
                    else f"status {r['status']}"
                ),
                "body_head": (r["body"] or "")[:200],
            }
        )
    return out


def probe_bill_path(creds: Creds, sample_ids: dict[str, str]) -> dict[str, Any]:
    """Which path reads a single bill — and therefore which one VOIDS it?

    ⚠ The two sources disagree and it is the most consequential disagreement
    in the whole surface. T1 documents ``/accounting/{billID}`` carrying GET
    *Get Bill*, POST *Update Bill* and **DELETE Void Bill**, with the
    ``bills/`` segment dropped; the working reference client calls
    ``accounting/bills/{billID}``. ``conflicts.md`` lists the client's form as
    "undocumented" and never noticed T1's.

    It matters because ``DELETE /accounting/{billID}`` is the ONLY documented
    reversal of a posted bill anywhere in the API — the money surface has
    exactly one other undo (deposit unreconcile). Picking the wrong path loses
    the single most consequential undo we have.

    And the spec's form is shaped to COLLIDE: ``/accounting/{billID}`` sits in
    the same slot as ``/accounting/accounts``, ``/accounting/ledgers`` and
    ``/accounting/deposits``, so whether the server routes a bare id there at
    all is a real question rather than a typo hunt.

    Read-only: both probes are GETs. The DELETE is never sent — establishing
    which path READS a bill is enough to know where the void lives, and a
    probe that voids a real bill to find out is not a probe.
    """
    ident = sample_ids.get("/accounting/bills/{id}")
    out: dict[str, Any] = {
        "question": "H. Which path reads a single bill — T1's or the client's?",
        "reading": (
            "Both 200 → both exist, use T1's for the void. Only one 200 → that "
            "is the real shape and the other source is wrong. Neither → the "
            "sample id was bad, not the paths."
        ),
        "cases": [],
    }
    if not ident:
        out["verdict"] = _UNKNOWN
        out["why"] = "no bill id was available, so a 404 would be ambiguous"
        return out

    for label, path in (
        ("T1 (spec) — carries DELETE Void Bill", f"/accounting/{ident}"),
        ("T2 (reference client)", f"/accounting/bills/{ident}"),
    ):
        r = _get(creds, path)
        out["cases"].append(
            {
                "source": label,
                "path": path,
                "status": r["status"],
                "body_head": (r["body"] or "")[:200],
            }
        )

    ok = [c for c in out["cases"] if c["status"] == 200]
    if len(ok) == 2:
        out["verdict"] = "BOTH exist — use the spec's path for the void, it is the documented one"
    elif len(ok) == 1:
        out["verdict"] = f"ONLY {ok[0]['source']} answered 200 — {ok[0]['path']} is the real shape"
    else:
        statuses = ", ".join(f"{c['path']}={c['status']}" for c in out["cases"])
        out["verdict"] = f"{_UNKNOWN} — neither answered 200 ({statuses}); check the bill id"
    return out


def collect_sample_ids(creds: Creds) -> dict[str, str]:
    """Find one real id per undocumented path, from DOCUMENTED collections.

    ⚠ Without a real id a 404 cannot be read: 'that route does not exist' and
    'that record does not exist' are the same status. So a path with no id
    available is reported COULD NOT ANSWER rather than guessed at.
    """
    ids: dict[str, str] = {}

    bills = _rows(_json(_get(creds, "/accounting/bills", {"pageSize": 1})["body"]))
    bid = next(iter(_identity(bills)), None)
    if bid:
        ids["/accounting/bills/{id}"] = bid

    statuses = _rows(_json(_get(creds, "/leases/statuses")["body"]))
    sid = next(iter(_identity(statuses)), None)
    if sid:
        ids["/leases/statuses/{id}"] = sid

    leases = _rows(_json(_get(creds, "/leases")["body"]))
    lid = next(iter(_identity(leases)), None)
    if lid:
        ids["/leases/{id}/occupants"] = lid

    # No documented collection lists files — only POST /files exists — so the
    # file id genuinely cannot be sourced here, and that is reported rather
    # than papered over.
    return ids


# --------------------------------------------------------------------------
# F. bare vs /search


def probe_bare_vs_search(creds: Creds) -> dict[str, Any]:
    out: dict[str, Any] = {
        "question": "F. What differs between /owners and /owners/search?",
        "why": (
            "conflicts.md resolved these as BOTH EXIST. The spec says the "
            "/search variants declare pageSize and the bare ones do not. "
            "Which a connector should call is still open."
        ),
        "pairs": [],
    }
    for bare, search in _BARE_VS_SEARCH:
        b = _get(creds, bare)
        s = _get(creds, search, {"pageSize": 1})
        rb, rs = _rows(_json(b["body"])), _rows(_json(s["body"]))
        out["pairs"].append(
            {
                "bare": bare,
                "bare_status": b["status"],
                "bare_rows": len(rb),
                "bare_envelope": sorted(_json(b["body"]).keys())
                if isinstance(_json(b["body"]), dict)
                else None,
                "search": search,
                "search_status": s["status"],
                "search_rows": len(rs),
                "search_envelope": sorted(_json(s["body"]).keys())
                if isinstance(_json(s["body"]), dict)
                else None,
                "search_honours_pageSize": len(rs) == 1,
            }
        )
    return out


# --------------------------------------------------------------------------
# G. identity types — the assumption the whole mirror design now rests on

#: Spec-documented collections, chosen to span the entity families the mirror
#: keys on AND the polymorphic ledger owner. Every path here was checked
#: against the captured spec before being named, so a failure means the server
#: disagreed rather than that we guessed a URL (gotcha 79).
_IDENTITY_COLLECTIONS = [
    ("/properties/search", "property"),
    ("/portfolios/search", "portfolio"),
    ("/tenants/search", "tenant"),
    ("/vendors/search", "vendor"),
    ("/owners/search", "owner"),
    ("/accounting/transactions/search", "transaction"),
]


def _json_type(value: Any) -> str:
    """Name the JSON type as it arrived on the wire.

    ⚠ ``bool`` is checked before ``int`` because ``isinstance(True, int)`` is
    True in Python — the same ordering trap CLAUDE.md gotcha 27 records for
    tool-example validation, where getting it backwards produced a payload the
    provider rejected.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    return type(value).__name__


def _id_fields(row: Any) -> list[tuple[str, Any]]:
    """Every ``*ID`` / ``*IDs`` field on a row, unwrapping one nesting level.

    Only identity keys are read, so nothing here can collect a name, an email
    or an address — the report is safe to paste into an issue.
    """
    inner = row
    if isinstance(row, dict) and len(row) == 1:
        only = next(iter(row.values()))
        if isinstance(only, dict):
            inner = only
    if not isinstance(inner, dict):
        return []
    return [(k, v) for k, v in inner.items() if k.endswith("ID") or k.endswith("IDs")]


def probe_identity_types(creds: Creds) -> dict[str, Any]:
    """Are Rentvine's record ids actually numeric, and in what range?

    ⚠ **This is the assumption ADR 0010 revision 3 rests on and it has never
    been observed.** The decision puts BOTH PMSs' record ids in the identity
    column each mirror table already has — columns that are `bigint`. The
    review that passed the ADR settled the question from the spec and found the
    spec contradicts itself: 126 of 127 ``*ID`` path parameters are declared
    integer, while most ``leaseID``/``propertyID`` *schema* declarations are
    string-typed. A document that disagrees with itself cannot settle it. One
    real GET can.

    Three outcomes, and the middle one is the dangerous one:

    - every id arrives as a JSON **int** → the assumption holds;
    - an id arrives as a **non-numeric string** → it is REFUTED, and a bigint
      column cannot hold it;
    - an id arrives as a **numeric string** (``"4471"``) → the subtle case. It
      coerces into a bigint without complaint, so nothing fails at write time,
      and the assumption is still wrong about the wire. Worth knowing before a
      client starts comparing ids.

    It also records the observed numeric RANGE, because revision 3 made
    ``[needs founder decision] a1`` (one PMS per company) load-bearing: with
    both systems' ids in one column, a Rentvine and a Buildium record sharing
    an id silently overwrite each other. Buildium issues small integers; if
    Rentvine's land in the same range, that collision is likely rather than
    exotic, and the range is what says so.
    """
    fields: dict[str, dict[str, int]] = {}
    numeric: list[int] = []
    numeric_strings: list[str] = []
    non_numeric_strings: list[tuple[str, str]] = []
    reached: list[str] = []
    unreadable: list[dict[str, Any]] = []

    for path, family in _IDENTITY_COLLECTIONS:
        params = {"pageSize": 5} if path != "/leases" else {}
        r = _get(creds, path, params)
        if r["status"] != 200:
            unreadable.append({"path": path, "status": r["status"], "error": r["error"]})
            continue
        rows = _rows(_json(r["body"]))
        if not rows:
            unreadable.append(
                {"path": path, "status": 200, "error": "no rows / unrecognised envelope"}
            )
            continue
        reached.append(family)
        for row in rows:
            for key, value in _id_fields(row):
                bucket = fields.setdefault(key, {})
                kind = _json_type(value)
                bucket[kind] = bucket.get(kind, 0) + 1
                if kind == "int":
                    numeric.append(value)
                elif kind == "str":
                    if value.strip().lstrip("-").isdigit():
                        numeric_strings.append(value)
                    else:
                        non_numeric_strings.append((key, value[:24]))

    if not reached:
        return {
            "question": "G. Are Rentvine record ids numeric, and in what range?",
            "verdict": _UNKNOWN,
            "detail": "no collection returned readable rows",
            "unreadable": unreadable,
        }

    if non_numeric_strings:
        sample = ", ".join(f"{k}={v!r}" for k, v in non_numeric_strings[:4])
        verdict = (
            f"REFUTED — {len(non_numeric_strings)} id value(s) are non-numeric strings "
            f"({sample}). A bigint identity column CANNOT hold these; ADR 0010 "
            f"Decision (a) needs re-opening before Phase 3 writes the migration."
        )
    elif numeric_strings:
        verdict = (
            f"QUALIFIED — every id is numeric, but {len(numeric_strings)} arrived as JSON "
            f"STRINGS, not integers. A bigint column coerces them silently, so nothing "
            f"fails at write time; any code that COMPARES ids must normalise first."
        )
    else:
        verdict = f"CONFIRMED — every id across {len(reached)} families arrived as a JSON integer."

    if numeric:
        lo, hi = min(numeric), max(numeric)
        collision = (
            "OVERLAPS Buildium's range" if lo < 10_000_000 else "above Buildium's usual range"
        )
        verdict += (
            f" Observed numeric range {lo}-{hi} ({collision}), so the one-PMS-per-company "
            f"rule (a1) is what prevents a silent overwrite."
        )

    return {
        "question": "G. Are Rentvine record ids numeric, and in what range?",
        "verdict": verdict,
        "families_reached": reached,
        "types_by_field": {k: dict(sorted(v.items())) for k, v in sorted(fields.items())},
        "numeric_range": [min(numeric), max(numeric)] if numeric else None,
        "numeric_string_samples": numeric_strings[:8],
        "non_numeric_string_samples": [f"{k}={v}" for k, v in non_numeric_strings[:8]],
        "unreadable": unreadable,
    }


# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# I. the two ABSENCE claims the mapper leaves NULL on

#: (path, family, key substrings that would REFUTE the claim, what rests on it)
#:
#: ⚠ SUBSTRINGS, not exact names. `map_lease`'s claim is that Rentvine carries
#: no deposit on a lease AT ALL — `deposit`, `securityDeposit` and
#: `depositAmount` each refute it, and picking one spelling to look for is
#: gotcha 79's error (an identifier somebody expects is not evidence about the
#: wire). The scan is case-insensitive for the same reason.
#: ``(path, family, needles, rests_on, subject, not_amounts)``.
#:
#: ``subject`` is the record a row is WRAPPED in on the wire (``/leases`` sends
#: ``{lease, unit}``; ``_row_envelopes.ROW_ENVELOPE`` is generated from the
#: spec, and ``test_rentvine_identity_probe`` pins these to it). The keys are
#: read off THAT record. Reading the wrapper's keys compared ``{lease, unit}``
#: against "deposit" and passed without looking at a lease (2026-10-03).
#:
#: ``not_amounts`` are keys that match a needle and are documented as something
#: else: ``depositRefundDueDate`` is the refund DEADLINE (a date, spec
#: ``updateLease``), and reading it as a deposit would report a discarded
#: amount that does not exist. A sibling record (the row's ``unit``, whose
#: ``deposit`` is the unit's default asking deposit) is never read at all.
_ABSENCE_CLAIMS: tuple[tuple[str, str, tuple[str, ...], str, str, tuple[str, ...]], ...] = (
    (
        "/leases",
        "lease",
        ("deposit",),
        "map_lease writes security_deposit=None because Rentvine is believed to "
        "carry no deposit on a lease. A deposit field would mean the mirror is "
        "nulling a number it could have had.",
        "lease",
        ("depositRefundDueDate",),
    ),
    (
        "/owners/search",
        "owner",
        ("propert",),
        "map_owner writes property_ids=[] because ownership is modelled through "
        "portfolios rather than a property list. A property field would mean the "
        "portfolio join is unnecessary work.",
        "contact",
        (),
    ),
)


def probe_row_shapes(creds: Creds) -> dict[str, Any]:
    """Do lease and owner rows carry fields the mapper says they do NOT?

    Two mapper fields are written as a deliberate BLANK —
    ``security_deposit=None`` and ``property_ids=[]`` — on the strength of a
    claim about what Rentvine does not send. Both blanks are correct if the
    claim holds and are data thrown away if it does not, so the claim is worth
    one GET each.

    ⚠ **AN ABSENCE CANNOT BE PROVED FROM ONE ROW, and this probe does not
    pretend otherwise.** A server that omits null fields shows a key on the row
    that has a value and hides it on the row that does not, so a single row
    absent a field says nothing. The evidence here is the UNION of keys across
    every row the page returns, and the row count is reported beside the
    verdict because it is what the verdict is worth.

    ⚠ **And a union over N rows is still not the schema.** If every sampled
    lease happens to have no deposit, the field can still exist on one that
    does. So a clean result is stated as *no row carried one*, never as
    *Rentvine has no such field* — which is gotcha 26's rule (a truncated read
    is not evidence of absence) applied to the tool that would otherwise make
    exactly that mistake.
    """
    findings: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = []

    for path, family, needles, rests_on, subject, not_amounts in _ABSENCE_CLAIMS:
        r = _get(creds, path, {"pageSize": 25})
        if r["status"] != 200:
            unreadable.append({"path": path, "status": r["status"], "error": r["error"]})
            continue
        rows = [row for row in _rows(_json(r["body"])) if isinstance(row, dict)]
        if not rows:
            unreadable.append(
                {"path": path, "status": 200, "error": "no rows / unrecognised envelope"}
            )
            continue

        keys: set[str] = set()
        wrapped = 0
        for row in rows:
            record = row.get(subject)
            if isinstance(record, dict):
                wrapped += 1
            else:
                record = row  # a bare record: the spec's shape for some paths
            keys.update(record.keys())
        matched = sorted(
            k for k in keys if any(n in k.lower() for n in needles) and k not in not_amounts
        )

        findings.append(
            {
                "family": family,
                "path": path,
                "rows_sampled": len(rows),
                # How many rows arrived wrapped in ``subject``; the rest were
                # read as bare records. Both are reported because a mixed
                # page is itself a finding about the envelope.
                "subject": subject,
                "wrapped_rows": wrapped,
                "rests_on": rests_on,
                # The COMPLETE key union is the artifact. A future reader
                # settling a different field should not need another live run.
                "keys": sorted(keys),
                "not_amounts_seen": sorted(k for k in keys if k in not_amounts),
                "matching_keys": matched,
                "verdict": (
                    f"REFUTED — {family} rows carry {matched}. The mapper is "
                    f"discarding a field Rentvine sends."
                    if matched
                    else f"CONSISTENT — no {family} row of {len(rows)} carried a "
                    f"{'/'.join(needles)} field. NOT proof the field does not exist: "
                    f"a server that omits nulls would look identical."
                ),
            }
        )

    if not findings:
        return {
            "question": "I. Do lease/owner rows carry the fields the mapper leaves blank?",
            "verdict": _UNKNOWN,
            "detail": "neither collection returned readable rows",
            "unreadable": unreadable,
        }

    refuted = [f for f in findings if f["matching_keys"]]
    return {
        "question": "I. Do lease/owner rows carry the fields the mapper leaves blank?",
        "verdict": (
            f"REFUTED on {len(refuted)} of {len(findings)} — "
            + "; ".join(f"{f['family']}: {f['matching_keys']}" for f in refuted)
            if refuted
            else f"CONSISTENT on all {len(findings)} — no sampled row carried the "
            f"fields the mapper leaves blank. Read each finding's row count: a "
            f"clean sample is weaker evidence than a schema."
        ),
        "findings": findings,
        "unreadable": unreadable,
    }


# --------------------------------------------------------------------------
# J. is `unit.leaseID` actually an occupancy signal?


def probe_unit_occupancy(creds: Creds) -> dict[str, Any]:
    """Does ``unit.leaseID`` clear when a unit is vacant?

    ``map_unit`` has no occupancy flag to read — Rentvine declares none — so
    "has a lease attached" is the only available reading, and the mapper says
    in as many words that it is an INFERENCE rather than a field. This settles
    whether the inference holds.

    ⚠ **THE CONTROL IS THE WHOLE PROBE.** "Vacant units carry a leaseID" is
    only a finding if OCCUPIED units are different; if every unit carries one,
    the field says nothing about occupancy and a one-sided look would report
    the same thing either way. So both groups are counted and the verdict is
    the DIFFERENCE between them.

    Occupancy is read off ``primaryLeaseStatusID`` through the same
    ``LEASE_STATUS`` map the mapper uses — 2 is Active. A unit whose status is
    anything else is the vacant group.
    """
    rows: list[dict[str, Any]] = []
    source = ""

    # The export is the widest sample available in one call. It may not answer
    # (that is probe K's question), so a per-property fallback follows.
    exp = _get(creds, "/properties/units/export", {"pageSize": 200})
    if exp["status"] == 200:
        rows = [r for r in _rows(_json(exp["body"])) if isinstance(r, dict)]
        if rows:
            source = "/properties/units/export"

    if not rows:
        props = _rows(_json(_get(creds, "/properties/search", {"pageSize": 5})["body"]))
        for prop in props:
            pid = prop.get("propertyID") if isinstance(prop, dict) else None
            if pid is None:
                continue
            r = _get(creds, f"/properties/{pid}/units")
            if r["status"] != 200:
                continue
            rows.extend(x for x in _rows(_json(r["body"])) if isinstance(x, dict))
            source = "/properties/{propertyID}/units"

    if not rows:
        return {
            "question": "J. Does unit.leaseID clear when a unit is vacant?",
            "verdict": _UNKNOWN,
            "detail": "no route returned readable unit rows",
        }

    def _has_lease(row: dict[str, Any]) -> bool:
        return row.get("leaseID") not in (None, "", 0, "0")

    occupied = [r for r in rows if r.get("primaryLeaseStatusID") == 2]
    vacant = [r for r in rows if r.get("primaryLeaseStatusID") not in (2, None)]
    unknown_status = len(rows) - len(occupied) - len(vacant)

    vacant_with = sum(1 for r in vacant if _has_lease(r))
    occupied_with = sum(1 for r in occupied if _has_lease(r))

    if not vacant:
        verdict = (
            f"{_UNKNOWN} — {len(rows)} unit(s) sampled and none was non-Active, so "
            f"there is no vacant group to compare against. Re-run against an "
            f"account that has one."
        )
    elif not occupied:
        verdict = (
            f"{_UNKNOWN} — no Active unit in the sample, so the control is missing "
            f"and 'vacant units carry a leaseID' cannot be distinguished from "
            f"'every unit carries one'."
        )
    elif vacant_with == 0 and occupied_with > 0:
        verdict = (
            f"CONFIRMED — leaseID is set on {occupied_with}/{len(occupied)} Active "
            f"units and 0/{len(vacant)} non-Active ones. The inference holds and "
            f"the mapper's reading is sound."
        )
    elif vacant_with == len(vacant) and occupied_with == len(occupied):
        verdict = (
            f"REFUTED — leaseID is set on every unit in BOTH groups "
            f"({occupied_with}/{len(occupied)} Active, {vacant_with}/{len(vacant)} "
            f"non-Active), so it carries no occupancy information at all. Anything "
            f"reading it as occupancy is reporting a fact the server did not send."
        )
    else:
        verdict = (
            f"QUALIFIED — leaseID is set on {occupied_with}/{len(occupied)} Active "
            f"and {vacant_with}/{len(vacant)} non-Active units. It CORRELATES with "
            f"occupancy without determining it, so the mapper is right to call it "
            f"an inference and wrong to rely on it alone."
        )

    return {
        "question": "J. Does unit.leaseID clear when a unit is vacant?",
        "verdict": verdict,
        "source": source,
        "units_sampled": len(rows),
        "active_units": len(occupied),
        "active_with_lease_id": occupied_with,
        "non_active_units": len(vacant),
        "non_active_with_lease_id": vacant_with,
        "units_with_no_status": unknown_status,
    }


# --------------------------------------------------------------------------
# K. does the units export carry the property link outside the row?


def probe_units_export_envelope(creds: Creds) -> dict[str, Any]:
    """Is the per-property N+1 units fetch avoidable?

    ``sync.py`` fetches units one property at a time and says why: the bulk
    ``/properties/units/export`` route's ``unit_export`` schema carries no
    ``propertyID``, and a unit row with no property is not a cheaper unit row,
    it is a broken one. The open question is whether the ENVELOPE carries the
    link even though the row schema does not — in which case the sync becomes
    one call instead of N.

    ⚠ **LOOK AT THE ENVELOPE, NOT THE ROWS.** That is the whole question, and
    it is the half a reader skims past: the rows have already been read (they
    are why the route was rejected). So the top-level keys are recorded
    verbatim, and the row keys only to say again what is missing from them.
    """
    r = _get(creds, "/properties/units/export", {"pageSize": 25})
    if r["status"] != 200:
        return {
            "question": "K. Does /properties/units/export carry the property link?",
            "verdict": _UNKNOWN,
            "detail": f"status {r['status']}",
            "status": r["status"],
            "error": r["error"],
        }

    payload = _json(r["body"])
    envelope_keys = sorted(payload.keys()) if isinstance(payload, dict) else []
    rows = [x for x in _rows(payload) if isinstance(x, dict)]
    row_keys: set[str] = set()
    for row in rows:
        row_keys.update(row.keys())

    row_link = sorted(k for k in row_keys if "propert" in k.lower())
    env_link = sorted(k for k in envelope_keys if "propert" in k.lower())

    if row_link:
        verdict = (
            f"CHANGE THE SYNC — the export's ROWS carry {row_link}, so the "
            f"per-property N+1 is unnecessary. sync.py's UNITS_PATH comment is "
            f"wrong about the row schema and should be corrected, not just the code."
        )
    elif env_link:
        verdict = (
            f"CHANGE THE SYNC — the rows carry no property key, but the ENVELOPE "
            f"carries {env_link}. That is exactly the case the marker asks about: "
            f"one call replaces N."
        )
    elif not rows:
        verdict = (
            f"{_UNKNOWN} — the route answered 200 with no readable rows "
            f"(envelope keys: {envelope_keys or 'not an object'}), so the link "
            f"question is unanswered rather than answered 'no'."
        )
    else:
        verdict = (
            f"KEEP THE N+1 — neither the {len(rows)} sampled rows nor the envelope "
            f"({envelope_keys or 'not an object'}) carries a property key. The "
            f"per-property fetch stays, and sync.py's reasoning is confirmed."
        )

    return {
        "question": "K. Does /properties/units/export carry the property link?",
        "verdict": verdict,
        "envelope_keys": envelope_keys,
        "envelope_property_keys": env_link,
        "rows_sampled": len(rows),
        "row_keys": sorted(row_keys),
        "row_property_keys": row_link,
    }


# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    ap.add_argument("--out", default="docs/rentvine/probe-report.json")
    ap.add_argument(
        "--check",
        action="store_true",
        help="report which credentials are set and exit; sends nothing",
    )
    args = ap.parse_args(argv)

    try:  # Windows console: the report prints ⚠ (CLAUDE.md gotcha 49)
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    creds = Creds()

    if args.check:
        print("Credentials (set/unset only — no values are printed):")
        for name in ("RENTVINE_ACCOUNT_CODE", "RENTVINE_API_KEY", "RENTVINE_API_SECRET"):
            print(f"  {name:24} {'set' if os.environ.get(name, '').strip() else 'UNSET'}")
        return 0 if creds.complete else 2

    if not creds.complete:
        print("VERDICT: COULD NOT RUN — missing " + ", ".join(creds.missing))
        print("\nSet them in the environment, never as arguments, one line at a time:")
        print('  $env:RENTVINE_ACCOUNT_CODE = "<your subdomain>"')
        print('  $s = Read-Host "RENTVINE_API_KEY" -AsSecureString')
        print('  $env:RENTVINE_API_KEY      = [Net.NetworkCredential]::new("", $s).Password')
        print('  $s = Read-Host "RENTVINE_API_SECRET" -AsSecureString')
        print('  $env:RENTVINE_API_SECRET   = [Net.NetworkCredential]::new("", $s).Password')
        print("\nExit 2 is not a pass — nothing was validated.")
        return 2

    print(f"Rentvine read-only probe — {creds.base()}")
    print("No request in this file can be anything but a GET.\n")

    report: dict[str, Any] = {
        "base_url": creds.base(),
        "probe": "scripts/rentvine_probe.py",
        "read_only": True,
        "probes": [],
    }

    auth = probe_auth(creds)
    report["probes"].append(auth)
    print(f"A. {auth['verdict']}")
    if not auth["ok"]:
        report["verdict"] = "COULD NOT RUN — auth failed"
        _write(args.out, report)
        print("\nVERDICT: COULD NOT RUN. Everything below depends on this, so nothing else ran.")
        print(f"Report: {args.out}")
        return 2

    for fn, label in (
        (probe_pagination, "B"),
        (probe_errors, "C"),
        (probe_double_slash, "D"),
        (probe_bare_vs_search, "F"),
        (probe_identity_types, "G"),
        (probe_row_shapes, "I"),
        (probe_unit_occupancy, "J"),
        (probe_units_export_envelope, "K"),
    ):
        result = fn(creds)
        report["probes"].append(result)
        print(f"{label}. {result.get('verdict', '(see report)')}")

    ids = collect_sample_ids(creds)
    undoc = probe_undocumented(creds, ids)
    report["probes"].append(undoc)
    found = [c for c in undoc["cases"] if c.get("status") == 200]
    unknown_paths = [c for c in undoc["cases"] if c.get("verdict") == _UNKNOWN]
    print(
        f"E. {len(found)} of {len(undoc['cases'])} undocumented paths answered 200"
        f"{f' ({len(unknown_paths)} unanswerable — no live id)' if unknown_paths else ''}"
    )

    bills = probe_bill_path(creds, ids)
    report["probes"].append(bills)
    print(f"H. {bills.get('verdict', '(see report)')}")

    unanswered = [p["question"] for p in report["probes"] if p.get("verdict") == _UNKNOWN]
    report["unanswered"] = unanswered
    report["verdict"] = "ALL ANSWERED" if not unanswered else "PARTIAL"

    _write(args.out, report)
    print(f"\nReport: {args.out}")
    print("Send that FILE rather than retyping — a hand-copied result tests the transcription.")

    if unanswered:
        print("\n⚠ NOT ANSWERED, and these are not passes:")
        for q in unanswered:
            print(f"  - {q}")
        return 1

    print("\nStill open and NOT probed here: whether a POST to a record path replaces")
    print("or merges. That is a write, and it needs its own gated harness.")
    return 0


def _write(path: str, report: dict[str, Any]) -> None:
    # ⚠ mkdir FIRST. This runs AFTER every probe, so a missing parent directory
    # raises FileNotFoundError on the last line of a live, credentialed run and
    # takes the whole result with it — gotcha 49's shape exactly ("a crash on
    # the LAST line of a long run destroys the report"), and this run cannot be
    # repeated without the operator's own API credentials.
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:  # gotcha 49: never the locale default
        json.dump(report, fh, indent=2, sort_keys=True)


if __name__ == "__main__":
    raise SystemExit(main())
