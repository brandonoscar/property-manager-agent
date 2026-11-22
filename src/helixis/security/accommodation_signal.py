"""Spot an inbound message that is a REASONABLE-ACCOMMODATION request, before
the planner drafts a reply treating it as a pet-policy matter.

This product defends fair housing thoroughly on the OUTPUT side and not at all
on the INPUT side. `buildium_post_charge` refuses a pet fee for an assistance
animal in its own docstring; `federal_fair_housing_block()` is injected into
the write-holding specialist because golden 419 measured it posting the charge;
`fair_housing_gate.check_draft` scans drafted replies for steering. Every one
of those reads text **we** produced.

Nothing read what the resident sent. `webhooks/categorization.py` is a static
event-name→category map whose own docstring says "the agent does NOT classify
at LLM-time"; `email_triage` stamps every ingested email `category: "Email"`;
the planner prompt contains no accommodation vocabulary at all. So:

    "My doctor prescribed an emotional support animal — can you waive the
     pet deposit?"

arrives as an ordinary message, and the drafted reply is about the pet policy.
Under the FHA that message starts an interactive process and the deposit is not
chargeable. Gotcha 86's sentence, pointed at the other direction: a rule
enforced where we WRITE protects the repository; only a rule where we READ
protects the person who sent it.

⚠ **THE ERROR ASYMMETRY IS INVERTED RELATIVE TO `check_draft`, and that drives
every choice here.** There, a false positive fires on the best reply in the
corpus and gets the guard switched off (gotchas 52/86), so it is tuned tight.
Here a false negative is an FHA complaint and a false positive is one advisory
line telling a manager to treat a message carefully. So this leans permissive —
but NOT unboundedly, because a caveat that fires when it is not true trains its
reader to discount the ones that are (gotchas 57/93). The line that survives
contact with real messages is to anchor on the **BASIS**, never on the request:
"can I have a dog?" carries none and must stay silent.

⚠ **INBOUND TEXT ONLY.** Callers pass `task.title` / `task.description`, which
are authored by the resident or applicant. Pointing this at a draft would flag
our own compliant refusal for quoting the vocabulary in order to decline — the
414/416 false-positive class this repo has recorded four times.

Deterministic, pure, and ADVISORY. Per #271 nothing here gates a run: it adds
an operator-authored note the planner is told how to read.
"""

from __future__ import annotations

import re

#: Reason code for the note and for anything that later counts these.
ACCOMMODATION_SIGNAL = "possible_accommodation_request"

# ── Tier 1: terms that are ALREADY the request ──────────────────────────────
#
# These are terms of art. They essentially do not occur outside an
# accommodation context, so they need no request frame: "I have a service
# animal" is not a question and still starts the interactive process — waiting
# for somebody to phrase it as a question is how the duty gets missed.
_STRONG_BASIS = re.compile(
    r"\b(reasonable\s+accommodation|reasonable\s+modification|"
    r"accommodation\s+request|request\s+an?\s+accommodation|"
    r"assistance\s+animal|emotional\s+support\s+(animal|dog|cat)|"
    r"service\s+(animal|dog)|comfort\s+animal|"
    r"fair\s+housing(\s+act)?|"
    r"americans\s+with\s+disabilities(\s+act)?)\b",
    re.I,
)

#: ⚠ CASE-SENSITIVE, and deliberately. Under IGNORECASE "esa" matches the
#: Spanish word, appears inside identifiers, and fires on ordinary text — the
#: same trap gotcha 38 records for a two-letter state code and gotcha 42 for a
#: bare "drive". The abbreviation is written upper-case when it means this.
_ESA = re.compile(r"\bESA\b")

# ── Tier 2: a basis that needs a request beside it ───────────────────────────
#
# "The wheelchair ramp out front is broken" is a maintenance request and must
# NOT read as an accommodation request — flagging it would be wrong and is
# exactly the noise that gets an advisory ignored. The same words paired with
# asking for an exception or a change to the unit are a different message.
_WEAK_BASIS = re.compile(
    r"\b(disabilit(y|ies)|disabled|handicap(ped)?|impairment|"
    r"my\s+(doctor|physician|therapist|psychiatrist|counselor|counsellor)|"
    r"medical\s+(condition|need|reason|letter|documentation)|"
    r"doctor'?s\s+(note|letter)|"
    # ⚠ ``\w*`` on every one of these, not a bare stem. ``issue`` followed by
    # ``\b`` does NOT match "mobility issues" — the ``s`` is a word character,
    # so the boundary fails and the commonest phrasing of all silently misses.
    # Found by the parametrised case, not by reading the pattern.
    r"mobility\s+(issue\w*|impair\w*|device\w*|aid\w*)|wheelchair|walker|"
    r"chronic\s+(illness|pain|condition)|mental\s+health)\b",
    re.I,
)

