"""The summarizer must not narrate a row whose enrichment is still in flight.

Measured on the live worker 2026-08-25, three rows in one batch:

    planner claimed   02:13:09.495
    summary claimed   02:13:09.685     <- 190ms later
    row received_at   02:12:30         <- 39s old

The planner and the summarizer are claimed in the SAME poll cycle. For a row
the receiver never enriched, the planner is what WRITES the enrichment (see
``worker.py``'s re-enrich branch) and its Buildium calls take seconds -- so at
summary time ``enrichment`` was still NULL. PR #128's guard was

    enrichment IS NOT NULL OR received_at < now() - interval '15 seconds'

and the row was 39 seconds old, so the wall-clock arm waved it straight
through. The summarizer then wrote, accurately for what it could see:

    "the enrichment didn't return a property, unit, reporter, or description"

onto cards whose enrichment landed seconds later carrying the property name,
the sub-type and three history rows -- and whose SUGGESTED ACTIONS, planned
from that same enrichment, cited them ("history_property shows 2 other tasks",
"a common-area fixture in a MultiFamily property"). The card contradicted
itself: prose saying nothing was known, above actions built from knowing it.

⚠ The trap: PR #128's guard is CORRECT for the path it was written for (the
receiver enriches inline in 1-3s, so 15s is generous). It fails on the path it
was not written for. A wall clock cannot express "has the producer had its
turn"; only the producer's own status can.

``test_pr92_summary_worker.py`` pins the summary clause itself, against the
SQL the function actually sends. This file covers the part that file cannot:
the deliberate ASYMMETRY between the two claims. The planner PRODUCES the
enrichment, so copying the fix onto it would deadlock every row the receiver
did not enrich -- the planner would wait for something only the planner makes.
An asymmetry nothing asserts is an asymmetry the next reader "tidies up".
"""

from __future__ import annotations

from helixis.webhooks import queue


def _claim_summary_sql() -> str:
    """Both query variants inside ``claim_summary_batch``.

    Sliced from the source rather than imported: the queries are f-string
    locals, so there is nothing to import, and reading the file is what makes
    this test see the same text the database does.
    """
    import inspect

    return inspect.getsource(queue.claim_summary_batch)


def _claim_planner_sql() -> str:
    import inspect

    return inspect.getsource(queue.claim_planner_batch)


def _guard_blocks(sql: str) -> list[str]:
    r"""Every ``AND ( ... enrichment IS NOT NULL ... )`` block in the source.

    ⚠ Matched line-wise, not with a lazy ``(.*?)\)``. The condition itself
    contains parentheses -- ``NOT IN ('pending', 'in_progress')`` -- so a lazy
    paren match truncates the block right before the clause under test and the
    assertions fail against the FIXED code. That happened on the first cut.
    """
    blocks: list[str] = []
    lines = sql.splitlines()
    for i, line in enumerate(lines):
        if line.strip() != "enrichment IS NOT NULL":
            continue
        # Collect until the closing paren that sits alone on its own line.
        block: list[str] = []
        for follow in lines[i:]:
            block.append(follow)
            if follow.strip() == ")":
                break
        blocks.append("\n".join(block))
    return blocks


def test_the_summary_claim_has_a_guard_at_all() -> None:
    """A parse that finds nothing would make everything below vacuous."""
    blocks = _guard_blocks(_claim_summary_sql())
    assert len(blocks) == 2, (
        f"expected the guard in both query variants, found {len(blocks)} -- "
        "if a variant was added or removed, this test is no longer covering it"
    )


def test_the_planner_claim_is_deliberately_left_alone() -> None:
    """The planner PRODUCES the enrichment, so it must not wait on it.

    Its own 15s grace covers the receiver's inline write and is correct. This
    pins that the fix was scoped -- copying it onto the producer would deadlock
    every row the receiver did not enrich: the planner would wait for an
    enrichment only the planner can create.
    """
    planner_blocks = _guard_blocks(_claim_planner_sql())
    assert planner_blocks, "planner guard not found -- re-check this test"
    for block in planner_blocks:
        assert "interval '15 seconds'" in block
        assert "planner_status NOT IN" not in block, (
            "the planner claim now waits on its own output; rows the receiver "
            "did not enrich can never be planned"
        )
