"""CRM lead AI assist — "what Helixis noticed" signals + a drafted first reply.

Uses the SAME one-shot LLM seam as the planner/summarizer
(``litellm.acompletion`` + Gemini via ``settings.llm`` + ``_api_key_for_model``
+ ``coerce_json``), NOT a parallel path. The assist is an *enhancement, never
load-bearing* (gotcha 7): any failure — no key, timeout, bad JSON, a spaCy/mem0
``SystemExit`` on some transitive path — degrades to empty signals + no draft so
the board always renders. The lead still shows; the PM just writes their own
reply.

Consumed by ``routes/crm.py::get_lead_draft`` (generate-on-first-open, cached
on the lead via ``repositories.leads.set_lead_draft``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import litellm

from helixis.common.json_extract import coerce_json
from helixis.db.repositories.leads import LeadRow, MessageRow
from helixis.llm.router import _api_key_for_model
from helixis.llm.sampling import sampling_kwargs
from helixis.security.prompt_delimit import UNTRUSTED_CONTENT_RULE, wrap_untrusted
from helixis.settings import Settings

logger = logging.getLogger(__name__)

# Tighter than the worker's 30s — this runs on a user-facing request (the lead
# drawer opening), so we cap it and degrade rather than hang the drawer.
_TIMEOUT_SECONDS = 18.0
_MAX_SIGNALS = 4

SYSTEM_PROMPT = """You are Occupella, the assistant for a property manager (PM). \
A prospective renter or owner has reached out about a rental. Read the \
conversation and return a JSON object with EXACTLY two fields:

- "signals": an array of 2 to 4 SHORT strings (max ~10 words each) — the \
concrete things a PM should know at a glance: what the person wants, the \
property/unit they named, any question they asked, timing (a move-in date, \
"this weekend"), and whether they read as a renter or an owner. Plain \
observations, no filler.
- "draft_reply": a warm, professional FIRST reply the PM can send by text — 1 \
to 3 short sentences. Acknowledge their message, answer what you can safely \
answer, and offer a clear next step (propose a tour time, or ask for theirs).

