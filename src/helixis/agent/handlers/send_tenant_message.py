"""Handler: ``send_tenant_message`` — message a tenant via Buildium's
Resident Center.

Tool catalog parameter shape:
  tenant_id: int (required — must be a tenant on the active lease)
  subject:   string (required, <=120 chars)
  body:      string (required, <=4000 chars)

Buildium's API surface for outbound tenant communication is the
Communications endpoint group; the documented path is
``POST /communications/announcements`` for broadcast and
``POST /communications/messages`` for direct messages. Coverage is
**not uniform** across Buildium account scopes — some plans expose
the write side via API, others restrict it to the web UI. When the
write returns 403/404 we surface a typed 501 (Not Implemented for
this account scope) so the UI can guide the PM toward the web
dashboard rather than silently no-opping.
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
from helixis.buildium.errors import BuildiumApiError

logger = logging.getLogger(__name__)

_MAX_SUBJECT = 120
_MAX_BODY = 4000


class SendTenantMessageHandler(Handler):
    tool_id = "send_tenant_message"

    async def execute(
        self,
        client: BuildiumClient,
        params: dict[str, Any],
        *,
        context: dict[str, Any] | None = None,
    ) -> HandlerResult:
        tenant_id = params.get("tenant_id")
        subject = params.get("subject")
        body = params.get("body")

        if not isinstance(tenant_id, int):
            raise HandlerError(
                "tenant_id is required and must be an integer",
                tool_id=self.tool_id,
                status_code=400,
            )
        if not isinstance(subject, str) or not subject.strip():
            raise HandlerError(
                "subject is required and must be a non-empty string",
                tool_id=self.tool_id,
                status_code=400,
            )
        if len(subject) > _MAX_SUBJECT:
            raise HandlerError(
                f"subject exceeds {_MAX_SUBJECT} characters",
                tool_id=self.tool_id,
                status_code=400,
            )
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

        # Fair-housing egress gate (#49): refuse to send steering / discriminatory
        # content to a tenant. Checks subject + body; a block is a revise-and-resend
        # prompt (the message is human-reviewed before this fires). ``source_request``
        # is the inbound resident request this message answers (threaded by the
        # executor from the enriched task); passing it as ``prompt`` activates the
        # protected-class-question deflection half of the gate — without it only the
        # steering denylist ran and the deflection check was silently dead.
        source_request = str((context or {}).get("source_request") or "")
        guard_fair_housing(f"{subject}\n{body}", tool_id=self.tool_id, prompt=source_request)

        logger.info(
            "executing send_tenant_message",
            extra={
                "tool_id": self.tool_id,
                "tenant_id": tenant_id,
                "subject_length": len(subject),
                "body_length": len(body),
            },
        )

        payload: dict[str, Any] = {
            "Subject": subject.strip(),
            "Body": body.strip(),
            "TenantIds": [tenant_id],
        }

        try:
            created = await client.request(
                "POST",
                "/communications/messages",
                body=payload,
            )
        except BuildiumApiError as exc:
            # 403 / 404 from Buildium when the account doesn't expose
            # this write surface. Translate into a clearer 501 so the
            # UI tells the PM the right next step (use Buildium web).
            if exc.status in (403, 404):
                logger.warning(
                    "Buildium does not expose tenant messaging via API for "
                    "this account scope (status=%d)",
                    exc.status,
                )
                raise HandlerError(
                    "Buildium does not expose tenant messaging via API for "
                    "this account's scope. Send the message from Buildium's "
                    "web dashboard for now.",
                    tool_id=self.tool_id,
                    status_code=501,
                ) from exc
            raise HandlerError(
                f"Buildium rejected the tenant message: {exc}",
                tool_id=self.tool_id,
            ) from exc

        message_id: Any = None
        if isinstance(created, dict):
            message_id = created.get("Id")

        return {
            "buildium_id": message_id,
            "tenant_id": tenant_id,
            "summary": f'Sent message to tenant #{tenant_id}: "{subject.strip()}"',
        }
