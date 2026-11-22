"""Did a turn give the user anything — the one answer to that question.

A turn's prose lands in ``orchestrator_runs.final_text``. A CARD does not:
``show_table`` and friends publish straight to the SSE bus, so a turn can put a
full rent roll on screen and leave that column empty (gotcha 56 measured it on
five of six questions). Reading ``final_text`` alone therefore calls a
card-heavy turn blank, and reading "any tool ran" calls a deadline death
answered — and the two consumers below need the same answer to the same
question, in opposite directions:

* **Billing** (`billing/gate.py::_turns_used`) — may we charge for this turn?
  Over-counting bills somebody for our own timeout.
* **Cost drift** (`evals/cost_drift.py::answered`) — does this run enter the
  latency sample? Under-counting drops the fast card-only turns and biases the
  median.

⚠ THIS MODULE EXISTS BECAUSE THE RULE HAD TWO HOMES AND THE WRONG ONE WAS
DECIDING MONEY. `answered()` was correct; the billing query wrote its own
weaker version — `jsonb_array_length(tool_calls) > 0`, i.e. ANY tool — while
its docstring cited gotcha 56 as the rule it implemented. So every deadline
death that made a specialist consult and returned nothing was billed, which is
the exact population gotcha 37 measured at 22% of runs. Gotcha 25/67's fan-out:
a rule documented at its first caller, invisible to the second.

⚠ WHAT THIS SET CANNOT SEE, stated rather than implied. A card queued inside a
SPECIALIST sub-run still reaches the user — the nested run inherits its run's
queues (``visual_tools.RunEmissions``) — but ``orchestrator_runs.tool_calls``
records the ORCHESTRATOR's calls only (gotcha 61). So a turn whose only output
was a table rendered by ``buildium_agent``, with no orchestrator prose, reads
as blank here. That is under-counting, which for billing is the
customer-favouring direction and the one `billing/gate.py` takes everywhere
(its rule 1), and for the latency sample it drops a run rather than inventing
one. Over-counting has no such safe reading, which is why the bar is a NAMED
tool rather than "something ran".

⚠ ``request_details`` (a form) and ``export_table`` (a CSV link) both put
something on the user's screen and are deliberately NOT here — recorded as a
decision, not an oversight. Adding them widens the cost-drift sample as well as
the billing count, which moves a measurement baseline; that is worth doing on
purpose with the numbers in front of you, not as a side effect of a billing
fix. Leaving them out under-counts, which is the safe direction for both.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Tools whose output reaches the user without passing through ``final_text``.
#:
#: ⚠ Kept in step with ``agents/visual_tools.py`` by
#: ``tests/unit/test_billable_turn.py``, which walks that module's
#: ``@function_tool`` definitions — a hand-typed list is how gotcha 99's
#: ``disclosure`` branch and gotcha 122's three untrapped dialogs were both
#: missed, and the failure mode here is "silently permits more" in one
#: direction and "silently bills more" in the other.
CARD_TOOLS = frozenset(
    {
        "show_table",
        "show_chart",
        "show_detail_card",
        "show_checklist",
        "suggest_next_steps",
        "show_schedule",
        "show_email_draft",
    }
)


def turn_answered(*, has_prose: bool, tool_names: Iterable[str]) -> bool:
    """Did this turn put anything in front of the user?

    ``has_prose`` is a non-empty ``final_text``; ``tool_names`` is what the
    orchestrator called. Keyword-only so a caller cannot transpose them — the
    two arguments are both truthy-ish and swapping them silently answers the
    opposite question for every run.
    """
    return has_prose or any(name in CARD_TOOLS for name in tool_names)
