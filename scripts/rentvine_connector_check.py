#!/usr/bin/env python3
"""Does the Rentvine CONNECTOR work — the four routes, against a running backend.

This is NOT the probe and NOT Phase 8, and the difference is the whole reason
it exists:

    scripts/rentvine_probe.py     — do these credentials reach Rentvine?
    scripts/rentvine_phase8.py    — does the mirror match a real account?
    THIS FILE                     — do OUR four routes work?

``api/routes/rentvine.py`` shipped with 27 unit tests and no operator harness,
so every claim about it was a claim about fakes. The routes are the only way a
credential can reach ``company_rentvine_credentials`` (gotcha 153), so "the
connector works" is a question about THESE FOUR CALLS and nothing else:

    GET    /api/v1/rentvine/credentials   is it mounted, am I an admin, is one stored
    PUT    /api/v1/rentvine/credentials   store it, encrypted
    POST   /api/v1/rentvine/test          does the stored pair reach Rentvine
    DELETE /api/v1/rentvine/credentials   forget it, and purge the mirror

⚠ `--discover` IS THE ONLY WAY TO EXERCISE THE PATH A CUSTOMER TAKES, and
without it nothing anywhere does. ``account_code`` is OPTIONAL on the PUT —
absent, ``rentvine/discovery.py`` generates candidate subdomains from the
company's name and the admin's email domain and probes each one with the key
pair itself, so a customer pastes two keys and nothing else. The default
`--execute` here always SENDS an ``account_code`` and then asserts the stored
value equals what it sent, which means it is structurally incapable of
reaching that code. Whether discovery resolves against a live Rentvine is
therefore untested by anything in this repo.

    `--execute`              the supplied path: we say the subdomain
    `--execute --discover`   the CUSTOMER path: the route finds it

⚠ AND A `--discover` RUN IS THE FIRST OBSERVATION OF A WRONG SUBDOMAIN. Every
candidate the route probes is a request to a host this credential does not
own, and ``discovery.py`` says in its own docstring that nobody has ever
watched a miss arrive — it treats a DNS failure, a TLS mismatch, a 404 and a
401 identically BECAUSE it cannot tell them apart from here (ADR 0010 blocker
B1). The elapsed time this harness prints is the evidence.

⚠ THE COMPANY IS THE TOKEN'S, NOT AN ARGUMENT. The routes read
``user.company_ids[0]``, so this harness acts as whoever the JWT belongs to and
cannot be pointed somewhere else. That is a safety property and a trap: the
disconnect purges THAT company's mirror. Use a throwaway workspace.

⚠ WHAT `--disconnect` ACTUALLY DELETES. ``purge_rentvine_mirror`` delegates to
``buildium/purge.PURGE_TABLES`` — one mirror, one list (ADR 0010 a1) — so it
removes every company-scoped row across 30 tables, NOT just Rentvine-era ones.
A workspace that holds Buildium-era mirror rows with no Buildium credentials row
(gotcha 62's shape) can accept a Rentvine credential and then lose those rows to
this flag. Hence the second, explicit acknowledgement.

Exit codes, and the middle one is the point:

    0   the routes did what they claim
    1   a real finding — the routes answered and the answer is wrong
    2   COULD NOT RUN, which is never a pass (LED-013's skip-floor)

⚠ EXIT 2 COVERS FOUR HEALTHY STATES, and reporting any of them as a defect
would send somebody after a phantom:

  * the connector is DARK (``PUT`` answers 503). That is the shipped state —
    ``render.yaml`` declares ``HELIXIS_RENTVINE__ENABLED`` false on web AND
    worker — so a 503 here means "flip the flag", not "the route is broken".
  * the workspace is already on Buildium (``PUT`` answers 409). Migration
    0100's trigger enforces one PMS per company; the refusal is the rule
    working.
  * the caller is not an admin (403). Every route here is admin-floored.
  * the input was rejected (422). Under `--discover` that is the honest
    refusal — no candidate authenticated, so the route asked for the subdomain
    rather than storing a guess; otherwise it is the supplied ``account_code``
    or key failing validation. Both are the route working, and both leave the
    workspace UNCONNECTED, which is why they cannot exit 0: a ``--execute``
    that exits 0 means "go and run Phase 8".

Credentials are read from the environment and NEVER from argv — they would
otherwise land in shell history and in ``ps``. The variable names are the ones
``rentvine_probe.py`` already uses, deliberately: an operator who ran the probe
has already done this step (gotcha 73).
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Any

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - exercised by the env report
    httpx = None  # type: ignore[assignment]

BASE_URL_ENV = "HELIXIS_RENTVINE_CHECK_BASE_URL"
TOKEN_ENV = "HELIXIS_RENTVINE_CHECK_TOKEN"
ACCOUNT_ENV = "RENTVINE_ACCOUNT_CODE"
KEY_ENV = "RENTVINE_API_KEY"
SECRET_ENV = "RENTVINE_API_SECRET"

#: Reading the connector's state needs only a way in.
READ_ENV = (BASE_URL_ENV, TOKEN_ENV)
#: Storing one additionally needs the credential itself.
WRITE_ENV = READ_ENV + (ACCOUNT_ENV, KEY_ENV, SECRET_ENV)
#: Discovering one needs the two KEYS and not the subdomain — that is the point.
#:
#: ⚠ ``RENTVINE_ACCOUNT_CODE`` stays OPTIONAL here and becomes the CONTROL. A
#: discovered subdomain with nothing to check it against is the route's word
#: for it; the same subdomain checked against one the operator already knows is
#: right is a measurement. Requiring it would delete the mode's whole point;
#: ignoring it would throw away the only thing that makes a hit evidence.
DISCOVER_ENV = READ_ENV + (KEY_ENV, SECRET_ENV)

TIMEOUT_S = 60.0
"""``POST /test`` makes a live Rentvine call behind ``RETRY_BACKOFF_S``.

