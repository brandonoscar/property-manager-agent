/**
 * The agent runner's event stream, projected into the ConversationEvent[]
 * that ``ConversationThread`` draws. Lifted out of ChatThread so the Inbox
 * uses the SAME projection.
 *
 * ⚠ Why it had to move. A question typed in the Inbox runs the same agent
 * (``POST /buildium/events/{id}/freeform`` → ``run_agent_streaming``), and its
 * events arrive on the Inbox stream RAW: ``token``, ``thinking``,
 * ``tool_start``, ``handoff``, ``done``. ``foldEvents`` draws none of those, so
 * the Inbox showed the user's message, "Recalled 5 notes from memory", and
 * then nothing: no steps, and no answer. A fix bound to one surface is not a
 * fix (gotcha 66).
 */

import type { HelixisEvent } from '../../services/agent';
import type { ConversationEvent } from './types';
import { CARD_EVENT_TYPES, isCardEvent } from './types';

export interface StoredMessage {
  id: string;
  // Live store rows use the legacy ``'user' | 'agent'`` shape; the
  // persisted/replayed rows from the backend get mapped to ``agent``
  // before they land here (see ``hydrateThreadMessages`` in the
  // store). One union covers both code paths.
  role: 'user' | 'agent';
  content: string;
  timestamp: number;
  events?: HelixisEvent[];
}

/**
 * Walk the store's messages and produce a flat conversation-events
 * array the ConversationThread renderer understands.
 *
 * - ``user`` messages render as a right-aligned assistant turn with the
 *   "→ {text}" convention the Inbox already uses (PR #70 set the
 *   pattern). Keeping the same primitive for user + assistant means
 *   one renderer covers both surfaces.
 * - ``assistant`` messages emit a start/token/end trio; the trio
 *   appears as ``streaming`` only when it's the latest turn and the
 *   run is still in flight.
 * - Tool calls inside an assistant message flatten into ``narration``
 *   events using the tool name as the human-readable status line.
 */
