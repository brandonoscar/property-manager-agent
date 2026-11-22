"""One company's webhook flood must not stop everybody else's Inbox.

MEASURED on helixis-test 2026-09-10, deliberately cross-tenant because queue
fairness is a property of the DISPATCHER and not of one company's books
(gotcha 47): company X enqueued **185 events in nine minutes** on
2026-07-25 (02:30-02:38, peaking at 38 in one minute). Against
``worker_batch_size = 10`` and ``worker_poll_interval_seconds = 2.0``, and a
claim that read ``ORDER BY received_at ASC … LIMIT $1`` across **all**
companies, a second company's event arriving at 02:35 sat behind roughly 150
rows belonging to somebody else.

⚠ **The expensive half is the planner and the summarizer, not the dispatch.**
``claim_planner_batch`` and ``claim_summary_batch`` used the identical global
ordering, and every row they claim costs an LLM call. So a company syncing its
portfolio did not merely delay other companies' cards — it spent the shared
model budget first, in arrival order, with no ceiling on its share.

The queue is EMPTY today (0 pending across all three companies, 599 rows
lifetime), so this is pre-launch hardening and not a live incident. The burst
that makes it real has already happened once.

**The fix is round-robin by company, oldest-first within a company.**
``row_number() OVER (PARTITION BY company_id ORDER BY <col>)`` ranks each
company's queue independently and the batch is then ordered by that rank, so a
batch of ten drawn from a flood plus two quiet companies serves the quiet ones
in positions 2 and 3 instead of never.

VERIFIED BY EFFECT before any code was written, and the CONTROL is what makes
it evidence — the OLD ordering was computed in the SAME query, because a probe
showing only the new order has measured Postgres agreeing with itself. Seeded
with the real 2026-07-25 shape (company A bursting, B and C sending one each
mid-burst), the first batch of ten came back:

    old_global_fifo = AAAAAAAAAA
    new_fair_order  = ABCAAAAAAA

⚠ **AND THE PROPERTY THAT MAKES IT SAFE TO SHIP: with ONE company the two
orderings are byte-for-byte identical** (ids ``1..10`` under both). ``row_number``
is assigned in ``<col>`` order within the partition, so ranking by it and then
by ``<col>`` reproduces plain ``<col>`` ordering exactly when there is one
partition. A single-tenant install, and every uncontended queue, is unchanged —
this can only alter the order in which two companies' rows interleave.

⚠ **A WINDOW FUNCTION AND ``FOR UPDATE`` CANNOT SHARE ONE SELECT.** The obvious
one-line fix — appending the ``row_number()`` to the existing claim — is refused
by Postgres outright::

    0A000: FOR UPDATE is not allowed with window functions

Probed against the live database rather than recalled (gotcha 79: an identifier,
or a remembered rule, is not evidence of what the server does). Hence the
two-CTE shape below: rank WITHOUT a lock, then lock the chosen ids by primary
key. Anyone "simplifying" this back into one SELECT will get that error at
runtime, on the worker, in production.

⚠ **The LIMIT now lands BEFORE the lock, so a contended batch can under-fill.**
In the old single-SELECT shape ``SKIP LOCKED`` skipped a locked row before it
counted toward the ``LIMIT``, so the batch always filled. Here the ten ids are
chosen first and any that a sibling worker holds are then skipped, returning
fewer. That is safe and self-correcting — the next poll is 2s away and the rows
are still pending — and it cannot happen at all on today's deploy, which runs a
single ``helixis-webhooks-worker``. It is written down because it is a real
difference, and a difference discovered in production is worse than one read
here.

⚠ **The ranking scans the pending set**, which is the set that grows during
exactly the flood this exists for. The partial indexes
(``buildium_events_queue_idx``, ``…_planner_pending_idx``, ``…_summary_pending_idx``)
keep the read to pending rows only, and sorting even a hundred thousand of them
is far inside a 2s poll interval. Revisit if a queue ever holds enough rows for
the sort to approach that budget — the trigger is a measurement, not a date.

⚠ **``buildium_events.company_id`` is NOT NULL** — checked in
``information_schema`` rather than assumed, because a nullable partition key is
the thing that would quietly break this. Should a future claim table allow NULL,
SQL puts every NULL in ONE partition, which is the safe direction: unattributed
rows queue behind each other instead of each taking a round-robin slot and
starving real companies.

**VERIFIED BY EFFECT against helixis-test 2026-09-10**, running the REAL
statement this module generates (pasted verbatim, ``$1`` → literal ``10``, since
a ``DO`` block takes no bind parameters) against the REAL table, seeded with the
measured burst shape — inside a block that RAISES at the end, so it rolled back
by construction (gotcha 81). Residue re-checked at 0 (599 rows before and after,
0 probe rows, 0 rows left ``claimed_by`` the probe)::

    CONTROL (old global FIFO) served = AAAAAAAAAA
    THIS MODULE               served = AAAAAAAABC

Same seed, same table, same UPDATE — only the ordering differs. The control is
what makes that evidence; the fix's line alone would have measured Postgres
agreeing with itself.
"""

from __future__ import annotations

#: Alias for the locked table inside the claim CTE. ``FOR UPDATE OF`` needs a
#: real table alias — the ranking CTE is not lockable — so this name is
#: load-bearing rather than decorative.
_LOCK_ALIAS = "b"


def fair_claim_cte(*, table: str, where: str, order_column: str) -> str:
    """The ``WITH ranked …, claimed …`` prefix of a round-robin claim.

    ``$1`` is the batch size; the caller's own placeholders continue from ``$2``,
    which is what every existing call site already assumes.

    The returned SQL ends with the ``claimed`` CTE closed, so a caller appends
    its ``UPDATE … FROM claimed WHERE <t>.id = claimed.id RETURNING …`` exactly
    as it did against the old single-CTE form.

    ⚠ ``table`` and ``order_column`` are interpolated. Every caller passes a
    literal or an allow-listed name (``_assert_planner_table``); nothing
    user-supplied reaches this function, and adding a caller that breaks that is
    the thing ``test_queue_fairness`` exists to catch.
    """
    a = _LOCK_ALIAS
    return f"""
        WITH ranked AS (
            SELECT id
              FROM (
                  SELECT id,
                         {order_column} AS _fair_order,
                         row_number() OVER (
                             PARTITION BY company_id ORDER BY {order_column}
                         ) AS _fair_rank
                    FROM {table}
                   WHERE {where}
              ) _fair
             -- Round-robin: every company's oldest row, then every company's
             -- second, and so on. With one company this is plain
             -- `ORDER BY {order_column}` (proven by effect — see the module
             -- docstring), so an uncontended queue is untouched.
             ORDER BY _fair_rank, _fair_order
             LIMIT $1
        ),
        claimed AS (
            SELECT {a}.id
              FROM {table} {a}
              JOIN ranked ON {a}.id = ranked.id
               FOR UPDATE OF {a} SKIP LOCKED
        )
    """  # noqa: S608 — see the docstring; callers pass literals / allow-listed names


__all__ = ["fair_claim_cte"]
