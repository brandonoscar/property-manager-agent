"""Every query against a `helixis_*` table carries `company_id`, or is listed here.

CLAUDE.md gotcha 47 states that rule, and a rule written in prose cannot fail.
It was learned expensively: one missing `WHERE company_id` in an ad-hoc query
returned another tenant's row, and the story built on it — a false-absence bug
report, a two-tenants theory, a recommendation to re-point live Buildium
credentials — cost a day and a half and was wrong at full confidence in four
separate places. The same omission inside `mirror_tools` would silently serve
another tenant's rows into a chat answer.

Finding #58 splits into "a month buys the real control" (a scoped connection
helper, or `SET LOCAL role authenticated` so the existing RLS policies engage
on the agent path, plus a two-company integration battery against a seeded
tenant B) and "four days buys another spot assertion". This is neither: it is
the **corpus guard** that finding names, and it needs no live database.

It is gotcha 45's rule — *measure what the control PERMITTED, not what a
function does*. A test feeding hand-written SQL to a scoping helper would pass
forever while the next unscoped literal walked straight past it. What has to be
measured is every SQL literal the repo actually contains.

Measured 2026-08-29: **220 queries touch a `helixis_*` table and 204 carry
`company_id`.** The 16 below are deliberate, each for a stated reason. The
guard's job is to hold that line — a seventeenth fails here.

⚠ **Two AST traps, both hit while writing this.**
1. An f-string is a `JoinedStr`, not a `Constant`. Scanning `Constant` alone
   sees only the fragments BETWEEN the holes, so a query whose `company_id`
   sits in a different fragment reads as unscoped — `turnovers.py` and
   `activity.py` were both false-flagged that way. Worse in the other
   direction: one fragment containing the words "company_id" would make an
   unscoped query look fine.
2. A `JoinedStr`'s inner `Constant`s are ALSO reached by `ast.walk`, at the
   same line number, so they must be skipped explicitly or every f-string is
   judged twice — once whole and once in pieces — and the fragment verdict
   silently wins.
"""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "helixis"
_TABLE = re.compile(r"\bhelixis_[a-z_]+\b")
_VERB = re.compile(r"\b(select|insert\s+into|update|delete\s+from)\b", re.I)


def _fingerprint(sql: str) -> str:
    """A stable short hash of the STATEMENT, independent of where it sits.

    Lowercased and whitespace-collapsed, so re-indenting or reflowing a query
    keeps its fingerprint — the churn this exists to reduce is line drift, not
    formatting. Any change to the SQL's actual words is a different statement
    and gets a different hash, which is the point.
    """
    return hashlib.sha256(" ".join(sql.lower().split()).encode()).hexdigest()[:10]


