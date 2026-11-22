"""Settings → Documents & Links routes — auth, scoping, and PDF validation.

Links are owner-scoped (user_id from the JWT); templates + documents are
company-scoped (primary company). Storage is mocked — these assert the route
logic (validation, scoping, wiring to repo/storage), not a live upload.
Mirrors ``test_crm_routes.py``.
"""

from __future__ import annotations

import contextlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
from fastapi.testclient import TestClient

from helixis.db.repositories.docs_links import DocumentRow, LinkRow, TemplateRow, _affected
from helixis.main import create_app
from helixis.settings import Settings
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"
COMPANY_ID = "5ca1ab1e-0000-4000-8000-000000000004"
ME_USER_ID = "00000000-0000-0000-0000-000000000001"
DOC_ID = "00000000-0000-0000-0000-0000000000dd"


def _token(sub: str = ME_USER_ID) -> str:
    return jwt.encode(
        {
            "sub": sub,
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


def _settings() -> Settings:
    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        memory={"enabled": False},
        supabase={"url": TEST_SUPABASE_URL, "service_key": "svc"},
    )


@contextlib.contextmanager
def _stack(patches):
    """Enter a list of patch() context managers together."""
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def _client(settings: Settings, **repo_mocks: object):
    """Build a TestClient with auth + pool + repo/storage patched. Extra kwargs
    patch names on ``helixis.api.routes.documents`` (repo.* / storage fns)."""
    patches = [
        patch("helixis.api.routes.documents.get_pool", return_value=MagicMock()),
        patch("helixis.api.auth._resolve_company_ids", side_effect=lambda _uid: [COMPANY_ID]),
        patch("helixis.api.auth.get_settings", return_value=settings),
        patch("helixis.main.get_settings", return_value=settings),
    ]
    for name, mock in repo_mocks.items():
        patches.append(patch(f"helixis.api.routes.documents.{name}", mock))
    return patches


# ── unit: command-tag helper ────────────────────────────────────────────────


def test_affected_parses_command_tag():
    assert _affected("DELETE 1") is True
    assert _affected("DELETE 3") is True
    assert _affected("DELETE 0") is False


# ── links (per user) ────────────────────────────────────────────────────────


def test_list_links_scoped_to_caller():
    settings = _settings()
    list_links = AsyncMock(return_value=[LinkRow(id="l1", label="Scheduling", url="https://cal")])
    with _stack(_client(settings, **{"repo.list_links": list_links})):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get("/api/v1/links", headers={"Authorization": f"Bearer {_token()}"})
    assert resp.status_code == 200
    assert resp.json() == [{"id": "l1", "label": "Scheduling", "url": "https://cal"}]
    assert list_links.await_args.kwargs["user_id"] == ME_USER_ID


def test_create_link_trims_and_returns():
    settings = _settings()
    create_link = AsyncMock(return_value="new-id")
    with _stack(_client(settings, **{"repo.create_link": create_link})):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/links",
                json={"label": "  Apply  ", "url": " https://apply "},
                headers={"Authorization": f"Bearer {_token()}"},
            )
    assert resp.status_code == 200
    assert resp.json() == {"id": "new-id", "label": "Apply", "url": "https://apply"}
    assert create_link.await_args.kwargs["user_id"] == ME_USER_ID


def test_delete_link_missing_is_404():
    settings = _settings()
    delete_link = AsyncMock(return_value=False)
    with _stack(_client(settings, **{"repo.delete_link": delete_link})):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.delete("/api/v1/links/nope", headers={"Authorization": f"Bearer {_token()}"})
    assert resp.status_code == 404


def test_links_unauthenticated_is_401():
    settings = _settings()
    with _stack(_client(settings)):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get("/api/v1/links")
    assert resp.status_code == 401


# ── templates (per company) ─────────────────────────────────────────────────


