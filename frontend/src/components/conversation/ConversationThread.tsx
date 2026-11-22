/**
 * The orchestrator. Accepts a list of conversation events (the
 * frontend-side projection of the backend's HelixisEvent stream) and
 * renders the right primitive per event type. Stateless — the parent
 * (InboxPage) owns the events array, drives picks back through
 * ``onPickSuggestion`` / ``onConfirm`` / ``onCancel`` callbacks, and
 * is responsible for opening / closing the underlying SSE.
 *
 * Built to be lifted into Chat in PR #71: the only Inbox-specific
 * thing here is the assumption that ``onPickSuggestion`` POSTs back
 * to the buildium routes — that mapping lives in the parent.
 */

import { Fragment, useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { ChevronDown, ChevronRight } from 'lucide-react';

import { AssistantBubble } from './AssistantBubble';
import { NarrationLine } from './NarrationLine';
import { SuggestionRow } from './SuggestionRow';
import { ConfirmGate } from './ConfirmGate';
import { UserBubble } from './UserBubble';
import type {
  ChartData,
  ChecklistData,
  DetailCardData,
  EmailDraftData,
  ScheduleData,
  ArtifactsData,
  ConfirmGateData,
  ConversationEvent,
  MapData,
  SuggestionChipData,
  TabularData,
  FormData,
  NextStepsData,
} from './types';
import { cardPayload } from './types';
import { TabularContent } from './TabularContent';
import { Skeleton } from '../Skeleton';
import { useElapsedSeconds } from '../../hooks/useElapsedSeconds';
import { MapCard } from './MapCard';
import { ArtifactCard } from './ArtifactCard';
import { EmailDraftCard } from './EmailDraftCard';
import { DetailCard } from './DetailCard';
import { ChartCard } from './ChartCard';
import { ScheduleCard } from './ScheduleCard';
import { ChecklistCard } from './ChecklistCard';
import { FormCard } from './FormCard';
import { NextStepsCard } from './NextStepsCard';
import {
  DisclosurePanel,
  parseDisclosure,
  type DisclosureData,
} from './DisclosurePanel';

export type ConversationVariant = 'chat' | 'inbox';

/** Seconds before the turn clock appears. Short turns get no counter. */
const _ELAPSED_VISIBLE_AFTER_S = 5;

export interface ConversationThreadProps {
  events: ConversationEvent[];
  /** ``"chat"`` renders right-aligned ``<UserBubble>`` for
   *  ``user_bubble`` events, gives assistant turns a soft surface, and
   *  collapses any narration lines into a single "Used N tools" pill
   *  above the next assistant bubble. ``"inbox"`` (default) preserves
   *  the existing typography-only layout. */
  variant?: ConversationVariant;
  /** Called when the user clicks a suggestion chip. The parent
   *  should POST back to the appropriate backend endpoint and
   *  fold the resulting events into the ``events`` array. */
  onPickSuggestion?: (chip: SuggestionChipData) => void;
  /** Send a next-step as the user's next message. Distinct from
   *  ``onPickSuggestion``, which EXECUTES a planner action against the
   *  Buildium routes — this one only asks the follow-up. */
  onAskFollowUp?: (prompt: string) => void;
  /** Called when the user clicks Execute on a confirm gate. */
  onConfirm?: (data: ConfirmGateData) => void;
  /** Called when the user clicks Cancel on a confirm gate. */
  onCancel?: (data: ConfirmGateData) => void;
  /** Whether the thread is currently waiting on a network round-trip
   *  (e.g. /execute in flight). Disables suggestion chips and the
   *  confirm gate's primary button. */
  busy?: boolean;
  /** Show a pulsing three-dot indicator below the last turn. The
   *  parent flips this on while waiting between assistant turns
   *  (e.g. after a click, before the next message starts streaming). */
  thinking?: boolean;
}

interface AssistantTurn {
  type: 'turn';
  turnId: string;
  content: string;
  streaming: boolean;
}


interface UserBubbleItem {
  type: 'user';
  id: number;
  content: string;
}

interface NarrationItem {
  type: 'narration';
  id: number;
  content: string;
}

/** One row of the chat activity trail.
 *
 * ⚠ The ``kind`` is what keeps the summary honest. Reasoning shares the trail
 * with the steps — that ordering is the point, a thought explains the step it
 * precedes — but a model thinking for ninety seconds would otherwise report
 * "47 steps" for four tool calls, and a count nobody can trust is worse than
 * no count. Steps are counted; thoughts are shown. */
export interface TrailLine {
  text: string;
  kind: 'step' | 'thought' | 'substep';
}

interface ToolPillItem {
  type: 'tool_pill';
  id: number;
  /** The trail in stream order ("→ Buildium Agent", the model's working-out,
   *  "Looking up the property", …). */
  lines: TrailLine[];
}

interface SuggestionsItem {
  type: 'suggestions';
  id: number;
  chips: SuggestionChipData[];
}

interface ConfirmGateItem {
  type: 'confirm';
  id: number;
  data: ConfirmGateData;
}

// PR #116 Phase B — structured tabular content (rent aging report,
// maintenance list, etc.) emitted by the agent as a ``tabular_data``
// event. Rendered via ``<TabularContent>`` (table + Badge for status
// cells) instead of inline markdown text.
interface TabularItem {
  type: 'tabular';
  id: number;
  data: TabularData;
  /** Held behind a Skeleton until the prose it belongs to finishes. */
  deferred?: boolean;
}

// maps #539 — structured static map emitted by a location tool as a ``map``
// event. Rendered via ``<MapCard>`` (an <img> pointed at our key-free static-map
// proxy) instead of a URL the model would have to relay.
interface MapItem {
  type: 'map';
  id: number;
  data: MapData;
}

// Downloadable artifacts a code-execution run produced, emitted as an
// ``artifacts`` event with signed links. Rendered via ``<ArtifactCard>``
// (a row of download buttons) instead of a URL the model would have to relay.
interface ArtifactsItem {
  type: 'artifacts';
  id: number;
  data: ArtifactsData;
}

// An email draft the agent composed, emitted as an ``email_draft`` event.
// Rendered via ``<EmailDraftCard>`` (To/Subject header + body + honesty
// footer) instead of pasting the draft into the assistant bubble as prose.
interface EmailDraftItem {
  type: 'email_draft';
  id: number;
  data: EmailDraftData;
}

// Gen-UI wave 2 — one record's facts / a numeric series / a day's slots / a
// step plan, each rendered by its typed card.
interface DetailCardItem {
  type: 'detail_card';
  id: number;
  data: DetailCardData;
}

interface ChartItem {
  type: 'chart';
  id: number;
  data: ChartData;
}

interface ScheduleItem {
  type: 'schedule';
  id: number;
  data: ScheduleData;
}

interface ChecklistItem {
  type: 'checklist';
  id: number;
  data: ChecklistData;
}

interface FormItem {
  type: 'form';
  id: number;
  data: FormData;
}

interface NextStepsItem {
  type: 'next_steps';
  id: number;
  data: NextStepsData;
}

/** What the user asked, said back to them within a couple of seconds of
 *  Enter — before the model has produced anything. */
interface RestatementItem {
  type: 'restatement';
  id: number;
  content: string;
}

interface MemoryRecallItem {
  type: 'memory_recall';
  id: number;
  count: number;
}

// S6 disclosure (#680 fast-follow) — the reasoning-loop's derived
// answer/reasoning/audit envelope, emitted once per loop turn just before
// ``done``. Rendered as a collapsed "How I got this" receipts panel under
// the answer. Non-loop turns never emit it; unknown-event clients ignore
// it (the ADR 0003 additive-event contract), so this is render-only.
interface DisclosureItem {
  type: 'disclosure';
  id: number;
  data: DisclosureData;
}

type RenderItem =
  | NextStepsItem
  | FormItem
  | AssistantTurn
  | UserBubbleItem
  | NarrationItem
  | ToolPillItem
  | SuggestionsItem
  | ConfirmGateItem
  | TabularItem
  | MapItem
  | ArtifactsItem
  | EmailDraftItem
  | DetailCardItem
  | ChartItem
  | ScheduleItem
  | ChecklistItem
  | MemoryRecallItem
  | RestatementItem
  | DisclosureItem;

// A table arrives COMPLETE (the agent queues the card while its tools run)
// while the prose above it is still streaming in. Rendering it immediately
// puts a finished ten-row table under a half-empty sentence — the reader's
// eye lands on the data before the answer that frames it, which is backwards.
// Deferred tables show a Skeleton until the turn they belong to stops
// streaming, so the answer arrives first and the table settles behind it.
const deferTablesWhileStreaming = (items: RenderItem[]): RenderItem[] => {
  // ⚠ Defer only what comes AFTER the sentence still being written.
  //
  // This used to defer EVERY table the moment ANY turn was streaming, which
  // is a different rule than the one it exists for. The model narrates
  // between its own tool calls, so one answer arrives as
  // `turn → tool_pill → turn → tool_pill → turn` — and every time a new turn
  // opened, a table that had already rendered above it collapsed back into a
  // 92px grey box and then popped out again. On a long answer that happens
  // several times, and it is the single most "broken-looking" thing in the
  // 2026-09-06 demo recording: finished content visibly un-rendering itself.
  //
  // The reason to defer is that a completed table under a half-written
  // sentence reads as an answer that jumped ahead of itself. That applies to
  // a table BELOW the live turn and to nothing else — a table above it
  // belongs to prose that is already finished.
  const firstStreaming = items.findIndex((i) => i.type === 'turn' && i.streaming);
  if (firstStreaming === -1) return items;
  return items.map((i, idx) =>
    i.type === 'tabular' && idx > firstStreaming ? { ...i, deferred: true } : i,
  );
};


/**
 * Fold the raw event stream into a render-ready list. We collapse
 * consecutive ASSISTANT_MESSAGE_TOKEN events for the same turn_id
 * into a single bubble; multiple SUGGESTION_CHIP events in a row
 * collapse into one SuggestionRow.
 *
 * PR #75 — when ``variant === "chat"`` we additionally collapse
 * consecutive narration lines into a single "Used N tools" pill so
 * the chat reads as a clean back-and-forth instead of a transcript
 * of every tool call.
 */
/**
 * ⚠ The ``variant`` parameter is GONE, not defaulted.
 *
 * It only ever decided whether narration folded into a trail, and it no
 * longer decides anything — both surfaces fold. Leaving it in the signature
 * would be an argument callers could still pass and that changes nothing,
 * which is worse than removing it: the next reader would believe the two
 * surfaces still differ here. ``variant`` remains on the COMPONENT, where it
 * still picks the assistant-bubble styling.
 */
export function foldEvents(events: ConversationEvent[]): RenderItem[] {
  const items: RenderItem[] = [];
  const turnsById = new Map<string, AssistantTurn>();
  let pendingChips: SuggestionChipData[] = [];
  let pendingNarrationLines: TrailLine[] = [];
  let nextId = 0;

  const flushChips = () => {
    if (pendingChips.length === 0) return;
    items.push({ type: 'suggestions', id: nextId++, chips: pendingChips });
    pendingChips = [];
  };
  const flushNarrationPill = () => {
    // Chat-variant only: emit a single activity-trail item carrying the run
    // of step lines we just buffered (routing + tool calls). In Inbox the
    // narrations already landed inline as ``NarrationLine`` items.
    if (pendingNarrationLines.length > 0) {
      items.push({
        type: 'tool_pill',
        id: nextId++,
        lines: pendingNarrationLines,
      });
      pendingNarrationLines = [];
    }
  };

  for (const event of events) {
    switch (event.type) {
      case 'narration':
      // ``reasoning`` rides the SAME trail as the steps, in stream order —
      // a thought explains the step it precedes, so separating them would
      // break the one thing that makes the wait readable. It is tagged
      // ``thought`` so it does not inflate the "N steps" summary.
      case 'reasoning':
      // A nested specialist's step. Same trail, same ordering — it happens
      // under the consult row directly above it — but its own kind, because
      // it is neither the parent's step nor a thought.
      case 'substep': {
        flushChips();
        const text = String((event.data as Record<string, unknown>).content ?? '');
        const kind: TrailLine['kind'] =
          event.type === 'reasoning' ? 'thought' : event.type === 'substep' ? 'substep' : 'step';
        // ⚠ BOTH variants now. This was `if (variant === 'chat')`, and the
        // inbox kept "its granular inline NarrationLines" — one italic row per
        // step, unfolded, forever.
        //
        // That was a reasonable split while the inbox was a card you read.
        // It stopped being one the moment typing turned an inbox item into a
        // conversation: the same run that put forty GMAIL_FETCH_EMAILS rows in
        // chat puts forty italic lines down the inbox pane, and every fix on
        // the trail — the folding, the promoted reasoning, the live status row
        // that reads the trail to say what it is doing — was inert there.
        // A fix bound to one surface is not a fix (gotcha 66).
        //
        // Buffer the line; flushed into one activity-trail item when we hit
        // the next non-trail event (the assistant bubble).
        pendingNarrationLines.push({ text, kind });
        break;
      }
      case 'user_bubble': {
        flushChips();
        flushNarrationPill();
        items.push({
          type: 'user',
          id: nextId++,
          content: String((event.data as Record<string, unknown>).content ?? ''),
        });
        break;
      }
      case 'assistant_message_start': {
        flushChips();
        flushNarrationPill();
        const turnId = String((event.data as Record<string, unknown>).turn_id ?? `t-${nextId}`);
        const turn: AssistantTurn = { type: 'turn', turnId, content: '', streaming: true };
        turnsById.set(turnId, turn);
        items.push(turn);
        break;
      }
      case 'assistant_message_token': {
        const data = event.data as Record<string, unknown>;
        const turnId = String(data.turn_id ?? '');
        const turn = turnsById.get(turnId);
        if (turn) {
          turn.content += String(data.content ?? '');
        }
        break;
      }
      case 'assistant_message_end': {
        const turnId = String((event.data as Record<string, unknown>).turn_id ?? '');
        const turn = turnsById.get(turnId);
        if (turn) turn.streaming = false;
        break;
      }
      case 'suggestion_chip': {
        const data = event.data as Record<string, unknown>;
        pendingChips.push({
          action_index: Number(data.action_index ?? 0),
          label: String(data.label ?? ''),
          parameter_patch:
            (data.parameter_patch as Record<string, unknown>) ?? {},
          is_destructive: Boolean(data.is_destructive ?? false),
        });
        break;
      }
      case 'confirm_gate': {
        flushChips();
        flushNarrationPill();
        const data = event.data as Record<string, unknown>;
        items.push({
          type: 'confirm',
          id: nextId++,
          data: {
            action_index: Number(data.action_index ?? 0),
            summary: String(data.summary ?? ''),
            payload_preview:
              (data.payload_preview as Record<string, unknown>) ?? {},
            confirm_label: String(data.confirm_label ?? 'Execute'),
            cancel_label: String(data.cancel_label ?? 'Cancel'),
            confirm_id:
              typeof data.confirm_id === 'string' ? data.confirm_id : undefined,
          },
        });
        break;
      }
      case 'tabular_data': {
        // PR #116 Phase B — structured table replaces markdown bullets
        // for the rent-aging / maintenance-list shapes the agent
        // emits. Tables flush any pending chips / narrations the same
        // way a confirm gate does — they're a "primary content"
        // surface, not a chip annotation.
        flushChips();
        flushNarrationPill();
        // The agent emits either {data: TabularData} or the TabularData
        // shape directly, and hydration can hand over JSON TEXT. Accept all
        // three -- see `cardPayload`.
        const td = cardPayload(event.data) as TabularData | null;
        if (td && typeof td === 'object' && Array.isArray(td.columns)) {
          items.push({
            type: 'tabular',
            id: nextId++,
            data: td,
          });
        }
        break;
      }
      case 'email_draft': {
        // The model composed a draft and chose to present it as a card —
        // primary content like a table, so flush pending chips/narration.
        // Accept {data: EmailDraftData} or the shape directly (demo + prod
        // parity, same convention as tabular/map/artifacts).
        flushChips();
        flushNarrationPill();
        const ed = cardPayload(event.data) as EmailDraftData | null;
        if (ed && typeof ed === 'object' && Array.isArray(ed.variants) && ed.variants.length) {
          items.push({
            type: 'email_draft',
            id: nextId++,
            data: {
              to: Array.isArray(ed.to) ? ed.to : [],
              cc: Array.isArray(ed.cc) ? ed.cc : [],
              variants: ed.variants.filter(
                (v) => v && typeof v === 'object' && typeof v.body === 'string' && v.body,
              ),
            },
          });
        }
        break;
      }
      case 'form': {
        // The assistant asking for the blanks. Rendered where it was raised,
        // so the question sits with the prose that led to it.
        flushChips();
        flushNarrationPill();
        const fd = cardPayload(event.data);
        if (fd && typeof fd === 'object' && Array.isArray(fd.fields) && fd.fields.length > 0) {
          items.push({ type: 'form', id: nextId++, data: fd as unknown as FormData });
        }
        break;
      }
      case 'next_steps': {
        // Rendered after the prose deliberately — the reply still says what
        // it recommends; these are the shortcut, not the recommendation.
        flushChips();
        flushNarrationPill();
        const nd = cardPayload(event.data);
        if (nd && typeof nd === 'object' && (Array.isArray(nd.steps) || nd.note_hint)) {
          items.push({
            type: 'next_steps',
            id: nextId++,
            data: nd as unknown as NextStepsData,
          });
        }
        break;
      }
      case 'detail_card':
      case 'chart':
      case 'schedule':
      case 'checklist': {
        // Gen-UI wave 2 — all four are primary content like a table; the
        // payload rides directly (or under {data}) and the renderer's typed
        // card owns the layout. Minimal shape guard per type.
        flushChips();
        flushNarrationPill();
        const d = cardPayload(event.data);
        const ok =
          d &&
          typeof d === 'object' &&
          (event.type === 'detail_card'
            ? Array.isArray(d.fields) && d.fields.length > 0
            : event.type === 'chart'
              ? Array.isArray(d.points) && d.points.length >= 2
              : event.type === 'schedule'
                ? Array.isArray(d.slots) && d.slots.length > 0
                : Array.isArray(d.items) && d.items.length > 0);
        if (ok) {
          items.push({
            type: event.type,
            id: nextId++,
            data: d,
          } as unknown as RenderItem);
        }
        break;
      }
      case 'map': {
        // maps #539 — a static map is primary content like a table, so flush
        // any pending chips / narration first. Accept {data: MapData} or the
        // MapData shape directly (demo + prod parity).
        flushChips();
        flushNarrationPill();
        const md = cardPayload(event.data) as MapData | null;
        if (md && typeof md === 'object' && typeof md.url === 'string' && md.url) {
          items.push({
            type: 'map',
            id: nextId++,
            data: md,
          });
        }
        break;
      }
      case 'artifacts': {
        // Code-execution downloads are primary content like a table/map, so
        // flush any pending chips/narration first. Accept {data: ArtifactsData}
        // or the ArtifactsData shape directly (demo + prod parity).
        flushChips();
        flushNarrationPill();
        const ad = cardPayload(event.data) as ArtifactsData | null;
        if (ad && typeof ad === 'object' && Array.isArray(ad.artifacts) && ad.artifacts.length) {
          items.push({
            type: 'artifacts',
            id: nextId++,
            data: ad,
          });
        }
        break;
      }
      case 'disclosure': {
        // S6 (#680 fast-follow) — receipts are meta-content that trails the
        // answer, but they still flush pending chips/narration so the panel
        // lands cleanly after the bubble instead of splitting a pill group.
        flushChips();
        flushNarrationPill();
        const parsed = parseDisclosure(event.data);
        if (parsed) {
          items.push({ type: 'disclosure', id: nextId++, data: parsed });
        }
        break;
      }
      case 'restatement': {
        // ⚠ Its own row, ABOVE the trail — never folded into the steps pill.
        // It is addressed to the USER ("Checking whether 4B is behind on
        // rent…"), not telemetry, and burying it at 11.5px inside a
        // collapsible pill would defeat the one job it has: being the first
        // thing on screen.
        flushChips();
        flushNarrationPill();
        items.push({
          type: 'restatement',
          id: nextId++,
          content: String((event.data as Record<string, unknown>).content ?? ''),
        });
        break;
      }
      case 'memory_recall': {
        // A "recalled from memory" line — its own primary signal, so flush
        // any buffered chips/narration first and never fold it into the
        // tool pill. ``results_found`` tells the user how much was recalled.
        flushChips();
        flushNarrationPill();
        items.push({
          type: 'memory_recall',
          id: nextId++,
          count: Number((event.data as Record<string, unknown>).results_found ?? 0),
        });
        break;
      }
      // Token / thinking / tool events from the wider HelixisEvent
      // surface are silently ignored here — Chat (PR #71) will route
      // them through this same component.
      default:
        break;
    }
  }
  flushChips();
  flushNarrationPill();
  return deferTablesWhileStreaming(items);
}

/**
 * Which assistant turns continue the one above them.
 *
 * The model now narrates between its own tool calls, so a single answer
 * arrives as several turns separated by the activity trail:
 * ``turn → tool_pill → turn → tool_pill → turn``. Rendering each with its own
 * avatar and "Occupella" badge stamps the same name five times down one reply.
 *
 * A turn continues the previous one when an assistant turn has already been
 * emitted since the user last spoke AND nothing but the activity trail sits
 * between them. Anything else in between — a table, a card, a confirm gate —
 * is a real break, and the badge earns its place again after it.
 */
/**
 * Generative action buttons that are SPENT once the user moves on.
 *
 * The rule (docs/decisions.md 2026-09-30, DESIGN.md "Generative actions"):
 * a button the assistant generated for THIS moment (a next step, a planner
 * chip, a form asking for the blanks) disappears as soon as the user takes
 * their next action, whether that is clicking one of them or typing a
 * prompt. Left in place, a transcript fills with stale offers ("Check vendor
 * capacity" three answers after the user already asked something else), and
 * every one of them still looks clickable.
 *
 * The signal is a user turn rendered AFTER the item: a click sends its prompt
 * down the same path as typing, so both land as a user bubble. Deriving it
 * from the transcript rather than from click state means a refused send
 * (a run still in flight) leaves the buttons where they were.
 *
 * ⚠ Confirm gates are deliberately NOT here. A gate is an approval for a
 * write the backend is waiting on; hiding it would leave that write pending
 * with no way to answer it.
 */
export const SPENT_AFTER_USER_TURN: ReadonlySet<RenderItem['type']> = new Set([
  'next_steps',
  'suggestions',
  'form',
]);

/** Indices of generative action items the user has already moved past. */
export function spentActionItems(items: RenderItem[]): Set<number> {
  const out = new Set<number>();
  let lastUser = -1;
  items.forEach((item, i) => {
    if (item.type === 'user') lastUser = i;
  });
  items.forEach((item, i) => {
    if (i < lastUser && SPENT_AFTER_USER_TURN.has(item.type)) out.add(i);
  });
  return out;
}

export function continuationTurns(items: RenderItem[]): Set<number> {
  const out = new Set<number>();
  let spokenSinceUser = false;
  items.forEach((item, i) => {
    if (item.type === 'user') {
      spokenSinceUser = false;
      return;
    }
    if (item.type !== 'turn') return;
    const prev = items[i - 1]?.type;
    if (spokenSinceUser && (prev === 'turn' || prev === 'tool_pill')) out.add(i);
    spokenSinceUser = true;
  });
  return out;
}

export function ConversationThread({
  events,
  variant = 'inbox',
  onPickSuggestion,
  onAskFollowUp,
  onConfirm,
  onCancel,
  busy,
  thinking,
}: ConversationThreadProps) {
  const items = useMemo(() => foldEvents(events), [events]);
  const continuations = useMemo(() => continuationTurns(items), [items]);
  const spent = useMemo(() => spentActionItems(items), [items]);
  // What the live status row says while the run works. Read off the trail
  // rather than tracked separately, so the two can never disagree about which
  // step is current.
  const elapsed = useElapsedSeconds(Boolean(thinking));
  const liveStatus = useMemo(() => {
    for (let i = items.length - 1; i >= 0; i -= 1) {
      const item = items[i];
      if (item.type === 'tool_pill') return latestTrailText(item.lines);
    }
    return '';
  }, [items]);

  return (
    <div className="flex flex-col" data-testid="conversation-thread" data-variant={variant}>
      <AnimatePresence initial={false}>
        {items.map((item, i) => {
          if (spent.has(i)) return null;
          switch (item.type) {
            case 'turn':
              return (
                <AssistantBubble
                  key={`turn-${item.turnId}`}
                  content={item.content}
                  streaming={item.streaming}
                  variant={variant}
                  continuation={continuations.has(i)}
                />
              );
            case 'user':
              return <UserBubble key={`user-${item.id}`} content={item.content} />;
            case 'narration':
              return <NarrationLine key={`narr-${item.id}`} content={item.content} />;
            case 'restatement':
              return (
                <div
                  key={`restate-${item.id}`}
                  data-testid="restatement-line"
                  className="mb-2 text-[14px] leading-snug"
                  style={{ color: 'var(--ink-muted)' }}
                >
                  {item.content}
                </div>
              );
            case 'memory_recall':
              return (
                <div
                  key={`recall-${item.id}`}
                  data-testid="memory-recall-line"
                  className="mb-2 text-[12px] italic"
                  style={{ color: 'var(--ink-muted)' }}
                >
                  {item.count > 1
                    ? `Recalled ${item.count} notes from memory`
                    : 'Recalled from memory'}
                </div>
              );
            case 'tool_pill':
              // The trail is finished the moment anything renders after it —
              // the assistant bubble, a card, a confirm gate.
              return (
                <ActivityTrail
                  key={`pill-${item.id}`}
                  lines={item.lines}
                  done={i < items.length - 1}
                />
              );
            case 'suggestions':
              return (
                <SuggestionRow
                  key={`sug-${item.id}`}
                  chips={item.chips}
                  onPick={(chip) => onPickSuggestion?.(chip)}
                  disabled={busy}
                />
              );
            case 'confirm':
              return (
                <ConfirmGate
                  key={`gate-${item.id}`}
                  data={item.data}
                  onConfirm={() => onConfirm?.(item.data)}
                  onCancel={() => onCancel?.(item.data)}
                  busy={busy}
                />
              );
            case 'tabular':
              // PR #116 Phase B — structured table renders inline in
              // the conversation stream, in the same horizontal band
              // an AssistantBubble would occupy. The Badge / currency
              // / status formatting lives inside TabularContent.
              //
              // While the answer above is still streaming, hold the table
              // behind a Skeleton: the data is already complete, so
              // rendering it now lands a finished table under a half-written
              // sentence. Reserve roughly the right height so the thread
              // does not jump when it resolves.
              if (item.deferred) {
                return (
                  <div
                    key={`tab-${item.id}`}
                    className="space-y-2 py-2"
                    data-testid={`tabular-${item.id}-pending`}
                    aria-busy="true"
                    aria-label="Preparing table"
                  >
                    <Skeleton variant="line" width="35%" />
                    <Skeleton variant="rect" height={92} />
                  </div>
                );
              }
              return (
                <TabularContent
                  key={`tab-${item.id}`}
                  data={item.data}
                  testIdPrefix={`tabular-${item.id}`}
                />
              );
            case 'map':
              // maps #539 — static map renders inline in the same band as a
              // table/bubble. The image is our key-free proxy (MapCard).
              return (
                <MapCard
                  key={`map-${item.id}`}
                  data={item.data}
                  testIdPrefix={`map-${item.id}`}
                />
              );
            case 'artifacts':
              // Code-execution downloads render inline as a row of Download
              // buttons in the same band as a table/bubble (ArtifactCard).
              return (
                <ArtifactCard
                  key={`artifacts-${item.id}`}
                  data={item.data}
                  testIdPrefix={`artifacts-${item.id}`}
                />
              );
            case 'email_draft':
              // The agent's composed draft renders as a real email card in
              // the same band as a table/bubble (EmailDraftCard).
              return (
                <EmailDraftCard
                  key={`email-draft-${item.id}`}
                  data={item.data}
                  testIdPrefix={`email-draft-${item.id}`}
                />
              );
            case 'detail_card':
              return (
                <DetailCard
                  key={`detail-${item.id}`}
                  data={item.data}
                  testIdPrefix={`detail-card-${item.id}`}
                />
              );
            case 'chart':
              return (
                <ChartCard
                  key={`chart-${item.id}`}
                  data={item.data}
                  testIdPrefix={`chart-card-${item.id}`}
                />
              );
            case 'schedule':
              return (
                <ScheduleCard
                  key={`schedule-${item.id}`}
                  data={item.data}
                  testIdPrefix={`schedule-card-${item.id}`}
                />
              );
            case 'checklist':
              return (
                <ChecklistCard
                  key={`checklist-${item.id}`}
                  data={item.data}
                  testIdPrefix={`checklist-card-${item.id}`}
                />
              );
            case 'form':
              return (
                <FormCard
                  key={`form-${item.id}`}
                  data={item.data}
                  onSubmit={onAskFollowUp}
                  testIdPrefix={`form-card-${item.id}`}
                />
              );
            case 'next_steps':
              return (
                <NextStepsCard
                  key={`next-steps-${item.id}`}
                  data={item.data}
                  onPick={onAskFollowUp}
                  testIdPrefix={`next-steps-${item.id}`}
                />
              );
            case 'disclosure':
              // S6 — collapsed "How I got this" receipts under the answer.
              return (
                <DisclosurePanel
                  key={`disclosure-${item.id}`}
                  data={item.data}
                  testIdPrefix={`disclosure-${item.id}`}
                />
              );
          }
        })}
      </AnimatePresence>

      {thinking && (
        <div
          aria-label="Occupella is working"
          data-testid="thinking-indicator"
          className="mb-6 flex items-center gap-1.5"
          style={{ color: 'var(--ink-muted)' }}
        >
          {/* ⚠ Say what it is DOING, or say NOTHING and let the dots carry it.
              "Thinking" was the first and often the only thing on screen for a
              minute and a half: true, useless, and indistinguishable from a
              hang. Keeping it as a fallback still put it there at the top of
              every single turn, which is the moment people actually notice it
              — a word that adds nothing is worse than no word beside three
              dots that already mean "working". */}
          {liveStatus ? <span className="text-[13px]">{liveStatus}</span> : null}
          {/* ⚠ Held back for the first few seconds. A counter that appears
              instantly on a two-second turn is nervous furniture; the reason
              it exists is the long tail, where the words alone cannot
              separate SLOW from STUCK. */}
          {elapsed >= _ELAPSED_VISIBLE_AFTER_S && (
            <span
              data-testid="thinking-elapsed"
              className="text-[12px] tabular-nums"
              style={{ color: 'var(--ink-faint)' }}
            >
              {elapsed}s
            </span>
          )}
          <span
            className="inline-block w-1 h-1 rounded-full"
            style={{ background: 'var(--ink-muted)', animation: 'pulse-dot 1.1s var(--ease) infinite' }}
          />
          <span
            className="inline-block w-1 h-1 rounded-full"
            style={{
              background: 'var(--ink-muted)',
              animation: 'pulse-dot 1.1s var(--ease) infinite',
              animationDelay: '160ms',
            }}
          />
          <span
            className="inline-block w-1 h-1 rounded-full"
            style={{
              background: 'var(--ink-muted)',
              animation: 'pulse-dot 1.1s var(--ease) infinite',
              animationDelay: '320ms',
            }}
          />
        </div>
      )}
    </div>
  );
}

/**
 * Chat activity trail — the "what I'm doing" transparency. Replaces the old
 * count-only "Used N tools" pill with the actual step lines in order: the
 * specialist routed to ("→ Buildium Agent") and each tool called. A summary
 * row ("N steps") toggles the trail; it starts EXPANDED so the work is
 * visible while an answer is loading, and can be collapsed to keep the
 * transcript tidy. The Inbox variant is unaffected — it keeps its granular
 * inline NarrationLines.
 */
/**
 * Group the trail so a specialist's internal calls fold under the step that
 * caused them.
 *
 * ⚠ A live turn put **forty** `GMAIL_FETCH_EMAILS` substeps on screen, one
 * per line, under a group that starts expanded. That is a debugger's view: it
 * says what the machine did and nothing about what it worked out, and it
 * buries the one line worth reading ("Checking whether all leases are being
 * fully enforced") a page above the fold. Substeps still exist — the trail is
 * the transparency promise and hiding them outright would be worse — they are
 * just behind their own count now.
 */
function groupTrail(lines: TrailLine[]): { line: TrailLine; subs: TrailLine[] }[] {
  const groups: { line: TrailLine; subs: TrailLine[] }[] = [];
  for (const line of lines) {
    if (line.kind === 'substep' && groups.length > 0) {
      groups[groups.length - 1].subs.push(line);
      continue;
    }
    groups.push({ line, subs: [] });
  }
  return groups;
}

/** The most recent line, for the live status row. Empty when nothing yet. */
export function latestTrailText(lines: TrailLine[]): string {
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const text = lines[i]?.text?.trim();
    if (text) return text.replace(/^→\s*/, '');
  }
  return '';
}

