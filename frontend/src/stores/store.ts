/**
 * Zustand store for Occupella frontend state.
 * Manages conversations, active tasks, and UI state.
 */

import { create } from 'zustand';
import type { User, Session } from '@supabase/supabase-js';
import type { HelixisEvent } from '../services/agent';
import type { TeamRole } from '../services/company';
import { CARD_HISTORY_PREFIX } from '../components/conversation/types';
import type { ChatMessage as PersistedChatMessage } from '../services/chat';

export interface ChatMessage {
  id: string;
  role: 'user' | 'agent';
  content: string;
  timestamp: number;
  events: HelixisEvent[];  // Tool cards, confirmations, etc.
}

export interface TaskThread {
  id: string;
  title: string;
  messages: ChatMessage[];
  /** ``watching`` (live-watch PR) — a teammate's agent is driving this
   *  thread and we're attached read-only via GET /agent/watch. */
  status: 'idle' | 'running' | 'waiting_confirm' | 'watching' | 'done' | 'error';
  createdAt: number;
  /** Live-watch PR — the backend reports a run in flight for this session
   *  (sidebar badge; clicking attaches as a watcher). */
  live?: boolean;
  /** How many teammates are watching this thread right now (drivers see
   *  this via the ``watcher_update`` presence event). */
  watcherCount?: number;
}

/** A message typed while a turn was running, waiting to send as a follow-up.
 *
 * ⚠ ``threadId`` is what stops it landing in the wrong conversation:
 * ``isRunning`` is global, so the user can queue on one thread and switch to
 * another before the turn ends. */
export interface QueuedMessage {
  text: string;
  threadId: string | null;
}

export interface ConfirmationPending {
  confirmId: string;
  action: string;
  title: string;
  details: string;
  items: string[];
  /** The ACTUAL outbound request payload when the backend supplied one
   *  (additive `payload_preview.request_body`, approval-pipeline PR) — shown
   *  so the approver sees what will really be sent, not just a summary. */
  requestBody?: Record<string, unknown>;
  /** Edit-before-approve: fields of requestBody the gate declared editable.
   *  The card renders these as prefilled inputs; edited values ride back on
   *  the approval. Absent/empty = read-only gate (e.g. every SMS gate —
   *  carrier-registered templates are never editable). */
  editableFields?: string[];
  /** The thread whose run raised this gate.
   *
   * ⚠ Without it, `setActiveThread` had to CLEAR the gate on every thread
   * switch — because the card renders from one store-wide value and would
   * otherwise appear over a conversation it has nothing to do with, offering
   * a write the reader never asked for.
   *
   * Clearing it loses it. A gate waits up to 300s server-side, so glancing at
   * another thread mid-approval silently destroyed the only way to say yes;
   * the run then timed out and denied by default. Safe, and completely
   * invisible — the user asked for a write, saw a card, looked away, and the
   * write simply never happened.
   *
   * Carrying the thread lets the card be HIDDEN rather than destroyed: it
   * renders only in its own thread and is still there when you come back.
   */
  threadId?: string | null;
}

interface HelixisStore {
  // Auth
  user: User | null;
  session: Session | null;
  companyId: string | null;
  companyIds: string[];
  /** The caller's role in the active company (ADR 0005). Null until the
   *  `GET /company/me` probe resolves; used to gate Settings-screen
   *  visibility (e.g. the Team & Permissions section is manager+). */
  companyRole: TeamRole | null;
  isAuthLoading: boolean;

  // Top-level navigation
  view: 'chat' | 'inbox' | 'properties' | 'crm' | 'search' | 'settings' | 'dev-runs';
  /**
   * Items waiting in the Inbox, for the sidebar badge.
   *
   * ⚠ `null` is UNKNOWN, not zero. Zero asserts "your inbox is clear", which
   * a fetch that failed has not earned — and the badge only draws above 0, so
   * unknown correctly shows nothing.
   */
  inboxCount: number | null;
  /** Which Settings section to open on. Team Activity left the sidebar for
   *  Settings (2026-09-02), and ⌘K / the `g a` chord still have to reach it —
   *  so the destination is a settings SECTION now, not a top-level view. */
  settingsSection: string;
  /**
   * Is there typed work on screen that leaving would destroy?
   *
   * ⚠ Written by `useUnsavedWork` so there is ONE registration point, and read
   * by `setView` / `openSettings` so every navigation is guarded from one
   * place — the ~20 setView call sites, the ⌘K palette, the `g <key>` chords
   * and Back/Forward all end here.
   */
  unsavedWork: boolean;

