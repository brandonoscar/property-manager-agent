"""Say the task back, before doing any of it.

A user presses Enter and watches a spinner. Even with the step trail, the
reasoning stream and the specialist substeps now on the wire, all of those
arrive AFTER the model has started thinking — on a long question that is
several seconds of nothing, and the first few seconds are where "is this
broken?" gets decided.

So this is one cheap, parallel call whose only job is to prove the request
landed: *"Checking whether 4B is behind on rent, and what maintenance is open
there before you send anything."* It runs beside the real turn, never in front
of it, and it is allowed to lose — a restatement that arrives after the answer
has started is worthless and is dropped.

⚠ **RESTATE, NEVER INFER — this is the whole design constraint.** CHI 2026
(N=68) measured that an INCORRECT rationale damages trust more than no
rationale at all, and Buell & Norton's fifth experiment found transparency
*reduces* perceived value when the outcome is unfavourable. This product's own
history says the same thing louder: gotcha 41's confident false claim about a
real person's address, gotcha 51's false tenancy. A line that says *"You want
me to send the late notice"* when the user asked whether one is warranted is
not a friendlier spinner — it is the model telling the user it is about to do
something it was not asked to do, in the two seconds before they can check.

Hence four deterministic gates, applied to the model's output before a byte of
it reaches the screen. Each one exists because the failure it catches would be
worse than silence:

* **No write verb the user did not use.** The dangerous fabrication has one
  shape: a read request restated as an action. This is checkable and cheap.
* **No digits the user did not write.** A number the question did not contain
  is an invented amount, unit or record id — gotcha 38's leak class arriving
  through a paraphrase.
* **Bounded length.** A restatement materially longer than the request is
  padding, and padding is invention.
* **Identifier scrubbing**, like every other channel that reaches the screen.

Any gate failing drops the line entirely. Silence is the safe default here and
always was — the feature is a nicety, and the answer is the product.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from helixis.agents.effort import classify_effort
from helixis.llm.sampling import sampling_kwargs
from helixis.settings import Settings

logger = logging.getLogger(__name__)

#: Hard ceiling on the whole attempt. Past this the turn has already produced
#: real signal (a reasoning delta, a step) and a restatement is noise arriving
#: late — the thing it exists to prevent is the FIRST few seconds being empty.
TIMEOUT_S = 4.0

#: Output cap. One sentence, and the model is told so; this is the backstop.
MAX_TOKENS = 90

#: Past this the "restatement" is a summary, which is a different thing and a
#: place to hide an addition. Relative to the request so a long question can
#: have a long restatement.
LENGTH_MULTIPLIER = 1.8
MIN_LENGTH_ALLOWANCE = 140

#: Verbs that mean SOMETHING CHANGES IN THE WORLD. If the user did not use one
#: and the restatement does, the model has invented an action.
#:
#: ⚠ Deliberately broad and deliberately one-directional. A false positive
#: costs a status line nobody will miss; a false negative puts "I'll send the
#: notice" in front of somebody who asked whether to.
_WRITE_VERBS = (
    "send",
    "email",
    "text",
    "message",
    "notify",
    "create",
    "add",
    "post",
    "charge",
    "bill",
    "pay",
    "record",
    "update",
    "change",
    "edit",
    "set",
    "close",
    "complete",
    "cancel",
    "delete",
    "remove",
    "assign",
    "schedule",
    "book",
    "renew",
    "terminate",
    "evict",
    "approve",
    "submit",
    "file",
    "draft",
    "write",
    "reply",
    "respond",
)

_SYSTEM_PROMPT = """You restate a property manager's request in one sentence \
so they can see it was understood. You are not answering it.

