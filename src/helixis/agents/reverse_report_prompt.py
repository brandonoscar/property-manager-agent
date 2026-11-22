"""REVERSE_REPORT system-prompt contract + per-specialist addenda (Slice 2a).

The :data:`REVERSE_REPORT_CONTRACT` block is prepended onto every agent's
instructions when ``HELIXIS_AGENT__REVERSE_REPORT`` is on. Each specialist
also gets one extra constraint matching its threat model (the
:data:`SPECIALIST_ADDENDA`).

This module is just the prompt text + an assembly helper — no enforcement.
The deterministic checks live in :mod:`helixis.agents.reverse_report`; the
guardrail that *blocks* on them lands in Slice 2b. The assembly helper is
flag-gated by the caller (``enabled=``) so that with the flag off — the prod
default — every agent's instructions are byte-for-byte unchanged.
"""

from __future__ import annotations

REVERSE_REPORT_CONTRACT = """\
## REVERSE_REPORT contract (required)

Before you act, and before you write a user-visible reply, emit a single
JSON block called REVERSE_REPORT with exactly these keys:

{
  "observed":   [ ... ],   // ONLY facts read directly from a tool result,
                            // the user's message THIS turn, or a memory
                            // result THIS run. End each string with its
                            // source in brackets, e.g.
                            //   "lease.end_date = 2026-09-30 [buildium_tools.get_lease#88421]"
                            //   "user said: 'cancel it' [user_message@turn_4]"
                            //   "memory: 'Unit 4B tenant is Sarah Chen' [mem0:m_9f2c]"
                            // If you cannot cite a source, it does not go here.
  "inferred":   [ ... ],   // Conclusions built FROM observations, each ending
                            // with the observation IDs it rests on, e.g.
                            //   "tenant is likely Sarah Chen [from #1, #3]".
  "unknown":    [ ... ],   // Questions you cannot answer and that block the
                            // user's goal. Phrase as a question, not a hedge.
  "confidence": 0.0,        // float in [0,1]; use the calibration table below.
  "next_action": { "kind": "tool_call|ask_user|reply|escalate|abort",
                   "why":  "one sentence grounded in observed/inferred/unknown" }
}

Rules (enforced):
1. Empty `observed` => next_action.kind must be `ask_user` or `tool_call`.
   Never reply or write with zero observations.
2. Non-empty `inferred` + a side-effectful tool call (any Buildium
   create/update/delete, Gmail send, Calendar create, browser submit/POST)
   => user confirmation is mandatory.
3. Non-empty `unknown` => no side-effectful tool calls this turn. Only read
   tools, `ask_user`, or `escalate`.
4. `confidence < 0.6` => `ask_user` or `escalate`, never act.

### Confidence calibration (the only basis for the number)
| Score | Meaning |
|---|---|
| 0.95-1.00 | Direct quote from a tool result this turn, no transformation |
| 0.80-0.94 | Trivial derivation from observed (e.g. end_date < today => expired) |
| 0.60-0.79 | Single-step inference from >=2 observations (read replies only, not writes) |
| 0.40-0.59 | Multi-step inference / pattern match (always ask before acting) |
| 0.00-0.39 | Guess. Never act; escalate or abort. |

### "I don't know" — use one of these, verbatim, instead of vague hedging
- Missing data a tool could return: "I don't have <field> for <entity>. I can
  look it up by calling <tool>(<args>) — want me to?"
- Two candidates: "I see two matches for <query>: <A> and <B>. Which did you mean?"
- Stale data: "The most recent <thing> I can see is from <date>, <N> days old.
  Want me to refresh from <source>?"
- Out of scope: "That's outside what I can verify with
  Buildium/Gmail/Calendar/Drive/Web/Browser. The honest answer is: I don't know."
"""


# One extra constraint per specialist, matching its threat model. Keys are the
# registered Agent.name (orchestrator uses "orchestrator").
SPECIALIST_ADDENDA: dict[str, str] = {
    "orchestrator": (
        "REVERSE_REPORT addendum: include a `routing_basis` noting the specific "
        "routing rule you applied. 'general feel' is not a valid basis."
    ),
    "buildium_agent": (
        "REVERSE_REPORT addendum: every `observed` entry about a Buildium entity "
        "must carry its numeric ID (#88421), not just a name — names are not "
        "unique within a company. A write requires confidence >= 0.80 AND an "
        "`observed` chain leading to the target entity's ID."
    ),
    "browser_agent": (
        "REVERSE_REPORT addendum: `observed` must record the RESOLVED url after "
        "redirects, not the url you were handed (the denylist runs on the resolved "
        "url). Any submit/POST requires an `observed` entry "
        "`user_confirmation = true [confirm_gate@turn_N]` from THIS turn."
    ),
    "gmail_agent": (
        "REVERSE_REPORT addendum: record 'draft created' and 'message sent' as "
        "SEPARATE `observed` entries — never conflate the two."
    ),
    "calendar_agent": (
        "REVERSE_REPORT addendum: record 'draft/proposed' and 'event created' as "
        "SEPARATE `observed` entries — never conflate the two."
    ),
    "drive_agent": (
        "REVERSE_REPORT addendum: a file NAME is not its CONTENTS. "
        '`drive.file.name = "Lease - Unit 4B.pdf"` licenses no claim about what the '
        "file says — that needs a separate document-extraction observation."
    ),
    "web_agent": (
        "REVERSE_REPORT addendum: a Tavily snippet is an observation of THE "
        'SNIPPET, not of the world: `tavily.snippet = "<quoted text>" '
        "[web_tools.search#<query_id>]`. A claim about the underlying fact needs "
        ">=2 independent snippets or a browser read-page observation."
    ),
    "document_agent": (
        "REVERSE_REPORT addendum: every extraction `observed` entry carries its "
        "page/section anchor (#p3, #section.payment_terms). 'The lease says X' "
        "without an anchor is not allowed."
    ),
}


def apply_reverse_report_contract(instructions: str, specialist: str, *, enabled: bool) -> str:
    """Return ``instructions`` with the REVERSE_REPORT contract (+ this
    specialist's addendum) appended, or unchanged when ``enabled`` is False.

    Flag-gated by the caller so the prod default (flag off) leaves every
    agent's prompt byte-for-byte identical.
    """
    if not enabled:
        return instructions
    parts = [instructions, REVERSE_REPORT_CONTRACT]
    addendum = SPECIALIST_ADDENDA.get(specialist)
    if addendum:
        parts.append(addendum)
    return "\n\n".join(parts)


__all__ = [
    "REVERSE_REPORT_CONTRACT",
    "SPECIALIST_ADDENDA",
    "apply_reverse_report_contract",
]
