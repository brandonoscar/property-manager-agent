/**
 * Chat history data layer (PR #71).
 *
 * Wraps the two backend endpoints the conversational Chat surface
 * uses:
 *   - GET /api/v1/chat/sessions — recent threads in the sidebar
 *   - GET /api/v1/chat/sessions/:id/messages — replay one thread
 *
 * The orchestrator's `/agent/run` SSE stream still handles live
 * turns; these endpoints are read-only — they only feed the threads
 * list and the optional click-to-replay path.
 */

import { apiFetch } from './api';

export interface ChatSession {
  session_id: string;
  preview: string;
  created_at: string;
  /** Live-watch PR — an agent run is in flight for this session right now,
   *  so the sidebar badges it and clicking it attaches as a watcher. */
  live?: boolean;
}

export interface ChatSessionList {
  rows: ChatSession[];
}

export type ChatRole = 'user' | 'assistant' | 'tool';

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  tool_name?: string | null;
  tool_result?: unknown;
  created_at: string;
  user_id?: string | null;
}

export interface ChatMessageList {
  rows: ChatMessage[];
}

export async function listChatSessions(limit = 50): Promise<ChatSessionList> {
  const res = await apiFetch(`/api/v1/chat/sessions?limit=${limit}`);
  return (await res.json()) as ChatSessionList;
}

export async function loadChatMessages(
  sessionId: string,
  limit = 500,
): Promise<ChatMessageList> {
  const res = await apiFetch(
    `/api/v1/chat/sessions/${encodeURIComponent(sessionId)}/messages?limit=${limit}`,
  );
  return (await res.json()) as ChatMessageList;
}