export function foldMessagesToConversation(
  messages: StoredMessage[],
  isRunning: boolean,
): ConversationEvent[] {
  const out: ConversationEvent[] = [];
  const lastIdx = messages.length - 1;

  messages.forEach((msg, idx) => {
    const turnId = `${msg.role}-${msg.id}`;
    if (msg.role === 'user') {
      // PR #75 — Chat renders the user's input as a real right-aligned
      // bubble (iris-purple, white text) instead of the Inbox-style
      // ``→ {text}`` line. ChatThread is the only place that emits
      // ``user_bubble``; the Inbox path keeps the assistant-turn
      // shape because it never has user input to surface.
      out.push({ type: 'user_bubble', data: { content: msg.content } });
      return;
    }

    // Routing, the model's own prose, and tool calls — in stream order.
    //
    // ⚠ TWO THINGS CHANGED HERE, and both were making the wait unreadable.
    //
    // 1. ``thinking`` is the MODEL'S OWN PROSE — the text it streams between
    //    tool calls saying what it is about to do and what it found. It was
    //    being turned into a ``narration`` line, which ``foldEvents`` buffers
    //    into the collapsed "N steps" pill at 11.5px, next to the call log.
    //    Prose was being rendered as telemetry. It now emits an assistant
    //    trio, so it renders at body size in stream order like any other
    //    assistant text — which also flushes the pending pill ahead of it,
    //    for free, without touching ``foldEvents``.
    //
    // 2. ONE ROW PER CALL. ``tool_start`` and ``tool_result`` each produced a
    //    line, so every action read "Calling X" then "Got the result from X"
    //    — two rows saying one thing. A call is now a single row whose label
    //    reflects its state, resolved by pairing each start with its result.
    const evts = msg.events ?? [];

    // ⚠ A DROPPED ``tool_result`` MUST NOT SPIN FOREVER. Pairing alone leaves
    // an unmatched start "Calling…" for the life of the transcript, and a
    // stalled run or a shed event under backpressure makes that routine, not
    // hypothetical. Once the turn is over, an unresolved call resolves — and
    // says it had no result rather than claiming a clean finish, because a
    // missing result laundered as success is the exact failure this codebase
    // keeps re-learning.
    const turnIsOver =
      !(isRunning && idx === lastIdx) ||
      evts.some((e) => e.type === 'done' || e.type === 'error');

    // ⚠ Pair by call_id FIRST. The SDK runs a turn's tool calls concurrently,
    // so a turn can hold five `buildium_agent` calls at once; pairing by name
    // and arrival order mis-attributes the failure when calls 1, 2, 4 and 5
    // return and 3 does not — the first four render resolved and the LAST one
    // renders as the failure. Correlating by identity is the whole fix, and
    // it is what every protocol that handles this does (Anthropic's block
    // `index`, AG-UI's `toolCallId`).
    const resultById = new Map<string, { status: string }>();
    for (const e of evts) {
      if (e.type === 'tool_result') {
        const id = ((e.data?.call_id as string | undefined) ?? '').trim();
        if (id) resultById.set(id, { status: (e.data?.status as string) ?? 'ok' });
      }
    }

    // The name+count fallback SURVIVES: a resumed or stored stream replays
    // events emitted before call_id existed, and losing the pairing entirely
    // would leave every one of those rows spinning.
    const resultsFor = new Map<string, number>();
    for (const e of evts) {
      if (e.type === 'tool_result') {
        const id = ((e.data?.call_id as string | undefined) ?? '').trim();
        if (id) continue; // counted by identity above
        const n = (e.data?.tool as string | undefined) ?? 'tool';
        resultsFor.set(n, (resultsFor.get(n) ?? 0) + 1);
      }
    }
    const pairedSoFar = new Map<string, number>();

    // ⚠ PARALLEL DISPATCH IS COUNTED HERE, ON THE CLIENT, DELIBERATELY. The
    // backend sees tool calls one stream event at a time and cannot know, at
    // the first one, how many more are coming — so a backend line would say
    // "2 things" while a third and fourth were still arriving. A wrong number
    // in a status line is worse than no line at all (the whole reason the
    // three-minute turn read as broken was a status the user couldn't trust).
    // The client holds the finished event list, so it can count the burst.
    //
    // Requires call_id, and stays SILENT without it: a replayed pre-call_id
    // stream gets no header rather than a guessed one.
    const burstHeader = new Map<number, number>();
    {
      const open: number[] = []; // indices of tool_starts not yet resolved
      let leader: number | null = null;
      for (let i = 0; i < evts.length; i++) {
        const e = evts[i];
        const id = ((e.data?.call_id as string | undefined) ?? '').trim();
        if (!id) continue;
        if (e.type === 'tool_start') {
          open.push(i);
          if (open.length === 2) leader = open[0];
          // Grows to the burst's PEAK concurrency as more calls join it.
          if (open.length >= 2 && leader !== null) burstHeader.set(leader, open.length);
        } else if (e.type === 'tool_result') {
          const k = open.findIndex(
            (j) => (((evts[j].data?.call_id as string | undefined) ?? '').trim()) === id,
          );
          if (k >= 0) open.splice(k, 1);
          if (open.length <= 1) leader = null;
        }
      }
    }

    for (let ei = 0; ei < evts.length; ei++) {
      const te = evts[ei];
      const concurrent = burstHeader.get(ei);
      if (concurrent) {
        out.push({
          type: 'narration',
          data: { content: `Checking ${concurrent} things at the same time` },
        });
      }
      if (te.type === 'handoff') {
        out.push({
          type: 'narration',
          data: { content: `→ ${(te.data?.agent as string | undefined) ?? 'Specialist'}` },
        });
      } else if (te.type === 'memory_recall') {
        // ⚠ Emitted HERE, in stream order, not with the cards. It says what
        // was recalled BEFORE answering, so it belongs above the prose — the
        // after-the-answer card run would render it as an afterthought about
        // a turn that already finished. Same drop as the cards had; different
        // right answer.
        out.push({
          type: 'memory_recall',
          data: (te.data ?? {}) as Record<string, unknown>,
        });
      } else if (te.type === 'restatement') {
        const said = ((te.data?.content as string | undefined) ?? '').trim();
        if (said) out.push({ type: 'restatement', data: { content: said } });
      } else if (te.type === 'substep') {
        // ⚠ What happens during the ninety seconds a consult row sits there.
        // The nested run's steps were already being watched server-side (for
        // the truncation signal) and thrown away; forwarding them is strictly
        // better than any synthetic heartbeat, which would say "still alive"
        // — something the user already assumed and cannot act on.
        const sub = ((te.data?.content as string | undefined) ?? '').trim();
        if (sub) out.push({ type: 'substep', data: { content: sub } });
      } else if (te.type === 'reasoning') {
        // ⚠ The model's WORKING-OUT, passed through in stream order so it
        // lands in the activity trail beside the step it explains. Kept
        // distinct from ``thinking`` on purpose: thinking is prose the model
        // wrote FOR the user and belongs at body size in a bubble; this is
        // the reasoning underneath it and belongs dimmed in the trail. A
        // ninety-second consult produces nothing else, so this is the only
        // thing standing between the user and a blank three minutes.
        const thought = ((te.data?.content as string | undefined) ?? '').trim();
        if (thought) out.push({ type: 'reasoning', data: { content: thought } });
      } else if (te.type === 'thinking') {
        const prose = ((te.data?.content as string | undefined) ?? '').trim();
        if (!prose) continue;
        // ⚠ Keyed on the EVENT's position (append-only), never on `out.length`.
        // A parallel burst inserts its "Checking N things" header above rows
        // that are already on screen, which shifted every later id by one:
        // React saw a new key, remounted the bubble and faded it in again.
        // That remount is one of the "the answer glitches in" flickers.
        const nid = `think-${msg.id}-${ei}`;
        out.push({ type: 'assistant_message_start', data: { turn_id: nid } });
        out.push({ type: 'assistant_message_token', data: { turn_id: nid, content: prose } });
        out.push({ type: 'assistant_message_end', data: { turn_id: nid } });
      } else if (te.type === 'tool_start') {
        const toolName = (te.data?.tool as string | undefined) ?? 'tool';
        const callId = ((te.data?.call_id as string | undefined) ?? '').trim();
        // Identity wins where it exists; the name+count fallback answers only
        // for a start that carries no id, so the two schemes cannot both claim
        // the same result.
        let resolved: boolean;
        let status = 'ok';
        if (callId) {
          const hit = resultById.get(callId);
          resolved = hit !== undefined;
          if (hit) status = hit.status;
        } else {
          const seen = pairedSoFar.get(toolName) ?? 0;
          resolved = seen < (resultsFor.get(toolName) ?? 0);
          pairedSoFar.set(toolName, seen + 1);
        }
        // ⚠ The BACKEND's sentence wins. `tool_start_event` carries a
        // `description` written by `agents/step_narration.py` — "Looking up
        // the property", not "buildium get property" — and this file used to
        // discard it and re-split the machine name on underscores. That is
        // what put "Called buildium agent" on screen five times during a
        // three-minute turn, which reads as a hang.
        //
        // The fallback stays for events emitted before that landed (a resumed
        // stream can replay them) and for any surface that omits it.
        // ⚠ A LEGACY description is worse than none. Before the sentence
        // existed the backend sent `Calling {name}...` — so a resumed stream,
        // or a stored transcript, replays a "description" that is either the
        // machine name again or a bare placeholder with no content at all.
        // Preferring it blindly renders "Calling…", which says less than the
        // fallback does. Our sentences never begin with "Calling"; that word
        // is the legacy marker.
        const raw = ((te.data?.description as string | undefined) ?? '').trim();
        const described = /^calling\b/i.test(raw) ? '' : raw;
        const label = described || humaniseTool(toolName);
        // ⚠ THREE OUTCOMES, THREE SENTENCES — and "we never saw it finish" is
        // NOT "it came back empty". A step that returned no rows is evidence
        // about the record; a result we never received is evidence about the
        // stream. Collapsing them would let a shed event read as a completed
        // lookup that found nothing, which is this codebase's worst failure
        // mode (gotcha 26) arriving through the UI.
        let content: string;
        if (!resolved) {
          content = turnIsOver ? `${label} — no result` : `${label}…`;
        } else if (status === 'empty') {
          content = `${label} — nothing came back`;
        } else if (status === 'error') {
          content = `${label} — that didn't work`;
        } else {
          content = label;
        }
        out.push({ type: 'narration', data: { content } });
      }
      // ``tool_result`` intentionally emits NOTHING: it is the state change
      // for the row its ``tool_start`` already put on screen.
    }

    // Card events render AFTER the prose, in the order CARD_EVENT_TYPES
    // declares (see that constant — the four hardcoded filters this replaces
    // are exactly why next_steps, map and form never appeared in chat at all).
    //
    // ⚠ ONE list, and it lives with the renderer. A card the backend
    // publishes, the store keeps and ConversationThread can draw is still
    // invisible if this fold does not re-emit it, and that failure is silent:
    // the turn succeeds and the card is simply dropped. For a FORM that means
    // the assistant waits for an answer to a question nobody was asked.
    const cardEvents = (msg.events ?? []).filter((e) => isCardEvent(e.type as string));
    const orderedCards = CARD_EVENT_TYPES.flatMap((t) =>
      cardEvents.filter((e) => e.type === t),
    );

    if (!msg.content && idx === lastIdx && isRunning) {
      // Live turn with no content yet — render the bubble in streaming
      // mode so the cursor pulses. The token stream will fill it as
      // the agent runner publishes deltas into the store.
      out.push({
        type: 'assistant_message_start',
        data: { turn_id: turnId },
      });
      return;
    }

    out.push({
      type: 'assistant_message_start',
      data: { turn_id: turnId },
    });
    out.push({
      type: 'assistant_message_token',
      data: { turn_id: turnId, content: msg.content || '' },
    });
    // Only close the turn if it's not the live one.
    if (!(idx === lastIdx && isRunning && msg.content)) {
      out.push({
        type: 'assistant_message_end',
        data: { turn_id: turnId },
      });
    }

    // Emit in the declared card order, after the prose turn closes. The
    // renderer treats these as primary content (they flush pending chips and
    // narration pills the way a confirm gate does).
    for (const ce of orderedCards) {
      out.push({
        type: ce.type,
        data: (ce.data ?? {}) as Record<string, unknown>,
      } as (typeof out)[number]);
    }
  });

  return out;
}

