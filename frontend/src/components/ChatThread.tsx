/**
 * Main chat area — conversational thread + empty hero (PR #71).
 *
 * The pre-PR-#71 implementation rendered a custom AgentTurn/UserTurn
 * surface fed by the local Zustand store's ``threads[]`` shape. PR #71
 * lifts the conversation primitives PR #70 built for the Inbox into
 * Chat: every assistant turn lives in an ``<AssistantBubble>``, tool
 * activity collapses into ``<NarrationLine>`` rows, and the orchestrator's
 * existing ``/agent/run`` SSE stream feeds the events array via the
 * store the runner already populates.
 *
 * EmptyHero stays as the first-paint surface — Brandon's design.
 * Stats strip was driven by the deleted ``useWebhooksStore`` /
 * ``useActivityStore`` (PR #71 dropped both); rendering it from real
 * backend data is PR #72 territory, so the strip is gone for now.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Eye } from 'lucide-react';
import { useStore } from '../stores/store';
import { useSubmitChatTask } from '../hooks/useSubmitChatTask';
import { ConversationThread } from './conversation/ConversationThread';
import type { ConversationEvent } from './conversation/types';
import { InputBar } from './InputBar';
import { GettingStartedCard } from './GettingStartedCard';
import { SyncProgressBanner } from './SyncProgressBanner';
import { ConfirmCard } from './ConfirmCard';
import { foldMessagesToConversation, lastEventIsToolOrUser } from './conversation/runnerFold';
import { firstNameFrom, greetingText, greetings } from '../lib/chatGreeting';

export function ChatThread() {
  const activeThreadId = useStore((s) => s.activeThreadId);
  const threads = useStore((s) => s.threads);
  const isRunning = useStore((s) => s.isRunning);
  const scrollRef = useRef<HTMLDivElement>(null);

  // A next-step chip sends its prompt down the SAME path as typing it, so a
  // click and a keystroke are indistinguishable to the backend — no parallel
  // execution route to keep in sync, and the turn shows up in history the way
  // the user's own words would. Ignored while a run is in flight; the shared
  // send path already refuses a concurrent turn.
  const submitTask = useSubmitChatTask();
  const askFollowUp = useCallback(
    (prompt: string) => {
      // A refusal (TaskNotSent) is expected here — a quick-action tapped
      // while a turn is finishing has nothing to restore, so it is swallowed
      // deliberately rather than surfacing as an unhandled rejection. The
      // shared path has already told the user via a toast.
      if (!isRunning) {
        void submitTask(prompt).catch(() => {});
      }
    },
    [isRunning, submitTask],
  );

  const activeThread = threads.find((t) => t.id === activeThreadId);

  // Project the store's per-message rows + per-event tool log into the
  // flat ConversationEvent[] the orchestrator expects. We do this
  // every render (cheap; memoised) so live token streams paint
  // immediately.
  const events = useMemo<ConversationEvent[]>(() => {
    if (!activeThread) return [];
    return foldMessagesToConversation(activeThread.messages, isRunning);
  }, [activeThread, isRunning]);

  // Stick to the bottom while the reader is there; always jump when they
  // have just sent something. It used to scroll on EVERY render, which pulled
  // the view down from under anyone reading an earlier answer mid-run.
  // Counted, not read off the last message: sending adds the user's message
  // AND the empty reply in one update, so the last row is never the user's.
  const sentCount = activeThread?.messages.filter((m) => m.role === 'user').length ?? 0;
  const seenSent = useRef(sentCount);
  useEffect(() => {
    const el = scrollRef.current;
    const justSent = sentCount !== seenSent.current;
    seenSent.current = sentCount;
    if (!el || typeof el.scrollTo !== 'function') return;
    const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (!justSent && gap > 240) return;
    el.scrollTo({ top: el.scrollHeight });
  });

  if (!activeThread) {
    return <EmptyHero scrollRef={scrollRef} />;
  }

  return (
    <div className="flex-1 overflow-y-auto" ref={scrollRef} data-follow-scroll>
      {/* Centered reading column. Inline margin/padding because a global
          unlayered `* { margin:0; padding:0 }` reset in globals.css beats
          Tailwind's layered spacing utilities (so mx-auto / px-* / py-*
          silently no-op here). See the styling note in globals.css. */}
      <div
        className="w-full max-w-[820px] flex flex-col"
        style={{
          marginInline: 'auto',
          paddingTop: 40,
          paddingBottom: 24,
          paddingLeft: 20,
          paddingRight: 20,
        }}
      >
        {/* Live-watch (multiplayer PR): watcher banner — this thread is
            driven by a teammate's run and we're attached read-only. */}
        {activeThread.status === 'watching' && (
          <div
            data-testid="watching-banner"
            className="flex items-center gap-2 mb-4 px-3 py-2 rounded-[var(--r-sm)] border text-[12px]"
            style={{
              borderColor: 'var(--line-strong)',
              background: 'var(--canvas-raised)',
              color: 'var(--ink-muted)',
            }}
          >
            <span
              className="shrink-0 w-1.5 h-1.5 rounded-full animate-pulse"
              style={{ background: 'var(--iris)' }}
            />
            Live — a teammate&apos;s agent is working in this thread. You&apos;re
            watching read-only.
          </div>
        )}
        {/* Presence-lite for the driver: teammates watching this run. */}
        {activeThread.status !== 'watching' &&
          (activeThread.watcherCount ?? 0) > 0 && (
            <div
              data-testid="watcher-count-chip"
              className="flex items-center gap-1.5 mb-4 text-[12px]"
              style={{ color: 'var(--ink-subtle)' }}
            >
              <Eye size={12} strokeWidth={1.75} />
              {activeThread.watcherCount === 1
                ? '1 teammate watching'
                : `${activeThread.watcherCount} teammates watching`}
            </div>
          )}
        <ConversationThread
          events={events}
          variant="chat"
          onAskFollowUp={askFollowUp}
          thinking={isRunning && (events.length === 0 || lastEventIsToolOrUser(events))}
        />
        <ConfirmCard />
        <div className="h-px" />
      </div>
    </div>
  );
}