  // Threads
  threads: TaskThread[];
  activeThreadId: string | null;

  // Active task state
  isRunning: boolean;
  streamingText: string;
  pendingConfirmation: ConfirmationPending | null;
  abortController: AbortController | null;

  // Composer draft — lifted from InputBar local state so suggestion
  // tiles on the empty hero (and anywhere else) can prefill it.
  composerDraft: string;
  setComposerDraft: (text: string) => void;

  // Messages typed WHILE a turn is running.
  //
  // ⚠ These send when the current turn FINISHES. They are not injected into
  // the run in flight — the SDK run is already dispatched and there is no
  // channel into it — so every surface that shows this queue has to say so.
  // A user who adds context believing the agent just read it, and gets an
  // answer that ignored it, has been misled by the UI rather than helped.
  //
  // The alternative was refusing the keystroke, which is what shipped: the
  // composer was ``disabled`` mid-run, so a thought that arrived during a
  // three-minute turn had nowhere to go.
  //
  // ⚠ Each carries the THREAD it was written for. ``isRunning`` is global, so
  // a user can queue a follow-up on thread A and switch to B before the turn
  // ends — draining into whatever is active would file their words in the
  // wrong conversation. Items for another thread stay queued until that
  // thread is active again rather than being dropped.
  queuedMessages: QueuedMessage[];
  queueMessage: (text: string, threadId: string | null) => void;
  removeQueuedMessage: (index: number) => void;
  /** Atomic drain of the messages for ``threadId`` — reads and removes in ONE
   *  ``set``, so two drains cannot both see the same items and send them
   *  twice (gotcha 25's reserve-before-awaiting, arriving in React). */
  takeQueuedMessages: (threadId: string | null) => string[];

  // Activity rail visibility — persisted to localStorage so the
  // user's "collapsed" preference survives reloads. Auto-opens when
  // the agent starts a task or a confirmation is pending.
  railOpen: boolean;
  setRailOpen: (open: boolean) => void;
  toggleRail: () => void;

  // Left sidebar — expanded (260px) vs icon-rail (60px). Persisted
  // to a cookie so the preference survives reloads. Toggle with
  // ⌘B / Ctrl+B or the button in the TitleBar.
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (collapsed: boolean) => void;
  toggleSidebar: () => void;

  // Auth actions
  setAuth: (user: User | null, session: Session | null) => void;
  setCompanies: (companyIds: string[]) => void;
  setCompanyId: (companyId: string) => void;
  setCompanyRole: (role: TeamRole | null) => void;
  clearAuth: () => void;

  // Navigation
  /** Returns false when the person chose to stay and finish what they typed. */
  setView: (view: 'chat' | 'inbox' | 'properties' | 'crm' | 'search' | 'settings' | 'dev-runs') => boolean;
  setInboxCount: (count: number | null) => void;
  /** Open Settings on a named section. */
  openSettings: (section?: string) => boolean;
  setUnsavedWork: (unsavedWork: boolean) => void;

