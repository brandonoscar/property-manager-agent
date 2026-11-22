"""Tests for JWT authentication module."""

from __future__ import annotations

import ast
import time
from pathlib import Path

import jwt
import pytest
from fastapi import HTTPException

from helixis.api.auth import (
    AuthenticatedUser,
    _decode_jwt,
    _extract_bearer_token,
    require_company_access,
)
from tests.unit._supabase_tokens import TEST_ISSUER, TEST_SUPABASE_URL

# Shared test secret — must match conftest settings
TEST_JWT_SECRET = "test-secret-key-for-unit-tests-only-not-production"


def _make_token(
    sub: str = "user-123",
    email: str = "test@example.com",
    role: str = "authenticated",
    exp_offset: int = 3600,
    secret: str = TEST_JWT_SECRET,
    audience: str = "authenticated",
) -> str:
    """Create a signed JWT for testing."""
    payload = {
        "sub": sub,
        "email": email,
        "role": role,
        "aud": audience,
        "exp": int(time.time()) + exp_offset,
        "iat": int(time.time()),
        "iss": TEST_ISSUER,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


# --- _extract_bearer_token ---


def test_extract_bearer_token_valid():
    token = _extract_bearer_token("Bearer abc123")
    assert token == "abc123"


def test_extract_bearer_token_missing():
    with pytest.raises(HTTPException) as exc:
        _extract_bearer_token(None)
    assert exc.value.status_code == 401
    assert "Missing" in exc.value.detail


def test_extract_bearer_token_invalid_format():
    with pytest.raises(HTTPException) as exc:
        _extract_bearer_token("Basic abc123")
    assert exc.value.status_code == 401


def test_extract_bearer_token_no_token_value():
    with pytest.raises(HTTPException) as exc:
        _extract_bearer_token("Bearer")
    assert exc.value.status_code == 401


# --- _decode_jwt ---


def test_decode_jwt_valid(monkeypatch):
    """Valid token decodes successfully."""
    from helixis.settings import Settings

    settings = Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: settings)

    token = _make_token()
    payload = _decode_jwt(token)
    assert payload["sub"] == "user-123"
    assert payload["email"] == "test@example.com"
    assert payload["role"] == "authenticated"


def test_decode_jwt_expired(monkeypatch):
    """Expired token raises 401."""
    from helixis.settings import Settings

    settings = Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: settings)

    token = _make_token(exp_offset=-3600)
    with pytest.raises(HTTPException) as exc:
        _decode_jwt(token)
    assert exc.value.status_code == 401
    assert "expired" in exc.value.detail


def test_decode_jwt_wrong_secret(monkeypatch):
    """Token signed with wrong secret raises 401."""
    from helixis.settings import Settings

    settings = Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: settings)

    token = _make_token(secret="wrong-secret")
    with pytest.raises(HTTPException) as exc:
        _decode_jwt(token)
    assert exc.value.status_code == 401


def test_decode_jwt_no_secret_configured(monkeypatch):
    """Nothing configured (no Supabase URL, no jwt_secret) raises 503.

    Re-pointed 2026-11-11, assertion unchanged: these settings never had a URL,
    and it is the missing URL that answers 503 now (no issuer to check). With a
    URL and no secret, an HS256 token is a 401, pinned in the next test.
    """
    from helixis.settings import Settings

    settings = Settings(
        env="test",
        auth={"jwt_secret": ""},
        memory={"enabled": False},
    )
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: settings)

    token = _make_token()
    with pytest.raises(HTTPException) as exc:
        _decode_jwt(token)
    assert exc.value.status_code == 503


def test_an_hs256_token_with_no_secret_configured_is_a_401(monkeypatch):
    """HS256 is off while HELIXIS_AUTH__JWT_SECRET is unset: the token is
    refused as a bad sign-in, saying which setting would accept it."""
    from helixis.settings import Settings

    settings = Settings(
        env="test",
        auth={"jwt_secret": ""},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: settings)

    with pytest.raises(HTTPException) as exc:
        _decode_jwt(_make_token())
    assert exc.value.status_code == 401
    assert "HELIXIS_AUTH__JWT_SECRET" in exc.value.detail


