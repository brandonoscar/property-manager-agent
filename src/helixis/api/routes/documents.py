"""Settings → Documents & Links endpoints.

Three resources behind one settings page:
  * ``/links``      — the caller's own URLs (application, scheduling). Per user.
  * ``/templates``  — company-shared message templates.
  * ``/documents``  — company-shared lease/addendum PDFs (upload + list +
                      signed download + delete). Bytes live in the
                      ``company_documents`` Supabase Storage bucket, keyed by
                      ``{company_id}/{id}.pdf``; the row holds metadata only.

All routes require auth. Links are owner-scoped (``user_id``); templates and
documents are company-scoped (primary company), same as the CRM routes. RLS on
the tables is the second line of defense.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.db.engine import get_pool
from helixis.db.repositories import docs_links as repo
from helixis.security.file_types import content_matches
from helixis.settings import Settings
from helixis.storage import StorageError, delete_object, sign_object_url, upload_object

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

_DOCUMENTS_BUCKET = "company_documents"
_MAX_PDF_BYTES = 15 * 1024 * 1024  # 15 MB — a lease/addendum is well under this.
_DOC_KINDS = {"lease", "addendum", "other"}
_DOWNLOAD_TTL_SECONDS = 300  # signed URL lifetime for a document download


def _settings_from(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _primary_company_id(user: AuthenticatedUser) -> str:
    if not user.company_ids:
        raise HTTPException(status_code=400, detail="User is not a member of any company")
    return user.company_ids[0]


# ── Links (per user) ────────────────────────────────────────────────────────


class LinkIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=1, max_length=2000)


class LinkOut(BaseModel):
    id: str
    label: str
    url: str


@router.get("/links")
async def list_links(user: AuthenticatedUser = Depends(get_current_user)) -> list[LinkOut]:
    rows = await repo.list_links(get_pool(), user_id=user.user_id)
    return [LinkOut(id=r.id, label=r.label, url=r.url) for r in rows]


@router.post("/links")
async def create_link(body: LinkIn, user: AuthenticatedUser = Depends(get_current_user)) -> LinkOut:
    label = body.label.strip()
    url = body.url.strip()
    if not label or not url:
        raise HTTPException(status_code=422, detail="Label and URL are required.")
    link_id = await repo.create_link(get_pool(), user_id=user.user_id, label=label, url=url)
    return LinkOut(id=link_id, label=label, url=url)


@router.delete("/links/{link_id}")
async def delete_link(
    link_id: str, user: AuthenticatedUser = Depends(get_current_user)
) -> dict[str, bool]:
    ok = await repo.delete_link(get_pool(), user_id=user.user_id, link_id=link_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Link not found.")
    return {"deleted": True}


# ── Message templates (per company) ─────────────────────────────────────────


class TemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=8000)


class TemplateOut(BaseModel):
    id: str
    name: str
    body: str


@router.get("/templates")
async def list_templates(
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[TemplateOut]:
    company_id = _primary_company_id(user)
    rows = await repo.list_templates(get_pool(), company_id=company_id)
    return [TemplateOut(id=r.id, name=r.name, body=r.body) for r in rows]


@router.post("/templates")
async def create_template(
    body: TemplateIn, user: AuthenticatedUser = Depends(get_current_user)
) -> TemplateOut:
    company_id = _primary_company_id(user)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Template name is required.")
    template_id = await repo.create_template(
        get_pool(),
        company_id=company_id,
        created_by=user.user_id,
        name=name,
        body=body.body,
    )
    return TemplateOut(id=template_id, name=name, body=body.body)


@router.delete("/templates/{template_id}")
async def delete_template(
    template_id: str, user: AuthenticatedUser = Depends(get_current_user)
) -> dict[str, bool]:
    company_id = _primary_company_id(user)
    ok = await repo.delete_template(get_pool(), company_id=company_id, template_id=template_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Template not found.")
    return {"deleted": True}


# ── Documents (per company; PDF bytes in Supabase Storage) ──────────────────


class DocumentOut(BaseModel):
    id: str
    name: str
    kind: str
    size_bytes: int


class DownloadOut(BaseModel):
    url: str


def _document_out(r: repo.DocumentRow) -> DocumentOut:
    return DocumentOut(id=r.id, name=r.name, kind=r.kind, size_bytes=r.size_bytes)


@router.get("/documents")
async def list_documents(
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[DocumentOut]:
    company_id = _primary_company_id(user)
    rows = await repo.list_documents(get_pool(), company_id=company_id)
    return [_document_out(r) for r in rows]


@router.post("/documents")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(...),
    kind: str = Form("other"),
    user: AuthenticatedUser = Depends(get_current_user),
) -> DocumentOut:
    company_id = _primary_company_id(user)
    settings = _settings_from(request)

    kind = kind.strip().lower()
    if kind not in _DOC_KINDS:
        raise HTTPException(status_code=422, detail="kind must be lease, addendum, or other.")
    display_name = (name or file.filename or "Document").strip()

    content = await file.read()
    if not content:
        raise HTTPException(status_code=422, detail="The uploaded file is empty.")
    if len(content) > _MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 15 MB).")
    # PDFs only — check the magic bytes, not just the declared content type.
    #
    # ⚠ This route had the rule right from the start and the sibling upload
    # path in agent.py never got it, so the signature now lives ONCE in
    # security/file_types.py and both read it (gotcha 25). The behaviour here
    # is unchanged: same %PDF check, one definition.
    #
    # This is the path where a mislabelled file would matter most — it stores
    # to a bucket and `download_document` hands back a SIGNED URL, so a
    # browser opening it renders whatever the file actually is. The hardcoded
    # `content_type="application/pdf"` below is the second layer.
    if content_matches(content, "application/pdf") is not True:
        raise HTTPException(status_code=422, detail="Only PDF files are accepted.")

    doc_id = str(uuid.uuid4())
    storage_path = f"{company_id}/{doc_id}.pdf"
    try:
        await upload_object(
            bucket=_DOCUMENTS_BUCKET,
            path=storage_path,
            content=content,
            content_type="application/pdf",
            settings=settings,
        )
    except StorageError as exc:
        logger.warning("document upload to storage failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not store the file.") from exc

    await repo.create_document(
        get_pool(),
        doc_id=doc_id,
        company_id=company_id,
        uploaded_by=user.user_id,
        name=display_name,
        kind=kind,
        storage_path=storage_path,
        content_type="application/pdf",
        size_bytes=len(content),
    )
    return DocumentOut(id=doc_id, name=display_name, kind=kind, size_bytes=len(content))


@router.get("/documents/{document_id}/download")
async def download_document(
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> DownloadOut:
    company_id = _primary_company_id(user)
    doc = await repo.get_document(get_pool(), company_id=company_id, document_id=document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    try:
        url = await sign_object_url(
            bucket=_DOCUMENTS_BUCKET,
            path=doc.storage_path,
            expires_in=_DOWNLOAD_TTL_SECONDS,
            settings=_settings_from(request),
        )
    except StorageError as exc:
        logger.warning("document sign failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not generate a download link.") from exc
    return DownloadOut(url=url)


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, bool]:
    company_id = _primary_company_id(user)
    doc = await repo.delete_document(get_pool(), company_id=company_id, document_id=document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    # Metadata is gone; remove the object too (best-effort — a leftover blob is
    # harmless and unreachable without the row).
    try:
        await delete_object(
            bucket=_DOCUMENTS_BUCKET, path=doc.storage_path, settings=_settings_from(request)
        )
    except StorageError as exc:
        logger.warning("document object delete failed (row already removed): %s", exc)
    return {"deleted": True}