function humaniseTool(name: string): string {
  // Reuse the same humanising the Inbox does — "buildium_list_properties"
  // → "buildium list properties". This is intentionally simple; the
  // ``narrate_tool`` map on the backend produces nicer phrasing but
  // requires a round-trip we'd rather avoid for live chat.
  // ``split + join`` over ``replaceAll`` because the TS lib target
  // doesn't include ES2021's String.prototype.replaceAll.
  return name.split('_').join(' ');
}

export function lastEventIsToolOrUser(events: ConversationEvent[]): boolean {
  // The thinking indicator only shows when the model is actively
  // working between turns — i.e. the last surfaced event is the
  // user's input or a tool result, not a half-rendered assistant
  // bubble. PR #75 — user input now flows through ``user_bubble``
  // events so we include that in the trigger set.
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (
      e.type === 'narration' ||
      e.type === 'assistant_message_end' ||
      e.type === 'user_bubble'
    ) {
      return true;
    }
    if (e.type === 'assistant_message_token') {
      return false;
    }
  }
  return false;
}

/** Events the agent RUNNER publishes. Everything else on the Inbox stream is
 *  the planner's own conversational shape (narration, the assistant trio,
 *  confirm gates), which ``foldEvents`` already draws and passes through. */
const RUNNER_EVENT_TYPES: ReadonlySet<string> = new Set([
  'token',
  'thinking',
  'tool_start',
  'tool_result',
  'handoff',
  'reasoning',
  'substep',
  'restatement',
  'memory_recall',
  'done',
  'error',
]);

