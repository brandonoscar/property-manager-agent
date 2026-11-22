"""Headless driver for the deployed agent — one seam every suite drives through.

Wraps :func:`helixis.agents.orchestrator.run_agent_streaming` so a suite can run
a single turn or a scripted multi-turn conversation WITHOUT the HTTP/SSE layer,
collecting the final reply, the streamed prose, the tool-call trail, the routed
specialists, and any confirm-gate — everything the assertions read.

Scoping is by ``company_id`` (+ a ``user_id`` label). That is the real tenancy
boundary for a headless run: Buildium/mirror tools resolve their company from
the run's memory context, so a company-A run can only ever see A's rows. The
JWT/RLS membership check (``require_company_access``) guards the HTTP path, which
we deliberately bypass here — we are testing the agent's behaviour, not the API
gateway (that boundary is covered by golden 42x + the API's own tests).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field

from helixis.common.events import EventType, HelixisEvent
from helixis.common.types import AgentRequest
from helixis.settings import Settings, get_settings
from helixis.streaming.bus import EventBus

# A turn against real Gemini + live Buildium can be slow; cap so a hung turn
# fails the scenario instead of wedging the whole suite.
_TURN_TIMEOUT_S = 180.0


@dataclass
class TurnResult:
    """Everything observable from one agent turn."""

    reply: str  # DONE summary (final_output) — the user-visible answer
    streamed_text: str  # concatenated assistant-message / token deltas
    tool_trail: list[str] = field(default_factory=list)  # tool names, in order
    handoffs: list[str] = field(default_factory=list)  # specialists routed to
    tool_results: list[str] = field(default_factory=list)  # stringified results
    confirm_gate: dict | None = None  # payload if a write paused for confirm
    #: Card payloads the user saw (tables, detail cards, checklists, charts).
    #: A turn can answer mostly in cards (gotcha 56), so a scorer reading only
    #: ``reply`` can fail an answer the user actually got.
    cards: list[dict] = field(default_factory=list)
    errored: bool = False
    error_message: str = ""
    timed_out: bool = False

    @property
    def all_text(self) -> str:
        """Reply + streamed prose + tool-result text — the full surface to scan
        for a leaked SSN/phone/PII token. A leak anywhere here is a leak."""
        return "\n".join([self.reply, self.streamed_text, *self.tool_results])


@dataclass
class Conversation:
    """A multi-turn session (stable ``session_id``) against one company."""

    company_id: str
    user_id: str
    session_id: str
    turns: list[TurnResult] = field(default_factory=list)


class AgentDriver:
    """Drives real agent turns headlessly. Reuse one per suite."""

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()
        # A real auth.users id, resolved from the DB by RunContext.ensure_db.
        # helixis_chat_messages.user_id has a FK to auth.users, so a synthetic id
        # violates it; a real member id satisfies the FK while the rows land in a
        # synthetic company (RLS-invisible to that user). Falls back to the
        # synthetic EVAL_USER_ID when no DB is available (offline).
        self.default_user_id: str | None = None

    def new_conversation(self, company_id: str, user_id: str | None = None) -> Conversation:
        from helixis.evals.live.config import EVAL_USER_ID

        return Conversation(
            company_id=company_id,
            user_id=user_id or self.default_user_id or EVAL_USER_ID,
            session_id=f"live-eval-{uuid.uuid4()}",
        )

    async def send(
        self,
        convo: Conversation,
        message: str,
        *,
        context: dict | None = None,
        source: str = "chat",
    ) -> TurnResult:
        """Run one turn, append it to the conversation, and return it.

        Confirm gates are NEVER approved: an eval turn that raises a gate is a
        turn that *stopped before writing* — which is exactly what several
        safety scenarios want to observe. We record the gate and let the run
        finish (the orchestrator emits the gate live via its per-run emitter,
        gotcha 4, so we see it even though no client answers).
        """
        request = AgentRequest(
            task=message,
            company_id=convo.company_id,
            session_id=convo.session_id,
            context=context or {},
            source=source,
        )
        result = await self._run_once(request, convo.user_id)
        convo.turns.append(result)
        return result

    async def one_shot(
        self, company_id: str, message: str, *, user_id: str | None = None
    ) -> TurnResult:
        """Convenience: a single-turn conversation."""
        convo = self.new_conversation(company_id, user_id)
        return await self.send(convo, message)

    async def _run_once(self, request: AgentRequest, user_id: str) -> TurnResult:
        from helixis.agents.orchestrator import run_agent_streaming

        bus = EventBus()
        session_id = request.session_id or f"live-eval-{uuid.uuid4()}"
        request.session_id = session_id
        bus.create_session(session_id)

        collected: list[HelixisEvent] = []

        async def _collect() -> None:
            async for event in bus.subscribe(session_id):
                collected.append(event)

        collector = asyncio.create_task(_collect())
        result = TurnResult(reply="", streamed_text="")
        try:
            await asyncio.wait_for(
                run_agent_streaming(request, self._settings, bus, user_id=user_id),
                timeout=_TURN_TIMEOUT_S,
            )
        except TimeoutError:
            result.timed_out = True
            result.errored = True
            result.error_message = f"turn exceeded {_TURN_TIMEOUT_S:.0f}s"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a raised turn IS a failure signal
            result.errored = True
            result.error_message = repr(exc)
        finally:
            await bus.close_session(session_id)
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(collector, timeout=5.0)
            if not collector.done():
                collector.cancel()

        _fold_events(collected, result)
        return result


#: Events that render a card in the chat thread.
_CARD_EVENTS = frozenset(
    {EventType.TABULAR_DATA, EventType.DETAIL_CARD, EventType.CHECKLIST, EventType.CHART}
)


def _fold_events(events: list[HelixisEvent], result: TurnResult) -> None:
    """Reduce the raw event stream into the observable fields."""
    streamed: list[str] = []
    for ev in events:
        data = ev.data or {}
        if ev.type is EventType.DONE:
            result.reply = str(data.get("summary", "") or "")
        elif ev.type is EventType.ERROR:
            result.errored = True
            result.error_message = str(data.get("message", "") or "")
        elif ev.type in (EventType.TOKEN, EventType.ASSISTANT_MESSAGE_TOKEN):
            streamed.append(str(data.get("content", "") or ""))
        elif ev.type is EventType.TOOL_START:
            tool = str(data.get("tool", "") or "")
            if tool:
                result.tool_trail.append(tool)
        elif ev.type is EventType.TOOL_RESULT:
            result.tool_results.append(str(data.get("result", "") or ""))
        elif ev.type is EventType.HANDOFF:
            agent = str(data.get("agent", "") or "")
            if agent:
                result.handoffs.append(agent)
        elif ev.type is EventType.CONFIRM_GATE:
            result.confirm_gate = dict(data)
        elif ev.type in _CARD_EVENTS:
            result.cards.append({"type": ev.type.value, **dict(data)})
    if streamed:
        result.streamed_text = "".join(streamed)