Hard rules:
- If a "Company facts" block is provided (distilled from the company's own \
documents), USE it to answer concrete questions (hours, pet policy, application \
fee, parking, screening). Those facts are authoritative.
- NEVER invent specifics you were not told (exact rent, availability dates, \
policies) — the company facts are the ONLY specifics you may state. If a fact \
isn't there, keep it general or offer to follow up.
- Fair Housing: treat every prospect identically. NEVER mention or infer race, \
religion, family/familial status, disability, national origin, sex, or any \
protected class, and never screen, score, or rank the person.
- Write as the property manager (a person), not as "Occupella" or an AI, and do \
not sign a specific name.
- Output ONLY the JSON object, nothing else."""


def _system_prompt() -> str:
    """Draft system prompt + the shared universal federal fair-housing reference.

    Lead drafts are the highest fair-housing-risk surface (outbound messages to
    prospects), so the same nationwide reference the chat assistant uses is
    appended here — one source of truth for both surfaces.

    ⚠ This said "as a hard guardrail" until 2026-08-29, and a prompt is not
    one. Gotcha 52 recorded the model declining a protected-class question
    correctly and then OFFERING the proxies that same prompt forbids by name,
    with the rubric judge catching that shape only sometimes. Calling prompt
    guidance a hard guardrail is the shape gotcha 33 warns about — a control
    stated as load-bearing that is not — and here it stood in for the
    deterministic gate this lane did not have.

    The real gate is at EGRESS now: ``routes/crm.py::reply_to_lead`` runs
    ``check_draft`` before the send, the same tuned check the two Inbox lanes
    use. It is an improvement and not full coverage — see
    ``tests/unit/test_crm_fair_housing_egress.py``, which pins what it catches
    AND the familial-status prose it does not."""
    from helixis.company_knowledge.federal_fair_housing import federal_fair_housing_block

    return SYSTEM_PROMPT + "\n\n" + UNTRUSTED_CONTENT_RULE + "\n\n" + federal_fair_housing_block()


@dataclass
class LeadDraft:
    """The generated assist. Empty (signals=[], draft_reply=None) is a valid,
    fully-degraded result — the board renders fine without it."""

    signals: list[str] = field(default_factory=list)
    draft_reply: str | None = None


def _model_name(settings: Settings) -> str:
    """Reasoning model, mirroring ``ActionPlanner._reasoning_model_name``."""
    llm = settings.llm
    models = llm.models or {}
    reasoning = models.get("reasoning") or {}
    return reasoning.get("model") or llm.default_model


_MAX_KNOWLEDGE_CHARS = 4000


def _build_context(lead: LeadRow, messages: list[MessageRow], company_knowledge: str = "") -> str:
    """Compact, deterministic context block for the model.

    The prospect's name, unit, note and messages are fenced: a stranger wrote
    them, and the reply drafted from them goes out under the PM's name. The
    profile is fenced too (it is distilled from uploaded documents and Drive
    titles). It stays the source for concrete answers; the fence only stops it
    from instructing. No agent run is bound on this route, so the fences' taint
    marks are no-ops.
    """
    lines: list[str] = []
    knowledge = (company_knowledge or "").strip()
    if knowledge:
        lines.append("Company facts (authoritative — use to answer concrete questions):")
        lines.append(
            wrap_untrusted(
                knowledge[:_MAX_KNOWLEDGE_CHARS],
                label="company profile, distilled from uploaded documents and Drive",
            )
        )
        lines.append("")
    who = "Owner lead" if lead.kind == "owner" else "Renter lead"
    lines.append(f"{who}, arrived via {lead.source}.")
    lead_lines: list[str] = []
    if lead.name:
        lead_lines.append(f"Name: {lead.name}")
    if lead.unit_label:
        lead_lines.append(f"Interested in: {lead.unit_label}")
    lead_lines.append("Conversation (oldest first):")
    convo = messages or []
    if convo:
        for m in convo:
            speaker = "PM" if m.direction == "outbound" else "Them"
            body = (m.body or "").strip()
            if body:
                lead_lines.append(f"{speaker}: {body}")
    elif lead.note:
        lead_lines.append(f"Them: {lead.note.strip()}")
    lines.append(wrap_untrusted("\n".join(lead_lines), label="the lead's details and messages"))
    return "\n".join(lines)


async def generate_lead_draft(
    settings: Settings,
    *,
    lead: LeadRow,
    messages: list[MessageRow],
    company_knowledge: str = "",
) -> LeadDraft:
    """Generate {signals, draft_reply} for a lead. Never raises — degrades to
    an empty :class:`LeadDraft` on any failure.

    ``company_knowledge`` is the company's standing profile (distilled from its
    uploaded documents). When present the model may answer concrete questions
    (hours / pet policy / fees) from it; empty is fine — the draft stays general.
    """
    try:
        model = _model_name(settings)
        api_key = _api_key_for_model(settings, model)
        kwargs: dict[str, object] = {
            "model": model,
            "messages": [
                {"role": "system", "content": _system_prompt()},
                {
                    "role": "user",
                    "content": _build_context(lead, messages, company_knowledge),
                },
            ],
            "response_format": {"type": "json_object"},
            **sampling_kwargs(model, 0.3),
            "timeout": _TIMEOUT_SECONDS,
        }
        if api_key:
            kwargs["api_key"] = api_key
        response = await litellm.acompletion(**kwargs)
        raw = response.choices[0].message.content
        data, _coerced = coerce_json(raw)
        if not isinstance(data, dict):
            return LeadDraft()
        raw_signals = data.get("signals") or []
        signals = [str(s).strip() for s in raw_signals if isinstance(s, str) and str(s).strip()][
            :_MAX_SIGNALS
        ]
        raw_draft = data.get("draft_reply")
        draft = str(raw_draft).strip() if raw_draft else ""
        return LeadDraft(signals=signals, draft_reply=draft or None)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — assist is never load-bearing
        logger.warning("CRM lead-draft generation failed; degrading gracefully: %s", exc)
        return LeadDraft()
