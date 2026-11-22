"""Chat session history persistence (PR #71, replay shape fixed in PR #72).

Reads and writes turns from ``public.helixis_chat_messages`` so the
orchestrator can pass the prior conversation into ``Runner.run_streamed``,
giving the model real cross-turn memory instead of starting cold every
message.

Three call sites in ``orchestrator.run_agent_streaming``:

1. **Before the run** — :func:`load_session_history` returns the prior
   turns as a list of ``{"role": "user" | "assistant", "content": ...}``
   dicts the OpenAI Agents SDK accepts as input. The current turn's
   ``user`` message is appended on top of this list.
2. **During the run** — every ``tool_output`` event persists a
   ``role='tool'`` row with the tool name + result so it's queryable
   later (the threads view + future analytics rely on it).
3. **After the run** — the assistant's ``final_output`` lands as a
   ``role='assistant'`` row.

PR #72 fix: the read path no longer emits ``role='tool'`` items
verbatim. The Agents SDK rejects a tool item without a matching
``tool_call_id`` linking it back to a call item (we don't have the
original call_id after the run completed), so feeding it back as-is
produced ``Unhandled item type or structure: {'role': 'tool', ...}``.
Instead, each persisted tool row is **collapsed into the preceding
assistant turn** as a parenthetical ``[Earlier in this conversation
I called <tool> and got: ...]`` note. If there's no preceding
assistant (e.g. the tool row sits right after a user message), we
emit a synthetic assistant turn carrying just the note so the model
still sees the prior result.

Every save is best-effort: if the DB blips, log and continue. The
user's chat turn must never fail because of a persistence error.

Defense in depth: we filter by **both** ``session_id`` AND
``company_id`` on every SELECT even though RLS already scopes by
company — the explicit predicate makes cross-tenant joins impossible
even if RLS is misconfigured upstream.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from helixis.db.engine import get_pool
from helixis.security.prompt_delimit import wrap_untrusted

logger = logging.getLogger(__name__)


# How far back the history loader reaches. 40 turns covers a normal
# back-and-forth (~20 question/answer pairs plus tool outputs) without
# blowing the model's context window. If a session grows longer than 40,
# the loader keeps the MOST RECENT 40 (see the DESC-then-ASC query in
# load_session_history) and drops the oldest — the model still has its own
# memory tools (Mem0) for cross-session recall.
DEFAULT_HISTORY_LIMIT = 40

#: What the model is told when the window did NOT reach the start of the
#: conversation. Before this, the drop was silent: the model saw the most
#: recent 40 rows and had no way to know the thread was longer, so a constraint
#: the user set forty rows ago ("never text that tenant", "always CC me") was
#: simply gone — and the honest-looking answer is the dangerous one, because
#: "you never mentioned that" is a confident false claim about something the
#: user demonstrably did say.
#:
#: ⚠ This is gotcha 26's rule, pointed at the CONVERSATION instead of at a tool
#: result: *a truncated read is never evidence of absence.* The wording is
#: modelled on the truncation footer for the same reason — it names the scope
#: and then forbids the specific wrong inference in those words, because
#: naming the limit alone is what the list tools already did when they reported
#: a task "does not exist anywhere in the portfolio".
#:
#: ⚠ No count. Knowing HOW MANY earlier turns exist would need a second query
#: on the hot path of every chat turn, and it buys nothing the model can act
#: on — the instruction is the same at 3 dropped turns and at 300.
_EARLIER_TURNS_DROPPED = (
    "[context note: this conversation is longer than the window above. Earlier "
    "turns exist and are NOT shown. Do not tell the user they never said or "
    "asked something — you cannot see that far back. If an instruction, "
    "preference or constraint might have been set earlier, say you cannot see "
    "it and ask them to restate it.]"
)

# Maximum characters of a tool result we feed back into the prompt.
# Tool outputs (a list of 47 tenants, etc.) can be huge and balloon
# the context. The model only needs a representative sample to know
# the data exists; truncated rows still get re-fetched on demand.
_TOOL_RESULT_RENDER_BYTES = 2000

#: How many of the MOST RECENT tool results replay at full fidelity. Past this,
#: a result is background rather than evidence — see the decay note in
#: ``load_session_history``.
_FULL_FIDELITY_TOOL_NOTES = 6

#: Budget for an older tool result. Enough to carry what the lookup was and the
#: shape of its answer; not enough to re-pay for the whole payload on every
#: subsequent turn. Truncation is always announced (``_format_tool_note``) so a
#: fragment can never be mistaken for a complete result.
_STALE_TOOL_RESULT_RENDER_BYTES = 300


async def save_message(
    *,
    session_id: str,
    company_id: str,
    user_id: str,
    role: str,
    content: str,
    tool_name: str | None = None,
    tool_result: Any = None,
) -> None:
    """Persist one turn. Never raises — a DB blip logs and swallows.

    ``role`` must be one of ``user`` / ``assistant`` / ``tool`` (enforced
    by the migration's CHECK constraint; we don't double-validate here
    so the DB error is the truthful source if drift sneaks in).

    For ``role='tool'`` rows, pass the tool's name + raw result. The
    result is JSON-serialised at write time; on read it comes back as
    jsonb. For everything else, ``tool_name`` and ``tool_result`` stay
    ``None``.
    """
    pool = get_pool()
    if pool is None:
        # Tests / scripts without a pool: silently no-op rather than
        # crash. Production always has a pool by the time the
        # orchestrator runs.
        logger.debug("save_message no-op: pool not initialised")
        return

    result_json: str | None = None
    if tool_result is not None:
        try:
            result_json = json.dumps(tool_result, default=str)
        except (TypeError, ValueError):
            # Defensive — a tool returning a non-serialisable object
            # shouldn't kill persistence. Stringify and move on.
            result_json = json.dumps(str(tool_result))

    try:
        await pool.execute(
            """
            INSERT INTO public.helixis_chat_messages
                (session_id, company_id, user_id, role, content,
                 tool_name, tool_result)
            VALUES ($1, $2, $3::uuid, $4, $5, $6, $7::jsonb)
            """,
            session_id,
            company_id,
            user_id or None,
            role,
            content,
            tool_name,
            result_json,
        )
    except asyncpg.exceptions.UndefinedTableError:
        # Migration 0013 not yet applied. Log once-per-warning, keep
        # the run going — the orchestrator stays functional, just
        # without memory.
        logger.warning(
            "helixis_chat_messages table missing — apply migration 0013 "
            "to enable cross-turn memory. Continuing without persistence."
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to persist chat message (session=%s role=%s) — continuing without persistence.",
            session_id,
            role,
        )


def _decode_tool_result(value: Any) -> Any:
    """Give back the JSON VALUE stored in the ``tool_result`` jsonb column.

    ⚠ **asyncpg returns ``jsonb`` as a ``str``** — the driver has no automatic
    codec for it and this project registers none (checked, not assumed:
    ``grep set_type_codec`` finds nothing). So the write side serialises an
    object into ``$7::jsonb`` and the read side hands back JSON TEXT, and the
    two halves silently disagree about the field's type.

    That reached the browser and TOOK CHAT DOWN (Sentry PYTHON-FASTAPI-3M,
    2026-09-07): the card renderer unwrapped its payload with
    ``'data' in payload``, ``in`` throws ``TypeError`` on a string, and the
    React error boundary replaced the whole conversation with "Occupella hit
    an error". Nothing was wrong with the card.

    ``db/repositories/leads.py`` already carries this exact guard for its own
    ``raw`` column — the rule was learned once and never reached here
    (gotcha 25). The frontend keeps its own guard too: a string arriving from
    any other source must cost a card, never the answer.

    A string that is not JSON is returned UNCHANGED rather than dropped. Old
    rows may hold a plain string, and the note renderer prints one fine.
    """
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _format_tool_note(tool_name: str | None, tool_result: Any, *, budget: int | None = None) -> str:
    """Render a stored ``role='tool'`` row as a parenthetical note that
    embeds inside an assistant turn.

    The Agents SDK does NOT accept a bare ``{"role": "tool", ...}`` item
    on replay (it expects a structured tool-output linked to a tool-call
    by id, which we don't preserve after a run completes). So we
    inline the tool result into surrounding prose instead — the model
    still sees what each prior tool returned, just embedded in plain
    text rather than as a structurally-typed tool item.

    Falls back to ``"a tool"`` when ``tool_name`` is empty or NULL — the
    pre-PR-#72 bug rendered the literal string ``"tool"`` here, which
    leaked into the SDK error output Brandon hit.
    """
    name = tool_name or ""
    # Guard against the legacy bug that persisted the literal "tool"
    # as the name when the SDK item didn't expose .name.
    if not name or name == "tool":
        name = "a tool"
    if tool_result is None:
        return f"[Earlier in this conversation I called `{name}` and got no result back]"
    try:
        body = tool_result if isinstance(tool_result, str) else json.dumps(tool_result, default=str)
    except (TypeError, ValueError):
        body = str(tool_result)
    cap = _TOOL_RESULT_RENDER_BYTES if budget is None else budget
    truncated = body[:cap]
    if len(body) > cap:
        # Say so. An older result silently cut to its first 300 characters
        # reads as a COMPLETE small result, and a truncated read taken as the
        # whole picture is this product's worst failure mode (gotcha 26). The
        # note also tells the model the remedy, so it re-reads instead of
        # answering from a fragment.
        # Terse on purpose: this rides EVERY compressed note, so a wordy
        # version spends more tokens on the disclaimer than on the payload it
        # disclaims. All three facts still have to survive — that it is
        # PARTIAL, that a fragment proves nothing about what is missing, and
        # that re-running is the remedy.
        #
        # ⚠ THE ANTI-ABSENCE CLAUSE IS LOAD-BEARING, and "PARTIAL" alone was
        # not doing its job. Compression is HEAD truncation, and every
        # safety footer in this product is appended LAST — the anti-absence
        # warning on a truncated task list, the unpaid-bills payment-history
        # gap, the "no email or phone on file" line, the maintenance
        # do-not-re-query footer. Measured on the live mirror (Blue Ridge
        # only, gotcha 47): of the stored results for the tools that carry
        # one, EVERY single one exceeds 300 characters — unpaid_bills 34/34
        # (shortest 3,332 chars), delinquency_report 21/21, open_tasks 4/4
        # at 7,220, expiring_leases 10/10. So once a result is more than
        # ``_FULL_FIDELITY_TOOL_NOTES`` old its footer is gone, in 38 of 318
        # sessions, across 119 replayed notes. What survived — "re-run for
        # the rest" — is an INVITATION to fetch more, not a prohibition on
        # concluding absence from what is visible, and those are different
        # sentences. Gotcha 26 exists because this model reads a fragment and
        # says the thing is not there.
        truncated += (
            f"\n…[+{len(body) - cap} chars. PARTIAL — NOT evidence of absence; "
            f"re-run the lookup for the rest.]"
        )
    # Replayed tool output can be untrusted content the tool fetched earlier (an
    # email body, a web page, a Buildium free-text field). Fence it as data so a
    # payload persisted on a prior turn can't act as an instruction when it's
    # re-injected into a later, write-capable orchestrator turn (LED-009).
    fenced = wrap_untrusted(truncated, label="earlier tool result")
    return f"[Earlier in this conversation I called `{name}` and got:]\n{fenced}"


async def load_session_history(
    session_id: str,
    company_id: str,
    limit: int = DEFAULT_HISTORY_LIMIT,
    *,
    user_id: str,
) -> list[dict[str, str]]:
    """Return prior turns of ONE PERSON's ``session_id`` in oldest-first
    order, formatted for the Agents SDK's ``input`` parameter.

    ⚠ The user scope matters MOST here (gotcha 85), because this is the read
    that reaches the MODEL rather than the screen. Company-only, a member
    posting to a session id belonging to the admin got the admin's whole
    conversation replayed into their turn as context — and the agent would
    then answer as if it were mid-conversation with somebody else.

    Returns an empty list if the session has never been seen, if the
    table is missing (migration 0013 not yet applied), or if the DB
    blips — the orchestrator falls back to the cold-start path
    automatically.
    """
    pool = get_pool()
    if pool is None:
        return []
    try:
        # Take the MOST RECENT ``limit`` turns, then present them oldest-first.
        # The inner ``DESC LIMIT`` selects the newest N; the outer ``ASC`` puts
        # them back in chronological order for the walk below. A plain
        # ``ORDER BY created_at ASC LIMIT N`` would instead keep the OLDEST N and
        # silently drop everything recent once a session exceeds N rows (every
        # tool output is its own row, so that happens fast) — i.e. the model
        # would see the conversation's beginning and forget what was just said.
        rows = await pool.fetch(
            """
            SELECT role, content, tool_name, tool_result
              FROM (
                SELECT role, content, tool_name, tool_result, created_at
                  FROM public.helixis_chat_messages
                 WHERE session_id = $1
                   AND company_id = $2
                   AND user_id = $3::uuid
                 ORDER BY created_at DESC
                 LIMIT $4
              ) recent
             ORDER BY created_at ASC
            """,
            session_id,
            company_id,
            user_id,
            # ⚠ ONE MORE than we intend to show, and the extra row is the whole
            # point: its EXISTENCE is what proves the conversation runs past
            # this window. ``len(rows) == limit`` cannot tell "the session has
            # exactly 40 rows" from "the session has 4,000" — an ambiguity that
            # would make the notice below fire on a complete conversation, and
            # a caveat that is sometimes false trains a reader to discount the
            # true ones (gotcha 57's reasoning about a computed caveat).
            limit + 1,
        )
    except asyncpg.exceptions.UndefinedTableError:
        return []
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to load chat history (session=%s) — running cold.",
            session_id,
        )
        return []

    # Walk rows in order. user/assistant turns emit straight through;
    # tool rows fold into the preceding assistant turn (or a synthetic
    # one if there's nothing immediately before — e.g. tool row right
    # after a user message). This produces a strict ``user|assistant``
    # alternation the Agents SDK accepts on replay.
    # Which tool rows are RECENT enough to replay in full. Rows come back
    # oldest-first, so the last ``_FULL_FIDELITY_TOOL_NOTES`` tool rows keep the
    # full budget and everything older is compressed.
    #
    # This is the single largest uncached-input line in the product. Measured
    # 2026-08-24: 24,519 uncached input tokens per turn at the full $3/MTok
    # (cache reads are $0.30) — and a 40-turn window where every tool row could
    # render 2,000 characters is ~80k characters ≈ 20k tokens, which is almost
    # exactly that figure. History changes every turn, so none of it caches.
    #
    # Decaying rather than dropping: what a tool returned three questions ago is
    # context, not evidence — the model needs to know the lookup happened and
    # roughly what came back, and can re-run it if it needs the detail. What it
    # returned on the previous turn is frequently the thing being discussed.
    # Drop the probe row BEFORE anything indexes into ``rows`` — the fidelity
    # window below is positional, so slicing later would decay one row too many.
    older_turns_exist = len(rows) > limit
    if older_turns_exist:
        rows = rows[-limit:]

    tool_row_indexes = [i for i, r in enumerate(rows) if r["role"] == "tool"]
    full_fidelity = set(tool_row_indexes[-_FULL_FIDELITY_TOOL_NOTES:])

    history: list[dict[str, str]] = []
    if older_turns_exist:
        # ⚠ An ASSISTANT turn, which is the convention this loader already
        # uses for anything that is not literally something a person typed —
        # the synthetic turns carrying orphan tool notes below do the same.
        # A ``user`` turn would put words in the user's mouth, and a mid-list
        # ``system`` role is not a shape the SDK's replay path guarantees.
        history.append({"role": "assistant", "content": _EARLIER_TURNS_DROPPED})
    for idx, row in enumerate(rows):
        role = row["role"]
        if role == "tool":
            note = _format_tool_note(
                row["tool_name"],
                row["tool_result"],
                budget=(
                    _TOOL_RESULT_RENDER_BYTES
                    if idx in full_fidelity
                    else _STALE_TOOL_RESULT_RENDER_BYTES
                ),
            )
            if history and history[-1]["role"] == "assistant":
                # Append to the preceding assistant turn. ``.strip()``
                # absorbs the case where the assistant content was
                # empty (very long tool runs that timed out).
                prev = history[-1]
                history[-1] = {
                    "role": "assistant",
                    "content": (prev["content"] + "\n\n" + note).strip(),
                }
            else:
                # No predecessor — emit a synthetic assistant turn
                # carrying just the note. Keeps the alternation valid
                # and ensures the model still sees what happened.
                history.append({"role": "assistant", "content": note})
            continue
        history.append({"role": role, "content": row["content"] or ""})
    return history


async def list_sessions_for_company(
    company_id: str,
    *,
    user_id: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Most-recent distinct ``session_id``s for ONE PERSON, with the first
    user-message preview and the latest ``created_at``. Powers the Chat
    sidebar's threads list.

    ⚠ SCOPED TO THE USER AS WELL AS THE COMPANY (2026-09-04, gotcha 85).
    This read was company-only, so the first invited teammate opened the app
    and saw every thread the admin had ever written — *"How many properties
    do I manage"*, *"can you draft an email to…"* — as their own sidebar. A
    chat thread is a person's working notes; the Inbox is the company's
    shared queue, and that difference is the whole design here.

    ``user_id`` was on the row the entire time (``save_message`` writes it,
    and it is populated on 3,189 of 3,235 live rows) and every read threw it
    away — gotchas 41 / 50 / 51 / 76, arriving as a dropped WHERE clause.

    ⚠ **Company stays in the predicate**, both because gotcha 47 requires it
    and because it is the outer fence: a user id alone would return a
    person's threads from a company they have since left.

    ⚠ **A NULL ``user_id`` row is nobody's and stays hidden.** 12 live
    sessions (2026-07-21..08-19, all fully anonymous — 0 sessions are
    partly attributed) predate consistent attribution. Showing them to
    everyone is the very leak being closed, and inventing an owner is a
    guess (gotcha 55); so they are invisible rather than mis-filed.
    """
    pool = get_pool()
    if pool is None:
        return []
    try:
        # DISTINCT ON gives us one row per session (the oldest user
        # message — that's the preview), and the outer ORDER BY +
        # subselect on last-touched-at sorts by recency. Two-step CTE
        # keeps the query readable.
        rows = await pool.fetch(
            """
            WITH latest AS (
                SELECT session_id,
                       MAX(created_at) AS last_touched
                  FROM public.helixis_chat_messages
                 WHERE company_id = $1
                   AND user_id = $2::uuid
                 GROUP BY session_id
            ),
            firsts AS (
                SELECT DISTINCT ON (session_id)
                       session_id,
                       content
                  FROM public.helixis_chat_messages
                 WHERE company_id = $1
                   AND user_id = $2::uuid
                   AND role = 'user'
                 ORDER BY session_id, created_at ASC
            )
            SELECT l.session_id,
                   l.last_touched,
                   COALESCE(f.content, '(no user message yet)') AS preview,
                   EXISTS (
                       SELECT 1
                         FROM public.orchestrator_runs r
                        WHERE r.session_id = l.session_id
                          AND r.company_id = $1::uuid
                          AND r.status = 'running'
                   ) AS live
              FROM latest l
              LEFT JOIN firsts f USING (session_id)
             ORDER BY l.last_touched DESC
             LIMIT $3
            """,
            company_id,
            user_id,
            limit,
        )
    except asyncpg.exceptions.UndefinedTableError:
        return []
    except Exception:  # noqa: BLE001
        logger.exception("Failed to list sessions for company %s", company_id)
        return []

    return [
        {
            "session_id": r["session_id"],
            "preview": (r["preview"] or "")[:140],
            "created_at": r["last_touched"].isoformat() if r["last_touched"] else "",
            # Live-watch PR: an orchestrator run is currently in flight for
            # this session — the sidebar badges it and clicking it attaches
            # as a watcher. Stale 'running' rows are bounded by the run
            # reaper (#708), so a crashed run stops reading as live within
            # its stale window.
            "live": bool(r["live"]),
        }
        for r in rows
    ]


async def load_session_messages_for_company(
    session_id: str,
    company_id: str,
    *,
    user_id: str,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Full message list for ONE PERSON's ``session_id``, oldest-first. Used
    by ``/chat/sessions/{id}/messages`` to replay a thread from the sidebar.

    ⚠ Scoped to the user as well as the company (gotcha 85). Hiding a
    teammate's thread from the LIST and still serving its messages to anyone
    who names the id is a guard that only works against people who do not
    look — the list and the fetch have to agree, or the fix is cosmetic."""
    pool = get_pool()
    if pool is None:
        return []
    try:
        rows = await pool.fetch(
            """
            SELECT id, role, content, tool_name, tool_result, created_at,
                   user_id
              FROM public.helixis_chat_messages
             WHERE session_id = $1
               AND company_id = $2
               AND user_id = $3::uuid
             ORDER BY created_at ASC
             LIMIT $4
            """,
            session_id,
            company_id,
            user_id,
            limit,
        )
    except asyncpg.exceptions.UndefinedTableError:
        return []
    except Exception:  # noqa: BLE001
        logger.exception("Failed to load messages for session %s", session_id)
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": str(r["id"]),
                "role": r["role"],
                "content": r["content"] or "",
                "tool_name": r["tool_name"],
                "tool_result": _decode_tool_result(r["tool_result"]),
                "created_at": r["created_at"].isoformat() if r["created_at"] else "",
                "user_id": str(r["user_id"]) if r["user_id"] else None,
            }
        )
    return out


__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "list_sessions_for_company",
    "load_session_history",
    "load_session_messages_for_company",
    "save_message",
]
