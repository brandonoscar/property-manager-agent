"""Send an email to a CRM lead via the PM's Gmail (Composio).

The one-click "Approve & send" on an *email* lead sends the reply out through
the live Composio Gmail connection — the same manual ``execute_tool`` seam the
email-ingest reply uses (``agent/handlers/reply_to_email``). Unlike a thread
reply, a first contact with a lead is a *fresh* message, so this uses the Gmail
**send** tool. Helixis never auto-sends — this fires only from the user-approved
``POST /crm/leads/{id}/reply`` route.

The pure helpers (argument building + message-id extraction) are unit-tested;
:func:`send_gmail_email` wraps the Composio call and degrades to ``(False, None)``
so the route can surface a clean 502/503.

Live seam — ✅ CONFIRMED live 2026-07-04: a real send through this path arrived
in the connected inbox with the exact subject + body, so ``GMAIL_SEND_EMAIL`` +
the arg names ``recipient_email`` / ``subject`` / ``body`` / ``is_html`` are
correct. Note the send tool takes ``body`` — UNLIKE the sibling reply tool
(``reply_to_email.GMAIL_REPLY_SLUG``), which takes ``message_body``. The slug +
args stay isolated to the constants below so a future toolkit rename is a
one-line change.
"""

from __future__ import annotations

import logging
from typing import Any

from helixis.security.prompt_delimit import strip_exfiltration

logger = logging.getLogger(__name__)

# Composio Gmail send-email slug + arg names. ✅ confirmed live 2026-07-04.
GMAIL_SEND_SLUG = "GMAIL_SEND_EMAIL"
_MAX_BODY = 12000
_MAX_SUBJECT = 300


def build_send_arguments(*, recipient_email: str, subject: str, body: str) -> dict[str, Any]:
    """Build the ``GMAIL_SEND_EMAIL`` argument payload (plain-text send).

    Pure + deterministic. The body is run through ``strip_exfiltration`` (risk #4)
    so a merge value that smuggled in a markdown-image / auto-loading URL can't
    ride out in the sent email; subject + body are length-capped.
    """
    return {
        "recipient_email": recipient_email,
        "subject": subject.strip()[:_MAX_SUBJECT],
        "body": strip_exfiltration(body)[:_MAX_BODY],
        "is_html": False,
    }


def extract_message_id(result: dict[str, Any] | None) -> str | None:
    """Best-effort pull of the sent message's id from the Composio result.

    Result shapes vary by toolkit version, so probe the common nestings
    (top-level ``id``/``message_id``/``thread_id``, ``data``/``response_data``
    wrappers). ``None`` just means the id wasn't exposed — the email still sent.
    """
    if not isinstance(result, dict):
        return None
    keys = ("id", "message_id", "thread_id")
    for key in keys:
        val = result.get(key)
        if isinstance(val, str) and val:
            return val
    for wrapper in ("data", "response_data", "response"):
        inner = result.get(wrapper)
        if isinstance(inner, dict):
            for key in keys:
                val = inner.get(key)
                if isinstance(val, str) and val:
                    return val
    return None


def send_gmail_email(
    composio: Any,
    *,
    user_id: str,
    recipient_email: str,
    subject: str,
    body: str,
) -> tuple[bool, str | None]:
    """Send the email. Returns ``(ok, message_id)``.

    ``ok`` is ``False`` when Gmail isn't connected or the Composio call fails —
    the route maps that to 503/502. On success the id may still be ``None`` if
    the result shape didn't expose one (the email was sent regardless).
    """
    if composio is None or not getattr(composio, "available", False):
        return False, None

    arguments = build_send_arguments(recipient_email=recipient_email, subject=subject, body=body)
    try:
        result = composio.execute_tool(user_id=user_id, slug=GMAIL_SEND_SLUG, arguments=arguments)
    except Exception:  # noqa: BLE001 — never let a send crash the route
        logger.exception("send_gmail_email: Composio Gmail send failed")
        return False, None

    if result is None:
        logger.info("send_gmail_email: Composio returned None (send failed)")
        return False, None
    return True, extract_message_id(result)


__all__ = [
    "GMAIL_SEND_SLUG",
    "build_send_arguments",
    "extract_message_id",
    "send_gmail_email",
]