_REQUEST_FRAME = re.compile(
    r"\b(request(ing|ed)?|asking\s+for|ask\s+for|need\s+(you\s+)?to|"
    r"would\s+like|i\s+need|can\s+you|could\s+you|please\s+(allow|approve|waive|"
    r"install|add|change|modify|permit)|"
    r"waive|waiver|exception|exempt|accommodate|accommodation|"
    r"install|modif(y|ication)|grab\s+bar|ramp\s+installed|"
    r"transfer\s+to|move\s+to|closer\s+(unit|parking|space)|"
    r"reserved\s+parking|accessible\s+(unit|parking|space|entrance))\b",
    re.I,
)


def accommodation_basis(*parts: str | None) -> str | None:
    """The basis on which this inbound text reads as an accommodation request,
    or ``None``.

    Accepts several fields (title and body, typically) and judges them
    together: a subject line of "ESA request" with a body that never repeats
    the term is one message, not two.

    Returns a short human phrase for the note — never a boolean — so the
    operator-facing line can say WHY it fired. A flag a reader has to decode is
    one they decode wrongly (gotcha 94).
    """
    # ⚠ COERCED, and the first cut was inconsistent: it tested ``str(p).strip()``
    # and then joined the raw ``p``, so anything that is not already a ``str``
    # raised `TypeError` out of ``join``. That is not a hypothetical — it took
    # down an unrelated planner test whose ``enriched`` is a MagicMock, and on
    # the live path an advisory signal must never be the reason a card fails to
    # render (gotcha 92's posture, and gotcha 7's "an enhancement is never
    # load-bearing").
    text = "\n".join(str(p) for p in parts if p is not None and str(p).strip())
    if not text.strip():
        return None

    if _STRONG_BASIS.search(text) or _ESA.search(text):
        return "assistance animal, accommodation or fair-housing language"

    if _WEAK_BASIS.search(text) and _REQUEST_FRAME.search(text):
        return "a disability or medical basis alongside a request for a change or exception"

    return None


def accommodation_floor_signal(*parts: str | None) -> str | None:
    """The deterministic "What Occupella noticed" line, or ``None``.

    ⚠ A FLOOR, for the reason the hazard line in `comms_triage` is one: the
    planner is an LLM call that times out, hits a spend ceiling or returns
    invalid JSON, and the card still renders from the deterministic surface.
    If the only place this signal lived were the planner prompt, the messages
    that most need it would lose it exactly when the model was unavailable —
    gotcha 92's shape, where the salvage path was blind to the runs it existed
    for.

    Worded for a property manager reading an Inbox card, not for the model.
    """
    basis = accommodation_basis(*parts)
    if basis is None:
        return None
    return (
        "This may be a REASONABLE ACCOMMODATION request under the Fair Housing "
        f"Act — it carries {basis}. Treat it as one rather than as a routine "
        "pet, parking or policy matter: it starts an interactive process, an "
        "assistance animal cannot be charged a pet fee or deposit, and a person "
        "should review it before anything is sent."
    )


#: The note the planner is shown. OPERATOR-AUTHORED and therefore deliberately
#: OUTSIDE the untrusted fence — it is our instruction about the message, not
#: part of the message. Putting it inside would make it something a resident
#: could appear to write, which is the ingress golden 435 exists for.
#:
#: ⚠ It states the DUTY rather than the answer. Telling the planner what the
#: law requires of the manager is a fact; telling it to grant the request would
#: be advice this product is not qualified to give, and telling it to refuse
#: would be the violation.
def accommodation_notice(basis: str) -> str:
    return (
        "\n\nOCCUPELLA NOTE (from the system, not from the sender) — "
        f"this message carries {basis}, so it may be a REASONABLE ACCOMMODATION "
        "request under the Fair Housing Act.\n"
        "- Treat it as one: it starts an interactive process and has its own "
        "timeline. Do NOT answer it as a routine pet, parking or policy matter.\n"
        "- Do NOT draft a reply that charges or quotes a pet fee, pet deposit "
        "or pet rent for an assistance animal — those are not chargeable.\n"
        "- Do NOT deny, approve or promise an outcome on the company's behalf, "
        "and do not ask for a diagnosis or medical records.\n"
        "- Say plainly in the card that this looks like an accommodation "
        "request so a person reviews it before anything is sent."
    )
