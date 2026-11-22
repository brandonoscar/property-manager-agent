"""Gmail-outbound suppression — "did anyone on the team already email them?"

Reminders v1.5: producers whose neglect gate should also respect
off-Helixis outreach (rent chasing happens in the PM's own Gmail) ask
this checker which of a candidate set of addresses got a sent email in
the last N days. The check runs ONLY for candidates that already
survived every DB-local gate, so the common case (nothing neglected)
costs zero API calls.

Cost shape per producer with survivors: one batched ``in:sent`` probe
per chunk of addresses; only a chunk that HAS recent sent mail pays a
per-address follow-up to attribute it. Failure posture is fail-open —
an errored check suppresses nothing (the reminder still fires; we only
go quiet on positive evidence of outreach).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

    from helixis.connectors.composio_client import ComposioClient

logger = logging.getLogger(__name__)

GMAIL_FETCH_SLUG = "GMAIL_FETCH_EMAILS"
_CHUNK_SIZE = 10


def _messages_of(result: Any) -> list[Any]:
    """Pull the message list out of a Composio GMAIL_FETCH_EMAILS result —
    same tolerant containers as email_ingest.parse_gmail_fetch_result, but
    we only need existence, not fields."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for container in (result, result.get("data"), result.get("response_data")):
            if isinstance(container, dict):
                messages = container.get("messages")
                if isinstance(messages, list):
                    return messages
    return []


class GmailOutboundChecker:
    """Answers "which of these addresses did the team email recently?"
    across every Gmail-connected user in the company."""

    def __init__(self, composio: ComposioClient, user_ids: list[str]) -> None:
        self._composio = composio
        self._user_ids = user_ids
        self._connected: list[str] | None = None  # resolved lazily, cached

    def _connected_users(self) -> list[str]:
        if self._connected is None:
            connected = []
            for user_id in self._user_ids:
                try:
                    if self._composio.is_connected(user_id, "gmail"):
                        connected.append(user_id)
                except Exception:  # noqa: BLE001
                    logger.warning("outbound check: is_connected failed for %s", user_id)
            self._connected = connected
        return self._connected

    def _sent_query_hits(self, user_id: str, addresses: list[str], days: int) -> bool:
        clause = " OR ".join(f"to:{a}" for a in addresses)
        if len(addresses) > 1:
            clause = f"({clause})"
        result = self._composio.execute_tool(
            user_id=user_id,
            slug=GMAIL_FETCH_SLUG,
            arguments={"max_results": 1, "query": f"in:sent newer_than:{days}d {clause}"},
        )
        return bool(_messages_of(result))

    async def recently_contacted(self, addresses: set[str], *, days: int) -> set[str]:
        """Subset of ``addresses`` with a sent email in the last ``days``
        days from ANY Gmail-connected teammate. Fail-open: errors mean
        "no evidence", never a crash and never blanket suppression."""
        cleaned = sorted({a.strip().lower() for a in addresses if a and a.strip()})
        if not cleaned:
            return set()
        contacted: set[str] = set()
        try:
            for user_id in self._connected_users():
                remaining = [a for a in cleaned if a not in contacted]
                for i in range(0, len(remaining), _CHUNK_SIZE):
                    chunk = remaining[i : i + _CHUNK_SIZE]
                    # Cheap batched probe first; attribute per-address only
                    # when the chunk actually has recent sent mail.
                    if not self._sent_query_hits(user_id, chunk, days):
                        continue
                    if len(chunk) == 1:
                        contacted.add(chunk[0])
                        continue
                    for address in chunk:
                        if self._sent_query_hits(user_id, [address], days):
                            contacted.add(address)
        except Exception:  # noqa: BLE001
            logger.exception("outbound check failed — suppressing nothing (fail-open)")
        return contacted


async def build_outbound_checker(
    pool: asyncpg.Pool, composio: ComposioClient | None, company_id: str
) -> GmailOutboundChecker | None:
    """Checker for one company's sweep, or None when Composio is dark or
    the company has no members to check."""
    if composio is None or not getattr(composio, "available", False):
        return None
    from helixis.db.repositories.gmail_poll_state import list_poll_targets

    targets = await list_poll_targets(pool)
    user_ids = [uid for uid, cid in targets if cid == company_id]
    if not user_ids:
        return None
    return GmailOutboundChecker(composio, user_ids)


__all__ = ["GmailOutboundChecker", "build_outbound_checker"]
