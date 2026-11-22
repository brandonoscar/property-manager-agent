/**
 * useWatchThread — attach read-only to a teammate's live agent run
 * (multiplayer live-watch PR).
 *
 * The single watch path, mirroring useSubmitChatTask's event routing so a
 * watched run renders exactly like a driven one: tokens fill the streaming
 * bubble, tool activity becomes narration cards, and a surfaced confirm
 * gate renders the same ConfirmCard (a watching manager may approve it —
 * POST /agent/confirm carries its own ownership + role gates server-side).
 *
 * Also the driver's own re-attach path: after a page refresh mid-run,
 * clicking the live thread replays the transcript and this hook picks the
 * stream back up — the run no longer has to be "lost" with the tab.
 */

import { useCallback } from 'react';

import { watchSession } from '../services/agent';
import type { HelixisEvent } from '../services/agent';
import { useStore } from '../stores/store';

export function useWatchThread(): (threadId: string) => Promise<void> {
  const startWatching = useStore((s) => s.startWatching);
  const endWatching = useStore((s) => s.endWatching);
  const appendToken = useStore((s) => s.appendToken);
  const addEvent = useStore((s) => s.addEvent);
  const setThreadWatcherCount = useStore((s) => s.setThreadWatcherCount);

  return useCallback(
    async (threadId: string) => {
      startWatching(threadId);
      await watchSession({
        sessionId: threadId,
        onEvent: (event: HelixisEvent) => {
          switch (event.type) {
            // ⚠ NAMED, not `activeThreadId`. Watching is the case where the
            // two differ by design — you attach to a teammate's thread and
            // then look at your own — so routing by whatever is selected put
            // their answer in your conversation.
            case 'token':
              appendToken(event.data.content as string, threadId);
              break;
            case 'done':
              endWatching(threadId, (event.data.summary as string) ?? '');
              break;
            case 'error':
              endWatching(threadId, (event.data.message as string) ?? '', true);
              break;
            case 'watcher_update':
              setThreadWatcherCount(threadId, (event.data.count as number) ?? 0);
              break;
            default:
              addEvent(event, threadId);
          }
        },
        onError: () => endWatching(threadId, '', true),
        // Stream closed without a terminal event (the driving run's session
        // ended, or there was nothing live to attach to) — endWatching is
        // idempotent, so this is a no-op after a proper 'done'.
        onDone: () => endWatching(threadId, ''),
      });
    },
    [startWatching, endWatching, appendToken, addEvent, setThreadWatcherCount],
  );
}
