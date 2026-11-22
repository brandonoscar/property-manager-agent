"""The backup-code endpoints.

⚠ **THIS FILE HAS NEVER EXECUTED. CI IS ITS FIRST RUN.** The dev container it
was written in cannot import these routes at all: `api/routes/mfa.py` pulls in
`helixis.api.auth` → `pyjwt` → `cryptography`, whose Rust bindings raise
`pyo3_runtime.PanicException` here, and `asyncpg` is not installed either. So
treat a failure on the first CI run as a defect in THIS FILE until proven
otherwise, and fix it rather than weakening what it asserts.

Gotcha 23 is the reason that warning is at the top rather than implied: a
credential-gated test was born broken in this repo and stayed red for a month
because the only host that ever ran it was a weekly cron nobody read. The
mitigation here is that these are ordinary unit tests on the PR's own CI run,
so the feedback arrives in minutes — but a test that has not run is not a
passing test, and this one has not run.

⚠ **WHAT IS AND IS NOT COVERED HERE.** The security-carrying logic — single
use, the atomic claim, per-user isolation, hashing, the alphabet — lives in
`helixis.auth.mfa_backup_codes` and is exercised by
`test_mfa_backup_codes.py`, which DOES run in the container, under mutation.
That split is deliberate and is why the module exists separately from the
handlers. What is left for this file is the HTTP contract: status codes, what
leaks in an error body, and the order of the two writes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.routes import mfa

USER = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def conn() -> MagicMock:
    """A connection whose async methods are awaitable.

    ⚠ Built from the auto-generated MagicMock tree and overridden narrowly —
    `MagicMock().acquire().__aenter__()` already yields an AsyncMock, so every
    method on it is awaitable. Hand-building it as a plain MagicMock breaks
    every OTHER await in the route with `object MagicMock can't be used in
    'await' expression`, far from the line that caused it (gotcha 69).
    """
    return MagicMock()


@pytest.fixture()
def client(conn: MagicMock, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(mfa, "get_pool", lambda: pool)

    app = FastAPI()
    app.include_router(mfa.router)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(user_id=USER)
    return TestClient(app)


# ── status ───────────────────────────────────────────────────────────


def test_status_reports_the_remaining_count(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mfa, "remaining_count", AsyncMock(return_value=3))
    body = client.get("/auth/mfa/backup-codes").json()
    assert body["remaining"] == 3
    assert body["issued_per_batch"] == mfa.CODE_COUNT


def test_status_never_returns_codes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The plaintext exists exactly once, in the POST response. A GET that
    # could return them would make every stolen session a 2FA bypass.
    monkeypatch.setattr(mfa, "remaining_count", AsyncMock(return_value=7))
    assert "codes" not in client.get("/auth/mfa/backup-codes").json()


# ── generation ───────────────────────────────────────────────────────


def test_generating_returns_the_plaintext_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    issued = ["AAAAA-BBBBB-CCCCC-DDDDD", "EEEEE-FFFFF-GGGGG-HHHHH"]
    monkeypatch.setattr(mfa, "replace_codes", AsyncMock(return_value=issued))
    assert client.post("/auth/mfa/backup-codes").json()["codes"] == issued


def test_generation_is_scoped_to_the_caller(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The user id comes from the verified token and is never a parameter, so
    # no request can mint codes for somebody else's account.
    spy = AsyncMock(return_value=[])
    monkeypatch.setattr(mfa, "replace_codes", spy)
    client.post("/auth/mfa/backup-codes")
    assert spy.await_args.args[1] == USER


# ── redemption ───────────────────────────────────────────────────────


def test_a_valid_code_disables_the_factor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mfa, "spend_code", AsyncMock(return_value=True))
    monkeypatch.setattr(mfa, "disable_all_factors", AsyncMock(return_value=1))
    r = client.post("/auth/mfa/backup-codes/redeem", json={"code": "AAAAA-BBBBB"})
    assert r.status_code == 200
    assert r.json()["factors_removed"] == 1


def test_the_code_is_spent_before_the_factor_is_removed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠ ORDER IS THE SECURITY PROPERTY, not a style preference.

    Spending is the atomic claim. A crash between the two steps in THIS order
    leaves a spent code and a still-enrolled factor — the user retries with
    another code and gets in. The other order leaves 2FA off with the code
    still live, which is a permanent bypass sitting on a piece of paper.
    """
    calls: list[str] = []

    async def spend(*_a, **_k) -> bool:
        calls.append("spend")
        return True

    async def disable(*_a, **_k) -> int:
        calls.append("disable")
        return 1

    monkeypatch.setattr(mfa, "spend_code", spend)
    monkeypatch.setattr(mfa, "disable_all_factors", disable)
    client.post("/auth/mfa/backup-codes/redeem", json={"code": "AAAAA-BBBBB"})
    assert calls == ["spend", "disable"]


