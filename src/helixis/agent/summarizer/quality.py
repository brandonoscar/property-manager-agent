"""Post-generation quality check for summarizer output.

The summarizer's pipeline used to be:

  1. Call Gemini → get text
  2. Sanitize (reject if it leaks Buildium IDs)
  3. Truncate (cap at 500 chars)
  4. Persist

Step 2 catches a specific failure (ID leak); step 3 caps length. Neither
asks "is this text actually any good?" In production we hit a row
(39bb7547 / "Leak at 100 Main Ave") where Gemini returned a
syntactically valid but semantically empty 32-char paragraph: "A task
was created this evening." Despite rich enrichment — full task
description, tenants on the active lease, 21 prior tickets on the
unit — the model bailed to a one-liner. We persisted it.

PR #122 adds this module: a heuristic quality validator that returns a
short human-readable reason when the text is obviously bad, so the
caller can retry the LLM with a corrective nudge (same pattern the
planner already uses for invalid-JSON outputs in
``action_planner.py:599-651``).

The heuristics here are intentionally conservative — we only flag
clearly-bad output. A summary that's stylistically clumsy but
factually grounded is NOT low-quality from this module's perspective;
that's a prompt-tuning problem, not a safety-net problem.

Three flags:

  * **too_short** — under ``_MIN_SUMMARY_CHARS`` (the production
    failure was 32 chars; the v4 prompt targets 2-4 sentences,
    which is roughly 150-400 chars).
  * **filler_opener** — starts with a generic stem like "A task was
    created" / "An event was reported" / "Something happened" /
    "This task was". These are the model's "I don't know what to
    say" defaults.
  * **ungrounded** — doesn't reference ANY anchor noun from the
    enrichment (task title, property address, unit number, tenant
    first names, assignee name). If we sent rich context and the
    model didn't use any of it, the output is decorative not
    informative.

If none flag, returns None. Callers treat None as "ship it".
"""

from __future__ import annotations

import re
from typing import Any

# Minimum sensible summary length. The v5 prompt asks for 2-4
# sentences — even a tight 2-sentence summary is ~80-120 chars. 60
# is a comfortable floor: anything shorter is the model giving up.
_MIN_SUMMARY_CHARS = 60

# Filler stems that signal the model didn't engage with the data.
# Each is a regex anchored at start-of-string (case-insensitive).
# Keep this list short and high-precision: false positives here mean
# we retry unnecessarily and burn LLM tokens.
#
# PR #127 — widened to catch "A new Todo task was created early this
# morning." (the production failure that escaped the v5 regex because
# the model wedged "Todo" between "new" and "task"). The new patterns
# allow an optional single noun between "new" and the type word so
# we catch "A new Todo task was created", "A new maintenance task
# was added", etc., without false-positiving on legitimate openings
# like "Clifton Munoz at 100 Main Ave reported…".
#
# PR #128 — widened again to handle QUOTED middle nouns. Production
# row a9b61f2e (Exhaust Fan at 33 Garden Row #3B) came back as ``A
# new "Todo" task was created on May 22, 2026.`` — the PR #127
# regex required a word-character middle noun but the model wrapped
# "Todo" in literal double quotes. ``["']?\w+["']?`` allows zero or
# one straight/single quote on each side of the noun.
_FILLER_OPENERS: tuple[re.Pattern[str], ...] = (
    # "A task was created" / "This task was created" — the bare
    # generic openers.
    re.compile(r"^a\s+task\s+was\s+(?:created|added|opened|reported)\b", re.IGNORECASE),
    re.compile(r"^this\s+task\s+was\s+(?:created|added|opened|reported)\b", re.IGNORECASE),
    # "A new [noun?] task/event/ticket/note/request was …" — the
    # main extension. Optional middle noun (with optional surrounding
    # quotes) handles "A new Todo task was created", "A new
    # \"Todo\" task was created", "A new maintenance ticket was
    # opened", etc.
    re.compile(
        r"^a\s+new\s+(?:[\"']?\w+[\"']?\s+)?"
        r"(?:task|event|ticket|note|request|item|update|message|todo)\s+was\s+"
        r"(?:created|added|opened|reported|received|made)\b",
        re.IGNORECASE,
    ),
    # "An event was reported" / "An event has been logged" — singular
    # event openers without "new".
    re.compile(r"^an?\s+event\s+(?:was|has\s+been)\b", re.IGNORECASE),
    # Bare "Something happened" / "Something was reported".
    re.compile(r"^something\s+(?:happened|was\s+reported|came\s+in)\b", re.IGNORECASE),
    # PR #127 — "A todo was created" / "A new todo" variants where
    # "todo" is the noun itself (not modifying "task"). Caught above
    # too, but pinned as its own pattern in case future LLM revisions
    # drop the type word entirely.
    re.compile(r"^a\s+(?:new\s+)?todo\b", re.IGNORECASE),
)


