"""Handler: ``reply_to_email`` — send the drafted reply to an inbound
email via the property manager's Gmail (Composio), on the original thread.

This is the executor side of the email-ingest feature. Unlike the other
handlers it does NOT touch Buildium — the reply goes out through Composio
Gmail. The executor recognises this tool and calls it WITHOUT a Buildium
client (it passes ``None``); everything the handler needs comes via
``context``:

  - ``composio``           — the ComposioClient (from app.state)
  - ``composio_user_id``   — the auth user id (Gmail is personal-scoped)
  - ``email_thread_id``    — the Gmail thread to reply on
  - ``email_to``           — the original sender (recipient of the reply)

The reply only ever fires from the execute endpoint, which is reached
after the user reviews + confirms the draft in the Inbox — it never
auto-sends.

⚠ Live seam: the Composio Gmail reply tool slug + argument names must be
confirmed against the live toolkit on first smoke; they're isolated to
the two constants below.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.agent.handlers.base import (
    Handler,
    HandlerError,
    HandlerResult,
    guard_fair_housing,
)
from helixis.buildium.client import BuildiumClient
from helixis.security.prompt_delimit import strip_exfiltration

logger = logging.getLogger(__name__)

# Composio Gmail reply tool slug + arg names. ⚠ confirm on first smoke.
GMAIL_REPLY_SLUG = "GMAIL_REPLY_TO_THREAD"
_MAX_BODY = 8000


class ReplyToEmailHandler(Handler):
    tool_id = "reply_to_email"

    async def execute(
        self,
        client: BuildiumClient,  # unused — email reply goes via Composio, not Buildium
        params: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> HandlerResult:
        ctx = context or {}
        body = params.get("body")
        if not isinstance(body, str) or not body.strip():
            raise HandlerError(
                "body is required and must be a non-empty string",
                tool_id=self.tool_id,
                status_code=400,
            )
        if len(body) > _MAX_BODY:
            raise HandlerError(
                f"body exceeds {_MAX_BODY} characters",
                tool_id=self.tool_id,
                status_code=400,
            )

        # Defense-in-depth (risk #4): strip markdown-image / link exfiltration
        # vectors from the outbound body. If an injection in the inbound email
        # steered the drafted reply toward an auto-loading image URL, this is
        # the egress point that neutralizes it before Gmail sends.
        body = strip_exfiltration(body)

        # Fair-housing egress gate (#49): a discriminatory reply must never
        # leave via Gmail. Sits alongside the exfiltration strip as the second
        # egress guard; a block is a revise-and-resend (the draft is
        # user-reviewed in the Inbox before this fires). The inbound email body is
        # threaded by the executor as ``source_request``; passing it as ``prompt``
        # activates the protected-class-question deflection check — without it only
        # the steering denylist ran (the deflection half was silently dead).
        guard_fair_housing(body, tool_id=self.tool_id, prompt=str(ctx.get("source_request") or ""))

        composio = ctx.get("composio")
        user_id = ctx.get("composio_user_id")
        thread_id = ctx.get("email_thread_id")
        recipient = ctx.get("email_to")

        if composio is None or not getattr(composio, "available", False):
            raise HandlerError(
                "Gmail is not connected — reconnect it in Settings → Connectors.",
                tool_id=self.tool_id,
                status_code=503,
            )
        if not isinstance(user_id, str) or not user_id:
            raise HandlerError(
                "missing user identity for the Gmail reply",
                tool_id=self.tool_id,
                status_code=400,
            )
        if not isinstance(thread_id, str) or not thread_id:
            raise HandlerError(
                "this email has no thread to reply on",
                tool_id=self.tool_id,
                status_code=400,
            )

        arguments: dict[str, Any] = {
            "thread_id": thread_id,
            "message_body": body,
        }
        if isinstance(recipient, str) and recipient:
            arguments["recipient_email"] = recipient

        result = composio.execute_tool(user_id=user_id, slug=GMAIL_REPLY_SLUG, arguments=arguments)
        if result is None:
            raise HandlerError(
                "Gmail reply failed to send — please try again.",
                tool_id=self.tool_id,
                status_code=502,
            )

        logger.info(
            "reply_to_email sent (user=%s thread=%s, %d chars)",
            user_id,
            thread_id,
            len(body),
        )
        to_label = recipient if isinstance(recipient, str) and recipient else "the sender"
        return {
            "buildium_id": None,
            "summary": f"Replied to {to_label}",
            "thread_id": thread_id,
        }
