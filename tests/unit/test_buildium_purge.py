"""Purge-on-disconnect (Buildium ToS §18) — the compliance contract.

Three properties pinned:
1. COMPLETENESS — every company-scoped table the migrations create is
   either purged or explicitly kept-with-a-reason. A new mirror table
   that nobody classifies fails this test, so data can't silently
   escape the §18 purge.
2. BEHAVIOR — the purge deletes every allowlisted table scoped to the
   company, child-before-parent, tolerates deploy drift (missing
   table/column), and reports counts.
3. WIRING — the disconnect route actually awaits the purge (the same
   source-scan idiom the confirm-gate tests use).
"""

from __future__ import annotations

import asyncio
import inspect
import re
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from helixis.buildium.purge import PURGE_TABLES, purge_buildium_mirror

REPO = Path(__file__).resolve().parents[2]

# Company-scoped tables that are deliberately NOT purged on Buildium
# disconnect, with the reason. Anything created by a migration that is
# neither here nor in PURGE_TABLES fails test_every_table_is_classified.
KEEP_TABLES: dict[str, str] = {
    "company_buildium_credentials": "deleted by the disconnect route itself, before the purge",
    # ⚠ KEEP, and for a reason that is NOT "it isn't Buildium data". A Buildium
    # disconnect must not touch a Rentvine company's credentials — and under
    # ADR 0010 a1 (one PMS per company, enforced by the 0100 trigger) a company
    # holding this row cannot hold a Buildium row at all, so this purge can
    # never legitimately reach it. When the Rentvine disconnect lands it gets
    # its own purge, which deletes this the way the Buildium route deletes its
    # own credentials row.
    "company_rentvine_credentials": (
        "Rentvine credentials — deleted by the RENTVINE disconnect, and a1 "
        "means a company with this row has no Buildium connection to purge"
    ),
    # The same reasoning as Rentvine's row, for the two systems migration 0107
    # added to the one-PMS trigger.
    "company_rentmanager_credentials": (
        "Rent Manager credentials — deleted by the RENT MANAGER disconnect, and "
        "a1 means a company with this row has no Buildium connection to purge"
    ),
    "company_propertyware_credentials": (
        "Propertyware credentials — deleted by the PROPERTYWARE disconnect, and "
        "a1 means a company with this row has no Buildium connection to purge"
    ),
    # Migration 0109's, for the same reason.
    "company_doorloop_credentials": (
        "DoorLoop credentials — deleted by the DOORLOOP disconnect, and a1 means "
        "a company with this row has no Buildium connection to purge"
    ),
    "company_users": "the Occupella account — not Buildium-derived",
    "company_member_invites": "an invite to an Occupella account (0112) — not Buildium-derived",
    "company_documents": "user-uploaded documents",
    "company_knowledge": "user-authored knowledge",
    "company_templates": "user-authored templates",
    "helixis_activity_log": "Occupella's own audit trail of user actions",
    "helixis_chat_messages": "conversation history — user-generated",
    "helixis_feedback": "user-generated feedback",
    "helixis_gmail_seen": "Gmail poll cursor — email-derived, not Buildium",
    "helixis_llm_usage": "Occupella's own metering",
    # ⚠ KEEP, and like the opt-out ledger this is not a judgement call.
    # Deleting it on a Buildium disconnect would drop the subscription, the
    # Stripe customer id and the trial clock for a company that is still
    # PAYING — disconnecting one integration would cancel their account. It is
    # also not Buildium-derived in any sense §18 reaches: it is our own
    # commercial record of what they bought from us.
    "helixis_billing": (
        "subscription + trial state — our own commercial record, and the "
        "customer keeps paying after a Buildium disconnect"
    ),
    "helixis_payment_promises": "email-derived (promise extraction), not Buildium API data",
    "helixis_pending_confirmations": "transient confirm-gate state; expires on its own",
    "helixis_sms_consent": "TCPA consent ledger — must be RETAINED as compliance evidence",
    # ⚠ KEEP, and this one is not a judgement call. Deleting an opt-out would
    # RESUME texting somebody who told us to stop — the disconnect would
    # manufacture a TCPA violation out of a data-hygiene step. Nothing here is
    # Buildium-derived either: it is written from an inbound message, so §18
    # does not reach it.
    "helixis_sms_opt_outs": (
        "opt-out record — deleting it would resume texting someone who said STOP"
    ),
    "helixis_a2p_registrations": (
        "A2P 10DLC registration — the customer's OWN business details, submitted "
        "to the carrier registry on their behalf. Not Buildium-derived, so §18 "
        "does not reach it; and purging it would destroy a brand registration "
        "that took 10-15 days to approve and cannot be re-created quickly."
    ),
    "helixis_sms_log": "SMS delivery ledger — retained compliance evidence",
    "helixis_twilio_numbers": (
        "Twilio number ownership — Twilio infrastructure, not Buildium-derived. "
        "Purging it on a Buildium disconnect would silently un-route the "
        "company's inbound texts and calls."
    ),
    "helixis_memories": "agent memory — separate erasure path (purge_company_memory)",
    "helixis_memories_entities": "agent memory index — same as above",
    "memory_audit": "memory audit trail — same as above",
    "crm_leads": "CRM leads arrive via email ingest, not the Buildium API",
    "crm_lead_messages": "lead correspondence — email-derived",
    "drive_index": "Google Drive index — Drive-derived, not Buildium",
    "orchestrator_runs": "Occupella's own agent-run telemetry (activity-log class)",
    "standing_tasks": "user-authored automations — instructions, not API data",
}

