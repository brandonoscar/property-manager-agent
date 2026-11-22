"""SQL guard for the Analyst capability (ADR 0007, slice 1).

The analyst slice lets the agent answer arbitrary quantitative questions
("average days-to-fill by unit type", "which vendor's spend grew most") by
WRITING SQL over the Buildium mirror instead of us hand-building every report.
Agent-written SQL is untrusted input, so before anything executes it must pass
this deterministic gate.

Defense-in-depth, in order of authority (see executor.py for 1–2):

1. **Read-only at the database**: the executor runs every query inside a
   Postgres ``READ ONLY`` transaction — the server refuses writes no matter
   what slipped past this guard.
2. **Tenancy via CTE shadowing**: the executor prepends a company-scoped CTE
   named after every referenced table; this guard makes that airtight by
   rejecting schema-qualified names (the one bypass) and CTE names that
   collide with mirror tables.
3. **This guard is the depth layer**: it rejects everything that isn't a
   single, comment-free SELECT/CTE over the allowlisted mirror tables, and
   hard-caps result size.
4. The REVERSE_REPORT contract handles *trust*: the executed SQL + its result
   are the citation for whatever the agent tells the user.

Design choices that keep this deterministic and hard to smuggle past:

- **Reject, don't sanitize.** Comments (``--``, ``/* */``), dollar-quoting
  (``$$``), and multi-statement input are rejected outright rather than
  stripped — every "cleanup" transform is a future bypass.
- **String-literal masking** before any keyword scan, so a tenant name like
  ``O'DROP TABLE`` in a WHERE clause can't false-trigger (or hide) a match.
- **Two-sided table check**: every FROM/JOIN target must be allowlisted (CTE
  names excepted), AND a denylist of sensitive identifiers (credentials,
  chat, memory, ``pg_*``, ``information_schema``) is rejected anywhere in
  the query, even where the FROM-parser might not look.

This module never touches a database — execution lives in executor.py.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

#: Read surface for the analyst — the ontology-v2 mirror (the 13 tables under
#: migration 0049's (company_id, buildium_id) uniqueness) plus their satellite
#: tables, AND the Helixis-native operational tables the product writes itself.
#: Widening this set is a reviewed change, never a runtime decision; the
#: excluded half and the reason for each live in ``EXCLUDED_TABLES`` below, and
#: a test requires every company-scoped table to appear in exactly one of them.
#:
#: ⚠ **Every table here MUST have a ``company_id`` column.** ``executor.py``
#: rewrites each referenced table into a CTE reading
#: ``SELECT * FROM public.<t> WHERE company_id = $1::uuid`` — that rewrite IS
#: the tenant boundary, and a table without the column would produce a CTE
#: that cannot compile. Pinned by a test rather than left to review.
ALLOWED_TABLES: frozenset[str] = frozenset(
    {
        # ── the Buildium mirror ──────────────────────────────────────
        "helixis_applicants",
        "helixis_bank_accounts",
        "helixis_bill_lines",
        "helixis_bills",
        "helixis_gl_accounts",
        "helixis_lease_balances",
        "helixis_lease_tenants",
        "helixis_lease_transactions",
        "helixis_leases",
        "helixis_owners",
        "helixis_properties",
        "helixis_tasks",
        "helixis_tenants",
        "helixis_units",
        "helixis_vendors",
        "helixis_work_orders",
        # ── what Helixis itself produces ─────────────────────────────
        # Added 2026-08-29. The analyst could compute over everything
        # Buildium sends and nothing the product had worked out for itself:
        # it could count overdue tasks but not read a single risk flag,
        # reminder, renewal offer or turnover.
        #
        # ⚠ It could not even read its OWN output. The nightly analyst
        # writes findings into ``helixis_risk_flags`` with
        # ``source='analyst'`` — 103 of the 1,033 rows on file — and had no
        # way to see them, so "did I already flag this?" was unanswerable
        # and every run started blind. That is the write-without-read
        # asymmetry the Buildium read census fixed, turned on the analyst's
        # own table.
        "helixis_entity_aliases",  # learned contact aliases
        "helixis_entity_signals",  # enrichment signals per entity
        "helixis_mirror_sync_state",  # per-table freshness — "how stale is this?"
        "helixis_payment_promises",  # promise-to-pay ledger
        "helixis_property_intel",  # hazards / geo intel per property
        "helixis_reminders",  # the desk: what the product is nudging about
        "helixis_renewal_offers",  # renewal state per lease
        "helixis_risk_flags",  # detector + analyst findings
        "helixis_turnovers",  # make-ready checklist per move-out
        "helixis_work_order_status_history",  # how a work order actually moved
    }
)

#: Company-scoped tables deliberately kept OUT of the analyst's reach, each
#: with the reason. An entry here is a decision somebody made; an ABSENCE is a
#: table nobody classified — and from the outside those look identical, which
#: is exactly how the operational tables above sat unreachable without anyone
#: deciding they should be. The completeness test is the mechanism.
EXCLUDED_TABLES: dict[str, str] = {
    "helixis_billing": (
        "our subscription state, not the customer's portfolio. Nothing a "
        "question about properties, leases or money owed can need — and two "
        "reasons to withhold it rather than merely not need it. It carries "
        "the Stripe customer and subscription ids, which an injected "
        "document could ask the model to read back (the 131 golden case's "
        "shape); and the billing surface is admin-only by design, so a "
        "readable table would let any member ask the chat what the admin "
        "endpoints refuse to tell them."
    ),
    "helixis_sms_opt_outs": (
        "who told us to stop texting, with the verbatim message as evidence. "
        "It is a personal phone number attached to a statement someone made "
        "to us, kept for TCPA defense — nothing a portfolio question needs, "
        "and the audience for it is a regulator, not a model."
    ),
    "helixis_a2p_registrations": (
        "carrier registration paperwork — an EIN, a home-ish business address "
        "and a named individual's direct contact details. Nothing a portfolio "
        "question needs, and exactly the shape that should not reach a model."
    ),
    "helixis_twilio_numbers": (
        "telephony routing config — which Twilio number belongs to which "
        "company. Operational plumbing that says nothing about the portfolio, "
        "and it is the table the inbound tenant boundary is derived FROM; "
        "letting an analyst query steer near it buys nothing and reads as an "
        "invitation."
    ),
    "helixis_action_idempotency": (
        "write-gate ledger — idempotency keys and claim state. Operational "
        "plumbing that says nothing about the portfolio, and reading it would "
        "let a query infer which writes were attempted and when."
    ),
    "helixis_activity_log": (
        "audit trail. It is evidence ABOUT the product's behaviour, and the "
        "suppression logic in several producers reads it — exposing it to "
        "generated SQL invites analysis that reasons about the guardrails."
    ),
    "helixis_chat_messages": (
        "conversation content. The most sensitive free text in the product, "
        "authored by users about anything at all."
    ),
    "helixis_feedback": "user feedback on answers — about us, not about the books.",
    "helixis_gmail_seen": (
        "email poll cursor and dedup ledger — external message ids, no portfolio content."
    ),
    "helixis_llm_usage": (
        "internal model-cost telemetry. Not tenant data; surfacing it in an "
        "answer would leak our own operating costs."
    ),
    "helixis_pending_confirmations": (
        "the confirm-gate ledger. Same reasoning as the idempotency table: a "
        "query must not be able to read the state of the safety gates."
    ),
    "helixis_sms_consent": (
        "TCPA consent ledger — phone numbers plus the legal record of consent "
        "and revocation. Reachable through the dedicated consent API, which "
        "scopes and audits access; generated SQL does neither."
    ),
    "helixis_sms_log": "outbound/inbound message log — comms metadata and inbound bodies.",
    "helixis_rentvine_webhook_deliveries": (
        "Rentvine deliveries the webhook receiver REFUSED, kept raw to derive "
        "the signature scheme. Unverified by definition: anyone who knows an "
        "account code can write a row, so a readable table would put "
        "stranger-written text in front of the model."
    ),
    "helixis_pms_sync_watermarks": (
        "the new connectors' incremental-sync cursors (migration 0108): when "
        "each resource was last read cleanly. No fact about the portfolio, "
        "and helixis_mirror_sync_state already answers how stale the data is."
    ),
}

#: Hard cap on returned rows — the guard wraps every query in an outer LIMIT.
MAX_ROWS = 500

#: Columns withheld from the analyst, per the company's PMS. ADR 0010 Decision (a).
#:
#: ⚠ **THIS IS WHAT MAKES "a Buildium customer is unchanged" TRUE, and the ADR
#: falsified the flat version of that claim twice before this existed.** The
#: Data Analyst is the one thing in the product that reads the COLUMN LIST:
#: ``describe_mirror_schema_impl`` queries ``information_schema`` live, on
#: purpose, so the model writes against the real schema rather than a drifting
#: copy. Migration 0101 adds ``pms`` to 20 of the tables in
#: :data:`ALLOWED_TABLES`, so without this set a Buildium customer's analyst
#: would be handed a column entry per table that nobody added for them.
#:
#: ⚠ **A NAMED LIST OF EXCLUSIONS, never "hide what I do not recognise."** The
#: second would silently blind the analyst to every column added from now on —
#: which is the worse bug, because a missing column is invisible while an extra
#: one is merely noise.
#:
#: ⚠ **PMS-AWARE, and the Rentvine row is not symmetry for its own sake.**
#: ``ledger_id``/``ledger_type`` carry zero information to a Buildium reader
#: (constant or NULL on every row it can see) and are load-bearing for a
#: Rentvine one — ADR 0010 Decision (b): a Rentvine analyst that cannot see
#: ``ledger_type`` computes WRONG money totals.
#:
#: Rent Manager's row is its own, not the fallback below (which is for a PMS
#: that could not be read): its pass fills no ledger column, because bill lines
#: and lease transactions are not synced (design/rentmanager-connector.md, "As
#: built (slice 1)"). ``test_rentmanager_analyst_columns`` reads both sides.
_HIDDEN_COLUMNS: dict[str, frozenset[str]] = {
    "buildium": frozenset({"pms", "ledger_id", "ledger_type"}),
    "rentvine": frozenset({"pms"}),
    "rentmanager": frozenset({"pms", "ledger_id", "ledger_type"}),
    "propertyware": frozenset({"pms"}),
    # DoorLoop's pass marks a bill line charged to no property with the
    # manager ledger (``doorloop.mapper.MANAGER_LEDGER``): hidden, the analyst
    # would add the company's own spend to no property and not know it.
    # ``test_doorloop_shared_layer`` reads both sides.
    "doorloop": frozenset({"pms"}),
}

#: What a company whose PMS we could not read is told.
#:
#: ⚠ The FALLBACK IS BUILDIUM'S SET, not the empty set, and not a refusal.
#: Empty would leak the discriminator on exactly the deployments that cannot
#: answer the question; a refusal would take the analyst down for a Buildium
#: customer where it worked yesterday, which is a real regression traded for a
#: cosmetic one. Buildium is also the only PMS any live company has.
_DEFAULT_PMS = "buildium"


def hidden_columns(pms: str | None = None) -> frozenset[str]:
    """Columns the analyst must not see for a company on ``pms``.

    Read by BOTH ``describe_mirror_schema_impl`` (which subtracts) and
    ``executor.compose_scoped_sql`` (which projects the complement), so the
    two cannot drift into disagreeing about what is visible — gotcha 28's
    both-sides rule, on the one surface where a disagreement is invisible:
    the describer could say a column does not exist while ``SELECT *`` hands
    it over anyway.
    """
    return _HIDDEN_COLUMNS.get((pms or _DEFAULT_PMS).strip().lower(), _HIDDEN_COLUMNS[_DEFAULT_PMS])


def visible_columns(all_columns: Sequence[str], pms: str | None = None) -> tuple[str, ...]:
    """``all_columns`` minus the hidden set, in the order given.

    ⚠ The input must come from a LIVE ``information_schema`` read, never from a
    list in this module. Phase 2.5 round 2 (F-11) probed the trap: **PostgreSQL
    has no ``SELECT * EXCEPT (...)``**, so the executor needs a POSITIVE column
    list — and the lazy way to get one is to hardcode it here, which is a second
    copy of the schema that drifts the first time a migration adds a column, and
    which would falsify this design's own control (a genuinely new
    non-discriminator column must still reach the model).
    """
    hidden = hidden_columns(pms)
    return tuple(c for c in all_columns if c not in hidden)


#: Statements/keywords that must never appear (word-boundary, outside string
#: literals). Includes everything that writes, locks, escapes, or sleeps.
_FORBIDDEN_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "merge",
    "drop",
    "alter",
    "create",
    "truncate",
    "grant",
    "revoke",
    "copy",
    "call",
    "do",
    "vacuum",
    "reindex",
    "cluster",
    "refresh",
    "lock",
    "listen",
    "notify",
    "prepare",
    "deallocate",
    "execute",
    "set",
    "reset",
    "comment",
    "into",  # SELECT ... INTO creates a table
    "returning",
)

#: Sensitive identifiers rejected ANYWHERE in the query (substring match on
#: masked SQL) — belt and braces beyond the FROM/JOIN allowlist.
_DENY_SUBSTRINGS = (
    "company_buildium_credentials",
    # Every PMS's credentials table (`company_<pms>_credentials`, one per entry
    # in `pms.registry`), so a fourth system is covered without an edit here.
    # No allowed table's name contains it (pinned by a test).
    "_credentials",
    "helixis_chat_messages",
    "memory_audit",
    "helixis_memories",
    "helixis_feedback",
    "helixis_pending_confirmations",
    "helixis_action_idempotency",
    "crm_lead",
    "company_users",
    "buildium_staff",
    "pg_",  # pg_catalog, pg_sleep, pg_read_file, pg_ls_dir, …
    "information_schema",
    "current_setting",
    "set_config",
    "dblink",
    "lo_import",
    "lo_export",
)

_FROM_JOIN_RE = re.compile(r"\b(?:from|join)\s+([a-z_][a-z0-9_.]*)", re.IGNORECASE)

#: Keywords that end a FROM clause's table list. Reaching one at paren-depth 0
#: means we scanned the whole table list without finding a top-level comma.
_CLAUSE_END_WORDS = frozenset(
    {
        "where",
        "group",
        "having",
        "order",
        "limit",
        "offset",
        "window",
        "union",
        "intersect",
        "except",
        "join",
        "on",
        "using",
    }
)
_CTE_NAME_RE = re.compile(r"(?:\bwith\s+|,\s*)([a-z_][a-z0-9_]*)\s+as\s*\(", re.IGNORECASE)
_ROW_LOCK_RE = re.compile(r"\bfor\s+(update|share|no\s+key\s+update|key\s+share)\b", re.IGNORECASE)


@dataclass(frozen=True)
class GuardResult:
    """Verdict for one agent-written query."""

    allowed: bool
    reason: str
    #: The input wrapped in an outer row cap (``SELECT * FROM (…) AS _analyst
    #: LIMIT MAX_ROWS``) — the standalone-safe form. None when rejected. The
    #: executor composes its own tenancy-scoped form from ``normalized_sql``.
    execution_sql: str | None = None
    #: The stripped (unwrapped) query, for the executor to compose with the
    #: company-scoping CTE prelude. None when rejected.
    normalized_sql: str | None = None
    #: The distinct allowlisted tables the query reads, in first-reference
    #: order — exactly the set the executor must build scoped shadows for.
    tables: tuple[str, ...] = ()


def _mask_strings(sql: str) -> str | None:
    """Replace single-quoted string literal contents with spaces.

    Keeps every character position outside literals intact so keyword scans
    can't be fooled by (or false-trigger on) literal contents. Returns None on
    an unterminated literal — reject rather than guess.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch != "'":
            out.append(ch)
            i += 1
            continue
        # Inside a literal: '' is an escaped quote, everything else masks.
        out.append("'")
        i += 1
        while i < n:
            if sql[i] == "'":
                if i + 1 < n and sql[i + 1] == "'":
                    out.append("  ")
                    i += 2
                    continue
                out.append("'")
                i += 1
                break
            out.append(" ")
            i += 1
        else:
            return None  # ran off the end inside a literal
    return "".join(out)


