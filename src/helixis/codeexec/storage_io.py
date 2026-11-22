"""Storage last-mile for code-execution (Inc.5b).

Two thin helpers over ``helixis.storage`` so a run can work with real files:

* ``persist_artifacts`` — upload the files a run produced to the artifacts
  bucket, company-scoped, and return time-limited signed download URLs. This is
  how "generate an owner statement / CSV" reaches the user.
* ``load_input_document`` — fetch an already-resolved company document's bytes
  so they can be fed into the sandbox as an ``input_files`` entry (the caller
  resolves the document from the repo first, so company-scoping + existence are
  enforced upstream, not here).

Both are best-effort at the edges: a storage hiccup degrades to "no URL" rather
than failing the whole run. Nothing here holds any authority beyond the storage
credentials already used by the documents feature.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from helixis.storage import StorageError, download_object, sign_object_url, upload_object

logger = logging.getLogger(__name__)

_ARTIFACTS_BUCKET = "codeexec_artifacts"
_DOCUMENTS_BUCKET = "company_documents"
_URL_TTL_SECONDS = 3600  # 1h signed link

_CONTENT_TYPES = {
    "csv": "text/csv",
    "json": "application/json",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "html": "text/html",
}


def _content_type(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


async def persist_artifacts(
    artifact_contents: dict[str, bytes], *, company_id: str, settings: Any
) -> list[dict[str, Any]]:
    """Upload each artifact under ``{bucket}/{company_id}/{run}/{name}`` and
    return ``[{name, size, url}]`` with a signed URL (``url`` is None on a
    storage error — the file was produced, the link just isn't available)."""
    out: list[dict[str, Any]] = []
    run = uuid.uuid4().hex[:12]
    for name, data in artifact_contents.items():
        path = f"{company_id}/{run}/{name}"
        entry: dict[str, Any] = {"name": name, "size": len(data), "url": None}
        try:
            await upload_object(
                bucket=_ARTIFACTS_BUCKET,
                path=path,
                content=data,
                content_type=_content_type(name),
                settings=settings,
            )
            entry["url"] = await sign_object_url(
                bucket=_ARTIFACTS_BUCKET,
                path=path,
                expires_in=_URL_TTL_SECONDS,
                settings=settings,
            )
        except StorageError as exc:
            logger.warning("codeexec: could not persist artifact %s: %s", name, exc)
        out.append(entry)
    return out


async def load_input_document(
    *, storage_path: str, filename: str, settings: Any
) -> tuple[str, bytes]:
    """Download an already-resolved company document's bytes for use as an
    ``input_files`` entry. The caller MUST have resolved ``storage_path`` from
    the documents repo with the requester's company scope first."""
    data = await download_object(bucket=_DOCUMENTS_BUCKET, path=storage_path, settings=settings)
    return filename, data


__all__ = ["load_input_document", "persist_artifacts"]
