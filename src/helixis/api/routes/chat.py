"""GET /api/v1/chat — session list + per-session message replay (PR #71).

The Chat sidebar's "Threads" panel needs a real backend after PR #70
deleted the legacy demo seed. Two endpoints land here:

- ``GET /chat/sessions`` — recent distinct session ids for the user's
  company, with the first user-message preview and the last touched
  timestamp. Powers the sidebar list.
- ``GET /chat/sessions/{session_id}/messages`` — every persisted turn
  for one session, oldest-first, scoped to the user's company. Used
  when the user clicks a thread to replay it before opening the live
  SSE.

Both endpoints lean on ``helixis.agents.chat_history`` for the heavy
lifting — the route layer just authenticates + scopes + serialises.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from helixis.agents.chat_history import (
    list_sessions_for_company,
    load_session_messages_for_company,
)
from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.rate_limit import connectors_limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatSession(BaseModel):
    session_id: str
    preview: str
    created_at: str
    # Live-watch PR — a run is in flight for this session right now.
    live: bool = False


class ChatSessionList(BaseModel):
    rows: list[ChatSession]


class ChatMessage(BaseModel):
    id: str
    role: Literal["user", "assistant", "tool"]
    content: str
    tool_name: str | None = None
    tool_result: Any = None
    created_at: str
    user_id: str | None = None


class ChatMessageList(BaseModel):
    rows: list[ChatMessage]


def _require_company(user: AuthenticatedUser) -> str:
    if not user.company_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No company associated with this account.",
        )
    return user.company_ids[0]


@router.get("/sessions")
async def list_sessions(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    limit: int = Query(default=50, ge=1, le=100),
) -> ChatSessionList:
    """Recent chat sessions for THIS PERSON. Returns at most ``limit`` rows
    (default 50, max 100), ordered by last-touched time descending. Used by
    the Chat sidebar's Threads panel.

    ⚠ Per-user, not per-company (gotcha 85). The first invited teammate saw
    the admin's entire thread list as their own sidebar. A chat thread is a
    person's working notes; the Inbox is the company's shared queue."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    rows = await list_sessions_for_company(company_id, user_id=user.user_id, limit=limit)
    return ChatSessionList(rows=[ChatSession(**row) for row in rows])


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    request: Request,
    session_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    limit: int = Query(default=500, ge=1, le=2000),
) -> ChatMessageList:
    """Full message list for one session, scoped to the CALLER.

    Returns rows oldest-first so the frontend can replay the thread
    into a ``<ConversationThread>`` before opening the live SSE. If
    the session doesn't exist or belongs to a different company, the
    response is an empty list (consistent with the rest of the
    Buildium read endpoints — never 404 on a missing-but-permissible
    row to keep the UI's empty-state code path simple)."""
    await connectors_limiter.check_by_user(request, user)
    company_id = _require_company(user)
    rows = await load_session_messages_for_company(
        session_id, company_id, user_id=user.user_id, limit=limit
    )
    return ChatMessageList(rows=[ChatMessage(**row) for row in rows])


__all__ = ["router"]