Gotcha 147: a retryable status costs the whole schedule before the route
answers, so a short client timeout would report our own impatience as a
connector failure.
"""


_MISS_TIMING_NOTE = """\
⚠ THE ELAPSED TIME ABOVE IS A MEASUREMENT NOBODY HAS TAKEN BEFORE. Candidates
are probed CONCURRENTLY, so the PUT cannot return until the SLOWEST miss does.
A run costing about one per-candidate timeout (discovery.PROBE_TIMEOUT_S) means
a wrong subdomain HANGS; a fast run means it fails fast (DNS or a 404). Write
down which — discovery.py's largest @unverified is exactly this, and it governs
how long a customer waits on the connect form when the guess is wrong."""


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _report_env(required: tuple[str, ...]) -> list[str]:
    """Print which variables are set. Never print a VALUE."""
    print("Environment:")
    missing = []
    for name in required:
        present = bool(_env(name))
        print(f"  {name:38} {'set' if present else 'UNSET'}")
        if not present:
            missing.append(name)
    return missing


def _url(path: str) -> str:
    return _env(BASE_URL_ENV).rstrip("/") + "/api/v1/rentvine" + path


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_env(TOKEN_ENV)}"}


def _detail(resp: Any) -> str:
    """The backend's own sentence, which is written for a person to read.

    ``api/routes/rentvine.py`` spends real care on these — the 409 names the
    rule and the remedy, the 503 says the connector is dark. Replacing them
    with a status line would throw away the only useful thing in the response
    (gotcha 137).
    """
    try:
        body = resp.json()
    except Exception:
        return (resp.text or "")[:300]
    if isinstance(body, dict):
        raw = body.get("detail")
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            return str(raw.get("error") or raw.get("message") or raw)
    return str(body)[:300]


def _leaked_fields(body_text: str, *, key: str, secret: str) -> list[str]:
    """Which secret material came back on the read.

    ⚠ ``api_key_last4`` is returned BY DESIGN, so a four-character key equals
    its own last4 and would false-positive. ``RentvineCredentialsInput`` allows
    ``min_length=4``, so the case is reachable; a real Rentvine key is long.
    The secret has no last4 at all (a deliberate omission in the response
    model), so any occurrence of it is unambiguous.
    """
    leaked = []
    if secret and secret in body_text:
        leaked.append("api_secret")
    if key and len(key) > 4 and key in body_text:
        leaked.append("api_key")
    return leaked


def _get_status(client: Any) -> tuple[int, Any]:
    resp = client.get(_url("/credentials"), headers=_headers())
    return resp.status_code, resp


def _print_status(body: dict[str, Any]) -> None:
    if not body.get("configured"):
        print("  configured       no — this workspace has no Rentvine credential stored")
        return
    print("  configured       yes")
    print(f"  account_code     {body.get('account_code')}")
    print(f"  api_key_last4    {body.get('api_key_last4')}")
    print(f"  last_tested_at   {body.get('last_tested_at') or 'never'}")


def _read(client: Any) -> int:
    """The read-only default. Proves mounting, auth and the admin floor."""
    code, resp = _get_status(client)
    if code == 403:
        print(f"\nVERDICT: COULD NOT RUN — {_detail(resp)}")
        print("Every route here is admin-floored. Use an admin's token.")
        return 2
    if code == 404:
        print("\nVERDICT: FINDING — GET /api/v1/rentvine/credentials answered 404.")
        print("The router is mounted unconditionally in main.py, so a 404 means this")
        print("deployment is running a build from before that landed.")
        return 1
    if code != 200:
        print(f"\nVERDICT: FINDING — GET answered {code}: {_detail(resp)}")
        return 1

    print("Stored credential:")
    _print_status(resp.json())
    print("\nVERDICT: THE ROUTES ANSWER — read path proven (mounted, authed, admin).")
    return 0


def _connect(client: Any, *, discover: bool = False) -> int:
    """Store a credential, read it back, and ask Rentvine whether it works.

    With ``discover`` the ``account_code`` is OMITTED from the body rather than
    sent empty — that is what a customer's form posts, and it is the only input
    that reaches ``_resolve_account_code``'s discovery branch.
    """
    account, key, secret = _env(ACCOUNT_ENV), _env(KEY_ENV), _env(SECRET_ENV)

    body: dict[str, str] = {"api_key": key, "api_secret": secret}
    if discover:
        print("PUT /credentials  (no account_code — the route discovers it)")
        print(f"  control          {ACCOUNT_ENV} {'set' if account else 'UNSET'}")
    else:
        body["account_code"] = account
        print(f"PUT /credentials  (account_code={account})")

    started = time.monotonic()
    put = client.put(_url("/credentials"), headers=_headers(), json=body)
    elapsed = time.monotonic() - started
    print(f"  elapsed          {elapsed:.1f}s")

    if put.status_code == 422:
        # ⚠ NOT a finding either way: the route answered, and correctly. In
        # discover mode it is the honest refusal the module promises when no
        # candidate authenticates; in supplied mode it is the input being
        # rejected. Reporting either as a defect points the operator at the
        # connector when the thing to change is a subdomain or a key.
        print(f"\n{_detail(put)}")
        if discover:
            print("\nVERDICT: DISCOVERY DECLINED — the fallback works, and discovery is")
            print("UNPROVEN for this account. No candidate subdomain authenticated, so")
            print("the route asked for one instead of storing a guess. Nothing was")
            print("stored, so the workspace is unchanged.")
            print(f"\n{_MISS_TIMING_NOTE}")
            print(f"\nNext: set {ACCOUNT_ENV} and re-run WITHOUT --discover.")
        else:
            print("\nVERDICT: COULD NOT RUN — the route rejected the input, which is the")
            print("route working. An account_code is the SUBDOMAIN — the word before")
            print("'.rentvine.com' — not a URL and not a company name.")
        return 2
    if put.status_code == 503:
        print(f"\nVERDICT: COULD NOT RUN — {_detail(put)}")
        print("This is the SHIPPED state, not a defect: render.yaml declares")
        print("HELIXIS_RENTVINE__ENABLED false on web and worker. The read and the")
        print("disconnect deliberately still work while dark; only the store refuses.")
        return 2
    if put.status_code == 409:
        print(f"\nVERDICT: COULD NOT RUN — {_detail(put)}")
        print("Migration 0100 allows one PMS per workspace. Use a throwaway workspace")
        print("with no Buildium connection.")
        return 2
    if put.status_code == 403:
        print(f"\nVERDICT: COULD NOT RUN — {_detail(put)}")
        return 2
    if put.status_code != 200:
        print(f"\nVERDICT: FINDING — PUT answered {put.status_code}: {_detail(put)}")
        return 1

    code, resp = _get_status(client)
    if code != 200:
        print(f"\nVERDICT: FINDING — the credential stored but GET answered {code}.")
        return 1
    body = resp.json()
    print("Read back:")
    _print_status(body)

    findings = []
    stored = body.get("account_code")
    if not body.get("configured"):
        findings.append("the PUT returned 200 and the read-back says nothing is stored")
    if discover:
        # ⚠ The route promises NOTHING IS STORED THAT WAS NOT VERIFIED — a
        # discovered code is only returned when the credential authenticated
        # against it. Both checks below test that promise rather than the
        # plumbing: an empty code means it stored a blank host, and a code that
        # contradicts the operator's own means either the credential
        # authenticated somewhere it should not have or ADR 0010's one-
        # credential-one-tenant model does not hold.
        if not stored:
            findings.append("discovery answered 200 and stored no account_code at all")
        elif account and stored != account:
            findings.append(
                f"discovery stored {stored!r} while {ACCOUNT_ENV} says {account!r} — "
                "the route stored a subdomain that is not this account's"
            )
    elif stored != account:
        findings.append(f"the stored account_code reads {stored!r}, not {account!r}")
    leaked = _leaked_fields(resp.text, key=key, secret=secret)
    if leaked:
        findings.append("the read returned secret material verbatim: " + ", ".join(leaked))
    if "api_secret_last4" in body:
        findings.append(
            "the read carries api_secret_last4 — the response model omits it on purpose, "
            "because a secret has no half that is safe to show"
        )
    if findings:
        print("\nVERDICT: FINDING")
        for f in findings:
            print(f"  - {f}")
        return 1

    print("\nPOST /test  (one live Rentvine call)")
    test = client.post(_url("/test"), headers=_headers())
    if test.status_code != 200:
        print(f"\nVERDICT: FINDING — /test answered {test.status_code}: {_detail(test)}")
        return 1
    result = test.json()
    if not result.get("ok"):
        # The ROUTE worked — it classified the failure and handed back advice.
        # What failed is the credential, so say which (gotcha 102's distinction).
        print("\nVERDICT: FINDING — Rentvine refused the stored credential.")
        print(f"  {result.get('error')}")
        print("\nThe route did its job: it classified the failure and returned advice.")
        print("The credential is what needs fixing, not the connector.")
        return 1

    print(f"  ok               yes — Rentvine reports {result.get('properties_count')} properties")
    if not discover:
        print("\nVERDICT: THE CONNECTOR WORKS — stored encrypted, read back masked, live call OK.")
        print("The workspace is now CONNECTED. Next: scripts/rentvine_phase8.py --execute")
        return 0

    print(f"\nVERDICT: THE CONNECTOR WORKS, AND DISCOVERY RESOLVED {stored!r}")
    if account:
        print(f"Checked against {ACCOUNT_ENV}: discovery found the subdomain you already")
        print("knew was right. A customer can connect by pasting two keys.")
    else:
        # ⚠ A hit means the credential authenticated against that host, which
        # under ADR 0010's model identifies the tenant. Without a control that
        # is the MODEL's word rather than a measurement, and the model is the
        # thing most worth testing — so this must not read as the checked case.
        print(f"⚠ UNCHECKED: {ACCOUNT_ENV} is unset, so nothing here can say that is the")
        print("right subdomain — only that the credential authenticates against it.")
        print(f"Set {ACCOUNT_ENV} and re-run to turn this into a measurement.")
    print(f"\n{_MISS_TIMING_NOTE}")
    print("\nThe workspace is now CONNECTED. Next: scripts/rentvine_phase8.py --execute")
    return 0


def _disconnect(client: Any) -> int:
    resp = client.delete(_url("/credentials"), headers=_headers())
    if resp.status_code == 403:
        print(f"\nVERDICT: COULD NOT RUN — {_detail(resp)}")
        return 2
    if resp.status_code != 200:
        print(f"\nVERDICT: FINDING — DELETE answered {resp.status_code}: {_detail(resp)}")
        return 1
    body = resp.json()
    purged = body.get("purged_rows")
    print(f"  purged_rows      {purged}")
    if body.get("configured"):
        print("\nVERDICT: FINDING — the DELETE returned 200 and the credential is still stored.")
        return 1
    if purged is None:
        print("\nVERDICT: FINDING — the disconnect reported no purge count at all.")
        print("ADR 0010 e1 requires the mirror to go with the credential; a missing")
        print("count means nothing can say whether it did.")
        return 1
    print("\nVERDICT: DISCONNECTED — credential forgotten and the mirror purged.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="report which variables are set and send NOTHING",
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="store the credential and make one live Rentvine call",
    )
    ap.add_argument(
        "--discover",
        action="store_true",
        help="with --execute: omit account_code and let the route discover it",
    )
    ap.add_argument(
        "--disconnect",
        action="store_true",
        help="forget the credential AND purge this workspace's mirror",
    )
    ap.add_argument(
        "--i-accept-the-mirror-purge",
        action="store_true",
        help="required by --disconnect: it deletes every company-scoped mirror row",
    )
    args = ap.parse_args(argv)

    if args.execute and args.disconnect:
        print("VERDICT: COULD NOT RUN — pass --execute or --disconnect, not both.")
        print("Connecting and immediately disconnecting proves neither one landed.")
        return 2

    if args.discover and not args.execute:
        # Discovery happens INSIDE the store — there is no read-only form of
        # it, and a flag that silently did nothing would read as one that ran.
        print("VERDICT: COULD NOT RUN — --discover only means something with --execute.")
        print("The route discovers the subdomain while storing the credential; there")
        print("is no call that discovers without storing.")
        return 2

    if args.execute:
        required = DISCOVER_ENV if args.discover else WRITE_ENV
    else:
        required = READ_ENV
    missing = _report_env(required)
    if args.discover and ACCOUNT_ENV not in required:
        print(f"  {ACCOUNT_ENV:38} {'set (control)' if _env(ACCOUNT_ENV) else 'unset (optional)'}")

    if args.check:
        print("\nNothing was sent.")
        if missing:
            print("Set them in the environment, never as arguments — an argument lands")
            print("in shell history and in `ps`.")
            return 2
        return 0

    if missing:
        print("\nVERDICT: COULD NOT RUN — missing " + ", ".join(missing))
        return 2
    if httpx is None:
        print("\nVERDICT: COULD NOT RUN — httpx is not installed in this interpreter.")
        return 2

    if args.disconnect and not args.i_accept_the_mirror_purge:
        print("\nVERDICT: COULD NOT RUN — --disconnect needs --i-accept-the-mirror-purge.")
        print("It deletes every company-scoped mirror row for the token's workspace")
        print("across all 30 purge tables, not only Rentvine-era ones (ADR 0010 a1).")
        return 2

    print(f"\nBackend: {_env(BASE_URL_ENV)}")
    print("Workspace: whichever company the token belongs to (the routes read it)\n")

    with httpx.Client(timeout=TIMEOUT_S) as client:
        if args.disconnect:
            return _disconnect(client)
        if args.execute:
            return _connect(client, discover=args.discover)
        return _read(client)


if __name__ == "__main__":
    raise SystemExit(main())
