"""Is this risk card still describing something that is true?

A detector finding becomes an Inbox card once, by design: `inbox_bridge` stamps
`bridged_at` so a resolved→new reactivation cannot re-spam the Inbox. The
condition behind it, though, keeps moving — `detectors/store` auto-resolves any
finding the detector no longer emits, and nothing told the card.

MEASURED on helixis-test, 2026-09-09, before any of this existed: **81 risk
cards, 35 of them (43%) fully healed** — every finding behind them resolved and
the card still sitting in the Inbox describing the problem — plus **9 more
overstating their count** ("Rent charged below the lease — 4 findings" when two
had since been fixed). 66 healed findings behind open cards in total. So more
than half the risk Inbox was either obsolete or wrong about how much was wrong
(coverage checklist #244).

⚠ **READ-TIME, not a write-time reconcile, and that is the design.** A sweep
that rewrote each card would be a second derived artifact to keep in sync — one
more thing that drifts, needs its own staleness check, and fails silently when
the sweep does not run. Computing the state as the card is READ cannot go stale
by construction. Same reasoning as the Drive index's freshness line and the
truncation footer: state the current truth on the surface where somebody reads
it, rather than trying to keep a copy correct.

⚠ **A DISMISSED finding is not a resolved one, and collapsing them would be a
false claim in the reassuring direction.** Resolved means the detector looked
again and the condition is gone. Dismissed means a person said "this is fine" —
the world did not change, an opinion was recorded. A card reported as healed on
the strength of a dismissal tells the PM a problem went away when nobody ever
fixed it.

⚠ **A flag id with no row is UNKNOWN, never resolved** (gotchas 55/57/70). The
row could be gone for reasons that have nothing to do with the condition — a
purge, a restore, an id that never existed. "We could not check" and "it is
fixed" are different sentences and only one of them is safe to guess.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The payload key `inbox_bridge.build_risk_event_payload` writes. Named once
#: here so the producer and this reader cannot drift into disagreeing about it
#: (gotcha 28) — a renamed key would otherwise make every card silently
#: unreadable and every card would report `unknown`, which reads like caution
#: rather than like a bug.
FLAG_IDS_KEY = "FlagIds"

#: Statuses `helixis_risk_flags` can hold (migration 0051's CHECK constraint).
OPEN_STATUSES = frozenset({"new", "confirmed"})
RESOLVED_STATUS = "resolved"
DISMISSED_STATUS = "dismissed"

STATE_OPEN = "open"
STATE_PARTLY_RESOLVED = "partly_resolved"
STATE_ALL_RESOLVED = "all_resolved"
STATE_UNKNOWN = "unknown"


def flag_ids_on(payload: Any) -> list[str]:
    """The flag ids a risk card was built from, or `[]` for any other card.

    Tolerant on purpose: the Inbox carries Buildium, email, comms and risk
    rows through one route, and a malformed payload must cost this annotation
    rather than the whole page.
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get(FLAG_IDS_KEY)
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if x]


def finding_state(flag_ids: list[str], statuses: dict[str, str]) -> dict[str, Any] | None:
    """How much of this card is still true, given the flags' CURRENT statuses.

    ``None`` when the card is not a risk card — the caller attaches nothing and
    every other Inbox row is untouched.

    The counts are the honest part. "3 of 4 have since been resolved" is a
    different instruction to a property manager than "this is done", and both
    are different from "1 was dismissed" — so all three are reported separately
    rather than folded into a single boolean.
    """
    if not flag_ids:
        return None
    unique = list(dict.fromkeys(flag_ids))
    open_n = resolved_n = dismissed_n = unknown_n = 0
    for fid in unique:
        status = statuses.get(fid)
        if status is None:
            unknown_n += 1
        elif status in OPEN_STATUSES:
            open_n += 1
        elif status == RESOLVED_STATUS:
            resolved_n += 1
        elif status == DISMISSED_STATUS:
            dismissed_n += 1
        else:
            # A status the CHECK constraint does not currently allow. Counting
            # it as resolved would be the reassuring guess; unknown is the one
            # that cannot mislead.
            unknown_n += 1

    if unknown_n:
        # ⚠ Any unreadable finding makes the WHOLE card's state unknown, not
        # just that finding's. "3 of 4 resolved" implies the fourth was
        # checked; if it was not, the sentence is a stronger claim than the
        # data supports.
        state = STATE_UNKNOWN
    elif open_n:
        state = STATE_PARTLY_RESOLVED if resolved_n else STATE_OPEN
    elif resolved_n:
        state = STATE_ALL_RESOLVED
    else:
        # Everything dismissed and nothing resolved: a person judged it, the
        # condition was never observed to go away.
        state = STATE_OPEN

    return {
        "state": state,
        "total": len(unique),
        "open": open_n,
        "resolved": resolved_n,
        "dismissed": dismissed_n,
        "unknown": unknown_n,
    }


