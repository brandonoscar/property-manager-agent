"""Storage last-mile helpers (Inc.5b) — artifacts out, documents in.

Storage is mocked; we test the company-scoped path, the signed-URL shape, the
content-type guess, and graceful degradation when storage errors.
"""

from __future__ import annotations

from typing import Any

import pytest

from helixis.codeexec import storage_io
from helixis.codeexec.storage_io import load_input_document, persist_artifacts
from helixis.storage import StorageError


@pytest.mark.asyncio
async def test_persist_artifacts_uploads_scoped_and_signs(monkeypatch: pytest.MonkeyPatch) -> None:
    uploaded: list[dict[str, Any]] = []

    async def _upload(*, bucket: str, path: str, content: bytes, content_type: str, settings: Any):
        uploaded.append({"bucket": bucket, "path": path, "ct": content_type, "n": len(content)})

    async def _sign(*, bucket: str, path: str, expires_in: int, settings: Any) -> str:
        return f"https://signed/{path}?token=abc"

    monkeypatch.setattr(storage_io, "upload_object", _upload)
    monkeypatch.setattr(storage_io, "sign_object_url", _sign)

    links = await persist_artifacts(
        {"report.csv": b"a,b\n1,2\n"}, company_id="co-123", settings=object()
    )
    assert uploaded[0]["bucket"] == "codeexec_artifacts"
    assert uploaded[0]["path"].startswith("co-123/")  # company-scoped
    assert uploaded[0]["path"].endswith("/report.csv")
    assert uploaded[0]["ct"] == "text/csv"  # content-type guessed from extension
    assert links[0]["name"] == "report.csv"
    assert links[0]["size"] == 8
    assert links[0]["url"].startswith("https://signed/")


@pytest.mark.asyncio
async def test_persist_artifacts_degrades_on_storage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(**_k: Any) -> None:
        raise StorageError("storage down")

    monkeypatch.setattr(storage_io, "upload_object", _boom)
    links = await persist_artifacts({"x.bin": b"..."}, company_id="co-1", settings=object())
    # The file was produced; only the link is missing.
    assert links[0]["name"] == "x.bin"
    assert links[0]["url"] is None


@pytest.mark.asyncio
async def test_load_input_document_downloads_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _download(*, bucket: str, path: str, settings: Any) -> bytes:
        assert bucket == "company_documents"
        assert path == "co-1/doc-9.pdf"
        return b"%PDF-1.7 ..."

    monkeypatch.setattr(storage_io, "download_object", _download)
    name, data = await load_input_document(
        storage_path="co-1/doc-9.pdf", filename="lease.pdf", settings=object()
    )
    assert name == "lease.pdf"
    assert data.startswith(b"%PDF")
