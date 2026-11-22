"""Reminder producers — each answers "what is being NEGLECTED right now?"

Design rules (2026-07-06 surface-model discussion):

* A producer emits a candidate only when the state holds AND nothing
  visible says the PM already acted (the neglect gate). Suppression
  signals: DB-local first (an offer/draft exists, a lead was touched, a
  payment posted, an activity-log row touched the entity recently), and
  — v1.5 — two more: a PENDING payment promise extracted from email
  ingest ("will pay the 17th" suppresses through the 17th, then the
  card returns as the follow-up), and Gmail-outbound outreach (the
  ``outbound`` checker asks the team's sent mail, but ONLY for
  candidates that already survived every DB gate, so the common case
  costs zero API calls).
* One candidate per (producer, entity_key); property-level grouping where
  per-entity cards would flood ("4 overdue tasks at Garden Row" is ONE
  card). Month-scoped keys (rent) reset naturally each month.
* Self-clearing is structural: a producer simply stops emitting the key
  and the repo clears the card.
* "Today" is the COMPANY's date, never the server's. Every producer here
  resolves it through ``common/company_clock`` (migration 0098) because the
  container is UTC and that is nobody's calendar: `date.today()` disagreed
  with the manager for 4 hours a day in New York and 10 in Honolulu, which
  reported a bill due TODAY as OVERDUE every evening. A wrong date here is
  not a cosmetic slip — it is a false claim about money, a severity that
  flips a day early, and a statutory deposit deadline computed on the wrong
  day. `tests/unit/test_company_clock.py` fails a new `dt.date.today()`.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from typing import Any

import asyncpg

from helixis.buildium.maintenance_status import open_status_sql
from helixis.db.repositories import leads as leads_repo
from helixis.db.repositories.reminders import ReminderCandidate
from helixis.db.tenant_links import current_link_sql
from helixis.geo import deadline_math, jurisdiction
from helixis.reminders.promises import promise_pending

logger = logging.getLogger(__name__)

RENEWAL_WINDOW_DAYS = 90
BILL_DUE_SOON_DAYS = 5
SUPPRESS_DAYS = 5
RENT_GRACE_DAY_OF_MONTH = 10
WEBHOOK_QUIET_HOURS = 24
DEPOSIT_CLOCK_LOOKBACK_DAYS = 90
DEPOSIT_CLOCK_LEAD_DAYS = 5
DEPOSIT_CLOCK_OVERDUE_GRACE_DAYS = 14

# Dark-by-default gate for new producers (the invoice_no_work_order detector
# precedent — HELIXIS_DETECTORS__ENABLED_IDS): a producer listed here runs
# only when its name appears in the comma-separated env allowlist, so merging
# it cannot flood the desk before it's deliberately switched on.
_ENABLED_IDS_ENV = "HELIXIS_REMINDERS__ENABLED_IDS"

#: Producers that are registered but gated OFF unless named in the env
#: allowlist above.
#:
#: ⚠ **Declared here rather than only inside the producer body, because a
#: self-gate is invisible to everything upstream.** A dark producer returns
#: ``[]``, the sweep records ``count = 0``, and that is byte-for-byte what a
#: producer which ran and legitimately found nothing records. So the one
#: question worth asking — *is this thing switched off, or switched on and
#: silent?* — had no answer anywhere in the process. That is the same
#: silently-dead-subsystem shape as gotcha 45 (a control that stopped working
#: looks exactly like one that is working) and gotcha 54 (a documented
#: keepalive that emitted nothing): the loud failure is the one you would
#: have noticed anyway.
#:
#: The producer keeps its OWN gate as well — this set is for reporting, and a
#: direct caller must not be able to bypass the switch by not consulting it.
DARK_BY_DEFAULT: frozenset[str] = frozenset({"deposit_return_clock"})


def _extra_enabled_producer_ids() -> frozenset[str]:
    raw = os.getenv(_ENABLED_IDS_ENV, "")
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def dark_producer_ids() -> frozenset[str]:
    """The registered producers that will not run on this process's config.

    Read by the sweep so ``0 candidates`` and ``never ran`` stop being the
    same observation.
    """
    return DARK_BY_DEFAULT - _extra_enabled_producer_ids()


async def renewal_window(
    pool: asyncpg.Pool, company_id: str, *, today: dt.date
) -> list[ReminderCandidate]:
    """Leases in the renewal window with NO offer started — grouped per
    property. An offer row in any status = the PM engaged = suppressed
    (drafting counts as acting; dismissing the offer means "not renewing")."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT l.property_id, p.name AS property_name,
                   COUNT(*) AS lease_count,
                   MIN(l.lease_to_date) AS soonest,
                   ARRAY_AGG(l.buildium_id ORDER BY l.lease_to_date) AS lease_ids
              FROM helixis_leases l
              LEFT JOIN helixis_properties p
                ON p.buildium_id = l.property_id AND p.company_id = l.company_id
              LEFT JOIN helixis_renewal_offers o
                ON o.lease_buildium_id = l.buildium_id AND o.company_id = l.company_id
             WHERE l.company_id = $1::uuid
               AND l.status = 'Active'
               AND l.lease_to_date BETWEEN now() AND now() + make_interval(days => $2)
               AND o.id IS NULL
               AND l.property_id IS NOT NULL
             GROUP BY l.property_id, p.name
            """,
            company_id,
            RENEWAL_WINDOW_DAYS,
        )
    out: list[ReminderCandidate] = []
    for r in rows:
        days_left = (r["soonest"].date() - today).days
        n = r["lease_count"]
        name = r["property_name"] or f"property #{r['property_id']}"
        # Escalates in place as the soonest lease approaches: 90→60→30.
        escalation = 2 if days_left <= 30 else 1 if days_left <= 60 else 0
        severity = "high" if escalation == 2 else "normal"
        out.append(
            ReminderCandidate(
                entity_key=f"property:{r['property_id']}",
                title=(
                    f"{n} lease{'s' if n > 1 else ''} at {name} "
                    f"{'are' if n > 1 else 'is'} in the renewal window"
                ),
                body=(f"Soonest ends in {days_left} days. No renewal offer has been started."),
                severity=severity,
                escalation=escalation,
                link_path=f"/properties/{r['property_id']}",
                payload={"lease_ids": list(r["lease_ids"]), "days_left": days_left},
            )
        )
    return fold_when_noisy(
        out,
        entity_key="renewal_window:all",
        title="{n} properties have leases in the renewal window",
        lead="Renewal offers not started at:",
        link_path="/operations",
    )


#: Above this many per-property cards from one producer, fold them into a
#: single roll-up. A desk that shows fifteen cards saying the same thing in
#: fifteen places has buried the one card that mattered — and on a portfolio
#: of single-unit buildings, per-property grouping IS per-lease grouping.
FOLD_THRESHOLD = 5


def fold_when_noisy(
    candidates: list[ReminderCandidate],
    *,
    entity_key: str,
    title: str,
    lead: str,
    link_path: str,
) -> list[ReminderCandidate]:
    """Collapse a noisy per-property set into one roll-up card.

    Under the threshold the per-property cards are the better UX — they're
    individually dismissible and name the place. Over it they're a wall, so
    one card carries the count and lists what it covers. Keeps the highest
    severity/escalation in the set: folding must never quietly downgrade the
    most urgent item.
    """
    if len(candidates) <= FOLD_THRESHOLD:
        return candidates
    names = [c.title for c in candidates]
    shown = "; ".join(names[:6])
    more = len(names) - min(len(names), 6)
    return [
        ReminderCandidate(
            entity_key=entity_key,
            title=title.format(n=len(candidates)),
            body=f"{lead} {shown}" + (f" (+{more} more)" if more else "") + ".",
            severity=("high" if any(c.severity == "high" for c in candidates) else "normal"),
            escalation=max((c.escalation for c in candidates), default=0),
            link_path=link_path,
            payload={
                "folded": len(candidates),
                "entity_keys": [c.entity_key for c in candidates],
            },
        )
    ]


async def leads_gone_cold(
    pool: asyncpg.Pool, company_id: str, *, today: dt.date, outbound: Any = None
) -> list[ReminderCandidate]:
    """Active leads with no touch in the cold threshold — one card per lead
    (cold leads are few by construction; the threshold IS the neglect gate).
    v1.5: a lead the team emailed from their own Gmail recently isn't cold —
    the CRM thread just didn't see it.

    ⚠ ``today`` is accepted and UNUSED, deliberately. The sweep hands the
    company's date to every producer so a new one cannot silently reach for
    the server clock — a set of "date-aware" producers to opt into is the
    thing somebody forgets (gotcha 88). Deleting it here turns a guarantee
    the signature enforces back into a convention.
    """
    cold = await leads_repo.cold_leads(pool=pool, company_id=company_id)
    if outbound is not None and cold:
        addresses = {lead.email.lower() for lead in cold if lead.email}
        if addresses:
            contacted = await outbound.recently_contacted(addresses, days=SUPPRESS_DAYS)
            if contacted:
                cold = [
                    lead for lead in cold if not (lead.email and lead.email.lower() in contacted)
                ]
    out: list[ReminderCandidate] = []
    for lead in cold:
        label = lead.name or lead.email or lead.phone or "A lead"
        out.append(
            ReminderCandidate(
                entity_key=f"lead:{lead.id}",
                title=f"{label} is going cold",
                body=(
                    f"No touch in {leads_repo.COLD_LEAD_DAYS}+ days"
                    + (f" · interested in {lead.unit_label}" if lead.unit_label else "")
                    + ". A quick nudge keeps them warm."
                ),
                severity="normal",
                link_path="/crm",
                payload={"lead_id": lead.id, "stage": lead.stage},
            )
        )
    return out


async def bills_due(
    pool: asyncpg.Pool, company_id: str, *, today: dt.date
) -> list[ReminderCandidate]:
    """Unpaid bills due within BILL_DUE_SOON_DAYS or overdue — **one card per
    vendor per due date**, not one per bill.
    Payment posting self-clears via ``is_paid`` (Bill.Payment.* webhook).

    ⚠ **This shipped ungrouped on a premise that measurement disproved.** The
    old docstring said "one card per bill (amounts and vendors differ; these
    are few and each is actionable)". Amounts and vendors do NOT reliably
    differ, and they are not few: the day this producer started working it put
    **16 cards carrying ONE distinct title** into Blue Ridge's Inbox --
    ``Redbud General Maintenance: $265.00 ... OVERDUE`` sixteen times, one per
    property (the recurring gutter-cleaning charge gotcha 53 records). Nothing
    was duplicated and nothing was wrong; sixteen copies of one sentence simply
    read as a glitch and buried everything else.

    That is the THIRD producer to learn this. ``tasks_overdue`` groups per
    property "so a messy week is one card per building, not ten", and
    ``rent_missing`` was RETIRED outright for "41 open cards out of 67, a wall
    of '1 Lease at NM / NJ / NH…' burying everything else". Grouping is the
    house rule; this was the producer still opting out of it.

    ⚠ **The card must NOT say "duplicate".** Same vendor + same amount + same
    due date is what a double-billed invoice looks like AND exactly what a
    legitimate per-property recurring charge looks like. ``unpaid_bills``
    already states both readings and refuses to call it on that signal alone
    (gotcha 53); a card that decided otherwise would be the confident false
    claim of gotcha 41, pointed at money. So this reports the COUNT and the
    TOTAL -- facts -- and leaves the judgement to the human opening it.

    ⚠ **A group of one still reads like one bill.** "1 bill totalling $265" is
    a rollup wearing a costume; the single-bill wording is preserved exactly.

    ⚠ **``$2::int`` is load-bearing, and without it this producer had never
    emitted a single card.** asyncpg sends a bare parameter as ``unknown`` and
    lets Postgres infer, but ``date + unknown`` cannot be resolved — there are
    ``date + integer``, ``date + interval`` and ``date + time`` candidates, so
    the server answers ``42725 operator is not unique`` and the whole query
    raises. The sweep catches per-producer exceptions and logs "cards left
    as-is", so the failure was completely silent.

    Measured on helixis-test 2026-08-29: **80 bills qualified across three
    companies, ``bill_due`` had produced 0 cards ever**, while six other
    producers held 102 between them — those compare against a bare
    ``current_date`` with no parameter, which is why they work. Six detector
    rules carried the identical shape and had likewise never fired; see
    ``tests/unit/test_sql_date_param_casts.py``, which scans for the pattern
    repo-wide rather than trusting that these seven were all of them.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT COALESCE(b.vendor_id, 0)      AS vendor_id,
                   v.name                        AS vendor_name,
                   b.due_date,
                   COUNT(*)                      AS bill_count,
                   -- ⚠ SUM over a column that is NULL on some rows returns the
                   -- sum of the rest, silently. `priced` counts what actually
                   -- carried a figure so the card can say so rather than
                   -- laundering a partial total into a complete-looking one
                   -- (gotcha 25's "a missing amount must never become $0.00").
                   SUM(b.total_amount)           AS total_amount,
                   COUNT(b.total_amount)         AS priced,
                   ARRAY_AGG(b.buildium_id ORDER BY b.buildium_id) AS bill_ids
              FROM helixis_bills b
              LEFT JOIN helixis_vendors v
                ON v.buildium_id = b.vendor_id AND v.company_id = b.company_id
             WHERE b.company_id = $1::uuid
               AND b.is_paid IS NOT TRUE
               AND b.due_date IS NOT NULL
               AND b.due_date <= current_date + $2::int
             GROUP BY COALESCE(b.vendor_id, 0), v.name, b.due_date
            """,
            company_id,
            BILL_DUE_SOON_DAYS,
        )
    out: list[ReminderCandidate] = []
    for r in rows:
        n = int(r["bill_count"])
        ids = list(r["bill_ids"])
        due = r["due_date"]
        overdue = due < today
        vendor = r["vendor_name"] or (
            f"Vendor #{r['vendor_id']}" if r["vendor_id"] else f"Bill #{ids[0]}"
        )
        priced = int(r["priced"])
        amount = f"${float(r['total_amount'] or 0):,.2f}"

        if n == 1:
            # Unchanged wording. A single bill was never the problem.
            title = f"{vendor}: {amount} " + (
                f"was due {due.isoformat()}" if overdue else "due soon"
            )
            body = f"Due {due.isoformat()}." + (" OVERDUE." if overdue else "")
        else:
            # ⚠ Count first, total second, and NEITHER is a verdict. See the
            # docstring: this shape is a double-billed invoice and a
            # per-property recurring charge, and the card cannot tell them
            # apart -- so it states what it counted and stops.
            unpriced = n - priced
            total_part = (
                f"{amount} total"
                if not unpriced
                else f"{amount} across {priced} of them ({unpriced} with no amount recorded)"
            )
            title = f"{vendor} — {n} unpaid bills, {total_part}"
            body = (
                f"All due {due.isoformat()}."
                + (" OVERDUE." if overdue else "")
                + " Open to see each one before paying or withholding."
            )

        out.append(
            ReminderCandidate(
                # ⚠ The key changed from `bill:<id>` to the group. `sync_producer`
                # self-clears any key this producer no longer emits, so the 16
                # per-bill cards retire themselves on the next sweep -- no
                # migration, no manual cleanup.
                entity_key=f"vendor:{r['vendor_id']}|due:{due.isoformat()}",
                title=title,
                body=body,
                severity="high" if overdue else "normal",
                escalation=1 if overdue else 0,
                payload={
                    "bill_ids": ids,
                    "bill_count": n,
                    "due_date": due.isoformat(),
                    # Kept for the single-bill case so anything reading
                    # `bill_id` still resolves; a group has no one bill.
                    **({"bill_id": ids[0]} if n == 1 else {}),
                },
            )
        )
    return out


async def tasks_overdue(
    pool: asyncpg.Pool, company_id: str, *, today: dt.date
) -> list[ReminderCandidate]:
    """Open tasks past their due date — grouped per property so a messy week
    is one card per building, not ten."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT COALESCE(t.property_id, 0) AS property_id,
                   p.name AS property_name,
                   COUNT(*) AS task_count,
                   MIN(t.due_date) AS oldest_due,
                   ARRAY_AGG(t.buildium_id ORDER BY t.due_date) AS task_ids
              FROM helixis_tasks t
              LEFT JOIN helixis_properties p
                ON p.buildium_id = t.property_id AND p.company_id = t.company_id
             WHERE t.company_id = $1::uuid
               AND {open_status_sql("t.status")}
               AND t.due_date < current_date
             GROUP BY COALESCE(t.property_id, 0), p.name
            """,
            company_id,
        )
    out: list[ReminderCandidate] = []
    for r in rows:
        n = r["task_count"]
        where = r["property_name"] or (
            f"property #{r['property_id']}" if r["property_id"] else "no property"
        )
        days_over = (today - r["oldest_due"]).days
        out.append(
            ReminderCandidate(
                entity_key=f"property:{r['property_id']}",
                title=f"{n} overdue task{'s' if n > 1 else ''} at {where}",
                body=f"Oldest is {days_over} day{'s' if days_over != 1 else ''} past due.",
                severity="high" if days_over >= 7 else "normal",
                link_path=(f"/properties/{r['property_id']}" if r["property_id"] else None),
                payload={"task_ids": list(r["task_ids"])},
            )
        )
    return out


async def rent_missing(
    pool: asyncpg.Pool,
    company_id: str,
    *,
    today: dt.date,
    grace_day: int = RENT_GRACE_DAY_OF_MONTH,
    suppress_days: int = SUPPRESS_DAYS,
    outbound: Any = None,
) -> list[ReminderCandidate]:
    """Active leases with NO payment posted this month, after the grace day —
    grouped per property, month-keyed so cards reset each cycle.

    Neglect gates, in order of cost:
    1. DB (in SQL): a payment posted this month, or an entity-linked
       activity-log action within ``suppress_days``.
    2. Payment promise (in SQL + ``promise_pending``): a tenant's ingested
       email promised a date that hasn't passed yet — quiet until it does,
       then the card returns as the follow-up (a past promise is called
       out in the body, not silently forgotten).
    3. Gmail-outbound (live, survivors only): the team's own sent mail
       reached a tenant on the lease within ``suppress_days``.
    """
    if today.day < grace_day:
        return []
    month_key = today.strftime("%Y-%m")
    async with pool.acquire() as conn:
        # ⚠ Current links only (``db.tenant_links``), in both gates: mail the
        # team sent a tenant who moved out, or a promise that tenant made,
        # says nothing about the rent the people living there now owe.
        rows = await conn.fetch(
            f"""
            SELECT l.buildium_id, l.property_id, p.name AS property_name,
                   u.unit_number,
                   ARRAY(
                       SELECT DISTINCT lower(t.email)
                         FROM helixis_lease_tenants lt
                         JOIN helixis_tenants t
                           ON t.buildium_id = lt.tenant_buildium_id
                          AND t.company_id = lt.company_id
                        WHERE lt.company_id = l.company_id
                          AND lt.lease_buildium_id = l.buildium_id
                          AND {current_link_sql()}
                          AND t.email IS NOT NULL) AS tenant_emails,
                   (SELECT MAX(pp.promised_date)
                      FROM helixis_payment_promises pp
                      JOIN helixis_lease_tenants lt
                        ON lt.company_id = l.company_id
                       AND lt.lease_buildium_id = l.buildium_id
                      JOIN helixis_tenants t
                        ON t.buildium_id = lt.tenant_buildium_id
                       AND t.company_id = lt.company_id
                     WHERE pp.company_id = l.company_id
                       AND {current_link_sql()}
                       AND pp.email = lower(t.email)) AS promised_date
              FROM helixis_leases l
              LEFT JOIN helixis_properties p
                ON p.buildium_id = l.property_id AND p.company_id = l.company_id
              LEFT JOIN helixis_units u
                ON u.buildium_id = l.unit_id AND u.company_id = l.company_id
             WHERE l.company_id = $1::uuid AND l.status = 'Active'
               AND l.property_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM helixis_lease_transactions t
                    WHERE t.company_id = l.company_id
                      AND t.lease_id = l.buildium_id
                      AND t.transaction_type = 'Payment'
                      AND t.transaction_date >= date_trunc('month', now()))
               AND NOT EXISTS (
                   SELECT 1 FROM helixis_activity_log a
                    WHERE a.company_id = l.company_id
                      AND a.entity_type IN ('lease', 'tenant')
                      AND a.entity_id = l.buildium_id
                      AND a.created_at >= now() - make_interval(days => $2))
             ORDER BY u.unit_number NULLS LAST, l.buildium_id
            """,
            company_id,
            suppress_days,
        )

    # Gate 2 — a pending promise buys quiet; a PAST promise stays in and
    # gets called out (the follow-up the promise was suppressing toward).
    leases = [r for r in rows if not promise_pending(r["promised_date"], today=today)]

    # Gate 3 — team already emailed a tenant on the lease (sent-mail check,
    # only for the survivors so a clean month costs zero API calls).
    if outbound is not None and leases:
        addresses = {a for r in leases for a in (r["tenant_emails"] or [])}
        if addresses:
            contacted = await outbound.recently_contacted(addresses, days=suppress_days)
            if contacted:
                leases = [r for r in leases if not (set(r["tenant_emails"] or []) & contacted)]

    grouped: dict[int, list[Any]] = {}
    for r in leases:
        grouped.setdefault(r["property_id"], []).append(r)

    out: list[ReminderCandidate] = []
    for property_id, group in grouped.items():
        n = len(group)
        name = group[0]["property_name"] or f"property #{property_id}"
        labels = [
            f"Unit {r['unit_number']}" if r["unit_number"] else f"#{r['buildium_id']}"
            for r in group
        ]
        units = ", ".join(labels[:6])
        more = n - min(n, 6)
        broken = sum(1 for r in group if r["promised_date"] is not None)
        body = (
            f"Past the {grace_day}th with nothing posted: {units}"
            + (f" (+{more} more)" if more > 0 else "")
            + ". No recent outreach on file."
        )
        if broken:
            body += (
                f" {broken} promised payment{'s' if broken > 1 else ''}"
                f" {'have' if broken > 1 else 'has'} now passed without posting."
            )
        out.append(
            ReminderCandidate(
                entity_key=f"{month_key}:property:{property_id}",
                title=(
                    f"{n} lease{'s' if n > 1 else ''} at {name} "
                    f"{'have' if n > 1 else 'has'} no rent payment this month"
                ),
                body=body,
                severity="high",
                link_path=f"/properties/{property_id}",
                payload={"month": month_key, "count": n, "broken_promises": broken},
            )
        )
    return out


async def webhooks_quiet(
    pool: asyncpg.Pool, company_id: str, *, today: dt.date
) -> list[ReminderCandidate]:
    """Helixis has gone deaf: a previously-active Buildium webhook feed has
    produced nothing for WEBHOOK_QUIET_HOURS. Usually a broken subscription
    or secret — the kind of silent failure that costs a week unnoticed.

    ⚠ ``today`` is accepted and UNUSED, deliberately. The sweep hands the
    company's date to every producer so a new one cannot silently reach for
    the server clock — a set of "date-aware" producers to opt into is the
    thing somebody forgets (gotcha 88). Deleting it here turns a guarantee
    the signature enforces back into a convention.
    """
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            """
            SELECT MAX(event_time) AS last_event,
                   COUNT(*) FILTER (
                       WHERE event_time >= now() - interval '14 days') AS recent
              FROM buildium_events
             WHERE company_id = $1::uuid
            """,
            company_id,
        )
    if not r or r["last_event"] is None or not r["recent"]:
        return []  # never active (or brand new) — nothing to alarm about
    last = r["last_event"]
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.UTC)
    hours = (dt.datetime.now(dt.UTC) - last).total_seconds() / 3600
    if hours < WEBHOOK_QUIET_HOURS:
        return []
    return [
        ReminderCandidate(
            entity_key="feed",
            title="Buildium webhooks have gone quiet",
            body=(
                f"No events in {int(hours)}h from a previously active feed. "
                "Check the webhook subscription + signing secret in "
                "Settings → Connectors → Buildium."
            ),
            severity="high",
            link_path="/settings",
            payload={"hours_quiet": int(hours)},
        )
    ]


# ── deposit-return clock (statutory deadline after a move-out) ────────


@dataclass(frozen=True)
class DepositClockReminder:
    """One scheduled deposit-return reminder from the pure core."""

    due_date: dt.date
    completion_date: dt.date
    severity: str
    escalation: int
    body: str


def deposit_clock_schedule(
    move_out_date: dt.date,
    state_abbr: str,
    deposit_group: dict[str, Any] | None,
    *,
    today: dt.date,
    lead_days: int = DEPOSIT_CLOCK_LEAD_DAYS,
    overdue_grace_days: int = DEPOSIT_CLOCK_OVERDUE_GRACE_DAYS,
) -> DepositClockReminder | None:
    """Pure scheduling core: move-out date + the state's deposit rules →
    whether to remind today, and with what text.

    ``deposit_group`` is the merged ``deposit`` block from the jurisdiction
    rules (with pending changes already resolved). Returns ``None`` when the
    state encodes no numeric return deadline (no reliable clock — never
    guess), when today is before the reminder window (opens ``lead_days``
    business-calendar days before the compliance completion date), or when
    the due date is more than ``overdue_grace_days`` past (the card
    self-clears rather than nagging forever).
    """
    if not isinstance(deposit_group, dict):
        return None
    days = deposit_group.get("return_deadline_days")
    if not isinstance(days, int | float) or isinstance(days, bool):
        return None
    unit = deposit_group.get("return_deadline_unit") or "calendar"
    computed = deadline_math.compute_deadline(move_out_date, int(days), unit, state=state_abbr)
    due = computed.due_date
    completion = deadline_math.compliance_completion_date(due, state=state_abbr)
    window_start = completion - dt.timedelta(days=lead_days)
    if today < window_start or today > due + dt.timedelta(days=overdue_grace_days):
        return None

    citation = deposit_group.get("citation")
    cite = f" ({citation})" if citation else ""
    base = (
        f"Move-out {move_out_date.isoformat()}: the security deposit must be "
        f"returned by {due.isoformat()}{cite}. Schedule completion by "
        f"{completion.isoformat()} — never rely on weekend roll-forward."
    )
    for flag in computed.flags:
        base += f" ⚠ {flag}."
    if today > due:
        return DepositClockReminder(due, completion, "high", 2, base + " The deadline has PASSED.")
    if today > completion:
        return DepositClockReminder(
            due, completion, "high", 1, base + " The completion window is closing."
        )
    return DepositClockReminder(due, completion, "normal", 0, base)


async def deposit_return_clock(
    pool: asyncpg.Pool, company_id: str, *, today: dt.date
) -> list[ReminderCandidate]:
    """Deposit-return statutory clock: recent move-outs whose deposit-return
    compliance date is approaching (or past) — one card per lease.

    DARK BY DEFAULT — runs only when ``deposit_return_clock`` is listed in
    ``HELIXIS_REMINDERS__ENABLED_IDS`` (the invoice_no_work_order precedent).

    Data source: ``helixis_turnovers`` — the webhook mirror rows the
    ``Lease.MoveOut.Created`` handler opens (migration 0046), joined to
    ``helixis_properties`` for the resolved ``state_fips`` (migration 0067).
    Known limitation: move-outs recorded before the turnover table existed,
    or in companies whose properties aren't jurisdiction-resolved yet, have
    no clock — the producer skips them honestly rather than guessing a
    state. Deadlines come from geo/deadline_math + the bundled jurisdiction
    rules (pending changes resolved as of today). Neglect gate: an
    activity-log touch on the lease within SUPPRESS_DAYS suppresses; a
    dismissed turnover ("not tracking in Helixis") is respected.
    """
    if "deposit_return_clock" not in _extra_enabled_producer_ids():
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT tv.lease_buildium_id, tv.move_out_date, tv.property_id,
                   p.name AS property_name, p.state_fips, u.unit_number
              FROM helixis_turnovers tv
              LEFT JOIN helixis_properties p
                ON p.buildium_id = tv.property_id AND p.company_id = tv.company_id
              LEFT JOIN helixis_units u
                ON u.buildium_id = tv.unit_id AND u.company_id = tv.company_id
             WHERE tv.company_id = $1::uuid
               AND tv.status != 'dismissed'
               AND tv.move_out_date IS NOT NULL
               AND tv.move_out_date >= current_date - make_interval(days => $2)
               AND NOT EXISTS (
                   SELECT 1 FROM helixis_activity_log a
                    WHERE a.company_id = tv.company_id
                      AND a.entity_type IN ('lease', 'tenant')
                      AND a.entity_id = tv.lease_buildium_id
                      AND a.created_at >= now() - make_interval(days => $3))
            """,
            company_id,
            DEPOSIT_CLOCK_LOOKBACK_DAYS,
            SUPPRESS_DAYS,
        )
    out: list[ReminderCandidate] = []
    for r in rows:
        state_abbr = jurisdiction.STATE_FIPS_TO_ABBR.get(str(r["state_fips"] or ""))
        if not state_abbr:
            continue  # jurisdiction not resolved — no clock, never guess
        answer = jurisdiction.lookup(state_abbr, today=today)
        reminder = deposit_clock_schedule(
            r["move_out_date"], state_abbr, answer.state.get("deposit"), today=today
        )
        if reminder is None:
            continue
        where_bits = [
            r["property_name"] or (f"property #{r['property_id']}" if r["property_id"] else None),
            f"Unit {r['unit_number']}" if r["unit_number"] else None,
        ]
        where = " · ".join(b for b in where_bits if b) or f"lease #{r['lease_buildium_id']}"
        out.append(
            ReminderCandidate(
                entity_key=f"lease:{r['lease_buildium_id']}",
                title=f"Deposit return due {reminder.due_date.isoformat()} — {where}",
                body=reminder.body,
                severity=reminder.severity,
                escalation=reminder.escalation,
                link_path=(f"/properties/{r['property_id']}" if r["property_id"] else None),
                payload={
                    "lease_id": r["lease_buildium_id"],
                    "state": state_abbr,
                    "due_date": reminder.due_date.isoformat(),
                    "completion_date": reminder.completion_date.isoformat(),
                },
            )
        )
    return out