def test_no_supabase_url_refuses_even_a_token_the_secret_signed(monkeypatch):
    """With no URL there is no issuer to compare, so a correctly signed HS256
    token is still refused (503, not configured): no environment accepts a
    token whose issuer nobody checked."""
    from helixis.settings import Settings

    settings = Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        memory={"enabled": False},
    )
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: settings)

    with pytest.raises(HTTPException) as exc:
        _decode_jwt(_make_token())
    assert exc.value.status_code == 503
    assert exc.value.detail == "Authentication not configured"


# --- the issuer ---


def _hs256_settings(url: str = TEST_SUPABASE_URL):
    from helixis.settings import Settings

    return Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": url},
        memory={"enabled": False},
    )


def _token_with(claims: dict) -> str:
    payload = {
        "sub": "user-123",
        "role": "authenticated",
        "aud": "authenticated",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        **claims,
    }
    return jwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")


def test_the_issuer_is_the_project_url_plus_auth_v1() -> None:
    """The form measured on helixis-test's edge logs (2026-11-11): every
    `authenticated` token carried `https://<ref>.supabase.co/auth/v1`, no
    trailing slash. A trailing slash on the configured URL is dropped."""
    from helixis.api.auth import expected_issuer

    live = "https://abcdefghijklmnopqrst.supabase.co"
    assert expected_issuer(live) == "https://abcdefghijklmnopqrst.supabase.co/auth/v1"
    assert expected_issuer(live + "/") == "https://abcdefghijklmnopqrst.supabase.co/auth/v1"


@pytest.mark.parametrize(
    "iss",
    [
        "supabase",  # what the legacy API keys carry, and what tests used to mint
        "https://other-project.supabase.co/auth/v1",
        "https://test.supabase.co/auth/v1/",
        "https://test.supabase.co",
        "",
    ],
)
def test_a_token_from_another_issuer_is_refused(monkeypatch, iss: str) -> None:
    """Signed with the right secret, so only the issuer can refuse it."""
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: _hs256_settings())
    with pytest.raises(HTTPException) as exc:
        _decode_jwt(_token_with({"iss": iss}))
    assert exc.value.status_code == 401


def test_a_token_with_no_issuer_is_refused(monkeypatch) -> None:
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: _hs256_settings())
    with pytest.raises(HTTPException) as exc:
        _decode_jwt(_token_with({}))
    assert exc.value.status_code == 401
    assert "iss" in exc.value.detail


def test_the_issuer_follows_the_configured_url(monkeypatch) -> None:
    """The control for the refusals above: the same token shape with this
    project's issuer decodes, including when the URL is written with a
    trailing slash."""
    monkeypatch.setattr(
        "helixis.api.auth.get_settings", lambda: _hs256_settings(TEST_SUPABASE_URL + "/")
    )
    payload = _decode_jwt(_token_with({"iss": TEST_ISSUER}))
    assert payload["iss"] == TEST_ISSUER


def test_a_refused_issuer_is_logged_with_both_values(monkeypatch, caplog) -> None:
    """A deploy whose HELIXIS_SUPABASE__URL doesn't match the project would 401
    everybody; the log line is what says why. Neither value is a secret."""
    import logging

    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: _hs256_settings())
    wrong = "https://other-project.supabase.co/auth/v1"
    with caplog.at_level(logging.WARNING, logger="helixis.api.auth"):
        with pytest.raises(HTTPException):
            _decode_jwt(_token_with({"iss": wrong}))
    messages = [r.getMessage() for r in caplog.records]
    assert any(wrong in m and TEST_ISSUER in m for m in messages), messages


# --- require_company_access ---


def test_require_company_access_allowed():
    user = AuthenticatedUser(user_id="u1", company_ids=["company-a", "company-b"])
    # Should not raise
    require_company_access(user, "company-a")


def test_require_company_access_denied():
    user = AuthenticatedUser(user_id="u1", company_ids=["company-a"])
    with pytest.raises(HTTPException) as exc:
        require_company_access(user, "company-b")
    assert exc.value.status_code == 403


def test_require_company_access_empty_companies():
    user = AuthenticatedUser(user_id="u1", company_ids=[])
    with pytest.raises(HTTPException) as exc:
        require_company_access(user, "any-company")
    assert exc.value.status_code == 403


class _FakeResp:
    def __init__(self, rows):
        self._rows = rows

    def raise_for_status(self):
        return None

    def json(self):
        return self._rows


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, *_a, **_kw):
        return _FakeResp(self._rows)