#: One query per Inbox PAGE, not per card.
#:
#: ⚠ `payload` is deliberately NOT added to `RECENT_EVENTS_COLUMNS`. That tuple
#: is the boot-time `verify_schema` contract, and it is fetched for every Inbox
#: row — widening it to carry a whole jsonb envelope so that a handful of risk
#: rows can be annotated would charge every card for one card type's feature.
#: The ids are extracted server-side instead.
#:
#: ⚠ Company-scoped twice, on the EVENT and again on the JOIN (gotcha 47). The
#: second one is not redundant: a card's payload is a list of ids we did not
#: validate, so without it a crafted or corrupted `FlagIds` could read another
#: tenant's finding statuses. Scoping the join makes a foreign id resolve to no
#: row, which the core then reports as `unknown` — the safe answer.
_CARD_FLAG_STATUS_SQL = """
SELECT e.id::text AS event_id,
       fid        AS flag_id,
       f.status   AS status
  FROM buildium_events e
  CROSS JOIN LATERAL jsonb_array_elements_text(
      CASE WHEN jsonb_typeof(e.payload -> $3) = 'array'
           THEN e.payload -> $3
           ELSE '[]'::jsonb
      END) AS fid
  LEFT JOIN helixis_risk_flags f
    ON f.id::text = fid
   AND f.company_id = e.company_id
 WHERE e.company_id = $1::uuid
   AND e.id = ANY($2::uuid[])
"""


async def finding_states_for_events(
    pool: Any, company_id: str, event_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """``{event_id: finding_state}`` for whichever of these are risk cards.

    ⚠ Never raises, and returns `{}` rather than a partial answer on failure.
    This is an ANNOTATION on somebody's Inbox: losing it costs a caveat, while
    letting it raise would 500 the whole page — and the Inbox is where the
    product's work lives. Gotcha 62's asymmetry, and the same call the Drive
    freshness line makes.

    ⚠ The guard covers the READ **and the parse**, and the second half is the
    one that was missing. A row whose shape is not what this query returns —
    the column renamed, a caller handing back something else — raised a bare
    `KeyError` straight out of a function whose entire contract is that it
    cannot, and 500'd the whole Inbox page for an annotation on a handful of
    rows. Gotcha 48's shape: the docstring above asserted the guarantee, the
    `try` only covered the fetch, and prose cannot fail.
    """
    if not event_ids:
        return {}
    try:
        rows = await pool.fetch(_CARD_FLAG_STATUS_SQL, company_id, event_ids, FLAG_IDS_KEY)

        per_event: dict[str, list[str]] = {}
        statuses: dict[str, str] = {}
        for row in rows:
            event_id, flag_id, status = row["event_id"], row["flag_id"], row["status"]
            per_event.setdefault(str(event_id), []).append(str(flag_id))
            if status is not None:
                statuses[str(flag_id)] = str(status)
    except (Exception, SystemExit):
        logger.warning("could not read finding states for the inbox page", exc_info=True)
        return {}

    out: dict[str, dict[str, Any]] = {}
    for event_id, ids in per_event.items():
        state = finding_state(ids, statuses)
        if state is not None:
            out[event_id] = state
    return out


__all__ = [
    "DISMISSED_STATUS",
    "FLAG_IDS_KEY",
    "OPEN_STATUSES",
    "RESOLVED_STATUS",
    "STATE_ALL_RESOLVED",
    "STATE_OPEN",
    "STATE_PARTLY_RESOLVED",
    "STATE_UNKNOWN",
    "finding_state",
    "finding_states_for_events",
    "flag_ids_on",
]