PRODUCERS: dict[str, Any] = {
    "renewal_window": renewal_window,
    "lead_cold": leads_gone_cold,
    "bill_due": bills_due,
    "task_overdue": tasks_overdue,
    # ``rent_missing`` is DELIBERATELY NOT REGISTERED (2026-08-21). It tested
    # the same condition as ``RentNotCollectedDetector`` — active lease, no
    # payment posted since the 1st, past the grace day — but grouped by
    # PROPERTY, which on a portfolio of single-unit buildings degenerates into
    # one card per building: 41 open cards out of 67, a wall of "1 Lease at NM
    # / NJ / NH…" burying everything else. The detector groups ONE card per
    # rule and rides the enrichment→planner bridge, so it produces the full
    # message format instead of a title and a Dismiss button. Two systems
    # answering one question, and the thin one was the one switched on.
    # The function is kept below for reference; delete it once the detector
    # has a pilot's worth of mileage.
    "webhooks_quiet": webhooks_quiet,
    "deposit_return_clock": deposit_return_clock,
}

#: Every table each producer reads. The sweep uses it to skip a producer for a
#: company whose PMS never fills one of them (``db/pms_coverage.py``), same
#: rule as ``Detector.reads_tables``. ``test_pms_coverage.py`` checks every
#: entry against the SQL the producer actually executes, and that every
#: registered producer has one: a producer missing from this map would
#: otherwise read as depending on nothing and always run.
PRODUCER_READS: dict[str, frozenset[str]] = {
    "renewal_window": frozenset({"helixis_leases", "helixis_properties", "helixis_renewal_offers"}),
    "lead_cold": frozenset({"crm_leads", "crm_lead_messages"}),
    "bill_due": frozenset({"helixis_bills", "helixis_vendors"}),
    "task_overdue": frozenset({"helixis_tasks", "helixis_properties"}),
    # Buildium's own webhook log, and the card's copy names Buildium.
    "webhooks_quiet": frozenset({"buildium_events"}),
    "deposit_return_clock": frozenset(
        {"helixis_turnovers", "helixis_properties", "helixis_units", "helixis_activity_log"}
    ),
}

__all__ = ["PRODUCERS", "PRODUCER_READS", *sorted(PRODUCERS)]