// ── Empty hero ────────────────────────────────────────────────────

function EmptyHero({
  scrollRef,
}: {
  scrollRef: React.RefObject<HTMLDivElement | null>;
}) {
  const setView = useStore((s) => s.setView);
  return (
    <div className="flex-1 overflow-y-auto px-12 max-md:px-4 flex flex-col" ref={scrollRef}>
      <div className="w-full flex-1 flex flex-col">
        {/* Stage 3 audit: the first-run checklist lives INSIDE the hero
            column (it used to be a full-width band above the page, which
            pushed the hero off-center and voided the mobile layout). The
            old top-right History pill (its querySelector target never
            existed) and the hardcoded 'T' avatar are gone — the title
            bar already carries the real account affordance. */}
        <div className="w-full max-w-[580px] mx-auto pt-6">
          {/* Sits ABOVE the checklist: a half-loaded mirror changes how the
              user should read every answer, so it outranks setup steps.
              `GET /buildium/sync-status` shipped with a docstring saying the
              UI polls it and had zero callers — this is that caller. */}
          <SyncProgressBanner />
          <GettingStartedCard onNavigate={(target) => setView(target)} />
        </div>

        {/* flex-1 + justify-center: the composer stack centers in the space
            UNDER the checklist (founder 2026-08-20: the justify-start
            version sat too high). The wordmark and tagline that sat here are
            gone (brief, 2026-09-29): the header already names the product,
            and the tagline was an approval claim held for TODO(brandon). A
            greeting took their place (founder, 2026-09-30). */}
        <div className="relative flex-1 flex flex-col justify-center items-center gap-3 py-8">
          {/* PR #119 — hero column constrained to 580px to match the
              mockup. The pre-#119 hero spanned 1100px which made the
              composer feel "page-wide" instead of "centered with the
              conversation". */}
          <div
            className="relative z-[1] w-full max-w-[580px] mt-6 animate-fade-in-up"
            style={{ animationDelay: '280ms' }}
          >
            <ChatGreeting />
            <InputBar embedded />
            <StarterQuestions />
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * One of fifteen greetings (lib/chatGreeting.ts), picked once per visit to
 * the empty chat so it does not change under the reader while they type.
 * Uses a first name only when the account carries one; every greeting has a
 * complete nameless form.
 */
function ChatGreeting() {
  const user = useStore((s) => s.user);
  const first = firstNameFrom(user?.user_metadata as Record<string, unknown> | undefined);
  const [pick] = useState(() => {
    const hour = new Date().getHours();
    return { index: Math.floor(Math.random() * greetings(hour).length), hour };
  });
  return (
    <h1
      data-testid="chat-greeting"
      className="mb-5 text-center text-[28px] leading-tight"
      style={{ fontFamily: 'var(--font-display)', color: 'var(--ink)', textWrap: 'balance' }}
    >
      {greetingText(pick.index, first, pick.hour)}
    </h1>
  );
}

/**
 * Four questions a property manager asks in the first week, each answered
 * from the synced portfolio. A click SENDS it. The 2026-08-30 chips were
 * removed for typing a sentence into the box the user was already looking at;
 * these ask the question (docs/decisions.md, 2026-09-29).
 */
export const STARTER_QUESTIONS = [
  "Who's behind on rent?",
  "What's open at my properties?",
  'Which leases end in the next 90 days?',
  'What do we owe vendors?',
] as const;

function StarterQuestions() {
  const submitTask = useSubmitChatTask();
  const isRunning = useStore((s) => s.isRunning);
  return (
    <ul className="mt-4 flex flex-wrap gap-2" aria-label="Try asking" data-testid="chat-starters">
      {STARTER_QUESTIONS.map((q) => (
        <li key={q}>
          <button
            type="button"
            disabled={isRunning}
            onClick={() => {
              void submitTask(q).catch(() => undefined);
            }}
            className="h-9 px-3 rounded-[var(--r-sm)] border text-[14px] transition-colors disabled:cursor-not-allowed hover:bg-[var(--canvas-raised)]"
            style={{ borderColor: 'var(--line-strong)', color: 'var(--ink)', background: 'var(--canvas)' }}
          >
            {q}
          </button>
        </li>
      ))}
    </ul>
  );
}

// ── Test surface ──────────────────────────────────────────────────
//
// Exported so the ChatThread test can pin the projection without
// rendering through React's component tree.

export const __test__ = {
  foldMessagesToConversation,
  STARTER_QUESTIONS,
};