async def test_resolve_company_ids_is_deterministically_sorted(monkeypatch):
    """#68: ``company_ids[0]`` is the de-facto active company at ~30 call
    sites. The REST result order is arbitrary, so we sort — a multi-company
    user must land in the SAME company every request, not a random one."""
    import httpx

    from helixis.api import auth
    from helixis.settings import Settings

    settings = Settings(
        env="test",
        auth={"jwt_secret": "s"},
        supabase={"url": "https://x.supabase.co", "service_key": "svc"},
    )
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: settings)
    # Rows come back in a jumbled order; the resolver must sort them.
    rows = [{"company_id": "c-charlie"}, {"company_id": "c-alpha"}, {"company_id": "c-bravo"}]
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient(rows))

    result = await auth._resolve_company_ids("user-1")
    assert result == ["c-alpha", "c-bravo", "c-charlie"]


# --- ES256 / JWKS path (Supabase's modern asymmetric signing) ---


def _make_es256_token_and_jwks() -> tuple[str, object]:
    """Sign a Supabase-shaped token with a generated ECC keypair and
    return ``(token, fake_signing_key)`` where ``fake_signing_key`` has
    a ``.key`` attribute holding the public key — matching the surface
    PyJWKClient.get_signing_key_from_jwt returns."""
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    payload = {
        "sub": "user-es256",
        "email": "es256@example.com",
        "role": "authenticated",
        "aud": "authenticated",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
        "iss": TEST_ISSUER,
    }
    token = jwt.encode(payload, private_key, algorithm="ES256")

    class _FakeSigningKey:
        def __init__(self, k: object) -> None:
            self.key = k

    return token, _FakeSigningKey(public_key)


def test_decode_jwt_accepts_es256_via_jwks(monkeypatch):
    """Supabase projects on ECC keys send ES256 tokens. The backend must
    fetch the JWKS, pick the matching public key, and verify."""
    from helixis.settings import Settings

    settings = Settings(
        env="test",
        auth={"jwt_secret": ""},
        supabase={"url": "https://test.supabase.co"},
        memory={"enabled": False},
    )
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: settings)

    token, fake_key = _make_es256_token_and_jwks()

    class _FakeJwksClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def get_signing_key_from_jwt(self, _token: str) -> object:
            return fake_key

    monkeypatch.setattr("helixis.api.auth.jwt.PyJWKClient", _FakeJwksClient)
    # Also reset the module-level cache so the new client is used.
    monkeypatch.setattr("helixis.api.auth._jwks_client", None)
    monkeypatch.setattr("helixis.api.auth._jwks_url", None)

    payload = _decode_jwt(token)
    assert payload["sub"] == "user-es256"
    assert payload["email"] == "es256@example.com"


def test_decode_jwt_surfaces_jwks_error_when_es256_fails(monkeypatch):
    """If the project is on ECC and the JWKS has no key for the token, the 401
    detail should include the JWKS error — not the misleading 'alg not allowed'
    that came from trying the HS256 path on an ES256 token.

    Re-pointed 2026-11-11: the stand-in raised the base ``PyJWKClientError``
    with a FETCH message ("connection refused"), but PyJWT raises its
    ``PyJWKClientConnectionError`` subclass for a fetch, which is now a 503
    (next test). The base class is what it raises when the set was read and
    holds no key for the token's ``kid``, so the stand-in raises that, in
    PyJWT's words, and the assertion reads it back.
    """
    from helixis.settings import Settings

    settings = Settings(
        env="test",
        auth={"jwt_secret": ""},
        supabase={"url": "https://test.supabase.co", "anon_key": "anon"},
        memory={"enabled": False},
    )
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: settings)

    token, _ = _make_es256_token_and_jwks()
    _use_jwks(monkeypatch, raises=jwt.PyJWKClientError(_NO_MATCHING_KEY))

    with pytest.raises(HTTPException) as exc:
        _decode_jwt(token)
    assert exc.value.status_code == 401
    # The diagnostic message must contain the JWKS-specific error so the
    # operator can see why the modern path failed.
    assert _NO_MATCHING_KEY in exc.value.detail


#: PyJWT's own words for a JWKS that holds no key for the token's ``kid``.
_NO_MATCHING_KEY = 'Unable to find a signing key that matches: "kid-unknown"'


