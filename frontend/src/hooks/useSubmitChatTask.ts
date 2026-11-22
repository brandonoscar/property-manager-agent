/**
 * useSubmitChatTask — the single send path for a chat turn.
 *
 * Extracted from InputBar so BOTH the composer and the chat
 * quick-actions rail submit through exactly the same flow (auth check →
 * thread → user message → agent run → streamed events). Returns
 * ``submitTask(task)``; callers handle their own input clearing.
 */

import { useCallback } from 'react';
import { toast } from 'sonner';

import { runAgent } from '../services/agent';
import type { HelixisEvent } from '../services/agent';
import { getAccessToken } from '../services/auth';
import { SUPPORT_EMAIL } from '../lib/support';
import { useStore } from '../stores/store';

/**
 * Thrown when a submit was REFUSED rather than attempted, so the composer can
 * restore the text it optimistically cleared. A refusal that looks identical
 * to a success is what made the message-loss bug invisible.
 */
export class TaskNotSent extends Error {}

export function useSubmitChatTask(): (task: string) => Promise<void> {
  // ⚠ NOT subscribed: isRunning / activeThreadId / threads are read from
  // ``useStore.getState()`` INSIDE the callback instead.
  //
  // Zustand's ``set`` is synchronous but React's re-render is not, so between
  // a state change and the commit that rebuilds this callback there is a
  // window where the handler React already holds closes over the PREVIOUS
  // values. A keystroke landing in that window ran a stale callback, and the
  // stale guard below (``if (!task || isRunning) return``) bailed silently —
  // after InputBar had already cleared the box. The user's first message
  // after starting a thread vanished with no error at all.
  // ⚠ `companyId` IS VOLATILE TOO, and it was the one value in this callback
  // still captured at render time while the three below it were moved to a
  // live read. It resolves ASYNCHRONOUSLY on boot — App.tsx awaits
  // `ensureCompanyProvisioned()` and only then calls `setCompanies` — so the
  // composer renders with it null, and a keystroke landing before React
  // commits the rebuilt callback reads null and refuses a send the store
  // could have served. Same window, same file, same defect as the header
  // describes; it is read from `getState()` below with the others.
  const setThreadWatcherCount = useStore((s) => s.setThreadWatcherCount);
  const createThread = useStore((s) => s.createThread);
  const addUserMessage = useStore((s) => s.addUserMessage);
  const startAgentResponse = useStore((s) => s.startAgentResponse);
  const appendToken = useStore((s) => s.appendToken);
  const addEvent = useStore((s) => s.addEvent);
  const finishAgentResponse = useStore((s) => s.finishAgentResponse);
  const setError = useStore((s) => s.setError);
  const setAbortController = useStore((s) => s.setAbortController);

  return useCallback(
    async (rawTask: string) => {
      const task = rawTask.trim();
      if (!task) return;

      // Live reads — see the note on the subscriptions above.
      const { isRunning, activeThreadId, threads, companyId } = useStore.getState();
      if (isRunning) {
        // ⚠ QUEUED, NOT REFUSED — and the message says so in those words.
        //
        // A thought that arrives during a three-minute turn used to have
        // nowhere to go: the composer was disabled, and this path threw. But
        // "queued" must never be read as "the agent is reading it now" — the
        // run is already dispatched and there is no channel into it. A user
        // who adds context, believes it landed, and gets an answer that
        // ignored it has been misled by the UI rather than helped, which is
        // strictly worse than being told to wait.
        useStore.getState().queueMessage(task, activeThreadId);
        toast.info('Queued — it sends as a follow-up when this turn finishes.');
        return;
      }

      // Live watch (multiplayer PR): a watched thread is being driven by a
      // teammate's run — the composer stays read-only until it finishes.
      // The backend enforces the same rule with a 409 (session lock); this
      // guard just gives the friendlier local message.
      const activeThread = threads.find((t) => t.id === activeThreadId);
      if (activeThread?.status === 'watching') {
        // ⚠ REFUSED, not queued. This thread is driven by a TEAMMATE's run;
        // queuing would silently attach your words to the end of someone
        // else's turn. The backend enforces the same rule with a 409 session
        // lock — this is the friendlier local message.
        //
        // ⚠ And it THROWS. This branch used to return, after InputBar had
        // already cleared the box, so the words were gone — the exact defect
        // gotcha 44 fixed one branch above and left standing here.
        toast.info("This thread is live — a teammate's agent is still working in it.");
        throw new TaskNotSent('thread is driven by a teammate');
      }

      // ⚠ BOTH OF THESE THROW, for the reason the branch above spells out.
      //
      // They used to `return` — after InputBar had already called
      // `setInput('')`, so the user's words were gone with a toast as the
      // only trace. That is gotcha 44's defect exactly, and gotcha 44's own
      // fix reached the `watching` branch three lines up and stopped there:
      // the same file, the same callback, two of its four refusal exits
      // still silent. The `isRunning` exit is fine because it QUEUES, so the
      // text has a destination; these two had none.
      const token = await getAccessToken();
      if (!token) {
        // A null token here means the stored session expired and the refresh
        // failed, so the SIGNED_OUT handler is about to say so too. Say what
        // happened rather than "not authenticated", which reads as a
        // contradiction on a screen the app only renders when signed in.
        toast.error('Your session expired. Sign in again to send this.');
        throw new TaskNotSent('session expired');
      }
      if (!companyId) {
        // ⚠ NOT "no company selected". There is no company picker for
        // somebody with zero companies, so naming the state was an
        // instruction nobody could carry out (gotchas 102/106/118/133).
        //
        // Reload is the one act that CAN change this: App.tsx re-runs
        // `ensureCompanyProvisioned()` on mount, which re-queries and, if the
        // membership row really is missing, POSTs the idempotent bootstrap.
        // A blank list is usually one failed request on a bad connection.
        toast.error(
          `Your workspace didn't finish loading. Reload the page — if it keeps happening, email ${SUPPORT_EMAIL}.`,
        );
        throw new TaskNotSent('no company resolved');
      }

      // ⚠ EVERY store write below names THIS thread explicitly.
      //
      // They used to route by `activeThreadId` — read fresh inside the store
      // each time an event arrived, minutes into a run. Switching threads (or
      // starting a new one, which is one click) redirected the rest of the
      // answer into whatever was selected then. The 2026-09-06 demo take
      // rendered a 200 E Colfax answer under "Which leads have gone quiet?"
      // because of it, and an answer that is CORRECT for another question
      // does not read as an error to anyone watching.
      let threadId = activeThreadId;
      if (!threadId) threadId = createThread();
      const runThreadId = threadId;

      addUserMessage(task, runThreadId);
      startAgentResponse(runThreadId);

      const controller = await runAgent({
        task,
        companyId,
        token,
        sessionId: threadId,
        onEvent: (event: HelixisEvent) => {
          switch (event.type) {
            case 'token':
              appendToken(event.data.content as string, runThreadId);
              break;
            case 'done':
              finishAgentResponse(event.data.summary as string, runThreadId);
              break;
            case 'error':
              // ⚠ ONE surface, not two. `setError` already writes the message
              // into the reply bubble, so a toast carrying the same words put
              // the identical sentence on screen twice — visible in the demo
              // take as a floating card repeating the message directly below
              // it. The toast is kept only for the case the bubble cannot
              // serve: the run's thread is not the one being looked at.
              setError(event.data.message as string, runThreadId);
              if (useStore.getState().activeThreadId !== runThreadId) {
                toast.error(event.data.message as string);
              }
              break;
            case 'watcher_update':
              // Presence-lite: a teammate joined/left this run's stream.
              setThreadWatcherCount(runThreadId, (event.data.count as number) ?? 0);
              break;
            default:
              addEvent(event, runThreadId);
          }
        },
        onError: (err) => {
          setError(err.message, runThreadId);
          if (useStore.getState().activeThreadId !== runThreadId) toast.error(err.message);
        },
        onDone: () => {
          /* Handled by 'done' event */
        },
      });

      setAbortController(controller);
    },
    [
      createThread,
      addUserMessage,
      startAgentResponse,
      appendToken,
      addEvent,
      finishAgentResponse,
      setError,
      setAbortController,
      setThreadWatcherCount,
    ],
  );
}
