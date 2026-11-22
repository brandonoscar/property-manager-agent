"""Manager alert digest — one SMS per consented manager per UTC day.

The post-sweep step of the reminder engine (Stage 3 of the SMS system):
when a company's sweep leaves reminder cards on the desk AND the Twilio
channel is live AND the company holds at least one live manager-kind
consent row, each consented manager gets AT MOST ONE ``account_notice``
template SMS per UTC day — "3 items need attention; top: <title>".

Guard layering (deliberate — nothing here re-implements a compliance
gate):

* Every send goes through :func:`helixis.comms.sms.send_sms`, so ALL the
  Stage-1 guards apply automatically — feature flag, config, per-number
  consent (fail closed), the TCPA quiet-hours window, and templates-only
  rendering. A :class:`SmsBlocked` is logged at info and NEVER fails the
  sweep (a quiet-hours block simply means a later sweep that day retries
  inside the window).
* The daily cap is enforced by querying ``helixis_sms_log`` for an
  outbound ``account_notice`` row to that phone since midnight UTC — no
  new table, and ``send_sms``'s best-effort log insert is the write side.
  (The log insert can theoretically fail after a send; that soft spot is
  accepted — worst case one extra digest, never a compliance breach.)
  ⚠ A row Twilio reported ``undelivered``/``failed`` does not count, or a
  message that never arrived would silently buy the manager a day of
  nothing. Rows with no outcome yet DO count: re-sending on a receipt that
  has not come back turns one blip into two texts.
* The summary param is truncated so the RENDERED message stays within
  :data:`MAX_RENDERED_CHARS` (two GSM-7 SMS segments).

Same failure posture as the sweep's other steps: per-company isolation,
loud logs, never raises past :func:`send_reminder_digests`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time
from typing import Any

from helixis.comms.delivery_health import TERMINAL_FAILURE_STATUSES
from helixis.comms.sms import TEMPLATES, SmsBlocked, redact_phone, send_sms
from helixis.db.repositories import reminders as reminders_repo

logger = logging.getLogger(__name__)

TEMPLATE_ID = "account_notice"

# Two SMS segments. GSM-7 concatenated segments carry 153 chars each; 320
# keeps a small safety margin under 2×160 either way.
MAX_RENDERED_CHARS = 320

# The template's fixed text around the {notice} param — everything that is
# NOT ours to trim. Computed from the registered template so a template
# edit can't silently push renders past the cap.
_TEMPLATE_OVERHEAD = len(TEMPLATES[TEMPLATE_ID].format(notice=""))
_MAX_NOTICE_CHARS = MAX_RENDERED_CHARS - _TEMPLATE_OVERHEAD


def build_notice(item_count: int, top_title: str | None) -> str:
    """The digest's {notice} param: "N item(s) need attention; top: <title>",
    truncated so the rendered template stays ≤ MAX_RENDERED_CHARS."""
    plural = "s" if item_count != 1 else ""
    notice = f"{item_count} item{plural} need{'' if plural else 's'} attention"
    top = (top_title or "").strip()
    if top:
        notice = f"{notice}; top: {top}"
    if len(notice) > _MAX_NOTICE_CHARS:
        notice = notice[: _MAX_NOTICE_CHARS - 1].rstrip() + "…"
    return notice


async def _already_sent_today(pool: Any, phone: str, now_utc: datetime) -> bool:
    """True when an outbound account_notice already went to ``phone`` since
    midnight UTC. Fails CLOSED (True) on a lookup error — an unverifiable
    cap must never mean "send anyway".

    ⚠ A message Twilio reported as ``undelivered`` or ``failed`` does NOT
    spend the day's one allowed notice. It was accepted, written down, and
    never arrived — so counting it means the manager silently gets nothing
    that day, which is the failure the cap exists to bound turned into a
    failure to notify at all.

    ⚠ And ONLY those two. A row still at ``queued``/``sent`` has no outcome
    yet, and re-sending on the strength of a receipt that has not come back
    is how one blip becomes two texts. Unknown counts as spent — the same
    decline-to-assert as the fail-closed arm below (gotchas 55/57).
    """
    midnight = datetime.combine(now_utc.date(), time.min, tzinfo=UTC)
    try:
        row = await pool.fetchrow(
            "SELECT 1 FROM public.helixis_sms_log "
            "WHERE phone = $1 AND direction = 'outbound' "
            "  AND template_id = $2 AND created_at >= $3 "
            "  AND NOT (status = ANY($4::text[])) "
            "LIMIT 1",
            phone,
            TEMPLATE_ID,
            midnight,
            sorted(TERMINAL_FAILURE_STATUSES),
        )
    except Exception:  # noqa: BLE001 — unverifiable cap → treat as spent
        logger.warning(
            "sms digest: daily-cap lookup failed for %s — skipping (fail closed)",
            redact_phone(phone),
            exc_info=True,
        )
        return True
    return row is not None


async def send_reminder_digests(
    pool: Any,
    settings: Any,
    company_id: str,
    counts: dict[str, int],
    *,
    now: datetime | None = None,
) -> int:
    """Send the per-manager digest for one company after its sweep.

    Returns the number of messages accepted by Twilio. Never raises —
    every failure is logged and skipped (the sweep must not care).
    """
    # Cheap short-circuits before any DB work: channel dark, or the sweep
    # produced nothing.
    if settings is None or not getattr(getattr(settings, "twilio", None), "enabled", False):
        return 0
    if not counts or sum(counts.values()) <= 0:
        return 0

    now_utc = now if now is not None else datetime.now(UTC)

    # Live manager-kind consents for this company.
    try:
        managers = await pool.fetch(
            "SELECT phone FROM public.helixis_sms_consent "
            "WHERE company_id = $1::uuid AND recipient_kind = 'manager' "
            "  AND revoked_at IS NULL",
            company_id,
        )
    except Exception:  # noqa: BLE001 — no roster, no digest
        logger.warning(
            "sms digest: consent lookup failed for company=%s", company_id, exc_info=True
        )
        return 0
    if not managers:
        return 0

    # The desk as it stands after the sweep — count + top card (list_active
    # is already ordered severity-first).
    try:
        cards = await reminders_repo.list_active(pool, company_id=company_id)
    except Exception:  # noqa: BLE001 — can't summarize, don't text
        logger.warning("sms digest: card listing failed for company=%s", company_id, exc_info=True)
        return 0
    if not cards:
        return 0
    notice = build_notice(len(cards), cards[0].title)

    sent = 0
    for row in managers:
        phone = row["phone"]
        if await _already_sent_today(pool, phone, now_utc):
            continue
        try:
            await send_sms(
                settings=settings,
                pool=pool,
                company_id=company_id,
                to_phone=phone,
                template_id=TEMPLATE_ID,
                params={"notice": notice},
                now=now_utc,
            )
            sent += 1
        except SmsBlocked as blocked:
            # A guard said no (quiet hours / revoked mid-flight / flag
            # flipped) — expected, never a sweep failure.
            logger.info(
                "sms digest blocked (%s) company=%s to=%s",
                blocked.reason,
                company_id,
                redact_phone(phone),
            )
        except Exception:  # noqa: BLE001 — a Twilio failure must not fail the sweep
            logger.warning(
                "sms digest send failed company=%s to=%s",
                company_id,
                redact_phone(phone),
                exc_info=True,
            )
    if sent:
        logger.info("sms digest sent company=%s messages=%d", company_id, sent)
    return sent


__all__ = ["MAX_RENDERED_CHARS", "TEMPLATE_ID", "build_notice", "send_reminder_digests"]