/**
 * Fold the Inbox stream so a typed question renders the way it does in chat.
 *
 * Each run of RUNNER events after a user message becomes one agent message
 * and goes through ``foldMessagesToConversation``, the chat's own projection:
 * the same step trail, the same one-row-per-call labels, the same answer
 * bubble. Card events inside a run ride with it (chat renders them after the
 * prose); planner events pass through untouched.
 *
 * ``live`` is whether the Inbox believes a run is in flight. Only the LAST run
 * can be live, and only until it has a ``done`` or ``error``.
 */
export function foldInboxStream(events: ConversationEvent[], live: boolean): ConversationEvent[] {
  const out: ConversationEvent[] = [];
  let run: { start: number; tokens: string; evts: HelixisEvent[] } | null = null;

  const flush = (isLast: boolean) => {
    if (!run) return;
    const r = run;
    run = null;
    const ended = r.evts.some((e) => e.type === 'done' || e.type === 'error');
    let content = r.tokens;
    if (!content.trim()) {
      // Same recovery the chat store does: `done` carries the authoritative
      // reply when no token applied, and a runner error is a sentence meant
      // for the user. Unknown never renders as an empty bubble when the run
      // told us what happened (rule 14).
      const done = r.evts.find((e) => e.type === 'done');
      const err = r.evts.find((e) => e.type === 'error');
      const summary = typeof done?.data?.summary === 'string' ? done.data.summary : '';
      const message = typeof err?.data?.message === 'string' ? err.data.message : '';
      content = summary.trim() || message.trim();
    }
    const message: StoredMessage = {
      id: `inbox-${r.start}`,
      role: 'agent',
      content,
      timestamp: 0,
      events: r.evts,
    };
    out.push(...foldMessagesToConversation([message], live && isLast && !ended));
  };

  events.forEach((event, i) => {
    const type = event.type as string;
    const isRunner = RUNNER_EVENT_TYPES.has(type);
    // A card belongs to the run it arrived inside; outside a run it is the
    // planner's and passes through.
    if (isRunner || (run && isCardEvent(type))) {
      // ⚠ The SUBSCRIPTION failing is not an agent reply.
      // `subscribeInboxThread` synthesises that error from a thrown fetch and
      // tags it `connection: true`; its text is `String(err)`, which is not a
      // sentence, so it never opens or fills a bubble. A runner error (a run
      // refused before its first step, say) does, as it does in chat.
      if (type === 'error' && event.data?.connection === true) return;
      if (!run) run = { start: i, tokens: '', evts: [] };
      if (type === 'token') {
        run.tokens += String(event.data?.content ?? '');
      } else {
        run.evts.push({ type, data: event.data ?? {}, timestamp: event.timestamp ?? 0 });
      }
      if (type === 'done' || type === 'error') flush(false);
      return;
    }
    flush(false);
    out.push(event);
  });
  flush(true);
  return out;
}

/**
 * Is an answer typing itself out right now? The Inbox hides its working dots
 * only then, since the bubble's own cursor already says it is working.
 *
 * ⚠ Asked this way round on purpose. "Show dots when the last thing is a
 * step or a user message" (chat's rule) drops them when a refused message's
 * echo is retracted, while a run really is still going (a 409): the pill
 * then says nothing is happening when something is.
 */
export function answerIsStreaming(events: ConversationEvent[]): boolean {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === 'assistant_message_token') {
      return String(e.data?.content ?? '').trim() !== '';
    }
    if (e.type === 'assistant_message_end' || e.type === 'user_bubble' || e.type === 'narration') {
      return false;
    }
  }
  return false;
}
