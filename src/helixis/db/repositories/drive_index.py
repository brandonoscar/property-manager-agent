"""Repository for ``drive_index`` — a static snapshot of a connected Drive (#67 PR-2).

One row per (company, connected user, Google Drive file): title + folder path +
mime type + modified time. Built in the background (the daily company-knowledge
cycle, :mod:`helixis.company_knowledge.drive`) so the agent knows WHAT documents
exist in a company's Drive WITHOUT a live API call per question.

Writes go through the app pool (service DSN, bypasses RLS). Reads are best-effort:
a missing table (migration 0063 not yet applied — gotcha 8) or a transient error
returns ``[]``/``0`` so a dead source never breaks the knowledge cycle or a turn.
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

import asyncpg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DriveFile:
    """One indexed Drive file. ``file_id`` + ``title`` are always present; the
    rest are best-effort (Drive doesn't always return a parent/modified time)."""

    file_id: str
    title: str
    mime_type: str | None = None
    path: str | None = None
    modified_at: datetime.datetime | None = None


async def replace_for_user(
    pool: asyncpg.Pool,
    *,
    company_id: str,
    composio_user_id: str,
    files: list[DriveFile],
) -> int:
    """Full-refresh the index for one (company, user): delete their rows and
    re-insert ``files`` in a single transaction.

    This is the prune half of full-refresh + prune (gotcha 15) — a file removed
    from Drive since the last cycle vanishes from the index. Returns the number
    of rows written. Best-effort: on any failure (missing table / transient) it
    logs and returns 0 WITHOUT partially mutating (the transaction rolls back),
    so a good prior index is never left half-wiped.
    """
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM public.drive_index "
                    "WHERE company_id = $1::uuid AND composio_user_id = $2",
                    company_id,
                    composio_user_id,
                )
                if files:
                    await conn.executemany(
                        "INSERT INTO public.drive_index "
                        "(company_id, composio_user_id, file_id, title, mime_type, "
                        " path, modified_at, indexed_at) "
                        "VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, now())",
                        [
                            (
                                company_id,
                                composio_user_id,
                                f.file_id,
                                f.title,
                                f.mime_type,
                                f.path,
                                f.modified_at,
                            )
                            for f in files
                        ],
                    )
    except Exception as exc:  # noqa: BLE001 — best-effort write
        logger.warning(
            "drive_index replace_for_user failed (company=%s user=%s): %s",
            company_id,
            composio_user_id,
            exc,
        )
        return 0
    return len(files)


async def list_for_company(
    pool: asyncpg.Pool, *, company_id: str, limit: int = 200
) -> list[DriveFile]:
    """All indexed Drive files across every connected user of a company, newest
    first. Returns ``[]`` on a missing table / any error (best-effort read)."""
    try:
        rows = await pool.fetch(
            """
            SELECT file_id, title, mime_type, path, modified_at
            FROM public.drive_index
            WHERE company_id = $1::uuid
            ORDER BY modified_at DESC NULLS LAST, title ASC
            LIMIT $2
            """,
            company_id,
            limit,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort read
        logger.warning("drive_index list_for_company failed (company=%s): %s", company_id, exc)
        return []
    return [
        DriveFile(
            file_id=row["file_id"],
            title=row["title"],
            mime_type=row["mime_type"],
            path=row["path"],
            modified_at=row["modified_at"],
        )
        for row in rows
    ]


async def count_for_user(pool: asyncpg.Pool, *, company_id: str, composio_user_id: str) -> int:
    """How many files are indexed for one (company, user). Used to decide whether
    a first-time index build is needed. 0 on a missing table / any error."""
    try:
        val = await pool.fetchval(
            "SELECT count(*) FROM public.drive_index "
            "WHERE company_id = $1::uuid AND composio_user_id = $2",
            company_id,
            composio_user_id,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort read
        logger.warning("drive_index count_for_user failed: %s", exc)
        return 0
    return int(val or 0)


__all__ = ["DriveFile", "replace_for_user", "list_for_company", "count_for_user"]
