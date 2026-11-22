"""Whose phone is this — and what happens to it when they leave.

THE DEFECT (found 2026-09-09 by tracing what a departure actually does)
-----------------------------------------------------------------------
The manager digest texts every live ``recipient_kind='manager'`` consent row
after each reminder sweep: *"3 items need attention; top: <the top card's
title>"*. That is portfolio content, and it kept reaching people who had left
the company.

``remove_company_member`` deletes the ``company_users`` row, writes an activity
entry and re-syncs seats. It touched the consent ledger nowhere — and until
migration 0096 it could not have, because nothing on the row said whose number
it was. ``display_name`` is free text; ``evidence->>'recorded_by'`` is whoever
TYPED it, which for a tenant's number is an admin.

WHAT THIS MODULE WILL NOT DO
----------------------------
⚠ **It never guesses an owner.** A row with ``owner_user_id`` NULL is left
alone, because acting on a guess means silencing somebody else's alerts on a
colleague's departure — gotcha 55's decline-to-assert, pointed at a revocation
rather than at a filter. The honest half is
:func:`unattributed_manager_numbers`: the product SAYS how many numbers it
could not settle instead of implying it settled them all.

⚠ **Manager-kind only.** Employment and tenancy are different relationships. A
property manager who also rents from the company might hold a
``recipient_kind='tenant'`` row on the same account; revoking that because they
changed jobs would stop texts about their own home. The digest is the leak, and
the digest reads manager rows.

⚠ **Revoke, never delete.** ``helixis_sms_consent`` is the TCPA record and has
to prove the grant AND the revocation (0068). Same posture as the consent API's
own DELETE, which is also an UPDATE.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Written to ``revocation_source`` so the ledger says WHY, distinguishing this
#: from a STOP text (``twilio_stop_webhook``) and from somebody using the
#: settings screen (``app_settings``). An auditor reading the row can tell an
#: opt-out from an offboarding.
REVOCATION_SOURCE = "staff_departure"

#: The only kind a departure touches. See the module docstring.
STAFF_KIND = "manager"

_REVOKE_SQL = """
UPDATE public.helixis_sms_consent
   SET revoked_at        = COALESCE(revoked_at, now()),
       revocation_source = COALESCE(revocation_source, $3)
 WHERE company_id     = $1::uuid
   AND owner_user_id  = $2::uuid
   AND recipient_kind = $4
   AND revoked_at IS NULL
RETURNING id
"""

_UNATTRIBUTED_SQL = """
SELECT count(*) AS n
  FROM public.helixis_sms_consent
 WHERE company_id     = $1::uuid
   AND recipient_kind = $2
   AND revoked_at IS NULL
   AND owner_user_id IS NULL
"""


async def revoke_for_departing_member(pool: Any, *, company_id: str, user_id: str) -> int | None:
    """Revoke the staff numbers this person CLAIMED as their own.

    Returns how many rows were revoked, or ``None`` when the ledger could not
    be reached.

    ⚠ ``None`` is not zero, and the difference is the whole point. Zero means
    "they had claimed no numbers"; ``None`` means "we could not look", and a
    caller that renders the second as the first tells an admin the offboarding
    is complete when nothing was checked (gotcha 57's unknown-must-not-collapse
    -into-zero, and gotcha 70's unverifiable-is-never-a-pass).

    Never raises: a failure here must not abort a removal that has already
    happened. It logs at ERROR, because a staff member still holding a live
    alert channel is exactly the thing somebody has to go and fix by hand.
    """
    try:
        rows = await pool.fetch(_REVOKE_SQL, company_id, user_id, REVOCATION_SOURCE, STAFF_KIND)
    except Exception:  # noqa: BLE001 — the removal already happened; never abort it
        logger.exception(
            "consent revoke on departure FAILED company=%s user=%s — "
            "their alert numbers may still be live; check Settings → Connectors → Twilio SMS",
            company_id,
            user_id,
        )
        return None
    if rows:
        logger.info(
            "revoked %d staff SMS number(s) on departure company=%s user=%s",
            len(rows),
            company_id,
            user_id,
        )
    return len(rows)


async def unattributed_manager_numbers(pool: Any, *, company_id: str) -> int | None:
    """Live staff numbers nobody has claimed — the ones a departure cannot act on.

    This is the honest counterpart to the revoke above. Every row here is a
    number that goes on receiving the digest no matter who leaves, because the
    ledger does not know whose it is. Surfacing the count is what turns a
    silent gap into a thing an admin can act on.

    ``None`` on a lookup failure, for the same reason as above.
    """
    try:
        row = await pool.fetchrow(_UNATTRIBUTED_SQL, company_id, STAFF_KIND)
    except Exception:  # noqa: BLE001
        logger.warning("unattributed-number count failed company=%s", company_id, exc_info=True)
        return None
    if row is None:
        return None
    return int(row["n"])


__all__ = [
    "REVOCATION_SOURCE",
    "STAFF_KIND",
    "revoke_for_departing_member",
    "unattributed_manager_numbers",
]
