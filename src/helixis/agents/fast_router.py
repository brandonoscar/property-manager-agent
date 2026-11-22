"""Haiku fast router (upgrade 2) — a routing-only precheck, dark by default.

The route-vs-synthesize split: in reasoning-loop mode the orchestrator both
routes AND synthesizes, so a cheap model can't simply take its seat without
degrading answers. Instead the cheap model gets the one job it's actually
good at — a ~300ms single-token classification BEFORE the orchestrator:

* ``buildium`` — an unambiguous read/lookup over the company's own
  portfolio (properties, units, leases, tenants, work orders, tasks,
  vendors, bills). Run the Buildium specialist directly; skip the
  orchestrator turn entirely.
* ``analyst`` — an unambiguous quantitative/aggregate question (counts,
  sums, rankings, trends). Run the Data Analyst directly.
* ``orchestrator`` — everything else: email/calendar/drive/web/browser/
  documents, anything multi-source, any write or action, anything
  ambiguous. Full orchestrator, exactly as without the router.

Only specialists constructible WITHOUT Composio fetches are direct-run
targets, so the fast path never pays the per-toolkit tool fetch it exists
to avoid.

Design invariant — FAIL OPEN: a classifier error, timeout, unparseable
reply, or any doubt routes to ``orchestrator``. The router may only ever
save a hop; it must never change behavior when unsure. Writes stay safe
on the fast path too: specialist write tools carry their own
``confirm_or_abort`` gates at the tool layer, identical to the handoff
path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from helixis.llm.sampling import sampling_kwargs
from helixis.settings import Settings

logger = logging.getLogger(__name__)

FastRoute = Literal["buildium", "analyst", "orchestrator"]

# Single-token contract: cheap to produce, trivial to parse, and a garbled
# reply fails closed to the orchestrator.
_ROUTER_SYSTEM_PROMPT = """\
You are a router for a property-management assistant. Read the user's
message and answer with EXACTLY ONE WORD — BUILDIUM, ANALYST, or
ORCHESTRATOR. No punctuation, no explanation.

BUILDIUM — the message is ONLY a read/lookup about the company's own
portfolio records: properties, units, leases, tenants, owners, work
orders, maintenance tickets, tasks, vendors, or bills. Examples: "what
units are at Riverside?", "show open work orders", "who lives in 2B?",
"do we have a vendor named Reliable?".

ANALYST — the message is ONLY a quantitative/aggregate question over
portfolio data: counts, sums, averages, rankings, comparisons, trends,
"most/least/top" questions. Examples: "which property is most at risk?",
"total unpaid rent by property", "average lease length".

ORCHESTRATOR — everything else. That includes: anything touching email,
calendar, Drive, documents, the public web, or browsing; anything that
needs MORE THAN ONE source; ANY action or write (create, update, close,
assign, send, remind, draft, schedule); anything about Occupella itself;
greetings and conversation; and anything you are not sure about.

When in doubt, answer ORCHESTRATOR.
"""


def parse_route(raw: object) -> FastRoute:
    """Map the classifier's reply onto a route, failing open.

    Pure and total: any input that isn't recognizably one of the two
    specialist tokens routes to the orchestrator.
    """
    if not isinstance(raw, str):
        return "orchestrator"
    word = raw.strip().split()[0].upper().strip(".,!:;\"'") if raw.strip() else ""
    if word == "BUILDIUM":
        return "buildium"
    if word == "ANALYST":
        return "analyst"
    return "orchestrator"


async def route(task: str, settings: Settings) -> FastRoute:
    """Classify ``task`` with the fast-router model. Fail-open everywhere.

    Honors the analyst capability flag: when analysis is disabled on the
    deployment, an ``analyst`` classification falls back to the
    orchestrator (whose Data Analyst hand-off surfaces the "not enabled"
    message properly) instead of direct-running a toolless specialist.
    """
    if not settings.agent.fast_router:
        return "orchestrator"
    try:
        import litellm

        response = await asyncio.wait_for(
            litellm.acompletion(
                model=settings.agent.fast_router_model,
                api_key=settings.llm.anthropic_api_key or None,
                messages=[
                    {"role": "system", "content": _ROUTER_SYSTEM_PROMPT},
                    {"role": "user", "content": (task or "")[:2000]},
                ],
                max_tokens=5,
                **sampling_kwargs(settings.agent.fast_router_model, 0),
            ),
            timeout=settings.agent.fast_router_timeout_s,
        )
        raw = response.choices[0].message.content  # type: ignore[union-attr]
        decision = parse_route(raw)
    except (Exception, SystemExit):  # noqa: BLE001 — fail open, never break the turn
        logger.warning("fast_router classification failed — falling open", exc_info=True)
        return "orchestrator"

    if decision == "analyst" and not settings.analysis.enabled:
        return "orchestrator"
    logger.info("fast_router decision=%s", decision)
    return decision


__all__ = ["FastRoute", "parse_route", "route"]
