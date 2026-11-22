"""WebSocket RPC server for browser automation commands.

Bidirectional channel between the backend and the browser extension.
When the agent needs to automate browser tabs, commands flow over
this WebSocket and the extension executes CDP commands.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, cast

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)


class BrowserRPCSession:
    """Manages a WebSocket connection to the browser extension."""

    def __init__(self, ws: WebSocket, session_id: str):
        self.ws = ws
        self.session_id = session_id
        self._pending: dict[str, asyncio.Future] = {}

    async def send_command(
        self,
        action: str,
        params: dict | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """Send a command to the extension and wait for the result."""
        request_id = str(uuid.uuid4())
        command = {
            "action": action,
            "params": params or {},
            "request_id": request_id,
        }

        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = future

        await self.ws.send_json(command)

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return cast(dict[Any, Any], result)
        except TimeoutError:
            self._pending.pop(request_id, None)
            raise TimeoutError(f"Browser command '{action}' timed out after {timeout}s")

    def handle_response(self, data: dict) -> None:
        """Called when the extension sends a response."""
        request_id = data.get("request_id")
        if request_id and request_id in self._pending:
            future = self._pending.pop(request_id)
            if not future.done():
                future.set_result(data)


# Active browser RPC sessions
_sessions: dict[str, BrowserRPCSession] = {}


def get_browser_session(session_id: str) -> BrowserRPCSession | None:
    return _sessions.get(session_id)


async def handle_browser_websocket(ws: WebSocket, session_id: str) -> None:
    """WebSocket endpoint handler for browser automation RPC.

    The extension connects here when the agent needs browser control.
    Commands flow backend -> extension, results flow extension -> backend.
    """
    await ws.accept()
    session = BrowserRPCSession(ws, session_id)
    _sessions[session_id] = session

    logger.info("Browser RPC session connected: %s", session_id)

    try:
        while True:
            data = await ws.receive_json()
            session.handle_response(data)
    except WebSocketDisconnect:
        logger.info("Browser RPC session disconnected: %s", session_id)
    except Exception as e:
        logger.exception("Browser RPC error: %s", e)
    finally:
        _sessions.pop(session_id, None)