def _has_comma_join(lowered: str) -> bool:
    """True if any FROM clause contains a top-level (paren-depth-0) comma.

    The FROM/JOIN allowlist + the executor's tenancy-shadow CTEs only cover
    tables captured by :data:`_FROM_JOIN_RE`, which matches a single identifier
    after each ``FROM``/``JOIN``. A comma-separated table list (``FROM a, b``)
    is an implicit cross join whose second+ tables are NOT captured — so they
    are neither allowlist-checked nor company-scoped, and resolve to the raw
    ``public`` table = every tenant's rows. Explicit ``JOIN`` is unaffected
    (each JOIN target is captured and scoped), so we reject only the comma form.

    ``lowered`` must already have string literals masked (so commas inside
    literals are gone) and comments stripped/rejected.
    """
    for m in re.finditer(r"\bfrom\b", lowered):
        depth = 0
        word: list[str] = []
        i, n = m.end(), len(lowered)
        while i < n:
            ch = lowered[i]
            if ch.isalnum() or ch == "_":
                word.append(ch)
                i += 1
                continue
            # Word boundary: a clause-ending keyword at depth 0 means the table
            # list is over — no comma join in this FROM.
            if word and depth == 0 and "".join(word) in _CLAUSE_END_WORDS:
                break
            word = []
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    break  # closing paren ends this FROM's enclosing scope
                depth -= 1
            elif ch == "," and depth == 0:
                return True  # top-level comma in the FROM table list
            i += 1
    return False