def _use_jwks(monkeypatch, *, key: object | None = None, raises: Exception | None = None):
    """Stand in for PyJWKClient: hand back ``key``, or raise ``raises``. Returns
    the list of tokens it was asked about, so a test can say it was never asked."""
    asked: list[str] = []

    class _FakeJwksClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def get_signing_key_from_jwt(self, token: str) -> object:
            asked.append(token)
            if raises is not None:
                raise raises
            return key

    monkeypatch.setattr("helixis.api.auth.jwt.PyJWKClient", _FakeJwksClient)
    monkeypatch.setattr("helixis.api.auth._jwks_client", None)
    monkeypatch.setattr("helixis.api.auth._jwks_url", None)
    return asked


def _es256_settings(jwt_secret: str = ""):
    from helixis.settings import Settings

    return Settings(
        env="test",
        auth={"jwt_secret": jwt_secret},
        supabase={"url": TEST_SUPABASE_URL},
        memory={"enabled": False},
    )


@pytest.mark.parametrize(
    "failure",
    [
        jwt.PyJWKClientConnectionError('Fail to fetch data from the url, err: "timed out"'),
        __import__("json").JSONDecodeError("Expecting value", "<html>", 0),
    ],
    ids=["unreachable", "not-json"],
)
def test_a_jwks_that_cannot_be_fetched_is_a_503_not_a_401(monkeypatch, failure):
    """Unreachable or unreadable keys say nothing about the token. A 401 would
    tell the app the sign-in is bad (and the books would count a wrong
    password); 503 says try again."""
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: _es256_settings())
    token, _ = _make_es256_token_and_jwks()
    _use_jwks(monkeypatch, raises=failure)

    with pytest.raises(HTTPException) as exc:
        _decode_jwt(token)
    assert exc.value.status_code == 503
    assert exc.value.headers == {"Retry-After": "5"}


@pytest.mark.parametrize(
    "failure",
    [
        jwt.PyJWKClientConnectionError("unreachable"),
        jwt.PyJWKClientError(_NO_MATCHING_KEY),
    ],
    ids=["unreachable", "no-matching-key"],
)
def test_an_es256_token_is_never_retried_as_hs256(monkeypatch, failure):
    """⚠ The fallback this replaced: with a secret configured, a failed JWKS
    check went on to try the token against the HS256 secret. Read the calls,
    not just the status — both paths ended in a refusal for a real ES256
    token, which is how the fallback survived looking harmless."""
    monkeypatch.setattr(
        "helixis.api.auth.get_settings", lambda: _es256_settings(jwt_secret=TEST_JWT_SECRET)
    )
    token, _ = _make_es256_token_and_jwks()
    _use_jwks(monkeypatch, raises=failure)
    seen: list[object] = []
    real_decode = jwt.decode

    def spy(*args, **kwargs):
        seen.append(kwargs.get("algorithms"))
        return real_decode(*args, **kwargs)

    monkeypatch.setattr("helixis.api.auth.jwt.decode", spy)

    with pytest.raises(HTTPException):
        _decode_jwt(token)
    assert ["HS256"] not in seen, f"the ES256 token was tried against the secret: {seen}"


def test_an_es256_token_from_another_issuer_is_refused(monkeypatch):
    """The issuer check holds on the JWKS path too, not only on HS256."""
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    token = jwt.encode(
        {
            "sub": "user-es256",
            "role": "authenticated",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iss": "https://other-project.supabase.co/auth/v1",
        },
        private_key,
        algorithm="ES256",
    )

    class _Key:
        key = private_key.public_key()

    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: _es256_settings())
    _use_jwks(monkeypatch, key=_Key())
    with pytest.raises(HTTPException) as exc:
        _decode_jwt(token)
    assert exc.value.status_code == 401


def test_an_hs256_token_is_verified_by_the_secret_without_the_jwks(monkeypatch):
    """An HS256 token goes to the secret and never costs a JWKS round trip,
    even when the JWKS is broken.

    Re-pointed 2026-11-11 from ``test_decode_jwt_falls_back_to_hs256_when_jwks_fails``:
    its token was HS256, so it never reached the JWKS and never exercised a
    fallback; it passed for the reason this name now says. The fallback it
    named is gone (``test_an_es256_token_is_never_retried_as_hs256``). Now it
    also asserts the JWKS was not asked.
    """
    from helixis.settings import Settings

    settings = Settings(
        env="test",
        auth={"jwt_secret": TEST_JWT_SECRET},
        supabase={"url": "https://test.supabase.co"},
        memory={"enabled": False},
    )
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: settings)
    asked = _use_jwks(monkeypatch, raises=jwt.PyJWKClientConnectionError("no jwks endpoint"))

    token = _make_token()  # HS256 with TEST_JWT_SECRET
    payload = _decode_jwt(token)
    assert payload["sub"] == "user-123"
    assert asked == [], "an HS256 token went to the JWKS"


