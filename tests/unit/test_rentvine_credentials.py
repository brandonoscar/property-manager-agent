"""Phase 3 guards for the Rentvine credential path (ADR 0010 Decision (e)).

Three properties, and the second and third are the ones that would rot quietly:

1. secrets round-trip through Fernet and never surface in plaintext columns;
2. there is **no env fallback** — gotcha 70's shape, where a harness fell back
   to env credentials pointing at a different account and verified the wrong
   books. A behavioural test alone cannot see a fallback being re-added, so the
   call site is checked with an AST walk too (gotchas 62/66/69/103);
3. the `pms` feature flag defaults OFF, and it is a DEPLOYMENT switch rather
   than the per-company gate (gotchas 72/73).
"""

from __future__ import annotations

import ast
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.rentvine import credentials as rv
from helixis.security import CredentialCipher
from helixis.settings import Settings

SRC = pathlib.Path(rv.__file__)

#: A valid Fernet key so the cipher constructs. Test-only.
_KEY = "8Bz3Qn5r7sW9xYzA1bC2dE3fG4hI5jK6lM7nO8pQ9rM="


def _settings() -> Settings:
    s = Settings()
    return s.model_copy(update={"data_key": _KEY})


def _encrypted_row(settings: Settings, **over: Any) -> dict[str, Any]:
    cipher = CredentialCipher(settings)
    row = {
        "company_id": "11111111-1111-1111-1111-111111111111",
        "account_code": "acme",
        "encrypted_api_key": cipher.encrypt("RV-KEY-123456"),
        "api_key_last4": "3456",
        "encrypted_api_secret": cipher.encrypt("RV-SECRET-abcdef"),
        "base_url": "https://acme.rentvine.com/api/manager",
        "last_tested_at": None,
    }
    row.update(over)
    return row


# ── 1. the round trip ────────────────────────────────────────────────────────


def test_credentials_round_trip_through_fernet() -> None:
    s = _settings()
    out = rv.decrypt_rentvine_settings_from(_encrypted_row(s), s)
    assert out.api_key == "RV-KEY-123456"
    assert out.api_secret == "RV-SECRET-abcdef"
    assert out.account_code == "acme"


def test_the_stored_row_never_holds_a_secret_in_plaintext() -> None:
    """The ciphertext columns must not contain the plaintext they protect."""
    s = _settings()
    row = _encrypted_row(s)
    assert "RV-KEY-123456" not in row["encrypted_api_key"]
    assert "RV-SECRET-abcdef" not in row["encrypted_api_secret"]
    # ...and the one deliberately-plaintext column is the subdomain, which is
    # in every request URL anyway (migration 0100's note 1).
    assert row["account_code"] == "acme"


def test_transport_knobs_survive_the_decrypt() -> None:
    """Only the per-company fields are overridden; the shape comes from config."""
    s = _settings()
    s = s.model_copy(update={"rentvine": s.rentvine.model_copy(update={"concurrency": 7})})
    assert rv.decrypt_rentvine_settings_from(_encrypted_row(s), s).concurrency == 7


def test_the_base_url_is_derived_from_the_account_code_not_the_column() -> None:
    """One home for the URL template (gotcha 25)."""
    s = _settings()
    out = rv.decrypt_rentvine_settings_from(
        _encrypted_row(s, base_url="https://STALE.example.com/nope"), s
    )
    assert rv.resolved_base_url(out) == "https://acme.rentvine.com/api/manager"


# ── 2. no env fallback ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_company_with_no_row_gets_none_not_the_env_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gotcha-70 property: absence must never resolve to env credentials."""
    s = _settings()
    s = s.model_copy(
        update={
            "rentvine": s.rentvine.model_copy(
                update={"account_code": "env-account", "api_key": "ENV", "api_secret": "ENV"}
            )
        }
    )
    monkeypatch.setattr(rv, "get_settings", lambda: s)

    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value=None)
    monkeypatch.setattr(rv, "get_pool", lambda: pool)

    assert await rv.load_rentvine_settings_for_company("some-company") is None


@pytest.mark.asyncio
async def test_an_empty_company_id_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=AssertionError("must not query"))
    monkeypatch.setattr(rv, "get_pool", lambda: pool)
    assert await rv.load_rentvine_settings_for_company("") is None


@pytest.mark.asyncio
async def test_a_decrypt_failure_returns_none_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rotated data key (gotcha 11) must not crash a turn."""
    s = _settings()
    monkeypatch.setattr(rv, "get_settings", lambda: s)
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value=_encrypted_row(s, encrypted_api_key="not-valid-ciphertext")
    )
    monkeypatch.setattr(rv, "get_pool", lambda: pool)
    assert await rv.load_rentvine_settings_for_company("c") is None


def test_no_env_fallback_exists_in_the_source() -> None:
    """A behavioural test cannot see a fallback being re-added later.

    Mutation-checked: re-introducing ``or get_settings().rentvine.api_key``
    into the loader fails this.
    """
    tree = ast.parse(SRC.read_text())
    loader = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "load_rentvine_settings_for_company"
    )
    # A fallback needs a `or`/`if` reaching settings.rentvine.<secret>.
    for node in ast.walk(loader):
        if isinstance(node, ast.Attribute) and node.attr in {
            "api_key",
            "api_secret",
            "account_code",
        }:
            raise AssertionError(
                "load_rentvine_settings_for_company reads a credential field "
                "directly — that is the env-fallback shape gotcha 70 records. "
                "It must return None when the company has no row."
            )


# ── 3. ambiguity declines rather than guesses ────────────────────────────────


@pytest.mark.asyncio
async def test_an_ambiguous_account_code_refuses_instead_of_limit_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two companies on one subdomain: routing to either is a cross-tenant leak."""
    s = _settings()
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[_encrypted_row(s), _encrypted_row(s)])
    monkeypatch.setattr(rv, "get_pool", lambda: pool)
    assert await rv.fetch_rentvine_row_by_account_code("acme") is None