_CREATE_RE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_COMPANY_COL_RE = re.compile(r"company_id", re.IGNORECASE)


def strip_sql_comments(text: str) -> str:
    """Drop ``--`` line comments and ``/* */`` blocks, respecting quotes.

    ⚠ **The scan must read STATEMENTS, never the file.** These migrations
    carry long prose headers explaining themselves, and prose says things like
    "the column beside it in the same CREATE TABLE got a rule" — which a
    whole-file scan reads as a table named ``got``. That is exactly what
    0095 did: the guard failed on a sentence, naming a table nobody had
    written, and the natural way to shut it up is to reword the comment, which
    leaves the next migration free to do it again.

    Same trap as gotcha 40 (a substring guard firing on the comment that
    documents it) and gotcha 59 (an SQL guard scoped to the file rather than
    the statements). Third instance in this repo, so it lives in a function
    with its own tests rather than inline.

    A single-quote state machine because ``--`` can legitimately appear inside
    a string literal; over-stripping there would delete real DDL.
    """
    out: list[str] = []
    i, n, in_quote = 0, len(text), False
    while i < n:
        ch = text[i]
        if in_quote:
            out.append(ch)
            if ch == "'":
                # '' inside a literal is an escaped quote, not the end.
                if i + 1 < n and text[i + 1] == "'":
                    out.append(text[i + 1])
                    i += 2
                    continue
                in_quote = False
            i += 1
        elif ch == "'":
            in_quote = True
            out.append(ch)
            i += 1
        elif ch == "-" and text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end == -1 else end  # keep the newline: line numbers hold
        elif ch == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def test_the_scanner_cannot_be_fooled_by_prose() -> None:
    """Both directions, because a stripper that ate everything would report
    zero tables and pass the completeness test vacuously."""
    fooled = "-- the same CREATE TABLE got a rule\ncreate table public.real_one (company_id uuid);"
    assert _CREATE_RE.findall(fooled) == ["got", "real_one"], (
        "the raw text must contain BOTH — otherwise this test proves nothing"
    )
    assert _CREATE_RE.findall(strip_sql_comments(fooled)) == ["real_one"]

    # A `--` inside a string literal is not a comment. Over-stripping here
    # would silently delete real DDL and hide an unclassified table.
    kept = "insert into t values ('a -- b');\ncreate table public.kept (company_id uuid);"
    assert _CREATE_RE.findall(strip_sql_comments(kept)) == ["kept"]
    assert "'a -- b'" in strip_sql_comments(kept)

    # And a block comment.
    assert _CREATE_RE.findall(strip_sql_comments("/* create table ghost */")) == []


