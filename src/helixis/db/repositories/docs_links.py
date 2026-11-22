"""Repository for Settings → Documents & Links.

Three small stores (migration 0033):
  * ``user_links``        — per-user URLs (owner-scoped).
  * ``company_templates`` — company-shared message templates.
  * ``company_documents`` — company-shared lease/addendum PDF *metadata* (the
                            bytes live in the ``company_documents`` storage
                            bucket; see ``helixis.storage``).

Plain asyncpg functions, mirroring ``repositories.leads``: every call takes the
pool plus the owning scope (``user_id`` for links, ``company_id`` for the
company tables) so a caller can never reach another tenant's rows. RLS is the
second line of defense.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import asyncpg

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LinkRow:
    id: str
    label: str
    url: str


@dataclass(frozen=True)
class TemplateRow:
    id: str
    name: str
    body: str


@dataclass(frozen=True)
class DocumentRow:
    id: str
    name: str
    kind: str
    storage_path: str
    content_type: str
    size_bytes: int
    created_at: datetime


def _affected(result: str) -> bool:
    """True when an asyncpg command tag (e.g. ``"DELETE 1"``) affected a row."""
    return result.rsplit(" ", 1)[-1] != "0"


# ── Links (per user) ────────────────────────────────────────────────────────


async def list_links(pool: asyncpg.Pool, *, user_id: str) -> list[LinkRow]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text AS id, label, url FROM public.user_links "
            "WHERE user_id = $1::uuid ORDER BY created_at",
            user_id,
        )
    return [LinkRow(id=r["id"], label=r["label"], url=r["url"]) for r in rows]


async def create_link(pool: asyncpg.Pool, *, user_id: str, label: str, url: str) -> str:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO public.user_links (user_id, label, url) "
            "VALUES ($1::uuid, $2, $3) RETURNING id::text AS id",
            user_id,
            label,
            url,
        )
    return str(row["id"])


async def delete_link(pool: asyncpg.Pool, *, user_id: str, link_id: str) -> bool:
    async with pool.acquire() as conn:
        result: str = await conn.execute(
            "DELETE FROM public.user_links WHERE user_id = $1::uuid AND id = $2::uuid",
            user_id,
            link_id,
        )
    return _affected(result)


# ── Message templates (per company) ─────────────────────────────────────────


async def list_templates(pool: asyncpg.Pool, *, company_id: str) -> list[TemplateRow]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text AS id, name, body FROM public.company_templates "
            "WHERE company_id = $1::uuid ORDER BY created_at",
            company_id,
        )
    return [TemplateRow(id=r["id"], name=r["name"], body=r["body"]) for r in rows]


async def get_template(
    pool: asyncpg.Pool, *, company_id: str, template_id: str
) -> TemplateRow | None:
    """One template by id, company-scoped. ``None`` if missing / cross-tenant."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id::text AS id, name, body FROM public.company_templates "
            "WHERE company_id = $1::uuid AND id = $2::uuid",
            company_id,
            template_id,
        )
    return TemplateRow(id=row["id"], name=row["name"], body=row["body"]) if row else None


