"""Record, in the activity feed, that OCCUPELLA changed something.

MEASURED on helixis-test 2026-09-10, cross-tenant because which provenance
values the product writes is a property of the code and not of one company's
books: **528 activity rows** — 452 `webhook` (Buildium telling us what somebody
else changed), 53 `chat/web_search`, 21 `manual` (notes, invites, an export, a
member removal). And **not one row for a Buildium write Occupella made.**

`grep -c write_activity` on `agents/buildium_tools.py` and `agents/sms_tools.py`
returns 0 for both. The read tools all log — `web_tools`, `browser_tools`,
`document_tools`, `image_tools` — so the product has recorded "Occupella
searched the web" fifty-three times and has never once recorded "Occupella
posted a charge to a tenant's ledger".

⚠ **The intent was written down and never reached the write surface.**
`db/repositories/activity.py`'s own module docstring says *"Every agent action,
webhook processed, and scheduled run should land here so there's a single audit
stream."* Gotcha 48's shape at the scale of a feature: a comment cannot fail, so
it went on describing a stream that was missing its most consequential half.

What existed instead is `helixis_action_idempotency`, and gotcha 70 is explicit
that a `succeeded` row there means *Buildium returned 2xx* — it is a dedup
ledger, not an audit trail, it carries no human-readable description, and
nothing renders it. So a property manager could not answer "what did Occupella
change in my account?" from inside the product (coverage checklist #361).

⚠ **This lives in `run_guarded`, NOT at the eleven write tools.** Nineteen call
sites reach that function and a twentieth is one PR away; patching each is the
fan-out that gotchas 25/32/81 all end at, and the one that forgets is silently
exempt — which is exactly how this gap was born. `run_guarded` is the choke
point every confirm-gated write already passes through.

⚠ **The `record=` hook was already on `run_guarded` and used by NOTHING.** It is
a per-caller callback, so wiring the audit through it would have re-created the
same fan-out one layer down. It stays as the caller's hook; this is the rule.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Outcomes worth a row, and the reasoning is the whole design.
#:
#: ``executed`` — the write fired. Obvious.
#: ``blocked_needs_review`` — the side effect DID fire and the read-back could
#: not confirm it (gotcha 70's 200-but-lie). That is the single most important
#: row this feed can carry: it is the one case where Buildium may hold a change
#: nobody can account for, so omitting it would leave the least explainable
#: event as the only invisible one.
#:
#: Deliberately ABSENT: ``replayed`` (a second approval of the same key returns
#: the prior result — nothing new happened in Buildium, and a row would make the
#: feed claim two charges where there was one), ``not_approved`` /
#: ``already_pending`` / ``blocked_store_unavailable`` (nothing happened at all).
#: A feed that records non-events is one people stop reading, which is how the
#: real row gets scrolled past (gotcha 84's canary).
RECORDED_OUTCOMES = frozenset({"executed", "blocked_needs_review"})

#: The feed already special-cases a `Buildium · …` label (`ActivityPage.tsx`
#: line 419), so this follows the same shape rather than inventing a second
#: convention the UI does not know about.
LABEL_PREFIX = "Occupella"

#: Bound on the copied approval text. Generous — the description IS the audit
#: record and a truncated one is worth less than a long one.
_MAX_DESCRIPTION = 1000


def _system_name(action_type: str) -> str:
    """The system a write went to, named from its action type.

    Every PMS write tool names its action ``<pms key>_<verb>``
    (``buildium_post_charge``, ``rentvine_create_work_order``), so the prefix
    says where to look. ⚠ Anything else reads as Buildium, which is what this
    row said for every write before a second system existed: the unattended
    note writer (``notes.autonomous``) writes to Buildium.
    """
    from helixis.pms import registry

    for key, found in registry.PMS.items():
        if action_type.startswith(f"{key}_"):
            return found.name
    return registry.PMS[registry.BUILDIUM].name


def _description(action_type: str, confirm: dict[str, Any] | None, outcome: str) -> str:
    """What the row says happened.

    ⚠ **Copied VERBATIM from the confirm payload, and deliberately not
    scrubbed.** `details` is the exact sentence the approver read before saying
    yes; the feed is company-scoped and shown to the same people, so scrubbing
    adds no protection and makes the audit record disagree with what was
    actually agreed to. Identifier hygiene (gotcha 38) exists to stop a record
    number reaching prose the user never asked for — this is the opposite case.
    """
    body = ""
    if isinstance(confirm, dict):
        body = str(confirm.get("details") or confirm.get("title") or "").strip()
    if not body:
        # No confirm payload means an UNATTENDED write, and there is no
        # human-readable summary to inherit. Naming the action beats an empty
        # description, which would render as a blank row.
        body = f"Occupella ran {action_type}."
    if len(body) > _MAX_DESCRIPTION:
        body = body[: _MAX_DESCRIPTION - 1].rstrip() + "…"
    if outcome == "blocked_needs_review":
        # ⚠ The wording is the point. "Occupella changed X" and "Occupella may
        # have changed X and could not confirm it" are different facts and the
        # second is the one somebody has to act on.
        where = _system_name(action_type)
        return f"{body} ⚠ The change was sent but could not be confirmed — check {where}."
    return body


async def record_write(
    *,
    company_id: str,
    action_type: str,
    confirm: dict[str, Any] | None,
    outcome: str,
    detail: str = "",
    approval_id: str | None = None,
    idem_key: str | None = None,
) -> None:
    """Write one activity row for a completed Occupella write. Never raises.

    ``approval_id`` is the ``confirm_id`` of the card the person approved, the
    same id the durable pending-confirm row carries, so the row can be joined to
    the approval. ``idem_key`` is the idempotency claim. Both are recorded for
    every PMS (Rentvine terms rule 22 asks for the approval token on writes).

    ⚠ Best-effort by construction, like `write_activity` itself: a failure here
    must cost the audit ROW, never the write that already happened. Losing the
    record of a charge is bad; raising after the charge landed and making the
    tool report a failure that did not occur is worse (gotcha 26's class).
    """
    if outcome not in RECORDED_OUTCOMES or not company_id:
        return
    try:
        from helixis.db import engine as db_engine
        from helixis.db.repositories.activity import ActivityEntry, write_activity

        pool = db_engine.get_pool()
        await write_activity(
            pool,
            ActivityEntry(
                # No Buildium AccountId on this path — the writer's own
                # docstring says chat and scheduled rows do not have one.
                account_id=None,
                company_id=company_id,
                # ⚠ `chat` vs `scheduled` is the auto-vs-manual split #361 asks
                # for, and `run_guarded` is the ONE place that knows it: a
                # `confirm` payload means the write passed a gate a person
                # answered, and its absence means nobody was there. Inferring it
                # anywhere downstream would be a guess about accountability.
                source="chat" if confirm is not None else "scheduled",
                source_label=f"{LABEL_PREFIX} · {action_type}",
                action_type=action_type,
                description=_description(action_type, confirm, outcome),
                raw={
                    # Explicit rather than re-derivable from `source`: two
                    # values cannot carry "was a human at the gate" without the
                    # reader having to know the encoding, and an audit fact a
                    # reader has to decode is one they will decode wrongly.
                    "approved_by_human": confirm is not None,
                    "outcome": outcome,
                    "detail": detail,
                    "approval_id": approval_id,
                    "idem_key": idem_key,
                },
            ),
        )
    except Exception:  # noqa: BLE001 — see the docstring
        logger.warning(
            "could not record the activity row for %s (%s)", action_type, outcome, exc_info=True
        )


__all__ = ["LABEL_PREFIX", "RECORDED_OUTCOMES", "record_write"]
