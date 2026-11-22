"""Visual-answer tools — let the agent render structured visuals instead of
prose.

Today the orchestrator answers only in text. ``show_table`` gives it a way to
say "this is a list — draw it as a table" mid-turn: the model calls it with a
title, column headers, and rows; we convert that to the frontend's
``TabularData`` shape and surface it as a ``tabular_data`` SSE event.

Mechanism mirrors the confirmation gate (``agents.tools``): the tool pushes its
payload onto its RUN's queue (:func:`bind_run_emissions`) and returns a short
confirmation string to the model; the orchestrator's stream loop drains the
queue after each step and publishes the events (``run_agent_streaming`` →
``drain_pending_visuals``).
Keeping the emit in the orchestrator means the tool needs no bus/session
handle, and ``build_table_payload`` stays a pure, unit-testable function.

The frontend already renders ``tabular_data`` (ConversationThread's
``tabular_data`` case → ``TabularContent``); this is only the backend half that
was missing.
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from agents import function_tool

from helixis.agents.table_policy import REFUSAL as TABLE_REFUSAL
from helixis.agents.table_policy import tables_are_surplus

logger = logging.getLogger(__name__)

# Guardrails so a runaway model can't emit an enormous table.
_MAX_COLUMNS = 12
_MAX_ROWS = 100

# Shared prompt snippet that teaches a specialist to reach for ``show_table``.
# Appended to a specialist's instructions wherever it carries the ``show_table``
# tool, so the behaviour matches the orchestrator's own "Answering with a table"
# guidance. Kept here (next to the tool) so the tool and the instruction to use
# it never drift apart.
TABLE_TOOL_HINT = """

## Answering with a table

You have ``show_table``. When your answer is a set of items that share the same
fields — a tenant roster, leases, work orders, overdue balances, emails,
calendar events, files, or search results — call ``show_table`` with short
column headers and one row per item INSTEAD of writing a long bulleted list. A
table is far easier to scan than prose.

- Keep cell values short; format currency and dates as text yourself (no raw
  ISO timestamps, no internal IDs).
- Use prose for a single item, a count, or a short explanation — a table is for
  a list of three or more comparable rows.
- Still write a one-line lead-in before the table when it helps
  ("Here are the 6 leases expiring this quarter:"), then call ``show_table``.