async def default_template(pool: asyncpg.Pool, *, company_id: str) -> TemplateRow | None:
    """The company's default (first-saved) message template, or ``None`` when
    the company has none. This is the template Helixis uses to pre-draft the
    first reply to a new lead ("default message first, conversational after")."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id::text AS id, name, body FROM public.company_templates "
            "WHERE company_id = $1::uuid ORDER BY created_at LIMIT 1",
            company_id,
        )
    return TemplateRow(id=row["id"], name=row["name"], body=row["body"]) if row else None


async def create_template(
    pool: asyncpg.Pool, *, company_id: str, created_by: str, name: str, body: str
) -> str:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO public.company_templates (company_id, created_by, name, body) "
            "VALUES ($1::uuid, $2::uuid, $3, $4) RETURNING id::text AS id",
            company_id,
            created_by,
            name,
            body,
        )
    return str(row["id"])


async def delete_template(pool: asyncpg.Pool, *, company_id: str, template_id: str) -> bool:
    async with pool.acquire() as conn:
        result: str = await conn.execute(
            "DELETE FROM public.company_templates WHERE company_id = $1::uuid AND id = $2::uuid",
            company_id,
            template_id,
        )
    return _affected(result)


# ── Documents (per company; metadata only) ──────────────────────────────────


def _row_to_document(r: asyncpg.Record) -> DocumentRow:
    return DocumentRow(
        id=r["id"],
        name=r["name"],
        kind=r["kind"],
        storage_path=r["storage_path"],
        content_type=r["content_type"],
        size_bytes=r["size_bytes"],
        created_at=r["created_at"],
    )


_DOC_COLUMNS = "id::text AS id, name, kind, storage_path, content_type, size_bytes, created_at"


async def list_documents(pool: asyncpg.Pool, *, company_id: str) -> list[DocumentRow]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {_DOC_COLUMNS} FROM public.company_documents "
            "WHERE company_id = $1::uuid ORDER BY created_at DESC",
            company_id,
        )
    return [_row_to_document(r) for r in rows]


# ── Knowledge digests (company_documents.text_digest, migration 0037) ────────


@dataclass(frozen=True)
class KnowledgeDoc:
    """A company document plus its cached text digest — what the knowledge
    generator reads. ``text_digest`` is ``None`` until the doc has been
    extracted (the generator fills it in, once, per document)."""

    id: str
    name: str
    kind: str
    storage_path: str
    content_type: str
    text_digest: str | None


async def documents_for_knowledge(
    pool: asyncpg.Pool, *, company_id: str, limit: int = 25
) -> list[KnowledgeDoc]:
    """The company's uploaded documents + their cached digests, newest first.
    ``COALESCE``-free select of ``text_digest`` so a pre-0037 DB errors loudly
    on the caller's best-effort try rather than silently — but the generator
    wraps this in a try/except and degrades to name-only."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id::text AS id, name, kind, storage_path, content_type, text_digest "
            "FROM public.company_documents WHERE company_id = $1::uuid "
            "ORDER BY created_at DESC LIMIT $2",
            company_id,
            limit,
        )
    return [
        KnowledgeDoc(
            id=r["id"],
            name=r["name"],
            kind=r["kind"],
            storage_path=r["storage_path"],
            content_type=r["content_type"],
            text_digest=r["text_digest"],
        )
        for r in rows
    ]


async def set_document_digest(
    pool: asyncpg.Pool, *, company_id: str, document_id: str, text_digest: str
) -> None:
    """Cache a document's extracted text digest (+ stamp ``digest_generated_at``).
    Company-scoped. Best-effort — a failed cache write must not fail the
    generation cycle that triggered the extraction."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE public.company_documents "
                "SET text_digest = $3, digest_generated_at = now() "
                "WHERE company_id = $1::uuid AND id = $2::uuid",
                company_id,
                document_id,
                text_digest,
            )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to cache document digest (doc=%s)", document_id)


async def create_document(
    pool: asyncpg.Pool,
    *,
    doc_id: str,
    company_id: str,
    uploaded_by: str,
    name: str,
    kind: str,
    storage_path: str,
    content_type: str,
    size_bytes: int,
) -> None:
    """Insert the metadata row for an already-uploaded PDF. ``doc_id`` is minted
    by the caller so the storage path (``{company_id}/{doc_id}.pdf``) and the row
    share one id."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO public.company_documents "
            "(id, company_id, uploaded_by, name, kind, storage_path, content_type, size_bytes) "
            "VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8)",
            doc_id,
            company_id,
            uploaded_by,
            name,
            kind,
            storage_path,
            content_type,
            size_bytes,
        )


async def get_document(
    pool: asyncpg.Pool, *, company_id: str, document_id: str
) -> DocumentRow | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {_DOC_COLUMNS} FROM public.company_documents "
            "WHERE company_id = $1::uuid AND id = $2::uuid",
            company_id,
            document_id,
        )
    return _row_to_document(row) if row is not None else None


async def delete_document(
    pool: asyncpg.Pool, *, company_id: str, document_id: str
) -> DocumentRow | None:
    """Delete the metadata row and return it (so the caller can remove the
    storage object), or ``None`` if it didn't exist."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM public.company_documents "
            "WHERE company_id = $1::uuid AND id = $2::uuid "
            f"RETURNING {_DOC_COLUMNS}",
            company_id,
            document_id,
        )
    return _row_to_document(row) if row is not None else None