#: `path:line` → (statement fingerprint, why this query is legitimately not
#: company-scoped). Adding an entry is a deliberate act with a reason attached —
#: which is the whole difference between a considered global and a forgotten
#: WHERE.
#:
#: ⚠ THE FINGERPRINT IS NOT DECORATION, and it was added the day a line-keyed
#: entry was caught licensing a query nobody had reviewed. `billing.py:777` was
#: written for the `invoice.paid` UPDATE; a new cross-tenant SELECT landed on
#: exactly that line, and BOTH existing tests stayed green — the undeclared scan
#: found the key present, and the stale check found the key matched. A silent
#: licence granted to unreviewed SQL is the precise failure this whole file
#: exists to prevent, arriving through the guard itself.
ALLOWED_GLOBAL: dict[str, tuple[str, str]] = {
    # ── Stripe webhooks ───────────────────────────────────────────────────
    # Stripe sends its OWN ids and nothing else, so these resolve the tenant
    # by stripe_customer_id / stripe_subscription_id rather than by
    # company_id. That is the "unique id issued company-scoped" category
    # gotcha 63 already recognises: migration 0087 puts a UNIQUE index on
    # both columns precisely so this lookup identifies exactly one company —
    # the database refuses to represent a shared customer rather than
    # leaving the webhook to pick one (the same reasoning as
    # helixis_twilio_numbers' unique phone_number).
    #
    # ⚠ Adding `company_id` here would NOT be safer, it would break the
    # feature: the event does not carry one. If these line numbers drift,
    # re-point them — do not delete them (gotchas 68/76).
    # ⚠ One correction worth keeping from that history: the event NAMES were
    # once wrong, each entry describing the statement BEFORE the one it pointed
    # at. Behaviourally identical — all three are "Stripe sends only its own
    # ids" — but a reason that names the wrong event is a comment that cannot
    # fail (gotcha 48). The zero-row warning these three needed came from the
    # same pass: they key on a STRIPE id rather than on our own company_id,
    # which is WHY they are listed here and also why "no such row" is a
    # reachable state for them. See test_billing_webhook.py.
    #
    # ⚠ Re-pointed six times between 2026-09-02 and 2026-09-05 as models and
    # helpers landed above them — twice by a change that was only a COMMENT, so
    # a line-keyed allowlist is broken by any edit above it and a change
    # anywhere in a file with entries here needs THIS file run, not just the
    # tests for the thing being changed.
    #
    # ⚠ THE SEVENTH RE-POINT IS WHY THE FINGERPRINTS EXIST. `billing.py:777`
    # was this list's entry for the `invoice.paid` UPDATE. The ordering guard's
    # new cross-tenant SELECT landed on exactly that line, and both existing
    # tests stayed green — one saw a declared key, the other saw a key that
    # matched — so the allowlist had transferred a licence to SQL nobody had
    # reviewed. That is the failure this whole file exists to prevent, arriving
    # through the guard itself. The fingerprint makes it loud
    # (`test_every_licence_still_points_at_the_statement_it_was_written_for`),
    # and line drift alone no longer risks silently re-aiming an exemption.
    "api/routes/billing.py:1185": (
        "711c62ba24",
        "webhook ordering guard — has a NEWER Stripe event already been applied "
        "to this customer's row (migration 0093)? The event names no company; "
        "`max` is the conservative read",
    ),
    "api/routes/billing.py:1309": (
        "0a4500a160",
        "customer.subscription.deleted — Stripe sends only its own ids; "
        "resolved via the unique stripe_subscription_id / stripe_customer_id",
    ),
    "api/routes/billing.py:1329": (
        "11d6319dc8",
        "invoice.payment_failed — same, keyed on those unique Stripe ids",
    ),
    "api/routes/billing.py:1349": (
        "4161ebf12d",
        "invoice.paid / invoice.payment_succeeded — same, keyed on those unique Stripe ids",
    ),
    # The daily smoke's every-company-has-a-billing-row invariant (gotcha 81).
    # ⚠ Deliberately cross-tenant: the question IS "is ANY company missing a
    # row", which cannot be asked company-scoped. `helixis_billing_missing` is
    # itself `security_invoker` and revoked from anon/authenticated (0089), so
    # the view cannot be the RLS bypass gotcha 82 records. The sample query
    # under it (a few of those companies' ids, so the alert names who to
    # repair) is just as global, but it selects the view's ``company_id``
    # column, which this scan reads as scoped, so it needs no entry.
    "smoke/checks.py:1188": (
        "3bbdbbeb2e",
        "how many companies have no billing row — a fleet-wide invariant, not tenant data",
    ),
    # Keyed on the PHONE, across every company, on purpose: a STOP text
    # revokes consent everywhere that number appears (CLAUDE.md, SMS Stage 2)
    # and START restores only previously-revoked rows. Scoping these would
    # leave a person opted in at a company they never named.
    # ⚠ These two MOVED out of api/routes/twilio.py into comms/consent_keywords
    # so the CRM's inbound route applies the identical rules — a Twilio number
    # has ONE inbound URL, so whichever route the console names must honour
    # opt-out. Same statements, new home.
    "comms/consent_keywords.py:97": (
        "493bafbb02",
        "STOP revokes consent for that phone in ALL companies",
    ),
    "comms/consent_keywords.py:127": (
        "ef7f18ff83",
        "START restores that phone's revoked rows in ALL companies",
    ),
    # ⚠ The opt-out record (migration 0086) takes the SAME cross-company
    # posture as the two statements above, deliberately: one STOP silences
    # every sender. It exists because the ledger UPDATE is a no-op for anyone
    # who never consented — every CRM lead — so their STOP was recorded
    # nowhere and the Leasing lane kept texting them.
    # ⚠ RE-POINTED 2026-09-09 with NEW fingerprints, which means the
    # statements were REWRITTEN rather than merely moved — read before
    # trusting. Both now match on `phone = ANY($1::text[])` (the several
    # spellings one number arrives in) instead of a single literal, and both
    # live as module constants. The cross-company posture and the reason for
    # it are unchanged, and `opt_out.py` states them in prose above each one.
    # The INSERT beside them is company-scoped and correctly absent here:
    # writes record which number received the STOP, reads must not care.
    # Both re-pointed 2026-09-09 (+8): `recipient_block` gained an
    # unsupported-region refusal above the clock check, so the statements
    # below moved. Same SQL, same deliberate cross-company scope — re-pointed,
    # never deleted (gotchas 68/76: moving code is the guard working).
    "comms/opt_out.py:130": (
        "04729c31f6",
        "is this number opted out — one STOP silences every sender",
    ),
    "comms/opt_out.py:159": (
        "7939d896dd",
        "START lifts the block everywhere it was applied",
    ),
    # ── Two-factor backup codes ───────────────────────────────────────────
    # ⚠ THESE ARE NOT MISSING A `company_id` — the table has no such column,
    # and that is the design rather than an oversight. A second factor belongs
    # to the PERSON: one authenticator, one sheet of codes, following them
    # across every workspace they are a member of. Adding `company_id` would
    # mean somebody in two companies had two sets of codes for one phone, and
    # a code that worked in one workspace and not the other.
    #
    # So the tenant boundary here is the auth USER, and every statement below
    # carries `user_id = $1::uuid` taken from the verified token — never from
    # a request parameter, which is what stops one account reaching another's
    # codes (`test_mfa_backup_codes.py` pins that directly: one user's code is
    # worthless against another account, and dropping the predicate turns it
    # red). Migration 0094 says the same thing from the storage side.
    #
    # ⚠ The one statement without `user_id` is the UPDATE that spends a code:
    # it is keyed on the row `id` just selected under a user-scoped read, and
    # its `and used_at is null` is the atomic claim that stops two concurrent
    # redemptions both winning. Scoping it by user as well would read as
    # belt-and-braces and would actually hide that the claim is what matters.
    "auth/mfa_backup_codes.py:97": (
        "28c0e896d9",
        "regenerate discards this USER's old sheet; codes are per-person, not per-company",
    ),
    "auth/mfa_backup_codes.py:103": (
        "f87b59c3a1",
        "issues a code to this USER; the table has no company_id by design",
    ),
    "auth/mfa_backup_codes.py:116": (
        "92282310c4",
        "how many codes this USER has left, for the Settings count",
    ),
    "auth/mfa_backup_codes.py:139": (
        "7843891b3b",
        "candidate codes for this USER, hashed-compared without early exit",
    ),
    "auth/mfa_backup_codes.py:151": (
        "ac81c0b8c4",
        "claims one code by row id; `used_at is null` is the atomic claim, not a scope",
    ),
    # Twilio's MessageSid is globally unique; the log row it updates was
    # written company-scoped.
    "api/routes/twilio.py:222": (
        "32d19ec57f",
        "delivery receipt keyed on the provider's unique MessageSid",
    ),
    # The whole job of this query is to DISCOVER the company from a bare phone
    # number, so a company_id filter would be circular. The UNIQUE index on
    # phone_number bounds it to at most one row, and the caller declines to
    # route when there is none.
    # "does ANY company have a number mapped" — the single-tenant escape
    # hatch. Deliberately fleet-wide: the question is about the DEPLOYMENT,
    # not about one company, and scoping it would always answer "no" for a
    # company that has no row, which is exactly the case it must distinguish.
    "comms/number_routing.py:51": (
        "2e3b92daf4",
        "deployment-wide 'is anything mapped at all' probe",
    ),
    # Janitors. These sweep expired leases fleet-wide by design — a reaper
    # that only reaped one company's rows would leave the rest wedged.
    "db/repositories/action_idempotency.py:255": (
        "5942589224",
        "reaper: expired in_progress leases, fleet-wide",
    ),
    "db/repositories/action_idempotency.py:264": (
        "3fc1ab5daa",
        "reaper: expired approval leases, fleet-wide",
    ),
    "db/repositories/action_idempotency.py:335": (
        "0e8cbfe181",
        "shutdown release of confirm-waits, fleet-wide",
    ),
    "db/repositories/pending_confirmations.py:224": (
        "4db8bc9d2f",
        "process shutdown drains every pending confirm",
    ),
    # Keyed on a unique id that was itself issued company-scoped.
    "db/repositories/pending_confirmations.py:151": (
        "be1e79a897",
        "lookup by unique confirm_id",
    ),
    "db/repositories/pending_confirmations.py:165": (
        "0316e253f4",
        "resolve by unique confirm_id",
    ),
    "db/repositories/pending_confirmations.py:194": (
        "7ad7936b52",
        "resolve by unique confirm_id",
    ),
    "detectors/inbox_bridge.py:87": (
        "0f7fe81cab",
        "stamps ids returned by a company-scoped read in the same call",
    ),
    # This table's scope key is the USER, not the company.
    "db/repositories/gmail_poll_state.py:51": (
        "9f5132e9af",
        "helixis_gmail_seen is keyed by user_id",
    ),
    # The daily SMS cap is per PHONE — the same person must not get two
    # digests because they are consented at two companies.
    # Re-pointed 2026-09-09 (74 -> 91): the cap gained a
    # `NOT (status = ANY(...))` clause so a send Twilio reported as
    # undelivered no longer spends the day's one allowed notice. Same
    # statement, same deliberate cross-company scope — re-pointed, not
    # deleted (gotchas 68/76: moving SQL is the guard working).
    "reminders/sms_digest.py:91": (
        "6a4291a4ce",
        "daily send cap is per phone, deliberately across companies",
    ),
    # Deliberately global: which embedding models exist at all. A schema
    # health probe, not tenant data (gotcha 18's re-embed migration).
    # Re-pointed 2026-09-02 (+13, from the MemoryRead type above it) and again
    # 2026-09-10 (397 -> 450, the `recorded_age` helper landing above it). Same
    # statement both times — the hash below is the normalised SQL and has not
    # moved, which is what proves it is a re-point and not a new query.
    "memory/manager.py:450": (
        "f639a8ef01",
        "schema probe: which embedding models are present, cross-tenant",
    ),
    # ⚠ Scoping is REAL but lives in a Python list, not the SQL text:
    # `where_clauses = ["company_id = $1"]` is joined into the f-string. A
    # static scan cannot see it. Verified by reading, 2026-08-29.
    # RE-POINTED 2026-09-13 (167 → 181) when the ``cursor`` parameter grew a
    # comment explaining why it is typed. The fingerprint is UNCHANGED
    # (b526eb5ac9), which is what proves the licence still describes the same
    # statement rather than a new one that happens to sit at that line.
    "api/routes/activity.py:181": (
        "b526eb5ac9",
        "company_id = $1 is the first entry of where_clauses (verified)",
    ),
    # ⚠ `turnovers.py:150` is deliberately ABSENT. It looked unscoped under the
    # fragment-scanning version and I had written it an exemption; once the
    # JoinedStr child-skip landed, the reconstructed f-string showed its
    # `WHERE company_id = $1` and `test_the_allowlist_has_no_stale_entries`
    # rejected the entry. A stale exemption is a licence nobody needs that
    # makes the list read as more permissive than the code — this is what that
    # test is for, and it fired on its author first.
}