@pytest.mark.parametrize("alg", ["none", "HS512", "PS256", ""])
def test_an_algorithm_we_do_not_verify_is_refused(monkeypatch, alg):
    """Bad input: an unsigned or unexpected header names no verifier here."""
    import base64
    import json as _json

    def b64(obj: dict) -> str:
        raw = _json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = {"typ": "JWT", **({"alg": alg} if alg else {})}
    claims = {"sub": "u", "role": "authenticated", "aud": "authenticated", "iss": TEST_ISSUER}
    token = f"{b64(header)}.{b64(claims)}.sig"
    monkeypatch.setattr(
        "helixis.api.auth.get_settings", lambda: _es256_settings(jwt_secret=TEST_JWT_SECRET)
    )
    with pytest.raises(HTTPException) as exc:
        _decode_jwt(token)
    assert exc.value.status_code == 401
    assert "unsupported alg" in exc.value.detail


def test_a_header_that_is_not_a_jwt_is_a_401(monkeypatch):
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: _es256_settings())
    with pytest.raises(HTTPException) as exc:
        _decode_jwt("not-a-jwt")
    assert exc.value.status_code == 401


async def test_the_users_own_signup_shell_sorts_last(monkeypatch):
    """⚠ THE REGRESSION THIS EXISTS FOR. a teammate account belonged to My Company (11
    properties, Buildium connected) AND to their own signup shell. The shell's
    uuid sorted first, so every request resolved to the empty one — a blank
    workspace and an inert invite, for weeks. A second user in the identical
    state was fine because their real company happened to sort first.

    `test_resolve_company_ids_is_deterministically_sorted` above passed against
    that the whole time: the answer WAS the same every request. It pins
    consistency; this pins correctness.

    The signup trigger sets `companies.id = auth.users.id`, so the company whose
    id equals the caller's own user id is their personal shell.
    """
    import httpx

    from helixis.api import auth
    from helixis.settings import Settings

    settings = Settings(
        env="test",
        auth={"jwt_secret": "s"},
        supabase={"url": "https://x.supabase.co", "service_key": "svc"},
    )
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: settings)

    user_id = "5ca1ab1e-0000-4000-8000-000000000003"  # shell id == user id
    real = "5ca1ab1e-0000-4000-8000-000000000004"  # sorts AFTER the shell
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: _FakeClient([{"company_id": user_id}, {"company_id": real}]),
    )

    result = await auth._resolve_company_ids(user_id)
    assert result[0] == real, "company_ids[0] must be the company they were invited to"
    assert set(result) == {real, user_id}, "the shell is de-prioritised, never dropped"


async def test_a_single_company_user_is_untouched(monkeypatch):
    """A brand-new customer's ONLY company IS their shell (id == user id). A
    one-element list has nothing to order, so the rule cannot mis-fire on the
    common case."""
    import httpx

    from helixis.api import auth
    from helixis.settings import Settings

    settings = Settings(
        env="test",
        auth={"jwt_secret": "s"},
        supabase={"url": "https://x.supabase.co", "service_key": "svc"},
    )
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: settings)
    user_id = "5ca1ab1e-0000-4000-8000-000000000003"
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: _FakeClient([{"company_id": user_id}])
    )

    assert await auth._resolve_company_ids(user_id) == [user_id]


async def test_two_companies_is_logged_as_an_invariant_violation(monkeypatch, caplog):
    """One account, one company (founder directive 2026-09-03). The ordering
    makes the state survivable; the log is what makes it VISIBLE — the last one
    lasted weeks precisely because nothing said anything."""
    import logging

    import httpx

    from helixis.api import auth
    from helixis.settings import Settings

    settings = Settings(
        env="test",
        auth={"jwt_secret": "s"},
        supabase={"url": "https://x.supabase.co", "service_key": "svc"},
    )
    monkeypatch.setattr("helixis.api.auth.get_settings", lambda: settings)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **k: _FakeClient([{"company_id": "a"}, {"company_id": "b"}]),
    )

    with caplog.at_level(logging.ERROR):
        await auth._resolve_company_ids("u1")
    assert any("one-account-one-company" in r.getMessage() for r in caplog.records)


