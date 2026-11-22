"""Wire-format models for the action surface the planner emits.

Persisted into ``buildium_events.action_surface`` and consumed
verbatim by the Phase A.2 frontend. Both layers validate against
these models so a malformed LLM response is caught at the planner
boundary, not at render time.

Phase A.2.5 — ``schema_version`` is bumped to ``2`` and every
``ProposedAction`` may now carry a ``first_followup`` question
that powers the conversational click-chain in the Inbox. The
extension is purely additive: a v1 surface (no ``first_followup``,
or ``schema_version == 1``) still validates here so the two
production rows that already shipped under v1 keep rendering.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ToolParameterSchema(BaseModel):
    """One named parameter on a tool. The LLM fills these in; the
    frontend renders them as either pre-filled form fields (when the
    user clicks the action) or read-only confirmation lines."""

    name: str
    type: str
    """One of ``"string"``, ``"integer"``, ``"date"``, ``"enum"``."""

    required: bool = True
    description: str | None = None
    enum: list[str] | None = None
    """Populated when ``type == 'enum'`` to constrain the LLM."""

    max_length: int | None = None


# ── Phase A.2.5 — follow-up question schema ──────────────────────────


class FollowupOption(BaseModel):
    """One choice the user can pick on a click-chain follow-up question.

    Each option carries a ``parameter_patch`` that is **deep-merged**
    into the action's parameters when picked — only the keys this
    option actually changes from the action's defaults. ``needs_followup``
    flags whether picking this option should fan out to a deeper
    on-demand LLM call (``POST /actions/{i}/followup``) for the next
    question, rather than advancing straight to confirmation.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=80)
    """Short button-label-grade phrase. e.g. 'Today between 1-4pm'."""

    description: str | None = Field(default=None, max_length=160)
    """Optional one-line context shown under the label."""

    parameter_patch: dict[str, Any] = Field(default_factory=dict)
    """Partial dict deep-merged into the action's parameters when picked.
    MUST contain only the fields this option changes — never the full
    parameter set."""

    needs_followup: bool = False
    """If true, picking this option triggers an on-demand LLM call to
    generate a deeper question instead of advancing to confirmation."""

    followup_hint: str | None = Field(default=None, max_length=240)
    """Free-form hint passed to the deeper LLM call so it knows what
    to ask next. Required when ``needs_followup`` is true; validated
    by the model-level invariant below."""

    @model_validator(mode="after")
    def _followup_hint_present_when_needed(self) -> FollowupOption:
        if self.needs_followup and (self.followup_hint is None or not self.followup_hint.strip()):
            raise ValueError("followup_hint is required when needs_followup is True")
        return self