def guard_sql(sql: str) -> GuardResult:
    """Decide whether agent-written ``sql`` may execute, and in what form.

    Accepts only a single, comment-free ``SELECT``/``WITH`` statement whose
    every FROM/JOIN target is in :data:`ALLOWED_TABLES` (CTE names excepted)
    and which trips neither the forbidden-keyword nor the sensitive-identifier
    scan. On success returns the query wrapped in an outer ``LIMIT``
    :data:`MAX_ROWS` — the wrap applies regardless of any inner LIMIT, so the
    cap cannot be omitted or inflated.
    """
    text = (sql or "").strip()
    if not text:
        return GuardResult(False, "empty query")

    masked = _mask_strings(text)
    if masked is None:
        return GuardResult(False, "unterminated string literal")

    lowered = masked.lower()

    # Smuggling vectors: rejected outright, never stripped.
    if "--" in lowered or "/*" in lowered:
        return GuardResult(False, "comments are not allowed")
    if "$" in lowered:
        return GuardResult(False, "dollar-quoting / parameters are not allowed")
    if ";" in lowered.rstrip().rstrip(";"):
        return GuardResult(False, "multiple statements are not allowed")
    text = text.rstrip().rstrip(";")
    lowered = lowered.rstrip().rstrip(";")

    first_word = lowered.split(None, 1)[0] if lowered.split() else ""
    if first_word not in ("select", "with"):
        return GuardResult(False, f"only SELECT/WITH queries are allowed (got {first_word!r})")

    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", lowered):
            return GuardResult(False, f"forbidden keyword: {kw.upper()}")

    if _ROW_LOCK_RE.search(lowered):
        return GuardResult(False, "row-locking clauses are not allowed")

    for needle in _DENY_SUBSTRINGS:
        if needle in lowered:
            return GuardResult(False, f"references a restricted identifier: {needle}")

    # WITH RECURSIVE is rejected: recursion isn't needed for reporting, and
    # keeping CTE composition simple matters because the executor merges the
    # tenancy-shadow CTEs with the agent's own WITH clause.
    if re.search(r"\bwith\s+recursive\b", lowered):
        return GuardResult(False, "recursive CTEs are not allowed")

    # Reject implicit cross joins (``FROM a, b``): only the first table would be
    # captured + company-scoped, leaving the rest reading the raw public table
    # across all tenants. Explicit ``JOIN ... ON`` is captured and scoped, so
    # steer the model there instead.
    if _has_comma_join(lowered):
        return GuardResult(
            False,
            "comma joins (implicit cross joins) are not allowed; use explicit JOIN ... ON",
        )

    cte_names = {m.group(1).lower() for m in _CTE_NAME_RE.finditer(lowered)}
    # A CTE named after an allowlisted table would collide with (or spoof)
    # the executor's tenancy-shadow CTE of the same name.
    for name in cte_names:
        if name in ALLOWED_TABLES:
            return GuardResult(False, f"CTE name shadows a mirror table: {name}")

    targets = [m.group(1).lower() for m in _FROM_JOIN_RE.finditer(lowered)]
    # Subselects appear as "from (" — the regex only captures identifiers, and
    # their inner FROM/JOIN targets are captured by the same global scan.
    real_targets = [t for t in targets if t not in cte_names]
    if not real_targets:
        return GuardResult(False, "query must read from at least one mirror table")
    seen_tables: list[str] = []
    for t in real_targets:
        if "." in t:
            # Schema qualification (public.helixis_x) would resolve to the
            # REAL table and bypass the executor's company-scoped shadow CTE
            # of the same name — the one hole in CTE shadowing, so it's shut.
            return GuardResult(False, f"schema-qualified table references are not allowed: {t}")
        if t not in ALLOWED_TABLES:
            return GuardResult(False, f"table not in the analyst allowlist: {t}")
        if t not in seen_tables:
            seen_tables.append(t)

    # Outer row cap — applied unconditionally so it can't be forgotten or
    # out-negotiated by an inner LIMIT. (Postgres allows CTEs in subqueries,
    # so wrapping a WITH query is valid.)
    execution_sql = f"SELECT * FROM (\n{text}\n) AS _analyst LIMIT {MAX_ROWS}"
    return GuardResult(True, "ok", execution_sql, text, tuple(seen_tables))


__all__ = [
    "ALLOWED_TABLES",
    "EXCLUDED_TABLES",
    "MAX_ROWS",
    "GuardResult",
    "guard_sql",
    "hidden_columns",
    "visible_columns",
]
