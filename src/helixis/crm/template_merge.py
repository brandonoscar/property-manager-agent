"""Merge-field filling for CRM message templates.

Company message templates (Documents & Links) carry ``{token}`` placeholders —
e.g. ``"Hi {first_name}, thanks for your interest in {property}! Book a tour
here: {scheduling_link}."``. When a lead arrives, the company's default template
is filled from the lead's own fields to seed the first drafted reply. Pure +
deterministic so it's unit-testable without any live service.

Unknown tokens are left **as-is** (braces intact) rather than blanked — the PM
reviews the draft before sending, and a visible ``{unfilled}`` is a clearer
signal than a silent blank. A token that resolves to an empty value is also
left intact for the same reason.
"""

from __future__ import annotations

import re

# Only single-word ``{snake_case}`` tokens are treated as merge fields, so an
# email body that legitimately contains braces (JSON, code) isn't mangled.
_TOKEN_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def build_lead_merge_context(
    *,
    name: str | None,
    unit: str | None = None,
    tour_label: str | None = None,
    scheduling_link: str | None = None,
) -> dict[str, str]:
    """The values a lead offers a template. Empty/missing fields are omitted so
    :func:`fill_template` leaves their tokens intact (visible to the PM)."""
    full_name = (name or "").strip()
    first_name = full_name.split()[0] if full_name else ""
    ctx: dict[str, str] = {}
    if first_name:
        ctx["first_name"] = first_name
    if full_name:
        ctx["name"] = full_name
        ctx["full_name"] = full_name
    if unit:
        # A renter's unit doubles as "the property they're interested in".
        ctx["unit"] = unit
        ctx["property"] = unit
    if tour_label:
        ctx["tour_time"] = tour_label
    if scheduling_link:
        ctx["scheduling_link"] = scheduling_link
    return ctx


def fill_template(body: str, context: dict[str, str]) -> str:
    """Replace ``{token}`` with ``context[token]``. Unknown tokens (or ones whose
    value is empty) are left untouched, braces and all."""

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        value = context.get(key)
        return value if value else match.group(0)

    return _TOKEN_RE.sub(_sub, body)


def unfilled_tokens(body: str, context: dict[str, str]) -> list[str]:
    """Tokens still unfilled after a merge (for a 'these are blank' hint).
    Sorted + de-duplicated."""
    return sorted({m.group(1) for m in _TOKEN_RE.finditer(body) if not context.get(m.group(1))})


__all__ = ["build_lead_merge_context", "fill_template", "unfilled_tokens"]
