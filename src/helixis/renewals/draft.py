"""LLM draft of a lease-renewal offer email.

Same seam as the CRM lead assist (``crm/draft.py``): ``litellm.acompletion``
+ the reasoning model from ``settings.llm`` + ``coerce_json`` — an
*enhancement, never load-bearing*: any failure degrades to a deterministic
template so the PM always gets an editable draft. The fair-housing block
rides the system prompt, same as every tenant-facing draft.

Helixis NEVER sends this email — the PM edits and explicitly sends from the
property page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import litellm

from helixis.common.json_extract import coerce_json
from helixis.llm.router import _api_key_for_model
from helixis.llm.sampling import sampling_kwargs
from helixis.settings import Settings

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 25
_MAX_BODY_CHARS = 6000
_MAX_SUBJECT_CHARS = 200

SYSTEM_PROMPT = """You draft lease-renewal offer emails for a property \
manager (PM). You are given the lease facts (tenant names, unit, current \
rent, lease end date) and the PM's proposed terms (new rent, term length). \
Write a warm, professional, PLAIN-TEXT email from the PM to the tenant(s):

- Open by valuing them as residents.
- State the current lease end date and the offer: the new monthly rent and \
term. If the rent is unchanged, say so as a positive.
- Ask them to reply by a reasonable date (about two weeks out) to accept or \
discuss.
- Keep it under 180 words. No markdown, no placeholders like [Name] — use \
the real names given. Do not invent amenities, fees, or policies not in the \
facts.

Reply with ONLY a JSON object: {"subject": "...", "body": "..."}"""


def _system_prompt() -> str:
    from helixis.company_knowledge.federal_fair_housing import federal_fair_housing_block

    return SYSTEM_PROMPT + "\n\n" + federal_fair_housing_block()


def _model_name(settings: Settings) -> str:
    llm = settings.llm
    models = llm.models or {}
    reasoning = models.get("reasoning") or {}
    return reasoning.get("model") or llm.default_model


@dataclass
class RenewalDraft:
    subject: str
    body: str
    generated: bool  # False → deterministic fallback (LLM unavailable/failed)


def build_facts(
    *,
    tenant_names: str | None,
    unit_number: str | None,
    property_name: str | None,
    current_rent: float | None,
    lease_to_date: date | None,
    proposed_rent: float | None,
    term_months: int | None,
    pm_notes: str = "",
) -> str:
    """The user-prompt fact block. Pure — unit-tested."""
    lines = [
        f"Tenant(s): {tenant_names or 'the resident'}",
        f"Property: {property_name or 'the property'}"
        + (f", Unit {unit_number}" if unit_number else ""),
        f"Current rent: ${current_rent:,.2f}/month" if current_rent else "Current rent: n/a",
        f"Lease ends: {lease_to_date.isoformat()}" if lease_to_date else "Lease ends: soon",
        (
            f"Proposed new rent: ${proposed_rent:,.2f}/month"
            if proposed_rent
            else "Proposed new rent: unchanged"
        ),
        f"Proposed term: {term_months} months" if term_months else "Proposed term: 12 months",
    ]
    if pm_notes.strip():
        lines.append(f"PM notes to incorporate: {pm_notes.strip()[:500]}")
    return "\n".join(lines)


def fallback_draft(
    *,
    tenant_names: str | None,
    unit_number: str | None,
    property_name: str | None,
    current_rent: float | None,
    lease_to_date: date | None,
    proposed_rent: float | None,
    term_months: int | None,
) -> RenewalDraft:
    """Deterministic template when the LLM is unavailable. Pure — unit-tested."""
    who = tenant_names or "there"
    where = (property_name or "your home") + (f", Unit {unit_number}" if unit_number else "")
    ends = lease_to_date.isoformat() if lease_to_date else "soon"
    rent = proposed_rent if proposed_rent is not None else current_rent
    rent_part = f"${rent:,.2f}/month" if rent is not None else "your current rent"
    term = term_months or 12
    subject = f"Lease renewal offer — {where}"
    body = (
        f"Hi {who},\n\n"
        f"Your lease at {where} ends on {ends}, and we'd love to have you stay. "
        f"We're offering a {term}-month renewal at {rent_part}.\n\n"
        "Please reply within the next two weeks to accept or if you'd like to "
        "discuss the terms — happy to talk it through.\n\n"
        "Thank you for being a valued resident."
    )
    return RenewalDraft(subject=subject, body=body, generated=False)


async def generate_renewal_draft(
    settings: Settings,
    *,
    tenant_names: str | None,
    unit_number: str | None,
    property_name: str | None,
    current_rent: float | None,
    lease_to_date: date | None,
    proposed_rent: float | None,
    term_months: int | None,
    pm_notes: str = "",
) -> RenewalDraft:
    """LLM draft with the deterministic fallback. Never raises."""
    fb = fallback_draft(
        tenant_names=tenant_names,
        unit_number=unit_number,
        property_name=property_name,
        current_rent=current_rent,
        lease_to_date=lease_to_date,
        proposed_rent=proposed_rent,
        term_months=term_months,
    )
    try:
        model = _model_name(settings)
        api_key = _api_key_for_model(settings, model)
        kwargs: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": _system_prompt()},
                {
                    "role": "user",
                    "content": build_facts(
                        tenant_names=tenant_names,
                        unit_number=unit_number,
                        property_name=property_name,
                        current_rent=current_rent,
                        lease_to_date=lease_to_date,
                        proposed_rent=proposed_rent,
                        term_months=term_months,
                        pm_notes=pm_notes,
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            **sampling_kwargs(model, 0.4),
            "timeout": _TIMEOUT_SECONDS,
        }
        if api_key:
            kwargs["api_key"] = api_key
        response = await litellm.acompletion(**kwargs)
        raw = response.choices[0].message.content
        data, _ = coerce_json(raw)
        if not isinstance(data, dict):
            return fb
        subject = str(data.get("subject") or "").strip()[:_MAX_SUBJECT_CHARS]
        body = str(data.get("body") or "").strip()[:_MAX_BODY_CHARS]
        if not subject or not body:
            return fb
        return RenewalDraft(subject=subject, body=body, generated=True)
    except (Exception, SystemExit):  # noqa: BLE001 — enhancement, never load-bearing
        logger.exception("renewal draft generation failed; using fallback")
        return fb


__all__ = [
    "RenewalDraft",
    "build_facts",
    "fallback_draft",
    "generate_renewal_draft",
]
