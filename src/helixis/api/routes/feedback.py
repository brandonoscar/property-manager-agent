"""In-app feedback (BR #19) — a durable row, a Sentry notice, a PostHog event.

The form is deliberately tiny (category + message + the view it came from).
The DB row is the record; the Sentry notice and the PostHog capture are both
best-effort and neither can fail the submission.

**What changed on 2026-09-08.** Nothing read the row. `helixis_feedback` was
written by this route and by nothing else — no admin endpoint, no UI, no job —
so a person who described a defect was told "we reply by the next business
day" and reached a table that was never queried. The notice puts the ARRIVAL
in the stream the operator already reads. Reading the complaint still means
opening the row, and that gap is deliberate: the message is the customer's own
words and does not go to third-party telemetry.

**What changed on 2026-09-05, and why.** The row carried no way to find the
turn the person was complaining about. When chat fails the SSE response is
already a 200, so the user reads "something went wrong" and the traceback
lives only in Render's logs — gotcha 28 is this product taking chat out for
every substantive turn while 4308 tests stayed green. A report that says "it
broke" and nothing else costs a round trip to the customer before diagnosis
can even start.

So a submission now carries the chat thread, the frontend build and the
browser, and the server resolves the `orchestrator_runs` row itself.

⚠ **`run_id` is resolved HERE, never accepted from the client.** Two reasons,
and the second is the load-bearing one:
  * a client-supplied id is a client-supplied claim about which run to
    attribute a complaint to, scoped to nothing;
  * the report is most valuable exactly when the turn FAILED, and a failed
    turn is the least likely to have handed the browser a run id — the
    disclosure envelope that carries one is emitted only in loop mode and
    only when there is a final output. Asking the client for it would make
    the field reliably absent in the case it exists for.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from helixis.analytics import capture as ph_capture
from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.db.engine import get_pool
from helixis.observability.sentry_setup import capture_notice

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feedback", tags=["feedback"])

CATEGORIES = ("bug", "idea", "other")


class FeedbackIn(BaseModel):
    category: str = "other"
    message: str = Field(min_length=3, max_length=5000)
    view: str | None = Field(default=None, max_length=40)
    session_id: str | None = Field(default=None, max_length=120)
    """The chat thread that was open. What the client reliably has."""
    app_version: str | None = Field(default=None, max_length=60)
    """Frontend build. Without it, "already fixed" and "still broken" are
    indistinguishable in the report."""
    user_agent: str | None = Field(default=None, max_length=400)
    """⚠ Bounded, because it is client-supplied free text landing in a row an
    operator will read. 400 is generous for a real UA string and small enough
    that nobody can push a wall of text into the triage view."""


class FeedbackOut(BaseModel):
    ok: bool


async def _run_for_session(conn: object, company_id: str, session_id: str | None) -> str | None:
    """The most recent run on this thread, or `None`.

    ⚠ Scoped to `company_id` (gotcha 47). `session_id` arrives from the client,
    so an unscoped lookup would happily attach another tenant's run to this
    company's bug report — and the row is exactly what an operator would then
    go and read.

    ⚠ Best-effort by design. A report whose run cannot be resolved is still a
    report, and losing the message a person wrote because a diagnostic join
    failed would invert the value of the two.
    """
    if not session_id:
        return None
    try:
        row = await conn.fetchrow(  # type: ignore[attr-defined]
            """
            SELECT id
              FROM public.orchestrator_runs
             WHERE session_id = $1 AND company_id = $2::uuid
             ORDER BY started_at DESC
             LIMIT 1
            """,
            session_id,
            company_id,
        )
    except Exception:  # noqa: BLE001 — diagnostics never break the submission
        logger.warning("feedback: could not resolve a run for the session", exc_info=True)
        return None
    return str(row["id"]) if row else None


@router.post("", response_model=FeedbackOut)
async def submit_feedback(
    payload: FeedbackIn,
    user: AuthenticatedUser = Depends(get_current_user),
) -> FeedbackOut:
    if payload.category not in CATEGORIES:
        raise HTTPException(status_code=400, detail="Category must be bug, idea, or other.")
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="Say a little about what happened.")
    if not user.company_ids:
        raise HTTPException(status_code=400, detail="User is not a member of any company")
    company_id = user.company_ids[0]

    pool = get_pool()
    async with pool.acquire() as conn:
        run_id = await _run_for_session(conn, company_id, payload.session_id)
        row = await conn.fetchrow(
            """
            INSERT INTO helixis_feedback
                (company_id, user_id, category, message, view,
                 session_id, run_id, app_version, user_agent)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5,
                    $6, $7::uuid, $8, $9)
            RETURNING id
            """,
            company_id,
            user.user_id,
            payload.category,
            payload.message.strip(),
            payload.view,
            payload.session_id,
            run_id,
            payload.app_version,
            payload.user_agent,
        )
    feedback_id = str(row["id"]) if row else ""

    # ⚠ The report used to end here. `helixis_feedback` is written by this
    # route and READ BY NOTHING — no admin endpoint, no UI, no job — so a
    # customer who took the trouble to describe a defect was told "we reply by
    # the next business day" and reached a table nobody queries.
    #
    # Sentry is the stream this product's operator already reads daily, so the
    # arrival goes there. A `bug` is a WARNING and everything else is INFO: a
    # feature request filed at the same level as a 500 erodes the one signal
    # that has to stay loud (gotcha 35's reasoning about spending `safety` on
    # an outage, one surface over).
    #
    # ⚠ What goes is the POINTER, not the message. Same line the PostHog
    # capture below draws — a customer's own words about their portfolio do
    # not belong in third-party telemetry. The operator reads the row.
    try:
        capture_notice(
            f"Feedback ({payload.category}) from a customer — read helixis_feedback {feedback_id}",
            op="feedback.submitted",
            level="warning" if payload.category == "bug" else "info",
            tags={
                "feedback_category": payload.category,
                "feedback_view": payload.view or "unknown",
                "company_id": company_id,
                "run_id": run_id or "unresolved",
                "app_version": payload.app_version or "unknown",
            },
        )
    except Exception:  # noqa: BLE001 — a notice must never cost the report
        logger.warning("feedback Sentry notice failed", exc_info=True)

    try:
        ph_capture(
            user.user_id,
            "feedback submitted",
            {
                "category": payload.category,
                "view": payload.view or "",
                # ⚠ The run id, NOT the message. The row is the record; PostHog
                # is third-party telemetry and a user's own words about their
                # portfolio do not belong in it — the same line `codeexec/audit`
                # draws when it sends a code sha256 and never the source.
                "run_id": run_id or "",
                "app_version": payload.app_version or "",
            },
            company_id=company_id,
        )
    except Exception:  # noqa: BLE001 — analytics is never load-bearing
        logger.warning("feedback PostHog capture failed", exc_info=True)
    return FeedbackOut(ok=True)


__all__ = ["router"]