class FollowupQuestion(BaseModel):
    """A single multiple-choice question on the click-chain."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=5, max_length=140)
    """The actual question text, e.g. 'When can the plumber arrive?'."""

    options: list[FollowupOption] = Field(min_length=2, max_length=5)
    """Mutually-exclusive choices. The UI also offers a free-text
    'Other…' option that bypasses these and writes a freeform_note."""

    allow_freeform: bool = True
    """If false, only the canned options are accepted (rare; use only
    when typing would produce invalid Buildium data)."""


class DoneSignal(BaseModel):
    """LLM-emitted signal that the follow-up chain has gathered enough
    context to execute. Surfaced by ``/actions/{i}/followup`` when the
    model determines no further questions are needed."""

    model_config = ConfigDict(extra="forbid")

    done: bool = True
    merged_parameters: dict[str, Any] = Field(default_factory=dict)
    """The full resolved parameter set the executor should hand to
    the handler. The frontend deep-merges this on top of the action's
    base parameters before posting to ``/execute``."""

    @field_validator("done")
    @classmethod
    def _done_must_be_true(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("done must be exactly True on a DoneSignal")
        return value


class ProposedAction(BaseModel):
    """A single button on the webhook detail card.

    The LLM populates ``tool_id`` (must match a key in TOOL_CATALOG),
    ``intent`` (a short button-label-grade sentence), and a parameter
    dict matching the tool's schema.

    Phase A.2.5: ``first_followup`` is the pre-baked next question for
    the click-chain. ``None`` means the action goes straight to the
    confirmation step on click. Always optional — v1 surfaces never
    set it.

    PR #99 — added optional ``type`` / ``confidence`` / ``primary_action``
    fields so the planner V2 prompt (gated by HELIXIS_PLANNER_INTENT_V2)
    can label suggestions as ``information_need`` vs ``bonus`` and
    carry a concrete primary action shape (label + kind + draft) for
    the OFFER / QUESTION card render. ``rationale`` / ``triggers`` /
    ``alternative`` round out the "Why this?" reasoning fields. All
    new fields are optional; back-compat with V1 surfaces is
    preserved.
    """

    model_config = ConfigDict(extra="forbid")

    tool_id: str
    label: str
    intent: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    first_followup: FollowupQuestion | None = None
    """Pre-baked next question for the click-chain. None = action
    executes immediately on click after the confirmation card."""

    # PR #99 — V2 fields. All optional so V1 outputs still validate.
    type: str | None = None
    """``information_need`` | ``bonus`` — the only two categories
    the V2 prompt is allowed to emit. None on V1 / legacy outputs."""

    confidence: str | None = None
    """``high`` | ``medium`` | ``low`` — drives the OFFER vs
    QUESTION card shape on the frontend. ``high`` confidence renders
    as a primary-action OFFER; ``medium``/``low`` render as a
    QUESTION with path chips."""

    primary_action: PrimaryAction | None = None
    """Concrete next button shape — label, kind, optional target,
    optional draft body for messages. The frontend's OfferCard
    primary button reads ``label``; messages with a ``draft`` field
    pre-fill the inline editor."""

    rationale: str | None = None
    """One-sentence "why now, why this" grounded in a fact from
    the event. Renders below the intent line with an iris-dot
    prefix on the suggestion card (PR #98)."""

    triggers: list[str] | None = None
    """1-3 specific event facts that justified this suggestion.
    Renders inside the "Why this?" reasoning panel (PR #98)."""

    alternative: str | None = None
    """The option the planner considered and rejected, one phrase.
    Renders inside the "Why this?" reasoning panel (PR #98)."""


class PrimaryAction(BaseModel):
    """PR #99 — the concrete next-button shape for a V2 suggestion.

    Drives the OFFER card's primary button on the frontend.
    ``kind`` chooses the icon/affordance the card renders;
    ``draft`` is the pre-filled message body for ``send_message`` /
    ``create_note`` so the inline editor opens to ready-to-send
    copy instead of a blank box.

    ``target`` names the recipient when applicable (tenant / owner /
    vendor / internal) — the card shows "Send to Jimmy" rather than
    just "Send"."""

    model_config = ConfigDict(extra="forbid")

    label: str
    """Button label, e.g. "Ask Jimmy". 1-60 chars on the wire."""

    kind: str
    """One of ``send_message`` | ``create_note`` | ``create_task``
    | ``schedule`` | ``request_info`` | ``freeform``. The frontend
    falls back to a generic primary-button render for unknown
    kinds so backend can add more without breaking the inbox."""

    target: PrimaryActionTarget | None = None
    """Who the action goes to. Optional — bonus suggestions like
    "drop an internal note" don't have a target."""

    draft: str | None = None
    """Pre-drafted message body for ``send_message`` / ``create_note``.
    None for other kinds. The frontend's "Edit before sending"
    pulls this into the inline editor."""


class PrimaryActionTarget(BaseModel):
    """PR #99 — recipient of a primary action."""

    model_config = ConfigDict(extra="forbid")

    kind: str  # ``tenant`` | ``owner`` | ``vendor`` | ``internal``
    name: str | None = None  # Human-readable name to show in the button


# Forward-ref resolution: ProposedAction references PrimaryAction
# which is defined below it. Pydantic v2 handles this via
# ``model_rebuild`` at module import time.
ProposedAction.model_rebuild()


class ActionSurface(BaseModel):
    """The full LLM-generated response the frontend renders.

    ``summary`` is a 1-2 sentence read of the situation that references
    concrete details from the enriched event (tenant name, unit, the
    specific issue) — the planner system prompt is strict about this
    so generic boilerplate is rejected at validation time.

    ``actions`` is between 0 and 4 entries — PR #99 relaxed the
    minimum from 1 to 0 because the V2 prompt is allowed to emit
    an empty array when it has no genuine ``information_need`` or
    ``bonus`` suggestions to make. Empty array is honest; padding
    with fake "Categorize this task" suggestions is not.

    ``freeform_placeholder`` is what the action surface input field
    suggests the user could type ("e.g. 'schedule plumbing inspection
    for next Tuesday'"). The placeholder is context-aware.

    ``schema_version`` is ``2`` for surfaces that may carry
    ``first_followup``. v1 surfaces (the two rows currently in prod)
    still parse here — the new fields are optional.

    PR #99 — ``signals`` is the optional planner-supplied list of
    facts the agent noticed about the event. When set, the frontend
    renders these in the "WHAT HELIXIS NOTICED" section verbatim
    (PR #98). When None, the frontend derives bullets from
    enrichment via ``buildAgentNoticed``.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 2

    summary: str = Field(min_length=10)
    # Cap raised 4 → 6 so the planner can render a genuinely variable
    # number of action cards — 1-2 for a simple event, up to 6 when the
    # signals (urgency, history, multiple stakeholders) justify it. The
    # frontend renders one card per action, so this is the real ceiling
    # on "how adaptive does the inbox look".
    actions: list[ProposedAction] = Field(min_length=0, max_length=6)
    freeform_placeholder: str = Field(min_length=5)
    signals: list[str] | None = None
    # UI refinement #2b — sparse emphasis. A short list of EXACT
    # substrings of ``summary`` the inbox highlights in Violet
    # (tenant/owner name, property/address, the core request — e.g.
    # "renew her lease"). The frontend wraps only these phrases;
    # everything else stays default body text. Optional + capped so the
    # emphasis stays sparse; rows written before this field still
    # deserialize (None → plain summary, no highlight).
    summary_highlights: list[str] | None = Field(default=None, max_length=5)
    # PR #107 — version tag stamped at planner-write time. The worker
    # re-plans rows whose ``prompt_version`` predates the current
    # in-code ``PLANNER_PROMPT_VERSION``, so bumping the constant in a
    # future PR automatically drains old V1-shape suggestions
    # ("Re-categorize as Maintenance" / "Raise priority") and replaces
    # them with current-prompt output. Optional on read so existing
    # rows in prod (which were written without this field) still
    # deserialize cleanly; they'll be treated as stale on the next
    # tick and re-planned.
    prompt_version: str | None = None

    @field_validator("actions")
    @classmethod
    def _every_action_has_a_label(cls, value: list[ProposedAction]) -> list[ProposedAction]:
        for action in value:
            if not action.label or not action.label.strip():
                raise ValueError("every action must have a non-empty label")
            if not action.intent or not action.intent.strip():
                raise ValueError("every action must have a non-empty intent")
        return value