  // Actions
  createThread: () => string;
  setActiveThread: (id: string) => void;
  /** PR #71 — merge backend-loaded chat sessions into the local
   *  threads list. Preserves any in-progress local thread (one with
   *  ``status='running'`` and a non-empty messages array) and appends
   *  the rest by id. Idempotent — re-running with the same payload
   *  is a no-op. */
  hydrateThreads: (
    rows: { session_id: string; preview: string; created_at: string; live?: boolean }[],
  ) => void;
  /** PR #71 — replace a thread's messages with a backend-loaded
   *  replay. Used when the user clicks a stored thread in the
   *  sidebar; the messages come from
   *  ``GET /api/v1/chat/sessions/{id}/messages``. */
  /**
   * ⚠ Takes the FULL persisted row, not a narrowed copy. The old signature
   * declared only `{role, content, id}` — so `tool_name` / `tool_result`, the
   * two fields the replay endpoint has always returned and the two that carry
   * a stored card, were invisible to this function by TYPE. Narrowing a
   * parameter to what the code happens to use today is how the data needed
   * to fix a bug ends up looking like data that does not exist.
   */
  hydrateThreadMessages: (threadId: string, rows: PersistedChatMessage[]) => void;
  /*
   * ⚠ EVERY ONE of these takes the thread the RUN belongs to, and routing by
   * anything else is the defect they were changed to fix.
   *
   * They all used to write to `activeThreadId` — whatever thread was selected
   * at the instant the event arrived, not the one whose question was being
   * answered. A run is minutes long and a thread switch is one click, so the
   * two are routinely different: the demo take of 2026-09-06 rendered q16's
   * answer about 200 E Colfax under q19's "Which leads have gone quiet?".
   * That is the worst failure this product has, because the answer is not
   * wrong — it is CORRECT for a question the user is not looking at, so
   * nothing about it reads as an error.
   *
   * `threadId` is optional only so the existing single-thread callers keep
   * working; omitting it falls back to `activeThreadId` exactly as before.
   * Any caller that KNOWS its thread must pass it — `useSubmitChatTask` and
   * `useWatchThread` both do, and an AST guard pins that they still do
   * (a behavioural test cannot see a caller that quietly stopped).
   */
  addUserMessage: (content: string, threadId?: string) => void;
  startAgentResponse: (threadId?: string) => void;
  appendToken: (token: string, threadId?: string) => void;
  addEvent: (event: HelixisEvent, threadId?: string) => void;
  finishAgentResponse: (summary: string, threadId?: string) => void;
  setError: (message: string, threadId?: string) => void;
  setPendingConfirmation: (confirm: ConfirmationPending | null) => void;
  setAbortController: (controller: AbortController | null) => void;
  reset: () => void;

  // Live watch (multiplayer PR) — a teammate's run, attached read-only.
  /** Mark a thread as being watched and append the empty agent bubble the
   *  incoming token stream will fill (mirror of startAgentResponse without
   *  touching isRunning — that belongs to the caller's OWN runs). */
  startWatching: (threadId: string) => void;
  /** End a watch: fill the blank bubble from the terminal event if nothing
   *  streamed, mark done/error, clear the live badge. Idempotent — the
   *  stream's 'done' event and its close both funnel here. */
  endWatching: (threadId: string, summary: string, isError?: boolean) => void;
  /** Presence-lite: the ``watcher_update`` event's count for a thread. */
  setThreadWatcherCount: (threadId: string, count: number) => void;
}

let nextId = 0;
const genId = () => `t_${Date.now()}_${nextId++}`;

const RAIL_KEY = 'helixis:railOpen';

function readRailOpen(): boolean {
  if (typeof window === 'undefined') return true;
  try {
    const raw = window.localStorage.getItem(RAIL_KEY);
    if (raw === null) return true;
    return raw === '1';
  } catch {
    return true;
  }
}

function writeRailOpen(open: boolean): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(RAIL_KEY, open ? '1' : '0');
  } catch {
    // Quota exceeded or Safari private mode — fine, just don't persist.
  }
}

const SIDEBAR_COOKIE = 'helixis_sidebar_collapsed';
const SIDEBAR_COOKIE_MAX_AGE = 60 * 60 * 24 * 30; // 30 days

function readSidebarCollapsed(): boolean {
  if (typeof document === 'undefined') return false;
  const match = document.cookie.match(
    new RegExp(`(?:^|; )${SIDEBAR_COOKIE}=([^;]*)`),
  );
  return match?.[1] === '1';
}

function writeSidebarCollapsed(collapsed: boolean): void {
  if (typeof document === 'undefined') return;
  document.cookie = `${SIDEBAR_COOKIE}=${collapsed ? '1' : '0'}; path=/; max-age=${SIDEBAR_COOKIE_MAX_AGE}; samesite=lax`;
}