# ── require_unambiguous_company: the three irreversible actions ──────────


def _user(*company_ids: str):
    from helixis.api.auth import AuthenticatedUser

    return AuthenticatedUser(
        user_id="00000000-0000-0000-0000-000000000001",
        email="a@example.com",
        role="authenticated",
        company_ids=list(company_ids),
    )


def test_one_company_resolves_normally() -> None:
    from helixis.api.auth import require_unambiguous_company

    assert require_unambiguous_company(_user("c1")) == "c1"


def test_two_companies_refuses_rather_than_picking() -> None:
    """⚠ The whole point. `company_ids[0]` is a TIE-BREAK, not knowledge — the
    resolver orders the caller's own signup shell last, which is right for an
    invited teammate and says nothing about someone holding two REAL companies.

    Buying a phone number is a recurring charge with no undo; the A2P
    attestation locks to `submitted` with a UNIQUE company_id so the real
    company can never get its own; a subscription pays for one company while
    `/agent/run` meters another. None of those can afford a coin flip
    (gotcha 55: a guard that cannot see must decline to act).
    """
    from fastapi import HTTPException

    from helixis.api.auth import require_unambiguous_company

    with pytest.raises(HTTPException) as exc:
        require_unambiguous_company(_user("c1", "c2"))
    assert exc.value.status_code == 409, "409 — the account is unsupported, not forbidden"
    assert "charged or submitted" in exc.value.detail, (
        "the message must say nothing happened; a refusal on a money path that "
        "leaves the user unsure whether they were billed is its own problem"
    )


def test_no_company_is_still_a_403() -> None:
    # A different state with a different meaning, and it predates this helper.
    from fastapi import HTTPException

    from helixis.api.auth import require_unambiguous_company

    with pytest.raises(HTTPException) as exc:
        require_unambiguous_company(_user())
    assert exc.value.status_code == 403


def test_the_three_irreversible_routes_use_it() -> None:
    """⚠ Reads the CALL SITES (gotcha 62). Every test above drives the helper
    directly, so all of them pass against routes that never call it.

    ⚠ And it walks the AST rather than searching the file for the NAME. The
    first cut did the latter and the mutation battery killed it: deleting the
    actual call leaves `from helixis.api.auth import require_unambiguous_company`
    behind, which satisfies a substring scan perfectly. Four mutants survived
    that way — a guard that is blind in exactly the direction it exists to
    watch (gotcha 40).

    Scoped to the three that cannot be undone. The other ~19 `company_ids[0]`
    sites deliberately keep it: a read or a reversible write can afford a
    wrong-workspace mistake, because the user sees an empty screen and says
    something. Widening this would refuse the whole product to anyone in the
    broken state instead of the three actions that matter.
    """
    root = Path(__file__).resolve().parents[2] / "src/helixis"
    expected = {
        "api/routes/sms.py": "buying a Twilio number is a recurring charge with no undo",
        "api/routes/a2p.py": "the A2P attestation is one-way and company_id is UNIQUE",
        "api/routes/billing.py": "Checkout and the portal move money",
    }
    missing = []
    for rel, why in expected.items():
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        called = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "require_unambiguous_company"
            for node in ast.walk(tree)
        )
        if not called:
            missing.append(f"{rel} — {why}")
    assert not missing, "these no longer CALL the guard:\n  " + "\n  ".join(missing)


def test_reads_are_deliberately_left_permissive() -> None:
    """The inverse, so the scope cannot creep. `/billing/status` must keep
    resolving [0]: showing a member the wrong meter is recoverable and they
    will say so; refusing to render the page is not an improvement.

    ⚠ Matches the function DEFINITION on the AST — `"async def _company_id" in
    src` also matches `_company_id_removed`, which is how this one survived
    its mutation too.
    """
    tree = ast.parse(
        (Path(__file__).resolve().parents[2] / "src/helixis/api/routes/billing.py").read_text(
            encoding="utf-8"
        )
    )
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    assert "_company_id" in names, (
        "billing lost its permissive resolver — /status should not refuse to "
        f"render for an account in an unsupported state. Found: {sorted(names)}"
    )
