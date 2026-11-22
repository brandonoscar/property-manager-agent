"""Did the message actually arrive? Nobody could answer that until now.

``helixis_sms_log`` (migration 0069) records the whole delivery lifecycle —
``queued`` → ``sent`` → ``delivered`` | ``undelivered`` | ``failed``, plus
Twilio's ``ErrorCode``. Measured 2026-09-09 by grepping every reader in the
tree: the table is written by three paths and **read by exactly one query** —
``reminders/sms_digest._already_sent_today``, which selects ``1`` to see
whether today's notice already went. Nothing anywhere reads ``status`` or
``error_code``.

So a text to a landline came back ``undelivered`` with error 30006, was
written down, and nobody ever found out (checklist #89). The manager keeps a
number in the consent table that has never once accepted a message, and every
reminder aimed at it evaporates.

⚠ **The quieter one is worse and is the reason for four values, not two.**
A carrier that filters a message as spam sends **no receipt at all**
(checklist #77). The row stays at ``sent`` for ever. That is not the same fact
as ``undelivered``, and collapsing the two would either invent a failure that
Twilio never reported or hide one it did — gotchas 55/57, unknown must not
render as a measured value. ``no_receipt`` says exactly what happened: we
handed it over, and nothing came back.

⚠ **This is pre-launch hardening, not a live incident.** SMS is dark until A2P
approval, so the log holds no failed production sends today. The loop being
open is what is being fixed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

#: How long an accepted message may sit with no receipt before we stop calling
#: it in flight.
#:
#: Twilio's own receipts arrive in seconds; the long tail is carrier-side. 30
#: minutes is deliberately generous — the cost of calling a slow delivery
#: "never confirmed" is a manager chasing a message that then lands, and a
#: signal that cries wolf is one people switch off (gotcha 52, and the same
#: reasoning behind ``mirror_sync_state.STALE_AFTER_HOURS`` being 26 not 24).
NO_RECEIPT_AFTER_MINUTES = 30

#: Twilio wrote a terminal failure.
UNDELIVERED = "undelivered"
#: Accepted, no receipt, and past the window — the carrier-filter shape.
NO_RECEIPT = "no_receipt"
#: Accepted, no receipt yet, still inside the window.
PENDING = "pending"
DELIVERED = "delivered"

#: Statuses Twilio writes that mean the message is definitively not arriving.
#:
#: ⚠ PUBLIC because ``reminders/sms_digest`` needs the same list for its daily
#: cap. Two copies of "which statuses mean it did not arrive" is gotcha 73's
#: ``"approved"``-in-three-modules, and a fourth spelling drifting by one
#: character would silently change who gets notified.
TERMINAL_FAILURE_STATUSES = frozenset({"undelivered", "failed"})
_TERMINAL_FAILURES = TERMINAL_FAILURE_STATUSES
#: Statuses that mean "handed over, outcome unknown".
_IN_FLIGHT = frozenset({"queued", "sent"})


def classify(status: str | None, created_at: datetime | None, now: datetime | None = None) -> str:
    """One outbound row's real outcome.

    ⚠ Four values, and the split that matters is ``UNDELIVERED`` vs
    ``NO_RECEIPT``. The first is Twilio telling us it failed; the second is
    Twilio telling us nothing. A manager acts differently on each — a bad
    number gets replaced, a silent one gets re-sent — and a two-valued flag
    forces one of them to be a lie.

    An unrecognised status returns ``NO_RECEIPT`` rather than ``DELIVERED``:
    the safe direction is admitting we cannot confirm arrival, the same way
    ``maintenance_status.is_open`` asks "is this NOT closed" so an unknown
    value over-reports instead of quietly dropping work (gotcha 50).
    """
    value = (status or "").strip().lower()
    if value == DELIVERED:
        return DELIVERED
    if value in _TERMINAL_FAILURES:
        return UNDELIVERED
    if value in _IN_FLIGHT:
        if created_at is None:
            # No age to measure means no basis for calling it stuck.
            return PENDING
        stamped = created_at if created_at.tzinfo else created_at.replace(tzinfo=UTC)
        reference = now or datetime.now(UTC)
        if reference - stamped >= timedelta(minutes=NO_RECEIPT_AFTER_MINUTES):
            return NO_RECEIPT
        return PENDING
    return NO_RECEIPT


@dataclass(frozen=True)
class DeliverySummary:
    """Outbound delivery over a window, for one company.

    ⚠ ``None`` on every count when the log could not be read — not zero.
    "Nothing failed" and "nothing was checked" need opposite readings, and
    zeroing the second tells a manager their texting is healthy on the
    strength of a query that never ran (gotchas 57/70/89).
    """

    window_days: int
    delivered: int | None = None
    undelivered: int | None = None
    no_receipt: int | None = None
    pending: int | None = None

    @property
    def readable(self) -> bool:
        return self.delivered is not None


async def summarize(
    pool: Any, company_id: str, *, window_days: int = 7, now: datetime | None = None
) -> DeliverySummary:
    """Count each outcome for this company's outbound sends in the window.

    Classification happens in Python rather than SQL on purpose: ``classify``
    is the ONE definition of what each status means, and a second copy in a
    ``CASE`` expression would be the two-spellings-of-open problem this repo
    already paid for once (gotcha 50).
    """
    since = (now or datetime.now(UTC)) - timedelta(days=window_days)
    try:
        rows = await pool.fetch(
            "SELECT status, created_at FROM public.helixis_sms_log "
            "WHERE company_id = $1::uuid AND direction = 'outbound' "
            "  AND created_at >= $2",
            company_id,
            since,
        )
    except Exception:  # noqa: BLE001 — a health panel must never break the page
        logger.warning("sms delivery summary failed for company=%s", company_id, exc_info=True)
        return DeliverySummary(window_days=window_days)

    counts = {DELIVERED: 0, UNDELIVERED: 0, NO_RECEIPT: 0, PENDING: 0}
    for row in rows:
        counts[classify(row["status"], row["created_at"], now)] += 1
    return DeliverySummary(
        window_days=window_days,
        delivered=counts[DELIVERED],
        undelivered=counts[UNDELIVERED],
        no_receipt=counts[NO_RECEIPT],
        pending=counts[PENDING],
    )


async def last_outcome_by_phone(
    pool: Any, company_id: str, *, now: datetime | None = None
) -> dict[str, tuple[str, str | None]]:
    """``phone -> (outcome, error_code)`` for each number's most recent send.

    This is the half that closes the landline case at the place somebody can
    act on it: the consent row itself. A number whose last three messages were
    ``undelivered`` is one to replace, and until now the only place that fact
    existed was a column nothing selected.

    Returns ``{}`` on any failure — the consent list still renders, just
    without outcomes. An empty map is honestly indistinguishable from "no
    sends yet", which is why the panel labels a missing entry "no messages
    sent" rather than anything about delivery.
    """
    try:
        rows = await pool.fetch(
            "SELECT DISTINCT ON (phone) phone, status, error_code, created_at "
            "FROM public.helixis_sms_log "
            "WHERE company_id = $1::uuid AND direction = 'outbound' "
            "ORDER BY phone, created_at DESC",
            company_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning("sms last-outcome lookup failed for company=%s", company_id, exc_info=True)
        return {}
    return {
        row["phone"]: (classify(row["status"], row["created_at"], now), row["error_code"])
        for row in rows
    }


__all__ = [
    "DELIVERED",
    "TERMINAL_FAILURE_STATUSES",
    "NO_RECEIPT",
    "NO_RECEIPT_AFTER_MINUTES",
    "PENDING",
    "UNDELIVERED",
    "DeliverySummary",
    "classify",
    "last_outcome_by_phone",
    "summarize",
]