"""


@dataclass
class RunEmissions:
    """One run's visuals, waiting for that run's stream loop to publish them.

    ⚠ PER RUN, never per process. These were five module-level queues, and
    every run drained ALL of them after each stream event and published what
    it found under its OWN session id. One worker serves every company, so a
    table, a signed download link or an email draft queued by company A's run
    went to whichever run streamed next — company B's chat — while A's model
    was told it had been shown (audit 2026-09-29). Bound per run through
    :data:`_run_emissions`, exactly as the confirm gate binds its emitter: the
    SDK's tool tasks, and a specialist's nested run, inherit the binding and
    push into THIS run's queues; another run cannot see them.
    """

    # ``tabular_data`` SSE events. ``show_table`` pushes here.
    visuals: asyncio.Queue[dict] = field(default_factory=asyncio.Queue)
    # ``map`` SSE events. The location tools (``properties_near`` /
    # ``find_places_near_property``) push here when they produce geocoded
    # points, so a static map renders inline WITHOUT the model relaying a URL.
    maps: asyncio.Queue[dict] = field(default_factory=asyncio.Queue)
    # ``artifacts`` SSE events. ``run_computation`` pushes here when a sandbox
    # run produces downloadable files with signed URLs, so download buttons
    # render inline WITHOUT the model relaying the link.
    artifacts: asyncio.Queue[dict] = field(default_factory=asyncio.Queue)
    # ``email_draft`` SSE events. ``show_email_draft`` pushes here. The card
    # renders the model's OWN composed draft (its to/subject/body, written
    # during the turn) — the tool is a renderer for reasoned content, never a
    # canned template (founder direction, 2026-08-19).
    email_drafts: asyncio.Queue[dict] = field(default_factory=asyncio.Queue)
    # The shared card pipeline, ``(event_type, payload)`` pairs (see
    # ``_queue_card`` below).
    cards: asyncio.Queue[tuple[str, dict]] = field(default_factory=asyncio.Queue)


_run_emissions: ContextVar[RunEmissions | None] = ContextVar("helixis_run_emissions", default=None)

# Where a caller that binds no run pushes and drains: unit tests calling a tool
# body directly, and the non-streaming ``Runner.run`` callers. Nothing that
# serves a chat drains it — ``run_agent_streaming`` always binds its own.
_UNBOUND = RunEmissions()


def bind_run_emissions() -> Token[RunEmissions | None]:
    """Give the current run its own queues. Call BEFORE the SDK starts the run
    so the tasks it creates inherit them; pass the token to
    :func:`reset_run_emissions` when the run ends."""
    return _run_emissions.set(RunEmissions())


def reset_run_emissions(token: Token[RunEmissions | None]) -> None:
    """Unbind what :func:`bind_run_emissions` bound."""
    _run_emissions.reset(token)


def _emissions() -> RunEmissions:
    return _run_emissions.get() or _UNBOUND


def build_table_payload(title: str, columns: list[str], rows: list[list[str]]) -> dict:
    """Convert the model's simple (title, columns, rows-of-strings) into the
    frontend ``TabularData`` shape — every cell a plain ``text`` cell. Pure +
    deterministic so it's unit-testable. Caps columns/rows defensively; a row
    with fewer cells than columns renders the missing cells as an em-dash, and
    extra cells are ignored by the renderer."""
    cols = [str(c) for c in (columns or [])][:_MAX_COLUMNS]
    col_keys = [f"c{i}" for i in range(len(cols))]
    out_rows: list[dict] = []
    for row in (rows or [])[:_MAX_ROWS]:
        cells = [str(v) for v in (row or [])][: len(cols)]
        out_rows.append(
            {col_keys[i]: {"kind": "text", "value": cells[i]} for i in range(len(cells))}
        )
    return {
        "kind": "agent_table",
        "title": (title or "").strip() or None,
        "columns": [{"key": col_keys[i], "label": cols[i]} for i in range(len(cols))],
        "rows": out_rows,
    }


async def drain_pending_visuals() -> list[dict]:
    """Non-blocking drain of pending visual emissions — the orchestrator calls
    this after each stream event and publishes each as a ``tabular_data`` SSE
    event. Never blocks; returns ``[]`` when empty. Mirrors
    ``agents.tools.drain_pending_confirmations``."""
    out: list[dict] = []
    queue = _emissions().visuals
    while not queue.empty():
        try:
            out.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


def build_map_payload(
    markers: list[tuple[float, float]],
    *,
    caption: str | None = None,
    size: str = "600x400",
) -> dict | None:
    """Convert geocoded points into the frontend ``MapData`` shape
    (``{url, caption?, markers}``). ``url`` is our key-free static-map proxy
    path; ``None`` when there are no markers or the proxy declines (no key is a
    provider concern, not this function's — it always builds the path). Pure +
    deterministic so it's unit-testable."""
    from helixis.geo.places import static_map_proxy_url

    url = static_map_proxy_url(markers, size=size)
    if url is None:
        return None
    return {
        "url": url,
        "caption": (caption or "").strip() or None,
        "markers": [{"lat": lat, "lng": lng} for lat, lng in markers],
    }


def queue_map(
    markers: list[tuple[float, float]],
    *,
    caption: str | None = None,
) -> bool:
    """Queue a static map for the current turn — the location tools call this
    with the points they already computed so a map renders inline. Best-effort:
    returns ``False`` (never raises) when there are no markers or the queue is
    full, so a map is a bonus that can never break the tool's text answer."""
    payload = build_map_payload(markers, caption=caption)
    if payload is None:
        return False
    try:
        _emissions().maps.put_nowait(payload)
    except asyncio.QueueFull:  # pragma: no cover — unbounded queue, defensive
        logger.warning("map-emission queue full; dropping map")
        return False
    return True


async def drain_pending_maps() -> list[dict]:
    """Non-blocking drain of pending map emissions — the orchestrator calls this
    after each stream event and publishes each as a ``map`` SSE event. Mirrors
    :func:`drain_pending_visuals`."""
    out: list[dict] = []
    queue = _emissions().maps
    while not queue.empty():
        try:
            out.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


def queue_artifacts(links: list[dict]) -> bool:
    """Queue downloadable artifacts for the current turn — ``run_computation``
    calls this with the ``[{name, size, url}]`` links it persisted. Only
    entries carrying a non-empty ``url`` are surfaced (a storage failure left
    others at ``url=None``). Best-effort: returns ``False`` (never raises) when
    there's nothing to show or the queue is full, so a download card is a bonus
    that can never break the run's text answer."""
    usable = [
        {"name": str(x.get("name") or "file"), "size": x.get("size"), "url": x["url"]}
        for x in (links or [])
        if isinstance(x, dict) and x.get("url")
    ]
    if not usable:
        return False
    try:
        _emissions().artifacts.put_nowait({"artifacts": usable})
    except asyncio.QueueFull:  # pragma: no cover — unbounded queue, defensive
        logger.warning("artifact-emission queue full; dropping artifacts")
        return False
    return True


async def drain_pending_artifacts() -> list[dict]:
    """Non-blocking drain of pending artifact emissions — the orchestrator calls
    this after each stream event and publishes each as an ``artifacts`` SSE
    event. Mirrors :func:`drain_pending_maps`."""
    out: list[dict] = []
    queue = _emissions().artifacts
    while not queue.empty():
        try:
            out.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


# Defensive caps for the email-draft card. Generous — a property-management
# email is short — but a runaway model can't flood the SSE stream.
_MAX_EMAIL_BODY = 8_000
_MAX_EMAIL_SUBJECT = 300
_MAX_EMAIL_RECIPIENTS = 20
_MAX_EMAIL_VARIANTS = 4
_MAX_VARIANT_LABEL = 60


def build_email_draft_payload(
    to: list[str],
    variants: list[dict],
    cc: list[str] | None = None,
) -> dict:
    """Convert the model's composed draft(s) into the frontend
    ``EmailDraftData`` shape: ``{to, cc, variants: [{label, subject, body}]}``.
    Pure + deterministic so it's unit-testable.

    Each variant is a COMPLETE alternative the model composed — its own label
    (the model's name for the approach, e.g. "Friendly reminder"), subject,
    and body, passed through verbatim modulo the defensive caps. This function
    frames content, it never writes it. Variants with an empty body are
    dropped; at most ``_MAX_EMAIL_VARIANTS`` survive — the frontend renders
    them as slide-through tabs, Inbox-stepper style (founder direction,
    2026-08-19)."""
    out_variants: list[dict] = []
    for v in variants or []:
        if not isinstance(v, dict):
            continue
        body = str(v.get("body") or "").strip()[:_MAX_EMAIL_BODY]
        if not body:
            continue
        out_variants.append(
            {
                "label": str(v.get("label") or "").strip()[:_MAX_VARIANT_LABEL],
                "subject": str(v.get("subject") or "").strip()[:_MAX_EMAIL_SUBJECT],
                "body": body,
            }
        )
        if len(out_variants) == _MAX_EMAIL_VARIANTS:
            break
    return {
        "to": [str(a).strip() for a in (to or []) if str(a).strip()][:_MAX_EMAIL_RECIPIENTS],
        "cc": [str(a).strip() for a in (cc or []) if str(a).strip()][:_MAX_EMAIL_RECIPIENTS],
        "variants": out_variants,
    }


async def drain_pending_email_drafts() -> list[dict]:
    """Non-blocking drain of pending email-draft emissions — the orchestrator
    calls this after each stream event and publishes each as an ``email_draft``
    SSE event. Mirrors :func:`drain_pending_maps`."""
    out: list[dict] = []
    queue = _emissions().email_drafts
    while not queue.empty():
        try:
            out.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


async def _show_email_draft_impl(
    to: list[str],
    labels: list[str],
    subjects: list[str],
    bodies: list[str],
    cc: list[str] | None,
) -> str:
    variants = [
        {
            "label": labels[i] if i < len(labels) else "",
            "subject": subjects[i] if i < len(subjects) else "",
            "body": bodies[i] if i < len(bodies) else "",
        }
        for i in range(len(bodies))
    ]
    payload = build_email_draft_payload(to, variants, cc)
    if not payload["variants"]:
        return "Nothing to show — compose the draft(s) first, then call show_email_draft."
    try:
        _emissions().email_drafts.put_nowait(payload)
    except asyncio.QueueFull:  # pragma: no cover — unbounded queue, defensive
        logger.warning("email-draft emission queue full; dropping draft card")
        return "Could not render the draft card; include the draft in your reply instead."
    n = len(payload["variants"])
    return (
        f"{'Draft' if n == 1 else f'{n} draft options'} shown to the user as an "
        "email card. Do NOT repeat the draft text in your reply, and do NOT "
        "call show_email_draft again this turn. If the user already asked you "
        "to send or reply, proceed to the send/reply tool NOW — its approval "
        "card is where they review and edit. Otherwise ask what they'd like "
        "to change."
    )


@function_tool
async def show_email_draft(
    to: list[str],
    labels: list[str],
    subjects: list[str],
    bodies: list[str],
    cc: list[str] | None = None,
) -> str:
    """Show the email draft(s) YOU composed as a real email card instead of
    pasting them as chat prose. The card renders To / Subject / body; when you
    supply several alternatives the user slides through them as numbered
    options. Nothing is sent: sending still goes through the send tool and its
    confirmation gate.

    The three lists align by index: labels[i] + subjects[i] + bodies[i] is one
    complete alternative you composed. Offer 2-4 alternatives when there are
    meaningfully different approaches (tone, firmness, what you offer the
    recipient); one is fine when there is only one sensible way to write it.

    Args:
        to: Recipient email addresses (or names when the address is unknown).
        labels: YOUR short name for each approach (e.g. "Friendly reminder",
                "Firm + pay link", "Offer a payment plan"). Same length as
                ``bodies``.
        subjects: The subject line you composed for each alternative. Same
                  length as ``bodies``.
        bodies: The full email body you composed for each alternative, exactly
                as it would be sent. 1 to 4 entries.
        cc: Optional CC addresses (shared by all alternatives).

    Returns:
        A short confirmation (the card itself is shown to the user directly).
    """
    return await _show_email_draft_impl(to, labels, subjects, bodies, cc)


# Shared prompt snippet that teaches a specialist to reach for
# ``show_email_draft``. Kept next to the tool, like TABLE_TOOL_HINT, so the
# tool and the instruction to use it never drift apart.
EMAIL_DRAFT_HINT = """

## Presenting an email draft

You have ``show_email_draft``. Use it when the user is DRAFTING — "draft a
reply", "what should I say to her", "write something I can look at" — or when
you have 2-4 genuinely different approaches to offer. The user sees a real
email card (To / Subject / body) and slides through your alternatives as
numbered options.

**When the user asked you to SEND or REPLY, do NOT stop at a draft card.**
Call the send/reply tool (``GMAIL_SEND_EMAIL`` / ``GMAIL_REPLY_TO_THREAD``)
directly — the approval card it raises shows your full draft and lets the
user EDIT it before anything sends, so that card IS the review step. Never
show a draft card and then the approval card for the same message: that is
asking twice.

- Offer 2-4 alternatives when there are genuinely different ways to handle it
  — a different tone, firmness, or offer — each with YOUR label for the
  approach ("Friendly reminder", "Firm + pay link"). One draft is fine when
  only one approach makes sense. Never pad with filler variants.
- Every draft must be your own composed text for THIS situation — never a
  boilerplate shell.
- After showing the card, keep your reply to one short line ("Two ways to
  play this — which do you prefer?").
- Call ``show_email_draft`` at most ONCE per turn.
- Showing drafts sends nothing; sending still requires the send tool and the
  user's explicit approval at the confirmation gate.
"""


@function_tool
async def show_table(title: str, columns: list[str], rows: list[list[str]]) -> str:
    """Render a table in the chat for the user, instead of writing a long text
    list. Use this whenever the answer is a set of items with shared fields —
    e.g. tenants behind on rent, leases expiring, open work orders, or leads —
    because a table is far easier to scan than prose.

    Args:
        title: A short heading for the table (e.g. "Tenants behind on rent").
        columns: The column headers, left to right (e.g. ["Tenant", "Unit",
                 "Balance", "Days late"]).
        rows: The rows, each a list of cell strings aligned to ``columns``.
              Keep values short; format currency/dates yourself as text.

    Returns:
        A short confirmation (the table itself is shown to the user directly).
    """
    return await _show_table_impl(title, columns, rows)


async def _show_table_impl(title: str, columns: list[str], rows: list[list[str]]) -> str:
    """Backs ``show_table`` — module-level so tests can exercise it without the
    ``@function_tool`` wrapper (mirrors the ``*_impl`` pattern in
    ``agents.tools``)."""
    if not columns:
        return "No table shown: a table needs at least one column."
    # A judgment question cannot be answered by rows, so a table beside the
    # verdict is surplus that buries it. Structural, because the docstring
    # already asked and was read past (see agents/table_policy).
    if tables_are_surplus():
        logger.info("table suppressed: question is judgment-shaped, not a list")
        return TABLE_REFUSAL
    payload = build_table_payload(title, columns, rows)
    try:
        _emissions().visuals.put_nowait(payload)
    except asyncio.QueueFull:  # pragma: no cover — unbounded queue, defensive
        logger.warning("visual-emission queue full; dropping table")
        return "Could not display the table right now."
    n = len(payload["rows"])
    # ⚠ THE ACK IS NOT THE ONLY CONSUMER. ``orchestrator.py`` does
    # ``gathered.add(tool_name, str(output))``, so this string is what
    # ``deadline_synthesis`` gets to write an answer from. A run that rendered
    # a table and then hit the wall clock salvaged "Displayed a table titled
    # 'Rental owners' with 8 row(s)" and nothing else — the data was on screen
    # and absent from the rescue.
    #
    # The ANSWERING model does not need this echo: it wrote the rows as the
    # function call's arguments and the SDK re-feeds that item verbatim. This
    # is for the salvage path, which only ever sees the output string. Capped,
    # because a 200-row table would otherwise crowd out the material that
    # actually needs synthesising.
    return (
        f"Displayed a table titled {payload['title']!r} with {n} row(s) to the user.\n"
        + _compact_rows(payload)
    )


def _compact_rows(payload: dict[str, Any], *, limit: int = 20) -> str:
    """A short text rendering of a table payload, for consumers that only see
    the tool's return string (the deadline salvage, Langfuse spans)."""
    columns = [str(c) for c in (payload.get("columns") or [])]
    rows = payload.get("rows") or []
    if not rows:
        return ""
    out = ["Data (for reference if this run has to be summarised): " + " | ".join(columns)]
    for row in rows[:limit]:
        cells = row if isinstance(row, list | tuple) else [row.get(c, "") for c in columns]
        out.append("  " + " | ".join(str(c)[:40] for c in cells))
    if len(rows) > limit:
        out.append(f"  … and {len(rows) - limit} more row(s) shown on screen.")
    return "\n".join(out)


# ── Generic card pipeline (gen-UI wave 2, 2026-08-19) ────────────────
#
# The table/map/artifact/email-draft queues each grew their own queue +
# drain + orchestrator publish. Every card type from here on shares ONE
# queue carrying ``(event_type, payload)`` — the orchestrator publishes
# each drained pair as ``HelixisEvent(type=event_type, data=payload)``.
# Same contract as the older queues: tools push reasoned content, the
# frontend renders typed cards, unknown types are ignored by old clients.

# The queue itself is ``RunEmissions.cards`` — per run, like the four above.

# Defensive caps, shared by the wave-2 cards.
_MAX_CARD_FIELDS = 16
_MAX_CHART_POINTS = 24
_MAX_SCHEDULE_SLOTS = 24
_MAX_CHECKLIST_ITEMS = 20
_MAX_LABEL = 80
_MAX_VALUE = 200

_CARD_TONES = {"neutral", "good", "caution", "danger"}


def _clip(v: object, cap: int) -> str:
    return str(v).strip()[:cap]


async def drain_pending_cards() -> list[tuple[str, dict]]:
    """Non-blocking drain of the shared card queue — the orchestrator calls
    this after each stream event and publishes each ``(event_type, payload)``
    pair. Mirrors :func:`drain_pending_visuals`."""
    out: list[tuple[str, dict]] = []
    queue = _emissions().cards
    while not queue.empty():
        try:
            out.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    return out


def _queue_card(event_type: str, payload: dict) -> bool:
    try:
        _emissions().cards.put_nowait((event_type, payload))
    except asyncio.QueueFull:  # pragma: no cover — unbounded queue, defensive
        logger.warning("card-emission queue full; dropping %s", event_type)
        return False
    return True


# ── Detail card (work orders, tenants, leases, vendors…) ─────────────


def build_detail_card_payload(
    title: str,
    subtitle: str,
    status: str,
    tone: str,
    fields: list[list[str]],
    footnote: str,
) -> dict:
    """Frontend ``DetailCardData`` shape. The model labels the fields itself —
    the card is a frame for whatever entity it just reasoned about, not a
    fixed form. Pure + deterministic so it's unit-testable."""
    out_fields = [
        {"label": _clip(row[0], _MAX_LABEL), "value": _clip(row[1], _MAX_VALUE)}
        for row in (fields or [])[:_MAX_CARD_FIELDS]
        if isinstance(row, (list, tuple)) and len(row) >= 2 and _clip(row[1], _MAX_VALUE)
    ]
    return {
        "title": _clip(title, _MAX_LABEL),
        "subtitle": _clip(subtitle, _MAX_VALUE),
        "status": _clip(status, 40),
        "tone": tone if tone in _CARD_TONES else "neutral",
        "fields": out_fields,
        "footnote": _clip(footnote, _MAX_VALUE),
    }


@function_tool
async def show_detail_card(
    title: str,
    fields: list[list[str]],
    subtitle: str = "",
    status: str = "",
    tone: str = "neutral",
    footnote: str = "",
) -> str:
    """Show ONE record — a work order, tenant, lease, vendor, or property —
    as a scannable detail card instead of a paragraph. Use it when the answer
    is a single entity's facts; use show_table for a LIST of comparable rows.

    Args:
        title: The record's name (e.g. "AC not cooling — unit 4B" or
               "Maria Alvarez").
        fields: Label/value pairs, YOUR choice of what matters for this
                answer (e.g. [["Status", "In progress"], ["Vendor",
                "Northside Plumbing"], ["Balance", "$850 past due"]]).
                Format values as text yourself — no raw IDs or ISO stamps.
        subtitle: Optional one-line context (address, unit, company).
        status: Optional short status word shown as a badge ("Open",
                "Past due", "Active lease").
        tone: Badge tone: "neutral", "good", "caution", or "danger" —
              pick what the status actually warrants.
        footnote: Optional one-line caveat shown small at the bottom.

    Returns:
        A short confirmation (the card itself is shown to the user directly).
    """
    payload = build_detail_card_payload(title, subtitle, status, tone, fields, footnote)
    if not payload["title"] or not payload["fields"]:
        return "Nothing to show — pass a title and at least one labeled field."
    _queue_card("detail_card", payload)
    return (
        "Detail card shown. Do NOT repeat these fields in your reply — add "
        "only what the card doesn't say."
    )


# ── Chart card (single series, self-rendered — no external libs) ─────


def build_chart_payload(
    title: str,
    kind: str,
    points: list[list[str]],
    unit: str,
) -> dict:
    """Frontend ``ChartData`` shape: ``{title, kind, unit, points:[{label,
    value}]}``. Values parse as floats (commas/$/% stripped); unparseable
    points are dropped. Pure + deterministic so it's unit-testable."""
    out_points: list[dict] = []
    for row in (points or [])[:_MAX_CHART_POINTS]:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        raw = str(row[1]).replace(",", "").replace("$", "").replace("%", "").strip()
        try:
            value = float(raw)
        except ValueError:
            continue
        out_points.append({"label": _clip(row[0], _MAX_LABEL), "value": value})
    return {
        "title": _clip(title, _MAX_LABEL),
        "kind": kind if kind in {"bar", "line"} else "bar",
        "unit": unit if unit in {"currency", "percent", "count"} else "count",
        "points": out_points,
    }


@function_tool
async def show_chart(
    title: str,
    points: list[list[str]],
    kind: str = "bar",
    unit: str = "count",
) -> str:
    """Render a small chart for a numeric series — collections by month,
    occupancy trend, expenses by category. Use it when the SHAPE of the
    numbers is the answer; use show_table when the individual rows are.

    Args:
        title: What the series is (e.g. "Rent collected, last 6 months").
        points: [label, value] pairs in display order (e.g. [["Mar",
                "11400"], ["Apr", "11850"]]). Values numeric; "$" and ","
                are tolerated.
        kind: "bar" for comparisons, "line" for a trend over time.
        unit: "currency", "percent", or "count" — controls axis formatting.

    Returns:
        A short confirmation (the chart itself is shown to the user directly).
    """
    payload = build_chart_payload(title, kind, points, unit)
    if len(payload["points"]) < 2:
        return "Nothing to chart — pass at least two [label, value] points."
    _queue_card("chart", payload)
    return "Chart shown. Add one line of interpretation, not the numbers again."


# ── Schedule strip (showings, vendor visits, a day's slots) ──────────

_SLOT_STATES = {"proposed", "confirmed", "busy"}


def build_schedule_payload(
    title: str,
    day: str,
    slots: list[list[str]],
) -> dict:
    """Frontend ``ScheduleData`` shape: ``{title, day, slots:[{time, label,
    state}]}``. Each slot row is [time, label] or [time, label, state]."""
    out_slots: list[dict] = []
    for row in (slots or [])[:_MAX_SCHEDULE_SLOTS]:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        state = str(row[2]).strip().lower() if len(row) >= 3 else "proposed"
        out_slots.append(
            {
                "time": _clip(row[0], 20),
                "label": _clip(row[1], _MAX_LABEL),
                "state": state if state in _SLOT_STATES else "proposed",
            }
        )
    return {"title": _clip(title, _MAX_LABEL), "day": _clip(day, 40), "slots": out_slots}


@function_tool
async def show_schedule(
    title: str,
    day: str,
    slots: list[list[str]],
) -> str:
    """Show a day's time slots as a schedule strip — proposed showing times,
    vendor visits, a day's calendar. Far easier to scan than timestamps in
    prose.

    Args:
        title: What the schedule is for (e.g. "Showing times for Unit 2B").
        day: The day, as the user should read it (e.g. "Thu Aug 21").
        slots: [time, label] or [time, label, state] rows, in time order.
               time is display text ("9:00–9:30"); state is "proposed",
               "confirmed", or "busy" (busy = shown struck, unavailable).

    Returns:
        A short confirmation (the strip itself is shown to the user directly).
    """
    payload = build_schedule_payload(title, day, slots)
    if not payload["slots"]:
        return "Nothing to show — pass at least one [time, label] slot."
    _queue_card("schedule", payload)
    return "Schedule shown. Ask which slot works rather than re-listing them."


# ── Checklist card (multi-step plans that tick off) ──────────────────


def build_checklist_payload(title: str, items: list[list[str]]) -> dict:
    """Frontend ``ChecklistData`` shape: ``{title, items:[{text, done}]}``.
    Each row is [text] or [text, done-flag] ("done"/"true"/"x" = done)."""
    out_items: list[dict] = []
    for row in (items or [])[:_MAX_CHECKLIST_ITEMS]:
        if isinstance(row, str):
            row = [row]
        if not isinstance(row, (list, tuple)) or not row:
            continue
        text = _clip(row[0], _MAX_VALUE)
        if not text:
            continue
        done = len(row) >= 2 and str(row[1]).strip().lower() in {"done", "true", "x", "1"}
        out_items.append({"text": text, "done": done})
    return {"title": _clip(title, _MAX_LABEL), "items": out_items}


@function_tool
async def show_checklist(
    title: str,
    items: list[list[str]],
) -> str:
    """Show a multi-step plan as a checklist card. Use it when you lay out
    steps the user (or you) will work through — a unit turnover, a move-out,
    lease-up prep. Re-call it with updated done-flags as steps complete and
    the card shows live progress.

    Args:
        title: The plan's name (e.g. "Turnover plan — Unit 1A").
        items: One row per step: [text] or [text, "done"] for completed
               steps, in order.

    Returns:
        A short confirmation (the checklist is shown to the user directly).
    """
    payload = build_checklist_payload(title, items)
    if not payload["items"]:
        return "Nothing to show — pass at least one step."
    _queue_card("checklist", payload)
    return "Checklist shown. Don't re-list the steps in your reply."


# Prompt snippet for the wave-2 cards — appended wherever they register.
CARD_TOOLS_HINT = """

## Visual answers beyond tables

You also have these renderers. Reach for them the way you reach for
``show_table`` — whenever the SHAPE fits, prefer the visual over prose. The
content is always YOURS: your labels, your numbers, your steps for this
specific situation.

- ``show_detail_card`` — ONE record (a work order, tenant, lease, vendor):
  title + the label/value facts that matter for this answer.
- ``show_chart`` — a numeric series where the trend/comparison is the
  answer (collections by month, expenses by category).
- ``show_checklist`` — a multi-step plan; re-call with done-flags as steps
  complete.

After showing a card, add only what it doesn't say — never repeat its
contents as prose.
"""

SCHEDULE_TOOL_HINT = """

## Proposing times

You have ``show_schedule``. When you propose meeting/showing times or lay
out a day's slots, call it with the slots YOU worked out instead of listing
timestamps in prose, then ask which slot works.
"""


# ── Next-steps card (the work loop's hand-off) ───────────────────────
#
# Answering is not finishing. A property manager who asks "who's behind on
# rent?" now has a NEXT move — send the notices, check the one with an open
# repair, note what they decided — and a product that stops at the answer
# makes them think of it themselves every single time.
#
# Two surfaces on purpose (founder directive, 2026-08-21): the chips are the
# fast path, and the reply still SAYS what it recommends. A chip row alone
# reads like a menu; prose alone makes the user retype. Unlike the other
# cards, this one does NOT tell the model to stay quiet about it.

#: One next-steps row per TURN. Specialists each write user-facing answers, so
#: a turn that touches two of them would stack two chip rows — the wallpaper
#: failure the cap exists to prevent, arriving by a different door. Bound per
#: run beside the call-budget ledger; unbound (scripts, tests) it degrades to
#: "allow", because suppressing a card is never worth breaking a caller.
_next_steps_offered: ContextVar[bool] = ContextVar("helixis_next_steps_offered", default=False)


def begin_turn_next_steps() -> object:
    """Arm the once-per-turn guard; returns the token to reset with."""
    return _next_steps_offered.set(False)


def reset_turn_next_steps(token: object) -> None:
    _next_steps_offered.reset(token)  # type: ignore[arg-type]


#: Cap on offered steps. Beyond this it stops being a recommendation and
#: becomes a menu, which is the thing it exists to replace.
_MAX_NEXT_STEPS = 4


def build_next_steps_payload(steps: list[list[str]], note_hint: str = "") -> dict:
    """Normalize ``[label, prompt]`` rows into the card payload.

    A step needs BOTH halves: a label short enough to scan and the exact
    request to run when it's clicked. A row missing either is dropped rather
    than rendered as a dead chip.
    """
    out: list[dict[str, str]] = []
    for row in steps:
        if not isinstance(row, list) or len(row) < 2:
            continue
        label = _clip(str(row[0]).strip(), 60)
        prompt = str(row[1]).strip()
        if not label or not prompt:
            continue
        out.append({"label": label, "prompt": prompt})
        if len(out) >= _MAX_NEXT_STEPS:
            break
    payload: dict = {"steps": out}
    if note_hint.strip():
        payload["note_hint"] = _clip(note_hint.strip(), 120)
    return payload


def _suggest_next_steps_impl(steps: list[list[str]], note_hint: str = "") -> str:
    """The tool body, callable without an SDK ToolContext (same split as the
    Buildium tools) so the once-per-turn guard is directly testable."""
    payload = build_next_steps_payload(steps, note_hint)
    if not payload["steps"] and not payload.get("note_hint"):
        return "Nothing to show — pass at least one [label, prompt] step."
    if _next_steps_offered.get():
        return (
            "Next steps were already offered this turn — one row per answer. "
            "Say what you'd do next in your reply instead."
        )
    _next_steps_offered.set(True)
    _queue_card("next_steps", payload)
    return (
        "Next steps shown. Still say which one you'd do first and why — "
        "the chips are a shortcut, not the recommendation itself."
    )


@function_tool
async def suggest_next_steps(
    steps: list[list[str]],
    note_hint: str = "",
) -> str:
    """Offer the next moves after you've answered something.

    Call this at the END of a substantive answer — anything where a property
    manager would reasonably do something next. Skip it for trivia ("how many
    properties do I have?") where there is no natural follow-up; a chip row
    under every reply becomes wallpaper.

    Each step is the REAL request you'd run, not a topic. "Draft the late
    notices for the ten 61-90 day accounts" is a step; "collections" is not.

    Use ``note_hint`` when this turn produced a decision or a judgement worth
    keeping — the thing a manager would otherwise scribble on a pad and lose.
    It offers to write it down.

    Args:
        steps: Up to 4 rows of [label, prompt]. ``label`` is the chip text
               (short, verb-first); ``prompt`` is the exact request sent if
               the user clicks it.
        note_hint: Optional. What's worth recording from this turn, phrased
               as the offer ("Note that Whitfield's leak blocks collections").

    Returns:
        A short confirmation. UNLIKE the other cards, still say what you
        recommend in your reply — the chips are a shortcut, not a substitute.
    """
    return _suggest_next_steps_impl(steps, note_hint)


# ── Form card (ask for the blanks instead of writing a paragraph) ────
#
# A vague request — "make a task for the leaky faucet" — has real blanks in
# it, and the old behaviour was a paragraph of questions: *"what should the
# task say (title/description), and is there a specific unit, priority, or due
# date?"* That is four questions in one sentence, and the user answers it by
# writing a second paragraph the model then has to parse. A form is the right
# shape for the same conversation: the blanks are visible, the ones already
# known are filled in, and the answer comes back structured.
#
# ⚠ **This is a way of ASKING, never a write path.** Submitting sends an
# ordinary chat message. Every Buildium write it eventually leads to still
# passes its own confirm gate, role floor and idempotency claim — a form that
# wrote directly would be a second write surface with none of those, which is
# the thing `test_write_confirm_gate.py` exists to prevent.
#
# ⚠ **Prefill may only carry facts the user or a tool actually gave.** The
# temptation is to fill every blank so the form looks finished, and a guessed
# unit number is a fabricated fact sitting in a field a busy person will
# submit without re-reading. Blank is honest; plausible is not.

#: One form per TURN, for `suggest_next_steps`' reason: two stacked forms is a
#: questionnaire, and the second one is always the model asking again rather
#: than waiting for the first answer.
_form_offered: ContextVar[bool] = ContextVar("helixis_form_offered", default=False)


def begin_turn_form() -> object:
    """Arm the once-per-turn guard; returns the token to reset with."""
    return _form_offered.set(False)


def reset_turn_form(token: object) -> None:
    _form_offered.reset(token)  # type: ignore[arg-type]


#: Beyond this a form stops being a quick fill-in and becomes paperwork. The
#: model should ask for what it cannot proceed without, not everything a
#: record can hold.
_MAX_FORM_FIELDS = 6
_FORM_KINDS = {"text", "textarea", "choice", "date"}
_MAX_FORM_OPTIONS = 6


def build_form_payload(
    title: str,
    fields: list[list[str]],
    intro: str = "",
    submit_label: str = "",
) -> dict:
    """Normalize ``[name, label, kind, value, options]`` rows into the payload.

    A row needs a name, a label and a usable kind. An unrecognised kind falls
    back to ``text`` rather than dropping the field: losing a blank silently
    would make the model believe it asked for something it did not, and a
    plain text box always works.

    ``options`` is a pipe-separated list, used only by ``choice`` — the SDK
    schema stays a flat list of strings, because a nested union here is
    exactly the shape gotcha 27's malformed example 400'd the whole request
    over.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for row in fields or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        name = _clip(row[0], 40)
        label = _clip(row[1], _MAX_LABEL)
        if not name or not label or name in seen:
            continue
        seen.add(name)
        kind = _clip(row[2], 20).lower() if len(row) > 2 else "text"
        if kind not in _FORM_KINDS:
            kind = "text"
        field: dict = {
            "name": name,
            "label": label,
            "kind": kind,
            "value": _clip(row[3], _MAX_VALUE) if len(row) > 3 else "",
        }
        if kind == "choice":
            raw = str(row[4]) if len(row) > 4 else ""
            opts = [_clip(o, 60) for o in raw.split("|") if _clip(o, 60)][:_MAX_FORM_OPTIONS]
            # A choice with nothing to choose from is an unusable control, so
            # it degrades to a text box rather than rendering as a dead select.
            if opts:
                field["options"] = opts
            else:
                field["kind"] = "text"
        out.append(field)
        if len(out) >= _MAX_FORM_FIELDS:
            break
    return {
        "title": _clip(title, _MAX_LABEL),
        "intro": _clip(intro, _MAX_VALUE),
        "submit_label": _clip(submit_label, 40) or "Send",
        "fields": out,
    }


def _request_details_impl(
    title: str,
    fields: list[list[str]],
    intro: str = "",
    submit_label: str = "",
) -> str:
    """The tool body, callable without an SDK ToolContext (same split as
    ``_suggest_next_steps_impl``) so the once-per-turn guard is testable."""
    payload = build_form_payload(title, fields, intro, submit_label)
    if not payload["title"] or not payload["fields"]:
        return "Nothing to show — pass a title and at least one [name, label] field."
    if _form_offered.get():
        return (
            "A form was already shown this turn — one per answer. Wait for the "
            "user to fill that one in rather than asking again."
        )
    _form_offered.set(True)
    _queue_card("form", payload)
    return (
        "Form shown. STOP HERE — do not ask the same questions again in prose, "
        "and do not guess the missing values and act anyway. End your reply in "
        "one short line (e.g. 'Fill that in and I'll take it from there') and "
        "wait for the user's next message."
    )


@function_tool
async def request_details(
    title: str,
    fields: list[list[str]],
    intro: str = "",
    submit_label: str = "",
) -> str:
    """Ask for missing details as a small FORM instead of a paragraph of
    questions. Use it when the user clearly wants something done but has not
    said enough to do it — "make a task for the leaky faucet", "send the
    vendor a note", "schedule the inspection".

    Prefer this over asking in prose whenever you need MORE THAN ONE thing.
    A single missing fact is a question; three is a form.

    ⚠ Prefill ONLY what the user actually said or what you read from a tool.
    Leave everything else blank. A plausible-looking value the user never gave
    is a fabricated fact in a field they will submit without re-reading.

    ⚠ This asks; it does not act. When the user submits, you get their answers
    as an ordinary message and THEN do the work — including every normal
    confirmation step for anything that writes.

    Args:
        title: What is being set up, in the user's words ("New maintenance
               task", "Note for Northside Plumbing").
        fields: Up to 6 rows of [name, label, kind, value, options].
                ``name`` is a short key ("unit", "priority"); ``label`` is
                what the user reads ("Which unit?"); ``kind`` is one of
                "text", "textarea", "choice" or "date" (default "text");
                ``value`` is the prefill, or "" when you were not told;
                ``options`` is only for "choice" — the alternatives separated
                by "|" (e.g. "Low|Normal|High").
        intro: Optional one line above the fields saying why you are asking.
        submit_label: The button text — the action it leads to ("Create
               task"). Defaults to "Send".

    Returns:
        A short confirmation. Then STOP and wait — do not re-ask in prose.
    """
    return _request_details_impl(title, fields, intro, submit_label)


FORM_HINT = """

## When a request is vague, show a form — don't interrogate

A manager who says "make a task for the leaky faucet in 4B" has told you the
job and left the rest blank. The wrong reply is a paragraph of questions
("what should the title be, and is there a specific unit, priority, or due
date?") — it makes them write a second paragraph, and you then have to parse
it back apart.

Call ``request_details`` instead: put the blanks on screen, PREFILL what they
already told you, and let them fill in the rest and submit.

- Use it when you need **more than one** missing thing. One missing fact is a
  question; three is a form.
- **Prefill only what you were actually given.** A guessed unit number is a
  fabricated fact in a box someone will submit without re-reading it.
- **One form, then wait.** After it is shown, end your reply in a line and
  stop. Do not ask the same things again in prose, and never fill the blanks
  in yourself and proceed.
- It asks, it does not act. When their answers come back, do the work — with
  every normal confirmation for anything that writes.
"""


NEXT_STEPS_HINT = """

## Finish like a colleague, not a search box

Answering the question is not finishing the job. When you've done something
substantive, close the loop the way a good property manager would:

1. **Say what you'd do next, and which one first.** In the reply, in prose.
   One or two sentences — a recommendation, not a menu.
2. **Call ``suggest_next_steps``** with those same moves as clickable steps,
   so the user doesn't have to retype what you just proposed. Each step is
   the real request ("Draft late notices for the ten 61-90 day accounts"),
   never a topic ("collections").
3. **Offer to write it down** when the turn produced a decision, a judgement,
   or something a manager would otherwise lose — pass ``note_hint``. A note on
   the record (``buildium_add_task_note``, ``buildium_add_work_order_note``,
   ``buildium_add_vendor_note``) is how the next person learns why. Never
   write one without being asked; offering IS the behaviour.

Skip all of it for trivia with no natural follow-up. A chip row under every
reply is wallpaper, and wallpaper is worse than nothing — it teaches the user
to stop reading the one place you put your recommendation.

**Next steps are OFFERED, not TAKEN.** Build them out of what you already
learned this turn — never consult a specialist again to enrich them, and never
keep researching because the job "feels" unfinished. "Answering is not
finishing" is about closing the loop with the user, not about doing more work
before you reply. A one-fact question gets one lookup and a direct answer; if
the follow-up needs data you don't have, propose it as a step and let the user
click it.
"""