def assess_summary_quality(
    text: str,
    payload: dict[str, Any],
    enrichment: dict[str, Any] | None,
) -> str | None:
    """Return a short human-readable reason when ``text`` is low
    quality, else None.

    ``text`` is the LLM's output AFTER sanitization + truncation.
    ``payload`` and ``enrichment`` are the same dicts handed to the
    summarizer so we can pull anchor words for the ungrounded check.

    The reason string is fed back to the LLM in a correction nudge,
    so it should be short and instructive ("too short" /
    "doesn't mention 100 Main Ave or Clifton" / "starts with generic
    filler"). The caller may format it further before sending.
    """
    if len(text) < _MIN_SUMMARY_CHARS:
        return f"too short ({len(text)} chars; aim for 2-4 sentences)"

    for pattern in _FILLER_OPENERS:
        if pattern.match(text):
            return (
                "starts with a generic filler stem instead of the actual "
                "people, place, and problem from the enrichment"
            )

    anchors = _collect_anchor_words(payload, enrichment)
    if anchors:
        lowered = text.lower()
        if not any(a.lower() in lowered for a in anchors):
            # Pick up to 3 anchors to surface in the nudge so the
            # model knows what we actually want it to use. Source-code
            # order in ``_collect_anchor_words`` is the implicit
            # semantic priority (title > address > unit > reporter >
            # tenants > assignee), so the first 3 are the most
            # useful anchors for grounding. Issue #151 — previously
            # this was a ``set`` whose iteration order varied per
            # process via PYTHONHASHSEED, producing flaky test
            # assertions AND non-deterministic production retry
            # nudges. List preserves insertion order, fixing both.
            hint = ", ".join(anchors[:3])
            return f"doesn't mention any concrete fact from the enrichment ({hint})"

    return None


def quality_flag(reason: str | None) -> str:
    """Map :func:`assess_summary_quality`'s human ``reason`` → a stable
    category slug for telemetry (Langfuse score comment / weekly baseline).

    The reason strings are stable, high-precision constants produced above,
    so a prefix match is a safe, unit-testable classifier. Keeping the map
    HERE (next to the strings it classifies) means anyone editing a reason
    string sees the slug that depends on it. ``None`` (shippable) → ``ok``.

    Categories mirror the three heuristics: ``too_short`` / ``filler_opener``
    / ``ungrounded``; an unrecognised reason falls back to ``other`` so a
    future flag still charts as a non-``ok`` bucket rather than being lost.
    """
    if reason is None:
        return "ok"
    lowered = reason.lower()
    if lowered.startswith("too short"):
        return "too_short"
    if lowered.startswith("starts with a generic filler"):
        return "filler_opener"
    if lowered.startswith("doesn't mention"):
        return "ungrounded"
    return "other"


