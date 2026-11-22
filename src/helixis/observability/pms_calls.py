"""One log line per property-management-system API call.

Rentvine's terms (``design/rentvine-terms-rules.md`` rule 22) ask for every call
Occupella makes to be logged: the customer, the endpoint, the action, the
approval token for writes, the result and the time, and never secrets or
payloads. The founder chose (2026-09-24) to log Buildium the same way, so both
clients call :func:`log_pms_call` once per attempt.

The line is ``pms_call`` followed by ``key=value`` pairs, one attempt each:

    pms_call pms=rentvine company=<uuid> account=<code> method=GET
      endpoint=/properties/{propertyID}/units status=200 outcome=ok attempt=1
      duration_ms=184 approval=-

⚠ What it never carries: the request or response body, query values, headers,
the key or the secret. ``endpoint`` is the path TEMPLATE, not the built path,
so record ids stay out too; a caller that only has a built path passes it
through :func:`template_of`.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("helixis.pms_calls")

#: A path segment that is a record id: all digits.
_ID_SEGMENT = re.compile(r"/\d+(?=/|$)")


def template_of(path: str) -> str:
    """A built path with its query dropped and every numeric id made ``{id}``."""
    return _ID_SEGMENT.sub("/{id}", path.split("?", 1)[0])


def current_company(explicit: str | None) -> str:
    """The company a call is for: the caller's, else the request's, else ``-``."""
    if explicit:
        return explicit
    try:
        from helixis.memory.context import current_memory_context

        mem = current_memory_context()
        return mem.company_id if mem and mem.company_id else "-"
    except Exception:  # noqa: BLE001 — a log line must never break the call
        return "-"


def current_approval() -> str:
    """The id of the confirm card approved in this task, or ``-``."""
    try:
        from helixis.agents.tools import current_confirm_id

        return current_confirm_id.get() or "-"
    except Exception:  # noqa: BLE001
        return "-"


def log_pms_call(
    *,
    pms: str,
    company_id: str | None,
    account: str | None,
    method: str,
    endpoint: str,
    status: int | None,
    outcome: str,
    attempt: int,
    duration_ms: float,
) -> None:
    """Emit the one line. ``approval`` is filled for writes only."""
    write = method.upper() not in {"GET", "HEAD", "OPTIONS"}
    logger.info(
        "pms_call pms=%s company=%s account=%s method=%s endpoint=%s status=%s "
        "outcome=%s attempt=%d duration_ms=%d approval=%s",
        pms,
        current_company(company_id),
        account or "-",
        method.upper(),
        endpoint,
        status if status is not None else "-",
        outcome,
        attempt,
        round(duration_ms),
        current_approval() if write else "-",
    )


__all__ = ["current_approval", "current_company", "log_pms_call", "template_of"]