def test_a_wrong_code_is_refused_and_removes_nothing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    disable = AsyncMock(return_value=1)
    monkeypatch.setattr(mfa, "spend_code", AsyncMock(return_value=False))
    monkeypatch.setattr(mfa, "disable_all_factors", disable)
    r = client.post("/auth/mfa/backup-codes/redeem", json={"code": "NOPE1-NOPE2"})
    assert r.status_code == 400
    disable.assert_not_awaited()


def test_a_code_of_pure_punctuation_never_reaches_the_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `normalise` strips everything outside the alphabet, so "----" arrives as
    # "" — which would otherwise be hashed and compared against every row.
    spend = AsyncMock(return_value=False)
    monkeypatch.setattr(mfa, "spend_code", spend)
    r = client.post("/auth/mfa/backup-codes/redeem", json={"code": "----"})
    assert r.status_code == 400
    spend.assert_not_awaited()


def test_the_refusal_says_the_same_thing_whether_or_not_codes_exist(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "You have no backup codes" is a hint about which accounts are worth
    # attacking. Both refusals are byte-identical.
    monkeypatch.setattr(mfa, "spend_code", AsyncMock(return_value=False))
    wrong = client.post("/auth/mfa/backup-codes/redeem", json={"code": "AAAAA-BBBBB"})
    empty = client.post("/auth/mfa/backup-codes/redeem", json={"code": "ZZZZZ-YYYYY"})
    assert wrong.json() == empty.json()


def test_a_spent_code_against_an_already_clean_account_is_not_an_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Zero factors removed is a real outcome — the code is spent either way —
    # and is reported rather than raised.
    monkeypatch.setattr(mfa, "spend_code", AsyncMock(return_value=True))
    monkeypatch.setattr(mfa, "disable_all_factors", AsyncMock(return_value=0))
    r = client.post("/auth/mfa/backup-codes/redeem", json={"code": "AAAAA-BBBBB"})
    assert r.status_code == 200
    assert r.json()["factors_removed"] == 0


# ── failure modes ────────────────────────────────────────────────────


def test_every_route_refuses_when_the_pool_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mfa, "get_pool", lambda: None)
    app = FastAPI()
    app.include_router(mfa.router)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(user_id=USER)
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/auth/mfa/backup-codes").status_code == 503
    assert c.post("/auth/mfa/backup-codes").status_code == 503
    assert c.post("/auth/mfa/backup-codes/redeem", json={"code": "AAAAA-BBBBB"}).status_code == 503


def test_a_database_error_does_not_read_as_a_wrong_code(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ⚠ 500, never 400. Telling somebody their code is wrong when the database
    # is down sends them to burn a second code, then a third — and the sheet
    # is finite.
    monkeypatch.setattr(mfa, "spend_code", AsyncMock(side_effect=RuntimeError("boom")))
    r = client.post("/auth/mfa/backup-codes/redeem", json={"code": "AAAAA-BBBBB"})
    assert r.status_code == 500


def test_an_http_refusal_is_not_swallowed_into_a_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The handler's `except Exception` wraps the same block that raises the
    # 400, so without the `except HTTPException: raise` ahead of it every
    # wrong code would report as a server error.
    monkeypatch.setattr(mfa, "spend_code", AsyncMock(return_value=False))
    r = client.post("/auth/mfa/backup-codes/redeem", json={"code": "AAAAA-BBBBB"})
    assert r.status_code == 400