def _collect_anchor_words(
    payload: dict[str, Any],
    enrichment: dict[str, Any] | None,
) -> list[str]:
    """Extract anchor words/phrases the summary OUGHT to reference
    somewhere in its text if it's genuinely engaging with the data
    we sent. The return is an **ordered, deduped list** — order is
    the source-code priority below (title > address > unit > reporter
    > tenants > assignee), and ``_add`` drops duplicates so a
    reporter who is also a tenant doesn't appear twice.

    Returns an empty list when there's nothing to ground against
    (pre-migration rows with no enrichment) — the caller skips the
    ungrounded check in that case so we don't false-positive on
    rows where the model legitimately had nothing to anchor with.

    Issue #151 — previously this returned a ``set[str]`` whose
    iteration order varied per process via CPython's PYTHONHASHSEED
    randomization. Callers that surfaced only the first few elements
    (``assess_summary_quality`` slices ``[:3]`` into the retry hint)
    saw non-deterministic output across pytest invocations (~5%
    flake) and across production processes (retry nudges sometimes
    listed the high-signal anchors, sometimes the low-signal ones).
    Switching to a list with explicit dedupe fixes both.
    """
    anchors: list[str] = []

    def _add(value: str) -> None:
        """Append ``value`` if it's non-empty and not already present.
        O(N) membership scan, fine for the worst-case ~14 anchors
        this function emits."""
        if value and value not in anchors:
            anchors.append(value)

    if not enrichment:
        return anchors

    task = enrichment.get("task") or {}
    unit = enrichment.get("unit") or {}
    lease = enrichment.get("active_lease") or {}

    # Task title — the most useful single anchor when present.
    title = (task.get("title") or "").strip()
    if len(title) >= 4:
        _add(title)

    # Address parts — "100 Main Ave" / "Unit B" / "#120".
    address = (unit.get("address_line1") or "").strip()
    if len(address) >= 4:
        _add(address)
    unit_number = (unit.get("unit_number") or "").strip()
    if unit_number:
        # Both "Unit B" and bare "B" are too short to anchor cleanly;
        # only include when it's substantive (e.g. "4B" or "120").
        if len(unit_number) >= 2:
            _add(unit_number)

    # Reporter first name (when the task carries one).
    reporter_first = (task.get("requested_by_first_name") or "").strip()
    if len(reporter_first) >= 3:
        _add(reporter_first)
    reporter_last = (task.get("requested_by_last_name") or "").strip()
    if len(reporter_last) >= 3:
        _add(reporter_last)

    # Active-lease tenant names. v5's no-reporter fallback explicitly
    # tells the model to mention these as context, so they're a fair
    # grounding signal.
    for tenant in lease.get("tenants") or []:
        first = (tenant.get("first_name") or "").strip()
        last = (tenant.get("last_name") or "").strip()
        if len(first) >= 3:
            _add(first)
        if len(last) >= 3:
            _add(last)

    # Assignee name (the backend joins buildium_staff to populate this
    # — see InboxEnrichmentTask.assignee_name).
    assignee = (task.get("assignee_name") or "").strip()
    if len(assignee) >= 3:
        _add(assignee)

    return anchors


def build_correction_nudge(
    reason: str,
    payload: dict[str, Any],
    enrichment: dict[str, Any] | None,
) -> str:
    """Build the user-role message we append on retry.

    Includes the assessed ``reason`` AND a short list of concrete
    facts the model has available, so the retry has more signal
    than just "try again". Mirrors the planner's
    ``action_planner.py:642-651`` correction nudge.
    """
    anchors = _collect_anchor_words(payload, enrichment)
    facts = (
        ", ".join(sorted(anchors)[:5]) if anchors else "the fields in the enrichment block above"
    )
    return (
        f"Your previous response was unusable: {reason}. "
        f"You have concrete facts to work with — at minimum: {facts}. "
        "Write the 2-4 sentence narrative again, leading with the property "
        "and the symptom, and naming the active-lease tenants as context "
        "when no reporter is on the task. No preamble. No quotation marks."
    )
