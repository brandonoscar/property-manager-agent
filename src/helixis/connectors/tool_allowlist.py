"""Which Composio tools a specialist actually needs.

WHY THIS EXISTS
---------------
``composio_client.get_tools`` fetched whole toolkits with no filter
(``tools.get(user_id=..., toolkits=slugs, limit=200)``) and ``drive_agent``
did a bare ``tools.extend(drive_tools)``. From the committed slug dump that is
**186 tool schemas** on the Drive specialist — GOOGLEDRIVE 90 + GOOGLEDOCS 43 +
GOOGLESHEETS 53 — re-sent on every one of its model calls.

Solved three independent ways from the production ledger (2026-08-26), that
block is **166,000-215,000 tokens**. For scale, the ENTIRE 74-tool Buildium set
is 32,953 tokens (measured) and the orchestrator is 11,195 (measured). Drive
was 5-7x the next largest thing in the product, and on one turn its prefix cost
**$0.72 — 59% of that turn** — so the specialist could reply that Sheets was
not connected.

Two consequences, and the second is worse than the money:

* **Cost.** The Anthropic cache breakpoint sits on ``tools[-1]``, so the tool
  array IS the cached prefix. A 180k-token array is a 180k-token cache write.
* **Capability.** At ~180k the specialist is near the context ceiling *before
  it reads a single document*. That is a mechanical explanation for gotcha 42's
  "``drive_agent`` was consulted exactly once and returned ``Max turns (8)
  exceeded``" — and it means raising that ceiling 8→16 multiplied the exposure
  instead of relieving it.

WHY AN EXPLICIT LIST AND NOT A PATTERN
--------------------------------------
⚠ The obvious implementation is a read-verb regex. It does not work, and the
failure is silent in the dangerous direction. Run against the real dump, a
``(LIST|FIND|GET|SEARCH)`` pattern admits ``GMAIL_DELETE_THREAD``,
``GOOGLESHEETS_CREATE_SPREADSHEET_ROW`` and
``GOOGLECALENDAR_CALENDAR_LIST_DELETE`` — destructive operations wearing a
read's prefix. That is the same shape as the write-gate bug in #915, where
``SET`` matched inside ``SETTINGS``: a pattern over slug names cannot tell a
verb from a noun, and here a false positive hands a specialist a delete.

So every entry is named, and every name is checked against the live dump by
``tests/unit/test_composio_allowlist.py`` — a typo would otherwise narrow a
specialist's abilities silently, which is exactly the failure this module is
meant to prevent.

⚠ FAILS OPEN, DELIBERATELY. If filtering a toolkit would leave a specialist
with NOTHING, ``filter_tools`` returns the unfiltered list and logs loudly.
A cost optimisation must never be able to silently remove a product's ability
to work: gotcha 42's Drive answer was wrong *because* the specialist could not
reach its source, and this module exists to fix that, not to reproduce it from
the other direction.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Per-toolkit allowlists. Derived from (a) the slugs this codebase actually
#: references, (b) the live ``helixis_pending_confirmations`` rows proving
#: which Drive calls production really makes, and (c) the workflow each
#: specialist's own prompt describes: find it, open it, read it.
#:
#: Writes ARE included where the product has a real feature behind them — they
#: stay confirm-gated by ``tool_gate``. Removing them here would delete the
#: feature, not secure it.
TOOLKIT_ALLOWLIST: dict[str, frozenset[str]] = {
    # ── Drive: find a document, open it, and tell a current copy from a
    # superseded one. The battery's hardest questions turn on exactly that
    # last part (an SOP v3.1 that supersedes a v2.0 someone still cites).
    "GOOGLEDRIVE": frozenset(
        {
            "GOOGLEDRIVE_FIND_FILE",
            "GOOGLEDRIVE_FIND_FOLDER",
            "GOOGLEDRIVE_LIST_FILES",
            "GOOGLEDRIVE_LIST_SHARED_DRIVES",
            "GOOGLEDRIVE_GET_FILE_METADATA",
            # Live-proven: 11 and 9 real calls respectively in the 2026-08-25/26
            # confirmations table. These two ARE how you read a Google Doc.
            "GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE",
            "GOOGLEDRIVE_PARSE_FILE",
            "GOOGLEDRIVE_DOWNLOAD_FILE",
            # Supersession: "is this the current version?" is unanswerable
            # without it, and answering it wrongly is a confident false claim.
            "GOOGLEDRIVE_LIST_REVISIONS",
        }
    ),
    # ── Docs: the SOP corpus. PLAINTEXT is the one that actually returns prose.
    "GOOGLEDOCS": frozenset(
        {
            "GOOGLEDOCS_SEARCH_DOCUMENTS",
            "GOOGLEDOCS_GET_DOCUMENT_BY_ID",
            "GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT",
        }
    ),
    # ── Sheets: the vendor/COI trackers. Read-only — the specialist is
    # LOOKUP-only by contract (see the routing prose in orchestrator.py), so
    # no Sheets write belongs here at all.
    "GOOGLESHEETS": frozenset(
        {
            "GOOGLESHEETS_SEARCH_SPREADSHEETS",
            "GOOGLESHEETS_GET_SPREADSHEET_INFO",
            "GOOGLESHEETS_GET_SHEET_NAMES",
            "GOOGLESHEETS_BATCH_GET",
            "GOOGLESHEETS_VALUES_GET",
            "GOOGLESHEETS_LOOKUP_SPREADSHEET_ROW",
        }
    ),
    # ── Gmail: read the thread, and the three writes the product ships
    # (all confirm-gated, all with edit-before-approve).
    "GMAIL": frozenset(
        {
            "GMAIL_FETCH_EMAILS",
            "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
            "GMAIL_FETCH_MESSAGE_BY_THREAD_ID",
            "GMAIL_LIST_THREADS",
            "GMAIL_LIST_MESSAGES",
            "GMAIL_GET_ATTACHMENT",
            "GMAIL_SEND_EMAIL",
            "GMAIL_REPLY_TO_THREAD",
            "GMAIL_CREATE_EMAIL_DRAFT",
        }
    ),
    "GOOGLECALENDAR": frozenset(
        {
            "GOOGLECALENDAR_EVENTS_LIST",
            "GOOGLECALENDAR_FIND_EVENT",
            "GOOGLECALENDAR_LIST_CALENDARS",
            "GOOGLECALENDAR_GET_CURRENT_DATE_TIME",
            "GOOGLECALENDAR_FIND_FREE_SLOTS",
            "GOOGLECALENDAR_FREE_BUSY_QUERY",
            "GOOGLECALENDAR_CREATE_EVENT",
            "GOOGLECALENDAR_UPDATE_EVENT",
            "GOOGLECALENDAR_DELETE_EVENT",
        }
    ),
    # ── Outlook has 305 slugs against a fetch limit of 200, so before this it
    # received an ARBITRARY 200 of 305 — a non-deterministic capability set,
    # not merely an expensive one. An allowlist makes it deterministic.
    #
    # ⚠ The slugs are ``OUTLOOK_SEND_EMAIL`` / ``OUTLOOK_REPLY_EMAIL``, NOT the
    # doubled ``OUTLOOK_OUTLOOK_*`` form. ``tool_gate._EDITABLE_SLUGS`` carried
    # the doubled spelling, so Outlook sends were never editable at the confirm
    # gate — a name that matches nothing fails silently. Found by this module's
    # own existence check; fixed in tool_gate at the same time.
    "OUTLOOK": frozenset(
        {
            "OUTLOOK_LIST_MESSAGES",
            "OUTLOOK_GET_MESSAGE",
            "OUTLOOK_SEARCH_MESSAGES",
            "OUTLOOK_SEND_EMAIL",
            "OUTLOOK_REPLY_EMAIL",
            "OUTLOOK_LIST_EVENTS",
        }
    ),
}


def _toolkit_of(slug: str) -> str | None:
    """Which allowlist governs this slug, by longest matching prefix.

    ⚠ LONGEST first. ``GOOGLEDOCS_...`` and ``GOOGLEDRIVE_...`` share no
    prefix, but a future ``GOOGLE_...`` toolkit would, and a shortest-match
    rule would quietly route it to the wrong list.
    """
    upper = slug.upper()
    best: str | None = None
    for toolkit in TOOLKIT_ALLOWLIST:
        if upper.startswith(toolkit + "_") and (best is None or len(toolkit) > len(best)):
            best = toolkit
    return best


def filter_tools(tools: list[Any]) -> list[Any]:
    """Keep only allowlisted tools, in a STABLE order.

    Two jobs, and the second is easy to miss:

    1. **Shrink the array.** It is the cached prefix; cutting it cuts the
       cache-write bill and the context pressure together.
    2. **Sort it.** Composio's ``tools.get`` returns whatever order the API
       gives (``list(tools or [])``, no sort) behind a 300s fetch cache. An
       unstable order is a DIFFERENT prefix, so the cache would miss on a
       payload that is otherwise byte-identical — and the 1h TTL from #913
       could never be collected for exactly the agents where it is worth most.
       Sorting makes that moot whether or not Composio's order is stable, which
       is better than depending on an answer nobody has measured.

    A slug from a toolkit with NO allowlist entry is kept: this filters what it
    knows about and never silently drops a toolkit somebody adds later.
    """
    if not tools:
        return tools

    kept: list[Any] = []
    dropped_by_toolkit: dict[str, int] = {}
    for tool in tools:
        slug = str(getattr(tool, "name", "") or "")
        toolkit = _toolkit_of(slug)
        allowed = TOOLKIT_ALLOWLIST.get(toolkit or "")
        if allowed is None or slug.upper() in allowed:
            kept.append(tool)
        else:
            dropped_by_toolkit[toolkit or "?"] = dropped_by_toolkit.get(toolkit or "?", 0) + 1

    # ⚠ FAIL OPEN. Filtering everything away would leave a specialist unable to
    # do its job while looking perfectly healthy — a silent capability outage,
    # which is strictly worse than the cost problem this module exists to fix.
    if not kept:
        logger.error(
            "composio allowlist matched NOTHING against %d tools (%s) — falling back to the "
            "unfiltered set. The slugs have almost certainly been renamed; the specialist "
            "keeps working but the payload is back to full size.",
            len(tools),
            sorted({str(getattr(t, "name", "?"))[:40] for t in tools})[:8],
        )
        return sorted(tools, key=lambda t: str(getattr(t, "name", "")))

    if dropped_by_toolkit:
        logger.info(
            "composio allowlist: kept %d of %d tools (dropped %s)",
            len(kept),
            len(tools),
            dropped_by_toolkit,
        )
    return sorted(kept, key=lambda t: str(getattr(t, "name", "")))


__all__ = ["TOOLKIT_ALLOWLIST", "filter_tools"]
