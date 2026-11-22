"""Communications — announcements, sent-email logs, phone logs, templates.

Read-only wrapper over Buildium's "Communications" API group. Buildium also
exposes POST paths to send announcements/emails; those are intentionally
omitted — Helixis sends tenant comms through Gmail (Composio), not Buildium.

Paths verified against developer.buildium.com + the LeadSimple/buildium SDK
on 2026-06-29.
"""

from __future__ import annotations

import builtins
from typing import Any, cast

from helixis.buildium.client import BuildiumClient
from helixis.buildium.schemas import (
    Announcement,
    CommunicationTemplate,
    EmailLog,
    PhoneLog,
)


class Communications:
    def __init__(self, client: BuildiumClient) -> None:
        self._client = client

    # ── Announcements ───────────────────────────────────────────────
    async def list_announcements(self, **filters: Any) -> builtins.list[Announcement]:
        return await self._client.list_all(
            "/communications/announcements", filters or None, Announcement
        )

    async def get_announcement(self, announcement_id: int) -> Announcement:
        return cast(
            Announcement,
            await self._client.request(
                "GET",
                f"/communications/announcements/{announcement_id}",
                schema=Announcement,
            ),
        )

    # ── Sent-email logs ─────────────────────────────────────────────
    async def list_emails(self, **filters: Any) -> builtins.list[EmailLog]:
        return await self._client.list_all("/communications/emails", filters or None, EmailLog)

    async def get_email(self, email_id: int) -> EmailLog:
        return cast(
            EmailLog,
            await self._client.request(
                "GET", f"/communications/emails/{email_id}", schema=EmailLog
            ),
        )

    # ── Phone logs ──────────────────────────────────────────────────
    async def list_phone_logs(self, **filters: Any) -> builtins.list[PhoneLog]:
        return await self._client.list_all("/communications/phonelogs", filters or None, PhoneLog)

    async def get_phone_log(self, phone_log_id: int) -> PhoneLog:
        return cast(
            PhoneLog,
            await self._client.request(
                "GET", f"/communications/phonelogs/{phone_log_id}", schema=PhoneLog
            ),
        )

    # ── Templates (reference data) ──────────────────────────────────
    async def list_templates(self, **filters: Any) -> builtins.list[CommunicationTemplate]:
        return await self._client.list_all(
            "/communications/templates", filters or None, CommunicationTemplate
        )

    async def get_template(self, template_id: int) -> CommunicationTemplate:
        return cast(
            CommunicationTemplate,
            await self._client.request(
                "GET",
                f"/communications/templates/{template_id}",
                schema=CommunicationTemplate,
            ),
        )