function SubstepGroup({ subs }: { subs: TrailLine[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="pl-3">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        data-testid="trail-substep-toggle"
        aria-expanded={open}
        className="inline-flex items-center gap-1 text-[13px]"
        style={{
          background: 'none',
          border: 'none',
          padding: 0,
          color: 'var(--ink-muted)',
          cursor: 'pointer',
        }}
      >
        {open ? <ChevronDown size={14} aria-hidden /> : <ChevronRight size={14} aria-hidden />}
        {subs.length === 1 ? '1 lookup' : `${subs.length} lookups`}
      </button>
      {open && (
        <div className="mt-1 flex flex-col gap-1">
          {subs.map((s, i) => (
            <div
              key={i}
              data-testid="trail-substep"
              className="flex items-center gap-2 text-[12px]"
              style={{ color: 'var(--ink-faint)' }}
            >
              <span
                aria-hidden
                className="inline-block w-1 h-1 rounded-full shrink-0"
                style={{ background: 'var(--ink-faint)' }}
              />
              <span>{s.text}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ActivityTrail({ lines, done = false }: { lines: TrailLine[]; done?: boolean }) {
  // ⚠ Auto-collapses when the answer lands, and a manual toggle wins
  // permanently. Expanded is right WHILE it works — that is the whole
  // point of a trail — and clutter under a finished answer. `null` means
  // nobody has expressed a preference yet, which is why this is not a
  // plain boolean: a `false` default would collapse a running turn.
  const [manual, setManual] = useState<boolean | null>(null);
  const open = manual ?? !done;
  const setOpen = (fn: (v: boolean) => boolean) => setManual(fn(open));
  if (lines.length === 0) return null;
  // ⚠ STEPS ONLY. A thought is not a step, and counting the model's
  // working-out would report "47 steps" for four tool calls.
  const steps = lines.filter((l) => l.kind === 'step').length;
  return (
    <motion.div
      data-testid="tool-pill"
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
      // The trail and the answer are separate things: a hairline and space
      // under the steps so the answer reads as the answer.
      className="mb-4 pb-3 border-b"
      style={{ borderColor: 'var(--line)' }}
    >
      {/* One status style across the turn: 13px muted text with a chevron,
          the same for the steps, their lookups and the live status line. */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="inline-flex items-center gap-1 text-[13px]"
        style={{ background: 'none', border: 'none', padding: 0, color: 'var(--ink-muted)', cursor: 'pointer' }}
      >
        {open ? <ChevronDown size={14} aria-hidden /> : <ChevronRight size={14} aria-hidden />}
        {/* ⚠ "0 steps" is honest and reads as a failure. A trail carrying
            only the model's working-out is a turn that thought and answered
            without calling anything — say that, rather than reporting a
            count of the thing it correctly did not do. */}
        {steps === 0 ? 'Thinking' : steps === 1 ? '1 step' : `${steps} steps`}
      </button>
      {open && (
        <div
          className="mt-1.5 ml-1 flex flex-col gap-1 border-l pl-3"
          style={{ borderColor: 'var(--line)' }}
        >
          {groupTrail(lines).map(({ line, subs }, i) => {
            const isRoute = line.kind === 'step' && line.text.startsWith('→');
            const isThought = line.kind === 'thought';
            return (
              <Fragment key={i}>
                <div
                  data-testid={isThought ? 'trail-thought' : 'trail-step'}
                  className={
                    isThought
                      ? 'text-[12px] leading-snug'
                      : 'flex items-center gap-2 text-[12px]'
                  }
                  style={{
                    // ⚠ The reasoning is the thing worth reading, so it is no
                    // longer the dimmest text on screen. It used to render
                    // faint AND italic — quieter than the tool names it
                    // explains, which is backwards.
                    color: isRoute
                      ? 'var(--iris-hover)'
                      : isThought
                        ? 'var(--ink)'
                        : 'var(--ink-subtle)',
                  }}
                >
                  {!isRoute && !isThought && (
                    <span
                      aria-hidden
                      className="inline-block w-1 h-1 rounded-full shrink-0"
                      style={{ background: 'var(--ink-faint)' }}
                    />
                  )}
                  <span style={{ fontWeight: isRoute ? 600 : 400 }}>{line.text}</span>
                </div>
                {subs.length > 0 ? <SubstepGroup subs={subs} /> : null}
              </Fragment>
            );
          })}
        </div>
      )}
    </motion.div>
  );
}
