"""POST /agent/extract-document — the in-chat document upload endpoint.

A thin, read-only wrapper over the document agent's tested extraction core
(``extract_document_fields_impl``). These pin the route's contract without a
live Gemini call (the core is mocked):

  - happy path → returns the DocumentExtraction envelope
  - cross-tenant company_id → 403 (require_company_access)
  - unsupported MIME (incl. the eval-only text/plain) → 415, core never called
  - core raising → 502 (no stack/traceback leaked to the client)
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from helixis.agents.document_tools import DocumentExtraction, SourceRef
from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
OTHER_COMPANY = "ffffffff-ffff-ffff-ffff-ffffffffffff"
USER_ID = "00000000-0000-0000-0000-000000000001"

# ⚠ A REAL PDF header, because the bytes now matter. This was `aGVsbG8=`
# ("hello") under a comment saying "the core is mocked, so the bytes never
# matter" — true until the route started checking that the content is what the
# client CLAIMS it is, at which point every test here 422'd. The comment
# recorded an assumption, and the change that invalidated it is exactly the one
# that had to come find it (gotcha 48).
_B64 = "JVBERi0xLjQKJeLjz9MKMSAwIG9iago8PCAvVHlwZSAvQ2F0YWxvZyA+PgplbmRvYmoK"

# Bytes that are NOT a PDF, for the mismatch case.
_B64_NOT_A_PDF = "aGVsbG8="


def _token() -> str:
    return jwt.encode(
        {
            "sub": USER_ID,
            "email": "brandon@example.com",
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "iss": TEST_ISSUER,
        },
        TEST_JWT_SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )


@pytest.fixture
def client(test_settings: Settings):
    from helixis.api.rate_limit import agent_action_limiter

    agent_action_limiter.counter._log.clear()

    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    with (
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=test_settings),
        patch("helixis.main.get_settings", return_value=test_settings),
    ):
        app = create_app(test_settings)
        with TestClient(app) as c:
            yield c


def _body(mime_type: str = "application/pdf") -> dict:
    return {
        "company_id": COMPANY_ID,
        "base64_content": _B64,
        "mime_type": mime_type,
        "filename": "lease.pdf",
    }


def test_extract_document_happy_path(client: TestClient) -> None:
    result = DocumentExtraction(
        doc_type="other",
        confidence=0.92,
        fields={"tenant_name": "Maria Lopez", "monthly_rent": "$1,840"},
        summary="Standard 12-month residential lease.",
        source_ref=SourceRef(kind="base64", id="upload"),
        warnings=[],
    )
    impl = AsyncMock(return_value=result)
    with patch("helixis.api.routes.agent.extract_document_fields_impl", new=impl):
        resp = client.post(
            "/api/v1/agent/extract-document",
            headers={"Authorization": f"Bearer {_token()}"},
            json=_body(),
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["doc_type"] == "other"
    assert data["fields"]["tenant_name"] == "Maria Lopez"
    impl.assert_awaited_once()
    # The endpoint passes the upload through to the tested core.
    kwargs = impl.await_args.kwargs
    assert kwargs["base64_content"] == _B64
    assert kwargs["mime_type"] == "application/pdf"


def test_extract_document_rejects_cross_tenant(client: TestClient) -> None:
    impl = AsyncMock()
    with patch("helixis.api.routes.agent.extract_document_fields_impl", new=impl):
        body = _body()
        body["company_id"] = OTHER_COMPANY
        resp = client.post(
            "/api/v1/agent/extract-document",
            headers={"Authorization": f"Bearer {_token()}"},
            json=body,
        )
    assert resp.status_code == 403
    impl.assert_not_awaited()  # never reached the extraction core


def test_extract_document_rejects_unsupported_mime(client: TestClient) -> None:
    impl = AsyncMock()
    with patch("helixis.api.routes.agent.extract_document_fields_impl", new=impl):
        resp = client.post(
            "/api/v1/agent/extract-document",
            headers={"Authorization": f"Bearer {_token()}"},
            json=_body(mime_type="application/zip"),
        )
    assert resp.status_code == 415
    impl.assert_not_awaited()


def test_extract_document_rejects_eval_only_text_plain(client: TestClient) -> None:
    # text/plain is in the document tool's supported set for eval fixtures
    # only — the upload endpoint must NOT accept it.
    impl = AsyncMock()
    with patch("helixis.api.routes.agent.extract_document_fields_impl", new=impl):
        resp = client.post(
            "/api/v1/agent/extract-document",
            headers={"Authorization": f"Bearer {_token()}"},
            json=_body(mime_type="text/plain"),
        )
    assert resp.status_code == 415
    impl.assert_not_awaited()


def test_extract_document_rejects_content_that_is_not_what_it_claims(
    client: TestClient,
) -> None:
    """⚠ The route-level half of the signature check, which did not exist and
    is why CI caught this and I did not.

    ``tests/unit/test_upload_content_matches_type.py`` covers
    ``content_matches`` thoroughly in isolation. Nothing asserted the ROUTE
    calls it — gotcha 62's rule, and the same shape as gotchas 66/69: a
    well-tested helper whose one production call site is invisible to every
    test of the helper.

    ``impl.assert_not_awaited`` is the load-bearing line: the point of the
    check is that a mislabelled file never reaches a multimodal call whose
    answer would then be reported as a confident extraction from a lease.
    """
    impl = AsyncMock()
    with patch("helixis.api.routes.agent.extract_document_fields_impl", new=impl):
        body = _body()
        body["base64_content"] = _B64_NOT_A_PDF
        resp = client.post(
            "/api/v1/agent/extract-document",
            headers={"Authorization": f"Bearer {_token()}"},
            json=body,
        )
    assert resp.status_code == 422
    impl.assert_not_awaited()


def test_extract_document_maps_core_failure_to_502(client: TestClient) -> None:
    impl = AsyncMock(side_effect=RuntimeError("gemini exploded"))
    with patch("helixis.api.routes.agent.extract_document_fields_impl", new=impl):
        resp = client.post(
            "/api/v1/agent/extract-document",
            headers={"Authorization": f"Bearer {_token()}"},
            json=_body(),
        )
    assert resp.status_code == 502
    impl.assert_awaited_once()


def test_a_file_bigger_than_the_reader_is_refused_before_it_is_decoded(
    client: TestClient, test_settings: Settings
) -> None:
    """413 from the STRING LENGTH — the reader's ceiling, enforced at the door.

    ⚠ This route had no size check at all, while the core it hands off to has
    had one since it was written. So an oversize attachment was accepted here,
    decoded in full, refused by `_fetch_bytes`, and returned as a **200**
    carrying the bare token `attachment_too_large` — which the chat client
    rendered verbatim under "### Notes". The person paid for the whole upload
    to be told, in machine vocabulary, something the product knew before the
    first byte arrived.

    `impl.assert_not_awaited` is the load-bearing line: a refusal that still
    ran the extraction would be a nicer message around the same waste.
    """
    max_bytes = test_settings.document.max_bytes
    # Base64 is 4 characters per 3 bytes, so this decodes to just over the cap.
    oversize = "A" * (((max_bytes + 1024) * 4) // 3)

    impl = AsyncMock()
    with patch("helixis.api.routes.agent.extract_document_fields_impl", new=impl):
        body = _body()
        body["base64_content"] = oversize
        resp = client.post(
            "/api/v1/agent/extract-document",
            headers={"Authorization": f"Bearer {_token()}"},
            json=body,
        )

    assert resp.status_code == 413
    impl.assert_not_awaited()
    detail = resp.json()["detail"]
    # The message names OUR limit. "That file is invalid" would send somebody
    # to re-save a perfectly good scan; the fix is a smaller one.
    assert f"{max_bytes // (1024 * 1024)} MB" in detail


def test_a_file_inside_the_limit_still_reaches_the_reader(client: TestClient) -> None:
    """The other direction, or the guard above passes against a route that
    refuses everything (gotchas 38/51) — which is the same outage wearing a
    tidier error message."""
    result = DocumentExtraction(
        doc_type="other",
        confidence=0.5,
        fields={},
        summary="",
        source_ref=SourceRef(kind="base64", id="upload"),
        warnings=[],
    )
    impl = AsyncMock(return_value=result)
    with patch("helixis.api.routes.agent.extract_document_fields_impl", new=impl):
        resp = client.post(
            "/api/v1/agent/extract-document",
            headers={"Authorization": f"Bearer {_token()}"},
            json=_body(),
        )
    assert resp.status_code == 200
    impl.assert_awaited_once()


def test_a_file_just_under_the_limit_is_not_refused_by_a_sloppy_estimate(
    test_settings: Settings,
) -> None:
    """⚠ A MUTATION FOUND THIS HOLE, not review.

    Inverting the base64 ratio (`* 4 // 3` instead of `* 3 // 4`) survived the
    whole battery, because every other test here posts a 68-character payload —
    nowhere near any ceiling, so the arithmetic could be upside-down and no
    assertion could tell. The distinguishing input is the only one that
    separates the two readings: a file comfortably UNDER the cap in decoded
    bytes and over it when the ratio is wrong.

    The cap is overridden to a few kilobytes so the fixture stays small, which
    also pins a second property worth having: the route reads the SETTING
    rather than a number somebody typed twice.
    """
    from helixis.api.rate_limit import agent_action_limiter

    agent_action_limiter.counter._log.clear()
    small = Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
        document={"max_bytes": 3000},
    )

    async def fake_resolve(_user_id: str) -> list[str]:
        return [COMPANY_ID]

    result = DocumentExtraction(
        doc_type="other",
        confidence=0.5,
        fields={},
        summary="",
        source_ref=SourceRef(kind="base64", id="upload"),
        warnings=[],
    )
    impl = AsyncMock(return_value=result)

    # 3200 base64 characters → 2400 decoded bytes, under the 3000 cap. Read
    # with the ratio inverted it estimates 4266 and is wrongly refused.
    payload = _B64 + "A" * (3200 - len(_B64))

    with (
        patch("helixis.api.auth._resolve_company_ids", side_effect=fake_resolve),
        patch("helixis.api.auth.get_settings", return_value=small),
        patch("helixis.main.get_settings", return_value=small),
        patch("helixis.api.routes.agent.extract_document_fields_impl", new=impl),
    ):
        app = create_app(small)
        with TestClient(app) as c:
            body = _body()
            body["base64_content"] = payload
            resp = c.post(
                "/api/v1/agent/extract-document",
                headers={"Authorization": f"Bearer {_token()}"},
                json=body,
            )

    assert resp.status_code == 200, f"a file inside the cap was refused: {resp.json()}"
    impl.assert_awaited_once()
    assert test_settings.document.max_bytes != small.document.max_bytes
