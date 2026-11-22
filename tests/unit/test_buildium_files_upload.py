"""Files upload + share (PR6) — endpoint contract + tool guard rails.

Endpoint side pins the spec-grounded wire contract (2026-07-05, LeadSimple
SDK): the upload-ticket body shape, the presigned-S3 multipart POST (form
fields then the binary), the eventual-consistency id resolution via the
``physicalfilenames`` filter, and the sharing GET/PUT paths.

Tool side pins the pre-confirm validation branches (unknown attach target,
empty/ambiguous library) and the share merge: only populated sections carry
over, only sections with the matching knob change, and a file type without
tenant/owner sharing is an honest no-op.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.buildium.endpoints.files import Files
from helixis.buildium.errors import BuildiumApiError

# ── endpoint: upload ─────────────────────────────────────────────


class _FakeS3Response:
    def __init__(self, status_code: int = 204, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _FakeHttpClient:
    """Captures the presigned-S3 POST."""

    calls: list[dict[str, Any]] = []
    status_code = 204

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _FakeHttpClient:
        return self

    async def __aexit__(self, *_a: Any) -> bool:
        return False

    async def post(self, url: str, *, data: Any = None, files: Any = None) -> _FakeS3Response:
        _FakeHttpClient.calls.append({"url": url, "data": data, "files": files})
        return _FakeS3Response(status_code=_FakeHttpClient.status_code)


_TICKET = {
    "BucketUrl": "https://buildium-bucket.s3.amazonaws.com/",
    "FormData": {"key": "uploads/abc", "policy": "p==", "x-amz-signature": "sig"},
    "PhysicalFileName": "abc-lease.pdf",
}


@pytest.fixture(autouse=True)
def _reset_fake_http() -> None:
    _FakeHttpClient.calls = []
    _FakeHttpClient.status_code = 204


def _files_with(request_mock: AsyncMock) -> Files:
    client = MagicMock()
    client.request = request_mock
    return Files(client)


@pytest.mark.asyncio
async def test_upload_ticket_body_carries_spec_fields() -> None:
    request = AsyncMock(return_value=_TICKET)
    files = _files_with(request)

    await files.create_upload_ticket(
        entity_type="Lease",
        entity_id=555,
        file_name="lease.pdf",
        title="Signed lease",
        category_id=9,
        description="probe",
    )

    method, path = request.await_args.args[:2]
    assert (method, path) == ("POST", "/files/uploadrequests")
    body = request.await_args.kwargs["body"]
    assert body == {
        "EntityType": "Lease",
        "FileName": "lease.pdf",
        "Title": "Signed lease",
        "CategoryId": 9,
        "EntityId": 555,
        "Description": "probe",
    }


@pytest.mark.asyncio
async def test_upload_ticket_rejects_unknown_entity_type() -> None:
    files = _files_with(AsyncMock())
    with pytest.raises(BuildiumApiError):
        await files.create_upload_ticket(
            entity_type="Spaceship",
            file_name="x.pdf",
            title="x",
            category_id=1,
        )


@pytest.mark.asyncio
async def test_upload_posts_form_then_file_and_resolves_id() -> None:
    record = {"Id": 3001, "Title": "Signed lease"}
    request = AsyncMock(side_effect=[_TICKET, [record]])
    files = _files_with(request)

    with patch("helixis.buildium.endpoints.files.httpx.AsyncClient", _FakeHttpClient):
        result = await files.upload(
            entity_type="Lease",
            entity_id=555,
            file_name="lease.pdf",
            title="Signed lease",
            category_id=9,
            content=b"%PDF-fake",
        )

    assert result == record
    # S3 POST: presigned form fields verbatim, binary as the `file` part.
    s3 = _FakeHttpClient.calls[0]
    assert s3["url"] == _TICKET["BucketUrl"]
    assert s3["data"] == _TICKET["FormData"]
    assert s3["files"]["file"] == ("lease.pdf", b"%PDF-fake")
    # Resolution goes through the spec'd physicalfilenames filter.
    resolve_call = request.await_args_list[1]
    assert resolve_call.args[:2] == ("GET", "/files")
    assert resolve_call.kwargs["query"] == {"physicalfilenames": "abc-lease.pdf"}


@pytest.mark.asyncio
async def test_upload_returns_none_when_ingest_lags() -> None:
    """Buildium ingests asynchronously — an unresolved id is None, not an
    error (the S3 POST succeeded; the record appears moments later)."""
    request = AsyncMock(side_effect=[_TICKET, [], [], [], [], [], [], [], []])
    files = _files_with(request)

    with (
        patch("helixis.buildium.endpoints.files.httpx.AsyncClient", _FakeHttpClient),
        patch("helixis.buildium.endpoints.files.asyncio.sleep", new=AsyncMock()),
    ):
        result = await files.upload(
            entity_type="Rental",
            file_name="x.pdf",
            title="x",
            category_id=1,
            content=b"%PDF",
        )
    assert result is None


@pytest.mark.asyncio
async def test_upload_raises_on_s3_reject() -> None:
    request = AsyncMock(return_value=_TICKET)
    files = _files_with(request)
    _FakeHttpClient.status_code = 403

    with (
        patch("helixis.buildium.endpoints.files.httpx.AsyncClient", _FakeHttpClient),
        pytest.raises(BuildiumApiError),
    ):
        await files.upload(
            entity_type="Rental",
            file_name="x.pdf",
            title="x",
            category_id=1,
            content=b"%PDF",
        )


@pytest.mark.asyncio
async def test_sharing_paths() -> None:
    request = AsyncMock(return_value={"Lease": {"Tenants": False, "RentalOwners": False}})
    files = _files_with(request)

    await files.get_sharing(42)
    assert request.await_args.args[:2] == ("GET", "/files/42/sharing")

    await files.put_sharing(42, {"Lease": {"Tenants": True, "RentalOwners": False}})
    method, path = request.await_args.args[:2]
    assert (method, path) == ("PUT", "/files/42/sharing")
    assert request.await_args.kwargs["body"]["Lease"]["Tenants"] is True


# ── tool: pre-confirm validation + share merge ───────────────────


from helixis.agents import buildium_tools as bt  # noqa: E402
from helixis.memory.context import memory_context  # noqa: E402


def _ctx() -> Any:
    return memory_context(company_id="co-1", user_id="u1", session_id="s1")


def _doc(name: str = "Lease agreement") -> Any:
    doc = MagicMock()
    doc.id = "doc-1"
    doc.name = name
    doc.storage_path = "co-1/doc-1.pdf"
    doc.size_bytes = 24_000
    return doc


@pytest.mark.asyncio
async def test_upload_tool_rejects_unknown_attach_target() -> None:
    with _ctx():
        out = await bt._upload_document_impl(
            document_name="Lease agreement",
            attach_to="spaceship",
            entity_id=1,
            entity_label=None,
            title="",
            description="",
        )
    assert "I can attach files to one of" in out


@pytest.mark.asyncio
async def test_upload_tool_is_honest_about_empty_library() -> None:
    with (
        _ctx(),
        patch("helixis.db.repositories.docs_links.list_documents", AsyncMock(return_value=[])),
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
    ):
        out = await bt._upload_document_impl(
            document_name="anything",
            attach_to="lease",
            entity_id=1,
            entity_label=None,
            title="",
            description="",
        )
    assert "Documents library is empty" in out


@pytest.mark.asyncio
async def test_upload_tool_asks_on_ambiguous_document() -> None:
    docs = [_doc("Lease agreement A"), _doc("Lease agreement B")]
    with (
        _ctx(),
        patch("helixis.db.repositories.docs_links.list_documents", AsyncMock(return_value=docs)),
        patch("helixis.db.engine.get_pool", return_value=MagicMock()),
    ):
        out = await bt._upload_document_impl(
            document_name="lease agreement",
            attach_to="lease",
            entity_id=1,
            entity_label=None,
            title="",
            description="",
        )
    assert "Multiple documents match" in out


@pytest.mark.asyncio
async def test_share_tool_requires_a_change() -> None:
    with _ctx():
        out = await bt._share_file_impl(
            file_id=42,
            share_with_tenants=None,
            share_with_rental_owners=None,
            file_label=None,
        )
    assert "Tell me what to change" in out


def _share_client_api(current_sharing: dict[str, Any]) -> tuple[Any, Any, AsyncMock, AsyncMock]:
    get_sharing = AsyncMock(return_value=current_sharing)
    put_sharing = AsyncMock(return_value={})
    api_mock = MagicMock()
    api_mock.files = MagicMock()
    api_mock.files.get_sharing = get_sharing
    api_mock.files.put_sharing = put_sharing
    client_mock = MagicMock()
    client_mock.__aenter__ = AsyncMock(return_value=client_mock)
    client_mock.__aexit__ = AsyncMock(return_value=False)
    return client_mock, api_mock, get_sharing, put_sharing


async def _run_share_approved(
    current_sharing: dict[str, Any],
    *,
    tenants: bool | None = True,
    owners: bool | None = None,
) -> tuple[str, AsyncMock]:
    """Run the share impl with the guard stubbed to execute immediately."""
    client_mock, api_mock, _get, put = _share_client_api(current_sharing)

    async def _fake_guard(**kwargs: Any) -> Any:
        result = await kwargs["execute"]()
        guard = MagicMock()
        guard.outcome = "executed"
        guard.result = result
        return guard

    with (
        _ctx(),
        patch.object(bt, "_open_api", AsyncMock(return_value=(client_mock, api_mock, None))),
        patch.object(bt, "run_guarded", new=_fake_guard),
    ):
        out = await bt._share_file_impl(
            file_id=42,
            share_with_tenants=tenants,
            share_with_rental_owners=owners,
            file_label="Signed lease",
        )
    return out, put


@pytest.mark.asyncio
async def test_share_merges_only_populated_sections() -> None:
    # GET returns every section, but only Lease is populated (spec behavior).
    current = {
        "Lease": {"Tenants": False, "RentalOwners": True},
        "Rental": None,
        "Tenant": None,
        "Account": None,
    }
    out, put = await _run_share_approved(current, tenants=True)
    assert "Updated sharing" in out
    body = put.await_args.args[1]
    # Only the populated section is sent; the requested flag flipped and the
    # untouched flag carried over (no accidental unshare).
    assert body == {"Lease": {"Tenants": True, "RentalOwners": True}}


@pytest.mark.asyncio
async def test_share_is_honest_when_file_type_has_no_tenant_knob() -> None:
    current = {"Account": {"AllStaff": True}}
    out, put = await _run_share_approved(current, tenants=True)
    assert "doesn't support tenant/owner sharing" in out
    put.assert_not_awaited()