def _created_company_scoped_tables() -> set[str]:
    """Every table the SQL sources create whose definition mentions a
    company_id column."""
    out: set[str] = set()
    sql_files = list((REPO / "supabase" / "migrations").glob("*.sql")) + list(
        (REPO / "src" / "helixis" / "db" / "schema").glob("*.sql")
    )
    for path in sql_files:
        text = strip_sql_comments(path.read_text(encoding="utf-8", errors="replace"))
        for match in _CREATE_RE.finditer(text):
            name = match.group(1).lower()
            # Look at the table body (up to the closing paren block) for
            # a company_id column; crude but stable for this schema style.
            tail = text[match.end() : match.end() + 4000]
            body = tail.split(";", 1)[0]
            if _COMPANY_COL_RE.search(body):
                out.add(name)
    return out


def test_every_company_scoped_table_is_classified() -> None:
    created = _created_company_scoped_tables()
    classified = set(PURGE_TABLES) | set(KEEP_TABLES)
    unclassified = sorted(created - classified)
    assert not unclassified, (
        "New company-scoped table(s) with no §18 classification: "
        f"{unclassified}. Add each to PURGE_TABLES (Buildium-derived) or "
        "KEEP_TABLES here (with the reason it survives a Buildium disconnect)."
    )
    # And nothing is classified both ways.
    assert not set(PURGE_TABLES) & set(KEEP_TABLES)


def test_children_purge_before_parents() -> None:
    order = {t: i for i, t in enumerate(PURGE_TABLES)}
    for child, parent in [
        ("helixis_bill_lines", "helixis_bills"),
        ("helixis_lease_transactions", "helixis_leases"),
        ("helixis_lease_balances", "helixis_leases"),
        ("helixis_lease_tenants", "helixis_leases"),
        ("helixis_units", "helixis_properties"),
        ("helixis_property_intel", "helixis_properties"),
        ("helixis_work_order_status_history", "helixis_tasks"),
    ]:
        assert order[child] < order[parent], f"{child} must purge before {parent}"


class _FakeConn:
    def __init__(self, fail_tables: dict[str, Exception] | None = None) -> None:
        self.fail_tables = fail_tables or {}
        self.calls: list[tuple[str, str]] = []

    async def execute(self, sql: str, company_id: str) -> str:
        table = sql.split("public.")[1].split(" ")[0]
        self.calls.append((table, company_id))
        if table in self.fail_tables:
            raise self.fail_tables[table]
        return "DELETE 3" if table in ("helixis_properties", "buildium_events") else "DELETE 0"


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        conn = self._conn

        class _CM:
            async def __aenter__(self) -> _FakeConn:
                return conn

            async def __aexit__(self, *exc: Any) -> None:
                return None

        return _CM()


def test_purge_hits_every_table_scoped_to_the_company() -> None:
    conn = _FakeConn()
    deleted = asyncio.run(purge_buildium_mirror(_FakePool(conn), "co-1"))  # type: ignore[arg-type]
    assert [t for t, _c in conn.calls] == list(PURGE_TABLES)
    assert all(c == "co-1" for _t, c in conn.calls)
    # Only tables with rows are reported.
    assert deleted == {"helixis_properties": 3, "buildium_events": 3}


def test_purge_tolerates_deploy_drift_but_not_real_errors() -> None:
    drift = _FakeConn(
        fail_tables={"helixis_turnovers": asyncpg.exceptions.UndefinedTableError("gone")}
    )
    deleted = asyncio.run(purge_buildium_mirror(_FakePool(drift), "co-1"))  # type: ignore[arg-type]
    assert deleted  # the sweep continued past the missing table
    real = _FakeConn(fail_tables={"helixis_bills": RuntimeError("connection lost")})
    with pytest.raises(RuntimeError):
        asyncio.run(purge_buildium_mirror(_FakePool(real), "co-1"))  # type: ignore[arg-type]


def test_disconnect_route_awaits_the_purge() -> None:
    # Same idiom as the confirm-gate source scans: the route must call
    # the purge — removing it is a compliance regression, not a refactor.
    from helixis.api.routes import buildium as route_module

    src = inspect.getsource(route_module.delete_credentials)
    assert "await purge_buildium_mirror(" in src
    assert "company_buildium_credentials" in src  # creds tombstone still first
