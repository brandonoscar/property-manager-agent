"""Universal federal fair-housing reference (roadmap §6 #12).

⚠ THIS DOCSTRING SAID THE PER-STATE LAYER WAS PARKED, AND THAT HAS BEEN FALSE
SINCE THE JURISDICTION DATA LANDED. It read: "the state-knowledge-base idea (a
per-state legal picker + one MD per state) was parked: per-state
landlord-tenant law has no clean source, needs human legal review, and carries
real liability." What actually ships today is
``geo/data/jurisdiction_rules.json`` (version 2026-08) — **51 states plus 13
local overlay jurisdictions**, each with deposit caps and return deadlines,
late-fee rules, notice periods, source-of-income protections, licensing,
eviction speed tier and quirks, every entry carrying a statute citation — read
by the ``jurisdiction_lookup`` agent tool with per-company overrides from
``jurisdiction_notes``. A comment cannot fail (gotcha 48), so this one went on
describing a decision that had been reversed, in the module most likely to be
read by anyone deciding what we know about state law.

The correct division of labour is: this module is the layer that is uniform
nationwide and needs no lookup — the **federal Fair Housing Act**, the same
seven protected classes everywhere, so there is nothing to pick between. State
landlord-tenant PARAMETERS are the jurisdiction layer, and they are
property-scoped (they resolve from a property's address), which is a real
limit worth stating: a question naming only a state, with no property, still
has nothing for that tool to resolve.

It is a **constant**, not per-company data: every company gets the identical
block, injected as standing context into the two surfaces where fair-housing
risk lives — the chat assistant (screening / advertising / tenant-selection
questions) and CRM lead drafts (messages to prospects). It carries the
"information, not legal advice → cite the source → escalate to counsel"
guardrails from the research, and deliberately defers to state/local law rather
than guessing at it.

Keep this tight: it rides in every relevant prompt, so brevity is a feature.

⚠ **THE PROXY-OFFER RULE LIVES HERE BECAUSE FOUR SURFACES NEED IT AND ONLY ONE
HAD IT** (2026-09-04, gotcha 84). Gotcha 52 found the shape — decline the
protected-class question correctly, then volunteer "school district ratings,
walkability, crime stats" as the helpful alternative, which the rubric judge
scores 0.00 because the offer is what the next turn delivers. The fix went into
``ORCHESTRATOR_INSTRUCTIONS``, and that is one prompt string in one module. This
block is what reaches ``buildium_agent``, ``crm/draft.py`` and
``renewals/draft.py`` — and ``crm/draft.py``'s own docstring calls itself *"the
highest fair-housing-risk surface (outbound messages to prospects)"*. It had no
proxy rule at all. Gotcha 67's exact shape: a rule that reached one lane and not
the one beside it.

The orchestrator keeps its own longer, routing-specific version (it also has to
say "do not hand off"), and that text is deliberately NOT moved — it is the
measured prompt, and shuffling a tuned block on launch eve buys nothing. Two
copies of one rule is a drift risk, so ``test_federal_fair_housing.py`` reads
BOTH and asserts each names every proxy family and the permitted substitute
(gotcha 28). If you change one, change the other.
"""

from __future__ import annotations

# The seven federally protected classes (Fair Housing Act, 42 U.S.C. §3601 et
# seq.; "sex" reads to include gender identity + sexual orientation per HUD's
# post-Bostock implementation). Ordering is the conventional HUD listing.
FEDERAL_PROTECTED_CLASSES: tuple[str, ...] = (
    "race",
    "color",
    "national origin",
    "religion",
    "sex (including gender identity and sexual orientation)",
    "familial status (children under 18 / pregnancy)",
    "disability",
)

FEDERAL_FAIR_HOUSING_REFERENCE = """\
## Fair Housing (Federal — applies nationwide)

The federal Fair Housing Act (42 U.S.C. §3601 et seq.) prohibits discrimination \
in housing based on seven protected classes: race, color, national origin, \
religion, sex (including gender identity and sexual orientation), familial \
status (children under 18 / pregnancy), and disability.

When answering leasing, screening, advertising, or tenant-selection questions, \
and when drafting any message to a prospect or resident:
- Treat every applicant and resident identically. Apply the same criteria to \
everyone; never ask about, infer, record, or act on protected-class membership.
- Never steer (guide someone toward or away from a unit or area based on a \
protected class) and never word an ad or reply to express a preference, \
limitation, or discouragement tied to a protected class.
- Reasonable accommodations/modifications for disability must be considered; \
you may not refuse them outright or ask about the nature/severity of a disability.
- **Declining and then OFFERING a proxy is the same violation.** After you \
refuse a protected-class request, do NOT volunteer, suggest, or ask whether \
they would like crime statistics or "safety" data, school district ratings or \
quality, "family-friendliness", who lives in an area, or how a neighbourhood \
"feels". The offer is what the next turn delivers, so it does not matter that \
nothing has been fetched yet — and the qualification ("just public data", "not \
framed around any protected class", "the same for any applicant") is the tell, \
not the cure.
- What you MAY offer instead is confined to the PROPERTY and the TRANSACTION, \
never the surrounding population: the unit's own features, condition, \
appliances and building safety equipment (locks, lighting, cameras); rent, \
deposit, fees, lease terms and availability; commute distance and transit \
lines; pet, parking and utility policy; and your own identical, objective \
screening standards. If none of those answers what they actually want, say so \
and stop — an honest "I can't help with that part" is a complete reply. Do not \
reach for the nearest adjacent fact to seem useful.
- This is general information, not legal advice. State and local law often add \
protected classes (e.g. source of income, age, marital status) and stricter \
rules. For a specific situation — a denial, an accommodation request, an ad, a \
policy — say this is general federal guidance and recommend confirming with \
fair-housing counsel or the state/local housing agency."""


def federal_fair_housing_block() -> str:
    """The standing fair-housing reference block for prompt injection. A plain
    accessor so callers don't import the constant name directly and the wrapping
    can evolve in one place."""
    return FEDERAL_FAIR_HOUSING_REFERENCE


__all__ = [
    "FEDERAL_FAIR_HOUSING_REFERENCE",
    "FEDERAL_PROTECTED_CLASSES",
    "federal_fair_housing_block",
]