def _render(node: ast.AST) -> str | None:
    """The full text of a str constant OR an f-string, holes as `{}`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            p.value if isinstance(p, ast.Constant) and isinstance(p.value, str) else "{}"
            for p in node.values
        )
    return None


def _skip(tree: ast.AST) -> set[int]:
    """Docstrings (prose naming a table is not a query) and every Constant
    nested inside a JoinedStr (see trap 2 in the module docstring)."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr):
                out.add(id(body[0].value))
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                out.add(id(part))
    return out


def _scan() -> tuple[dict[str, str], int]:
    """Returns ({unscoped `path:line` → its SQL}, total queries seen)."""
    unscoped: dict[str, str] = {}
    total = 0
    for path in sorted(_SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover — a broken file fails elsewhere
            continue
        skip = _skip(tree)
        for node in ast.walk(tree):
            if id(node) in skip:
                continue
            sql = _render(node)
            if sql is None or not (_TABLE.search(sql) and _VERB.search(sql)):
                continue
            total += 1
            if "company_id" not in sql.lower():
                unscoped[f"{path.relative_to(_SRC)}:{node.lineno}"] = sql
    return unscoped, total


def test_the_scan_finds_the_corpus_it_is_supposed_to_measure() -> None:
    """A bad glob or a wrong root collects zero and reports green — the
    self-check gotcha 49's guard needed for the same reason."""
    _unscoped, total = _scan()
    assert total > 150, (
        f"only {total} SQL literals found against ~220 measured — the scanner is "
        "not reading the corpus, so its verdict means nothing"
    )


def test_every_unscoped_query_is_a_declared_deliberate_global() -> None:
    unscoped, total = _scan()
    undeclared = [k for k in unscoped if k not in ALLOWED_GLOBAL]
    assert not undeclared, (
        "SQL against a helixis_* table with no company_id and no stated reason:\n  "
        + "\n  ".join(undeclared)
        + f"\n\n({total} queries scanned.) Every company-scoped table in this product is "
        "multi-tenant. Add `company_id` to the query, or add the line to ALLOWED_GLOBAL "
        "with the reason it is deliberately cross-tenant — gotcha 47 cost a day and a "
        "half to exactly this omission, and the wrong answer read as a product failure."
    )


def test_the_allowlist_has_no_stale_entries() -> None:
    """An entry for a query that no longer exists is a licence nobody is using,
    and it makes the list read as more permissive than the code is. It also
    goes stale silently the moment a line moves."""
    unscoped, _total = _scan()
    stale = sorted(set(ALLOWED_GLOBAL) - set(unscoped))
    assert not stale, (
        f"ALLOWED_GLOBAL entries that match nothing: {stale} — the query was scoped, "
        "moved, or deleted. Remove the entry (or re-point it) so the list keeps "
        "describing the code."
    )


def test_every_licence_still_points_at_the_statement_it_was_written_for() -> None:
    """⚠ A line number is not an identity, and this is the test that says so.

    `billing.py:777` was written for the `invoice.paid` UPDATE. A new
    cross-tenant SELECT was then added on exactly that line, and both of the
    tests above stayed GREEN: the undeclared scan saw a declared key, and the
    stale check saw a key that matched. The allowlist had silently granted a
    licence to SQL nobody had reviewed — which is the failure this whole file
    exists to prevent, arriving through the guard itself.

    The fingerprint is what makes a moved or replaced statement LOUD. Re-point
    with the value this failure prints; never paste one without reading the
    query it now names.
    """
    unscoped, _total = _scan()
    drifted: list[str] = []
    for key, (expected, reason) in sorted(ALLOWED_GLOBAL.items()):
        sql = unscoped.get(key)
        if sql is None:
            continue  # the stale-entry test above owns this case
        actual = _fingerprint(sql)
        if actual != expected:
            first = " ".join(sql.split())[:110]
            drifted.append(
                f'  "{key}": ("{actual}", ...)   # licensed as: {reason}\n      now reads: {first}'
            )
    assert not drifted, (
        "An ALLOWED_GLOBAL entry points at a DIFFERENT query than the one it "
        "was written for — its licence has been silently transferred:\n"
        + "\n".join(drifted)
        + "\n\nRead each query above. If it is genuinely a deliberate global, "
        "update the fingerprint AND the reason. If it is not, scope it — do "
        "not paste the new hash to make this pass."
    )


def test_every_exemption_states_a_reason() -> None:
    """The reason is the control. A bare path teaches the next reader that
    unscoped SQL is normal here."""
    for key, (fingerprint, reason) in ALLOWED_GLOBAL.items():
        assert len(reason.strip()) > 20, f"{key} has no real justification: {reason!r}"
        # A blank or truncated fingerprint would make the drift test above
        # vacuous for that entry — the one direction that turns the loud guard
        # back into the silent one it replaced.
        assert len(fingerprint) == 10 and fingerprint.isalnum(), (
            f"{key} has no usable statement fingerprint: {fingerprint!r}"
        )