def test_list_templates_scoped_to_company():
    settings = _settings()
    list_templates = AsyncMock(return_value=[TemplateRow(id="t1", name="Tour", body="Hi {name}")])
    with _stack(_client(settings, **{"repo.list_templates": list_templates})):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get("/api/v1/templates", headers={"Authorization": f"Bearer {_token()}"})
    assert resp.status_code == 200
    assert resp.json()[0]["name"] == "Tour"
    assert list_templates.await_args.kwargs["company_id"] == COMPANY_ID


def test_create_template_requires_name():
    settings = _settings()
    create_template = AsyncMock(return_value="t9")
    with _stack(_client(settings, **{"repo.create_template": create_template})):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/templates",
                json={"name": "   ", "body": "x"},
                headers={"Authorization": f"Bearer {_token()}"},
            )
    assert resp.status_code == 422
    create_template.assert_not_awaited()


# ── documents (company; storage mocked) ─────────────────────────────────────


def test_upload_document_stores_and_records():
    settings = _settings()
    upload = AsyncMock()
    create_document = AsyncMock()
    mocks = {
        "upload_object": upload,
        "repo.create_document": create_document,
    }
    with _stack(_client(settings, **mocks)):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/documents",
                files={"file": ("lease.pdf", b"%PDF-1.4 body", "application/pdf")},
                data={"name": "Standard Lease", "kind": "lease"},
                headers={"Authorization": f"Bearer {_token()}"},
            )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Standard Lease"
    assert body["kind"] == "lease"
    upload.assert_awaited_once()
    # path is company-scoped
    assert upload.await_args.kwargs["path"].startswith(f"{COMPANY_ID}/")
    assert create_document.await_args.kwargs["company_id"] == COMPANY_ID


def test_upload_document_rejects_non_pdf():
    settings = _settings()
    upload = AsyncMock()
    with _stack(_client(settings, **{"upload_object": upload})):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/documents",
                files={"file": ("note.txt", b"just text", "text/plain")},
                data={"name": "Note", "kind": "other"},
                headers={"Authorization": f"Bearer {_token()}"},
            )
    assert resp.status_code == 422
    upload.assert_not_awaited()  # never reached storage


def test_upload_document_rejects_bad_kind():
    settings = _settings()
    upload = AsyncMock()
    with _stack(_client(settings, **{"upload_object": upload})):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.post(
                "/api/v1/documents",
                files={"file": ("lease.pdf", b"%PDF-1.4", "application/pdf")},
                data={"name": "L", "kind": "contract"},
                headers={"Authorization": f"Bearer {_token()}"},
            )
    assert resp.status_code == 422
    upload.assert_not_awaited()


def test_download_document_signs_url():
    settings = _settings()
    doc = DocumentRow(
        id=DOC_ID,
        name="Lease",
        kind="lease",
        storage_path=f"{COMPANY_ID}/{DOC_ID}.pdf",
        content_type="application/pdf",
        size_bytes=10,
        created_at=__import__("datetime").datetime(2026, 7, 4),
    )
    get_document = AsyncMock(return_value=doc)
    sign = AsyncMock(return_value="https://signed/url")
    with _stack(_client(settings, **{"repo.get_document": get_document, "sign_object_url": sign})):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                f"/api/v1/documents/{DOC_ID}/download",
                headers={"Authorization": f"Bearer {_token()}"},
            )
    assert resp.status_code == 200
    assert resp.json() == {"url": "https://signed/url"}
    assert sign.await_args.kwargs["path"] == doc.storage_path


def test_download_missing_document_is_404():
    settings = _settings()
    get_document = AsyncMock(return_value=None)
    with _stack(_client(settings, **{"repo.get_document": get_document})):
        app = create_app(settings)
        with TestClient(app) as c:
            resp = c.get(
                f"/api/v1/documents/{DOC_ID}/download",
                headers={"Authorization": f"Bearer {_token()}"},
            )
    assert resp.status_code == 404