/**
 * Ask before throwing away typed work, on an IN-APP navigation.
 *
 * ⚠ `useUnsavedWork` covers `beforeunload` — closing the tab, reloading. It
 * cannot see a view change, because no document is unloading. So a half-filled
 * carrier registration (legal name, EIN, address, an authorised
 * representative, none of it autosaved) vanished silently the moment somebody
 * clicked Chat in the rail, with the tab-close warning looking like it had
 * that covered.
 *
 * ⚠ URL sync made this WIDER on 2026-09-08. Back and Forward now change the
 * view, so the browser's own buttons became a way to lose the form that did
 * not exist the day before.
 *
 * ⚠ `window.confirm`, deliberately, and this is the one place in the app that
 * uses it. A store action is synchronous and a React modal is not — returning
 * "did they agree" from `setView` is what lets Back/Forward put the address
 * bar back when they say no. A custom dialog here would mean every one of the
 * ~20 call sites awaiting a promise, which is how a guard ends up skipped on
 * the nineteenth.
 */
function confirmLeavingUnsavedWork(): boolean {
  if (!useStore.getState().unsavedWork) return true;
  if (typeof window === 'undefined' || typeof window.confirm !== 'function') return true;
  return window.confirm(
    'You have unsaved changes on this page. Leave and lose them?',
  );
}