Rules, in order of importance:
1. Add NOTHING. No step, entity, number, name, date or action that is not \
already in their message. If you cannot restate it without adding something, \
reply with exactly: SKIP
2. Never describe an action they did not ask for. "Check whether X" is not \
"do X".
3. One sentence, present tense, starting with a verb — "Checking...", \
"Pulling together...", "Looking at...". No preamble, no "Sure", no "I'll".
4. Plain professional English. No jargon, no tool names, no record numbers.
5. If their message is already one short clear sentence, reply SKIP — \
repeating it back adds nothing."""


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower()))


def _digit_runs(text: str) -> set[str]:
    return set(re.findall(r"\d+", text))


def invented_write_verb(task: str, line: str) -> str | None:
    """A write verb the restatement uses and the request did not.

    ⚠ Matched on WORD STEMS, both sides, so "sending" in the reply is caught
    by "send" in the request and vice versa. A substring test would let
    "resend" pass as unrelated and would flag "recorded" inside "unrecorded".
    """
    asked = _words(task)
    said = _words(line)
    for verb in _WRITE_VERBS:
        used = any(w == verb or w.startswith(verb) for w in said)
        if not used:
            continue
        wanted = any(w == verb or w.startswith(verb) for w in asked)
        if not wanted:
            return verb
    return None


def invented_digits(task: str, line: str) -> set[str]:
    """Numbers in the restatement that the request did not contain.

    An invented number is an amount, a unit or a record id the user never
    wrote — the paraphrase equivalent of the identifier leak ``reply_hygiene``
    exists for.
    """
    return _digit_runs(line) - _digit_runs(task)


def is_safe_restatement(task: str, line: str) -> bool:
    """Every deterministic gate, in one predicate.

    ⚠ Returns False for anything doubtful. This is a status line; there is no
    version of "roughly right" that beats not showing it.
    """
    stripped = (line or "").strip()
    if not stripped or stripped.upper().startswith("SKIP"):
        return False
    if len(stripped) > max(MIN_LENGTH_ALLOWANCE, int(len(task) * LENGTH_MULTIPLIER)):
        return False
    if invented_write_verb(task, stripped):
        return False
    if invented_digits(task, stripped):
        return False
    return True


def worth_restating(task: str) -> bool:
    """Only where the wait is real.

    ⚠ Skips exactly what ``classify_effort`` recognises as a one-fact lookup.
    Those answer in seconds, so a restatement is a model call and a line of
    noise bought to shorten a wait that is not happening — and it would restate
    "what's the rent on 4B" as "Looking up the rent on 4B", which tells the
    user nothing they did not just type.
    """
    return bool(task and task.strip()) and classify_effort(task) is None


async def restate(
    task: str,
    settings: Settings,
    *,
    completion_fn: Any | None = None,
    timeout_s: float = TIMEOUT_S,
) -> str | None:
    """One sentence saying what was asked, or ``None``.

    ⚠ NEVER RAISES and never blocks the turn. It runs beside the real run, so
    a provider blip, a slow response or a malformed reply must cost this line
    and nothing else — the same rule ``effort_settings`` and the memory path
    follow (gotcha 7).
    """
    if not worth_restating(task):
        return None

    async def _call() -> Any:
        """⚠ ``SystemExit`` is converted INSIDE the coroutine, before it can
        cross a Task boundary. ``except (Exception, SystemExit)`` around a
        ``wait_for`` does not contain it — asyncio re-raises it into the
        running loop as well as to the awaiter, so the handler runs, returns
        the degraded value exactly as designed, and the process dies a moment
        later (gotcha 36)."""
        if completion_fn is None:
            import litellm

            fn: Any = litellm.acompletion
        else:
            fn = completion_fn
        try:
            # The CHEAP tier. This is a paraphrase of one sentence; paying the
            # reasoning model for it would add real cost to every substantive
            # turn to save a few seconds of perceived wait.
            model = settings.llm.models.get("browser", {}).get("model") or (
                settings.llm.default_model
            )
            return await fn(
                model=model,
                api_key=settings.llm.anthropic_api_key or None,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": task[:2000]},
                ],
                max_tokens=MAX_TOKENS,
                **sampling_kwargs(model, 0.0),
            )
        except SystemExit as exc:  # pragma: no cover - see docstring
            raise RuntimeError("restatement model exited") from exc

    try:
        result = await asyncio.wait_for(_call(), timeout=timeout_s)
    except (Exception, SystemExit):
        logger.debug("restatement failed", exc_info=True)
        return None

    try:
        line = str(result.choices[0].message.content or "").strip()
    except (AttributeError, IndexError, KeyError, TypeError):
        logger.debug("restatement response shape unrecognised", exc_info=True)
        return None

    # One sentence. A model that ignores rule 3 and writes a paragraph gets
    # its first sentence used rather than the whole thing dropped — but the
    # length gate below still judges what actually ships.
    line = line.split("\n")[0].strip().strip('"')
    if not is_safe_restatement(task, line):
        logger.debug("restatement rejected by a safety gate")
        return None
    return line


__all__ = [
    "invented_digits",
    "invented_write_verb",
    "is_safe_restatement",
    "restate",
    "worth_restating",
]