@pytest.mark.asyncio
async def test_a_single_match_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control — the refusal above must not be 'refuses everything'."""
    s = _settings()
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[_encrypted_row(s)])
    monkeypatch.setattr(rv, "get_pool", lambda: pool)
    got = await rv.fetch_rentvine_row_by_account_code("acme")
    assert got is not None and got["account_code"] == "acme"


@pytest.mark.asyncio
async def test_an_empty_account_code_never_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=AssertionError("must not query"))
    monkeypatch.setattr(rv, "get_pool", lambda: pool)
    assert await rv.fetch_rentvine_row_by_account_code("") is None


# ── 4. the flag ──────────────────────────────────────────────────────────────


def test_the_pms_flag_defaults_off() -> None:
    assert Settings().rentvine.enabled is False


def test_the_flag_is_documented_as_a_deployment_switch_not_a_per_company_gate() -> None:
    """Gotchas 72/73: a global flag standing in for a per-company fact is how
    the first customer's approval flipped Twilio on for everybody."""
    from helixis.settings import RentvineSettings

    doc = RentvineSettings.__doc__ or ""
    assert "per-company" in doc.lower()
    assert "company_rentvine_credentials" in doc, (
        "the docstring must name what DOES answer the per-company question, "
        "or the next reader uses the flag for it"
    )


# ── 5. migration 0100's a1 trigger (finding R3-7) ────────────────────────────

_MIGRATION = (
    pathlib.Path(__file__).resolve().parents[2]
    / "supabase/migrations/0100_company_rentvine_credentials.sql"
)


def _sql_without_comments() -> str:
    """The statements only. The migration's own comments quote the rule, so a
    whole-file scan would grade the explanation (gotchas 40/59/114)."""
    lines = [ln.split("--")[0] for ln in _MIGRATION.read_text().splitlines()]
    return "\n".join(lines)


def test_the_one_pms_trigger_is_bound_to_both_credential_tables() -> None:
    """R3-7: with both PMSs' ids in one column under UNIQUE (company_id,
    buildium_id), violating a1 causes a SILENT cross-PMS overwrite — the
    upsert's DO UPDATE fires and one row survives, no error. This trigger is
    what prevents that, so it is not optional and it is not one-sided.

    Guarding only the new table would leave the invariant open to whichever
    PMS is connected second, which is the ordering nobody tests.
    """
    sql = _sql_without_comments().lower()
    assert "create trigger trg_one_pms_rentvine" in sql
    assert "on public.company_rentvine_credentials" in sql
    assert "create trigger trg_one_pms_buildium" in sql, (
        "the exclusivity trigger is bound to the Rentvine table only — a "
        "company that connects Rentvine FIRST can still add Buildium"
    )
    assert "on public.company_buildium_credentials" in sql


def test_the_a1_trigger_fails_closed() -> None:
    """It must RAISE, not prefer a PMS and not delete the other row."""
    sql = _sql_without_comments().lower()
    assert "raise exception" in sql
    for verb in (
        "delete from public.company_buildium_credentials",
        "delete from public.company_rentvine_credentials",
    ):
        assert verb not in sql, f"the trigger resolves the conflict by deleting: {verb}"


def test_the_migration_does_not_ship_a_webhook_secret_column() -> None:
    """Deliberate absence (ADR 0010 Decision (e)): the algorithm that would
    consume it is unknown, and a column holding an unvalidatable value is
    gotcha 79's shape. Phase 5 adds it with a captured delivery in hand."""
    sql = _sql_without_comments().lower()
    assert "webhook" not in sql


def test_no_select_star_against_the_credentials_table() -> None:
    """A later migration's column (a webhook secret Phase 5 has not validated)
    must not arrive in every caller's dict by accident.

    ⚠ Scoped to the SQL STATEMENTS, never the file. The first cut of this test
    scanned the raw source and failed against correct code, because the module
    comment explaining the rule quotes ``SELECT *`` in order to explain it —
    gotcha 40, arriving inside the guard written for it. Comments are not AST
    nodes, and f-string templates are reconstructed rather than read as
    fragments (gotcha 63).
    """
    tree = ast.parse(SRC.read_text())
    statements: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            rendered = "".join(
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
                for v in node.values
            )
            statements.append(rendered)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            statements.append(node.value)

    sql = [s for s in statements if "company_rentvine_credentials" in s and "SELECT" in s.upper()]
    assert sql, "found no SELECT against the credentials table — scan is broken"
    for s in sql:
        assert "SELECT *" not in " ".join(s.upper().split()), f"SELECT * in: {s[:80]!r}"