export const useStore = create<HelixisStore>((set, get) => ({
  // Auth
  user: null,
  session: null,
  companyId: null,
  companyIds: [],
  companyRole: null,
  isAuthLoading: true,

  // Navigation
  view: 'chat',
  inboxCount: null,
  settingsSection: 'account',
  unsavedWork: false,

  // Threads
  threads: [],
  activeThreadId: null,
  isRunning: false,
  streamingText: '',
  pendingConfirmation: null,
  abortController: null,
  composerDraft: '',
  setComposerDraft: (text) => set({ composerDraft: text }),

  queuedMessages: [],
  queueMessage: (text, threadId) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    set((s) => ({ queuedMessages: [...s.queuedMessages, { text: trimmed, threadId }] }));
  },
  removeQueuedMessage: (index) =>
    set((s) => ({ queuedMessages: s.queuedMessages.filter((_, i) => i !== index) })),
  takeQueuedMessages: (threadId) => {
    const all = get().queuedMessages;
    const mine = all.filter((q) => q.threadId === threadId);
    if (mine.length === 0) return [];
    set({ queuedMessages: all.filter((q) => q.threadId !== threadId) });
    return mine.map((q) => q.text);
  },

  railOpen: readRailOpen(),
  setRailOpen: (open) => {
    writeRailOpen(open);
    set({ railOpen: open });
  },
  toggleRail: () => {
    const next = !get().railOpen;
    writeRailOpen(next);
    set({ railOpen: next });
  },

  sidebarCollapsed: readSidebarCollapsed(),
  setSidebarCollapsed: (collapsed) => {
    writeSidebarCollapsed(collapsed);
    set({ sidebarCollapsed: collapsed });
  },
  toggleSidebar: () => {
    const next = !get().sidebarCollapsed;
    writeSidebarCollapsed(next);
    set({ sidebarCollapsed: next });
  },

  setAuth: (user, session) => {
    set({ user, session, isAuthLoading: false });
  },

  setCompanies: (companyIds) => {
    set({
      companyIds,
      companyId: companyIds.length > 0 ? companyIds[0] : null,
    });
  },

  setCompanyId: (companyId) => {
    set({ companyId });
  },

  setCompanyRole: (companyRole) => {
    set({ companyRole });
  },

  clearAuth: () => {
    set({
      user: null,
      session: null,
      companyId: null,
      companyIds: [],
      companyRole: null,
      isAuthLoading: false,
    });
  },

  setView: (view) => {
    if (view === get().view) return true;
    if (!confirmLeavingUnsavedWork()) return false;
    set({ view });
    return true;
  },
  setInboxCount: (inboxCount) => set({ inboxCount }),
  openSettings: (section = 'account') => {
    const s = get();
    if (s.view === 'settings' && s.settingsSection === section) return true;
    if (!confirmLeavingUnsavedWork()) return false;
    set({ view: 'settings', settingsSection: section });
    return true;
  },
  setUnsavedWork: (unsavedWork) => set({ unsavedWork }),

  createThread: () => {
    const id = genId();
    const thread: TaskThread = {
      id,
      title: 'New task',
      messages: [],
      status: 'idle',
      createdAt: Date.now(),
    };
    set((s) => ({
      threads: [thread, ...s.threads],
      activeThreadId: id,
      isRunning: false,
      streamingText: '',
      pendingConfirmation: null,
    }));
    return id;
  },

  setActiveThread: (id) => {
    // ⚠ `pendingConfirmation` is deliberately NOT cleared here any more.
    // It carries its own `threadId` and the card renders only in that
    // thread, so switching away HIDES the gate instead of destroying it —
    // and a glance at another conversation no longer silently costs the
    // user the only way to approve a write they asked for.
    set({ activeThreadId: id, streamingText: '' });
  },

  hydrateThreads: (rows) => {
    set((s) => {
      // Index existing threads so we can preserve in-progress local
      // state. The session_id from the backend matches our thread id
      // (the runner passes the same uuid back into save_message).
      const byId = new Map(s.threads.map((t) => [t.id, t] as const));
      const merged: TaskThread[] = [];
      // Backend rows first (most-recent at top — they arrive
      // already ordered DESC by last-touched).
      for (const row of rows) {
        const existing = byId.get(row.session_id);
        if (existing) {
          merged.push({
            ...existing,
            title: existing.title === 'New task' ? row.preview : existing.title,
            live: row.live ?? false,
          });
          byId.delete(row.session_id);
        } else {
          merged.push({
            id: row.session_id,
            title: row.preview || 'Untitled thread',
            messages: [],
            status: 'done',
            createdAt: row.created_at ? new Date(row.created_at).getTime() : Date.now(),
            live: row.live ?? false,
          });
        }
      }
      // Append any in-progress local threads the backend doesn't know
      // about yet (the orchestrator hasn't finished its first save).
      for (const t of byId.values()) {
        merged.push(t);
      }
      // PR #125 — belt-and-braces dedup pass. The construction above
      // ALREADY prevents duplicates (every backend row is removed
      // from byId after merge), but the Claude extension's UI audit
      // reported seeing the same thread twice in the sidebar after
      // submit. Likely cause was an effect re-firing in dev /
      // StrictMode that race-condition'd hydrate + createThread.
      // Filtering by first-seen id here makes the bug unreachable
      // regardless of upstream call ordering. Keeps the FIRST
      // occurrence so the merged ordering (backend rows first,
      // then leftovers) is preserved.
      const seen = new Set<string>();
      const deduped = merged.filter((t) => {
        if (seen.has(t.id)) return false;
        seen.add(t.id);
        return true;
      });
      return { threads: deduped };
    });
  },

  hydrateThreadMessages: (threadId, rows) => {
    // ⚠ Cards used to be lost on every reload. They are published on the live
    // SSE stream and, since 2026-09-01, ALSO written as `role='tool'` rows
    // named `card:<type>` — so replay can put them back. Before that a
    // refreshed thread kept the prose and dropped the form, the table, the
    // chart and the next-step chips; for a FORM that is a dead end, because
    // the assistant is waiting on an answer to a question no longer on screen
    // and was told not to re-ask it in prose.
    //
    // A card row is written DURING the turn and the assistant's own row lands
    // at the end, so in `created_at` order the cards arrive FIRST. They are
    // buffered and attached to the assistant message that follows — the turn
    // they belong to. `ChatThread` then renders them after that turn's prose,
    // exactly where the live path puts them.
    const messages: ChatMessage[] = [];
    let pendingCards: HelixisEvent[] = [];

    for (const r of rows) {
      if (r.role === 'tool') {
        const name = r.tool_name ?? '';
        if (name.startsWith(CARD_HISTORY_PREFIX) && r.tool_result) {
          pendingCards.push({
            type: name.slice(CARD_HISTORY_PREFIX.length),
            data: r.tool_result as Record<string, unknown>,
          } as HelixisEvent);
        }
        // Every other tool row is dropped: its content renders as narration
        // from the live stream and there is nothing to replay.
        continue;
      }
      // ``ChatMessage.role`` is the legacy ``'user' | 'agent'`` shape the live
      // SSE path writes; the persisted role is ``'user' | 'assistant'``.
      const isUser = r.role === 'user';
      messages.push({
        id: r.id,
        role: isUser ? ('user' as const) : ('agent' as const),
        content: r.content,
        timestamp: Date.now(),
        events: isUser ? [] : pendingCards,
      });
      // ⚠ Cleared for BOTH roles. A card buffered before a USER message
      // belongs to a turn that already ended; carrying it forward would
      // re-render the previous answer's form under the next reply.
      pendingCards = [];
    }

    set((s) => ({
      threads: s.threads.map((t) => (t.id !== threadId ? t : { ...t, messages })),
    }));
  },

  addUserMessage: (content, threadId) => {
    // Stale-gate fix (design review, 2026-08-18): starting a NEW turn while a
    // gate is still pending abandons that gate — the old card must not
    // re-render below the new turn's content. The backend side times the
    // unanswered confirm out safely (deny-by-default).
    if (get().pendingConfirmation) {
      set({ pendingConfirmation: null });
    }
    const target = threadId ?? get().activeThreadId;
    if (!target) return;

    const msg: ChatMessage = {
      id: genId(),
      role: 'user',
      content,
      timestamp: Date.now(),
      events: [],
    };

    set((s) => ({
      threads: s.threads.map((t) =>
        t.id === target
          ? { ...t, title: content.slice(0, 60), messages: [...t.messages, msg] }
          : t,
      ),
    }));
  },

  startAgentResponse: (threadId) => {
    const target = threadId ?? get().activeThreadId;
    if (!target) return;

    const msg: ChatMessage = {
      id: genId(),
      role: 'agent',
      content: '',
      timestamp: Date.now(),
      events: [],
    };

    set((s) => ({
      isRunning: true,
      streamingText: '',
      threads: s.threads.map((t) =>
        t.id === target ? { ...t, status: 'running', messages: [...t.messages, msg] } : t,
      ),
    }));
  },

  appendToken: (token, threadId) => {
    set((s) => {
      const target = threadId ?? s.activeThreadId;
      // ⚠ The reply accumulates on the MESSAGE, not on the store-wide
      // `streamingText`. The old shape rebuilt the bubble from that single
      // global buffer every token — and `setActiveThread` clears it — so
      // clicking another thread mid-run truncated the running answer to
      // whatever arrived after the click, in the thread that was still
      // legitimately streaming. `streamingText` is kept only because the
      // thinking-reset and the tests read it; nothing renders from it.
      if (!target) return { streamingText: s.streamingText + token };

      return {
        streamingText: s.streamingText + token,
        threads: s.threads.map((t) => {
          if (t.id !== target) return t;
          const msgs = [...t.messages];
          const last = msgs[msgs.length - 1];
          if (last?.role === 'agent') {
            msgs[msgs.length - 1] = { ...last, content: last.content + token };
          }
          return { ...t, messages: msgs };
        }),
      };
    });
  },

  addEvent: (event, threadId) => {
    const target = threadId ?? get().activeThreadId;
    if (!target) return;

    if (event.type === 'confirm' || event.type === 'confirm_gate') {
      // PR #75 Bug E — the live backend now emits ``confirm_gate``
      // (see ``helixis.common.events.EventType.CONFIRM_GATE``); the
      // legacy ``confirm`` shape from the demo runner is kept for
      // back-compat. Both shapes carry the same data: a ``confirm_id``
      // the user resolves via POST /agent/confirm, plus a title/
      // summary line and an optional ``items`` preview. Normalise into
      // ``pendingConfirmation`` so ``<ConfirmCard>`` renders.
      const data = event.data as Record<string, unknown>;
      const items = (data.items as string[] | undefined)
        ?? ((data.payload_preview as Record<string, unknown> | undefined)?.items as string[] | undefined)
        ?? [];
      set({
        pendingConfirmation: {
          threadId: target,
          confirmId: data.confirm_id as string,
          action: (data.action as string | undefined)
            ?? ((data.payload_preview as Record<string, unknown> | undefined)?.action as string | undefined)
            ?? '',
          title: (data.title as string | undefined) ?? (data.summary as string | undefined) ?? '',
          details: (data.details as string | undefined) ?? (data.summary as string | undefined) ?? '',
          items,
          requestBody: (data.payload_preview as Record<string, unknown> | undefined)
            ?.request_body as Record<string, unknown> | undefined,
          editableFields:
            ((data.payload_preview as Record<string, unknown> | undefined)
              ?.editable_fields as string[] | undefined) ??
            (data.editable_fields as string[] | undefined),
        },
      });

      set((s) => ({
        threads: s.threads.map((t) =>
          // A watcher's thread stays 'watching' — the ConfirmCard still
          // renders (a watching manager may legitimately approve), but the
          // composer stays locked to the driving run.
          t.id === target && t.status !== 'watching' ? { ...t, status: 'waiting_confirm' } : t,
        ),
      }));
    }

    // ⚠ The SYNTHESIZE footer is RECEIPTS, not prose — and it was being
    // rendered as both.
    //
    // A loop-mode answer ends with three contract lines:
    //
    //     Sources: buildium: $2,400 past due; lease §4: late fees capped at $50
    //     Confidence: likely — no corroborating payment record found
    //     Unverified: tenant's claimed payment on the 3rd
    //
    // Those stream as ordinary tokens, so they land in the reply bubble. The
    // backend then publishes a `disclosure` event whose `answer` is the SAME
    // text with that footer stripped, and the client rendered the panel and
    // left the raw footer sitting in the prose above it. The demo take shows
    // the result: an answer that ends "Sources: … Confidence: likely — the
    // data is internally consistent, but…", which reads as unfinished
    // scaffolding to anyone who is not us.
    //
    // Swap the bubble to the stripped text the server already computed.
    //
    // ⚠ The rule stays in ONE place. Re-implementing the strip in TypeScript
    // would be a second copy of a contract that lives in
    // `agents/disclosure.py::strip_synthesis_footer`, free to drift (gotcha
    // 25). This only USES its answer.
    //
    // ⚠ And it only acts when it can SEE that the two agree. `answer` is
    // derived from `final_output`, while the bubble holds streamed deltas, and
    // those legitimately differ (gotcha 56). So the swap requires the stripped
    // text to be a genuine PREFIX of what is on screen — the strip can only
    // ever remove a suffix, so a prefix match is proof we are looking at the
    // same answer. Anything else declines and leaves the text alone: a panel
    // beside a footer is untidy, replacing a good answer with an unrelated one
    // is a bug.
    if (event.type === 'disclosure') {
      const stripped = String(
        (event.data as Record<string, unknown> | undefined)?.answer ?? '',
      ).trimEnd();
      if (stripped) {
        set((s) => ({
          threads: s.threads.map((t) => {
            if (t.id !== target) return t;
            const msgs = [...t.messages];
            const last = msgs[msgs.length - 1];
            if (last?.role !== 'agent') return t;
            const current = last.content.trimEnd();
            if (current.length <= stripped.length || !current.startsWith(stripped)) return t;
            msgs[msgs.length - 1] = { ...last, content: stripped };
            return { ...t, messages: msgs };
          }),
        }));
      }
    }

    // Reasoning visibility: a ``thinking`` event is the backend flushing
    // the text the model streamed BEFORE its next tool call — the same
    // text ``appendToken`` has been accumulating into the reply bubble.
    // The thought now lives in the activity trail (this event), so reset
    // the bubble; the final reply is only what streams after the last
    // tool result. This is what un-glues "…property count now.I checked…".
    const isThought = event.type === 'thinking';

    set((s) => ({
      ...(isThought ? { streamingText: '' } : {}),
      threads: s.threads.map((t) => {
        if (t.id !== target) return t;
        const msgs = [...t.messages];
        const last = msgs[msgs.length - 1];
        if (last?.role === 'agent') {
          msgs[msgs.length - 1] = {
            ...last,
            ...(isThought ? { content: '' } : {}),
            events: [...last.events, event],
          };
        }
        return { ...t, messages: msgs };
      }),
    }));
  },

  finishAgentResponse: (summary, threadId) => {
    const target = threadId ?? get().activeThreadId;
    if (!target) return;

    set((s) => ({
      isRunning: false,
      streamingText: '',
      abortController: null,
      threads: s.threads.map((t) => {
        if (t.id !== target) return { ...t };
        const msgs = [...t.messages];
        const last = msgs[msgs.length - 1];
        // Blank-bubble recovery. The streamed ``token`` events are the
        // primary source of the reply text (see ``appendToken``), but under
        // rapid / long-thread load some can fail to apply client-side,
        // leaving a BLANK agent bubble even though the turn completed fine
        // server-side (observed ~1-in-3 in the #7 live battery; the same
        // query re-run paced returns full text). The terminal ``done`` event
        // carries the authoritative reply (``summary``), so use it as a
        // fallback — but ONLY when nothing streamed, so a good stream (which
        // may exceed the backend's summary truncation) is never clobbered.
        if (last?.role === 'agent' && !last.content.trim() && summary.trim()) {
          msgs[msgs.length - 1] = { ...last, content: summary };
        }
        return { ...t, messages: msgs, status: 'done' };
      }),
    }));
  },

  setError: (message, threadId) => {
    const target = threadId ?? get().activeThreadId;
    if (!target) return;

    // Surface the error IN the reply instead of leaving a blank agent bubble
    // (#7 F7 — a 429 daily-budget / any pre-stream failure showed nothing at
    // all). Fill the empty streaming placeholder if it exists; else append an
    // agent bubble carrying the message.
    const text = message || 'Something went wrong. Please try again.';
    set((s) => ({
      isRunning: false,
      streamingText: '',
      abortController: null,
      threads: s.threads.map((t) => {
        if (t.id !== target) return t;
        const msgs = [...t.messages];
        const last = msgs[msgs.length - 1];
        if (last && last.role === 'agent' && !last.content) {
          msgs[msgs.length - 1] = { ...last, content: text };
        } else {
          msgs.push({
            id: genId(),
            role: 'agent',
            content: text,
            timestamp: Date.now(),
            events: [],
          });
        }
        return { ...t, status: 'error', messages: msgs };
      }),
    }));
  },

  setPendingConfirmation: (confirm) => {
    set({ pendingConfirmation: confirm });
  },

  setAbortController: (controller) => {
    set({ abortController: controller });
  },

  reset: () => {
    const { abortController } = get();
    abortController?.abort();
    set({
      isRunning: false,
      streamingText: '',
      pendingConfirmation: null,
      abortController: null,
    });
  },

  startWatching: (threadId) => {
    const msg: ChatMessage = {
      id: genId(),
      role: 'agent',
      content: '',
      timestamp: Date.now(),
      events: [],
    };
    set((s) => ({
      // The token stream writes through streamingText → the LAST agent
      // bubble of the ACTIVE thread, same as a driven run.
      streamingText: '',
      threads: s.threads.map((t) =>
        t.id === threadId
          ? { ...t, status: 'watching', live: true, messages: [...t.messages, msg] }
          : t,
      ),
    }));
  },

  endWatching: (threadId, summary, isError = false) => {
    set((s) => ({
      streamingText: '',
      threads: s.threads.map((t) => {
        if (t.id !== threadId) return t;
        // Idempotent: 'done' fires this via the terminal event AND the
        // stream close — the second call must not clobber the first.
        if (t.status !== 'watching' && t.status !== 'waiting_confirm') return t;
        const msgs = [...t.messages];
        const last = msgs[msgs.length - 1];
        if (last?.role === 'agent' && !last.content.trim() && summary.trim()) {
          msgs[msgs.length - 1] = { ...last, content: summary };
        }
        return {
          ...t,
          messages: msgs,
          status: isError ? 'error' : 'done',
          live: false,
          watcherCount: undefined,
        };
      }),
    }));
  },

  setThreadWatcherCount: (threadId, count) => {
    set((s) => ({
      threads: s.threads.map((t) =>
        t.id === threadId ? { ...t, watcherCount: count } : t,
      ),
    }));
  },
}));
