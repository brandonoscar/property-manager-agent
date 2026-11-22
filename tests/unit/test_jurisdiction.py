"""Tests for the jurisdiction stack (geo/jurisdiction.py + the backfill pass
and the ``jurisdiction_lookup`` agent tool) — mocked HTTP + pool, zero live
calls.

Pins: the Census geographies parse (x/y transposition guard; absent
"Incorporated Places" = unincorporated, an answer not an error), the
never-raise degrade contract, the merge precedence (bundled JSON <
config-table overrides), the format_compact contract (footer always present,
conservative default for anything not encoded, overlay warnings), the
missing-data-file degrade, the honest not-yet-resolved tool path, the tool
description's safety pins, and the doctrine block in the built Buildium
agent's instructions. A conditional test validates the REAL data file's
shape once it lands (skips cleanly while absent).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from helixis.agents import mirror_tools
from helixis.db.repositories.geo import ResolvedProperty
from helixis.geo import intel_backfill, jurisdiction
from helixis.settings import Settings


def _settings() -> Settings:
    return Settings(env="test", memory={"enabled": False})


def _fake_httpx(monkeypatch: pytest.MonkeyPatch, *, json_body=None, raises=None) -> MagicMock:
    """Replace httpx.AsyncClient with a fake whose ``get`` returns a response
    exposing ``json()`` + ``raise_for_status()`` (or raises). Same pattern as
    test_geocode.py / test_geo_intel.py."""
    resp = MagicMock()
    resp.json = MagicMock(return_value=json_body if json_body is not None else {})
    resp.raise_for_status = MagicMock()
    get = AsyncMock(return_value=resp)
    if raises is not None:
        get.side_effect = raises

    client = MagicMock()
    client.get = get
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=cm))
    return get


# ── schema fixture: 3 fake states (the real file lands separately) ────

_RULES = {
    "version": "test-2026-08",
    "federal": {
        "cares_act_nonpay_notice_days": {
            "value": 30,
            "citation": "CARES Act §4024",
            "note": "covered properties only",
        }
    },
    "states": {
        "XA": {
            "name": "Alphaland",
            "deposit": {
                "cap_months": 2,
                "return_deadline_days": 30,
                "deadline_trigger": "tenancy_end",
                "penalty": "3x bad faith",
                "interest_owed": False,
                "citation": "§11-22-33",
                "verify_flag": False,
            },
            "late_fee": {"rule": "none set", "grace_days": None, "citation": "§11-22-34"},
            "notice": {
                "nonpay_days": "demand",
                "nonpay_unit": "calendar",
                "mtm_termination_days": 60,
                "entry_hours": None,
                "citation": "§11-22-35",
            },
            "screening": {"soi_protection": "N", "soi_note": "no state SOI statute"},
            "licensing": {"pm_license": "B", "note": "broker license required"},
            "eviction_speed_tier": 1,
            "quirks": ["quirk one", "quirk two", "quirk three", "quirk four"],
            "overlay_market": False,
            "overlay_note": None,
        },
        "XB": {
            "name": "Betaland",
            "deposit": {
                "cap_months": 1,
                "return_deadline_days": 21,
                "deadline_trigger": "tenancy_end",
                "citation": "§9.1",
                "verify_flag": True,
            },
            "notice": {"nonpay_days": 14, "nonpay_unit": "judicial", "citation": "§9.2"},
            "screening": {"soi_protection": "Y", "soi_note": "statewide SOI protection"},
            "eviction_speed_tier": 3,
            "overlay_market": True,
            "overlay_note": "cities layer their own ordinances on top of state law",
        },
        # Deliberately sparse — every group missing.
        "XC": {"name": "Gammaland"},
        # Module 2 fixture: rates table + a dated pending_change.
        "XR": {
            "name": "Ratesland",
            "deposit": {
                "cap_months": 2,
                "return_deadline_days": 30,
                "return_deadline_unit": "business",
                "deadline_trigger": "tenancy_end",
                "citation": "§77-1",
                "pending_change": {
                    "field": "cap_months",
                    "new_value": 1,
                    "effective_from": "2026-01-01",
                    "precision": "year",
                    "citation": "HB 99",
                },
            },
            "notice": {"nonpay_days": 5, "nonpay_unit": "business", "citation": "§77-2"},
            "screening": {"soi_protection": "N"},
            "eviction_speed_tier": 2,
            "rates": {
                "max_increase": {
                    "formula": "5% + CPI capped at 10%",
                    "publisher": "State DOJ, annually",
                    "citation": "§1947.12",
                    "value": None,
                    "unit": "percent",
                },
                "deposit_interest_rate": {
                    "formula": "rate set annually",
                    "publisher": "Banking Dept",
                    "citation": "§47a-21",
                    "value": 5,
                    "unit": "percent",
                },
                "screening_fee_cap": {
                    "formula": "statutory base + CPI",
                    "publisher": "annual CPI adjustment",
                    "citation": "§1950.6",
                    "value": None,
                    "unit": "dollars",
                },
                "fourth_rate": {"formula": "f4", "publisher": "p4", "value": None},
                "fifth_rate": {"formula": "f5", "publisher": "p5", "value": None},
            },
        },
    },
    "overlay_jurisdictions": {
        "XB": {
            "places": [
                {
                    "name": "Metro City",
                    "place_fips": "9904000",
                    "warning": "Metro City has just-cause eviction and its own deposit rules",
                    "license_to_operate": True,
                }
            ]
        }
    },
}


@pytest.fixture
def rules(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(jurisdiction, "load_rules", lambda: _RULES)
    return _RULES


# ── resolve_stack ─────────────────────────────────────────────────────

_CENSUS_FULL = {
    "result": {
        "geographies": {
            "States": [{"GEOID": "13", "STUSAB": "GA", "NAME": "Georgia"}],
            "Counties": [{"GEOID": "13121", "NAME": "Fulton County"}],
            "Incorporated Places": [{"GEOID": "1304000", "NAME": "Atlanta"}],
        }
    }
}


@pytest.mark.asyncio
async def test_resolve_stack_parses_full_stack(monkeypatch):
    get = _fake_httpx(monkeypatch, json_body=_CENSUS_FULL)
    stack = await jurisdiction.resolve_stack(33.749, -84.388, settings=_settings())
    assert stack is not None
    assert stack.state_fips == "13"
    assert stack.state_abbr == "GA"
    assert stack.county_fips == "13121"
    assert stack.county_name == "Fulton County"
    assert stack.place_fips == "1304000"
    assert stack.place_name == "Atlanta"
    # Census speaks x/y: x is LONGITUDE, y is LATITUDE — the transposition trap.
    params = get.await_args.kwargs["params"]
    assert params["x"] == -84.388
    assert params["y"] == 33.749
    assert params["benchmark"] == "Public_AR_Current"


@pytest.mark.asyncio
async def test_resolve_stack_no_incorporated_place_is_an_answer(monkeypatch):
    """Absent/empty "Incorporated Places" = unincorporated territory —
    place None, but the stack still resolves (and gets persisted)."""
    body = {
        "result": {
            "geographies": {
                "States": [{"GEOID": "13", "STUSAB": "GA", "NAME": "Georgia"}],
                "Counties": [{"GEOID": "13121", "NAME": "Fulton County"}],
            }
        }
    }
    _fake_httpx(monkeypatch, json_body=body)
    stack = await jurisdiction.resolve_stack(33.5, -84.5, settings=_settings())
    assert stack is not None
    assert stack.place_fips is None
    assert stack.place_name is None
    assert stack.state_abbr == "GA"


@pytest.mark.asyncio
async def test_resolve_stack_abbr_falls_back_to_fips_table(monkeypatch):
    body = {"result": {"geographies": {"States": [{"GEOID": "48", "NAME": "Texas"}]}}}
    _fake_httpx(monkeypatch, json_body=body)
    stack = await jurisdiction.resolve_stack(30.0, -97.0, settings=_settings())
    assert stack is not None
    assert stack.state_abbr == "TX"
    assert stack.county_fips is None


@pytest.mark.asyncio
async def test_resolve_stack_no_states_and_failures_return_none(monkeypatch):
    _fake_httpx(monkeypatch, json_body={"result": {"geographies": {}}})
    assert await jurisdiction.resolve_stack(0.0, 0.0, settings=_settings()) is None
    _fake_httpx(monkeypatch, raises=RuntimeError("boom"))
    assert await jurisdiction.resolve_stack(33.0, -84.0, settings=_settings()) is None


# ── load_rules ────────────────────────────────────────────────────────


def test_load_rules_missing_file_degrades_to_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(jurisdiction, "_RULES_PATH", tmp_path / "nope.json")
    jurisdiction.load_rules.cache_clear()
    try:
        assert jurisdiction.load_rules() == {}
    finally:
        jurisdiction.load_rules.cache_clear()


def test_load_rules_corrupt_file_degrades_to_empty(monkeypatch, tmp_path):
    bad = tmp_path / "rules.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(jurisdiction, "_RULES_PATH", bad)
    jurisdiction.load_rules.cache_clear()
    try:
        assert jurisdiction.load_rules() == {}
    finally:
        jurisdiction.load_rules.cache_clear()


# ── lookup: merge precedence ──────────────────────────────────────────


def test_lookup_reads_json_layer(rules):
    answer = jurisdiction.lookup("XA")
    assert answer.state_name == "Alphaland"
    assert answer.version == "test-2026-08"
    assert answer.state["deposit"]["cap_months"] == 2
    assert answer.federal["cares_act_nonpay_notice_days"]["value"] == 30
    assert answer.sources == {}
    # tolerated value shapes: string ("demand") and null (grace_days)
    assert answer.state["notice"]["nonpay_days"] == "demand"
    assert answer.state["late_fee"]["grace_days"] is None


def test_lookup_config_override_wins_over_json(rules):
    answer = jurisdiction.lookup(
        "XA", overrides={"deposit.cap_months": 3, "notice.mtm_termination_days": 30}
    )
    assert answer.state["deposit"]["cap_months"] == 3
    assert answer.state["notice"]["mtm_termination_days"] == 30
    assert answer.sources["deposit.cap_months"] == "config"
    assert answer.sources["notice.mtm_termination_days"] == "config"
    # The cached JSON is never mutated by an override (deep-copy guard).
    assert _RULES["states"]["XA"]["deposit"]["cap_months"] == 2


def test_lookup_overlay_place_matches_by_fips_and_name(rules):
    by_fips = jurisdiction.lookup("XB", place_fips="9904000")
    assert by_fips.overlay_market is True
    assert len(by_fips.overlay_places) == 1
    assert by_fips.overlay_places[0]["license_to_operate"] is True

    by_name = jurisdiction.lookup("XB", place_name="metro city")
    assert len(by_name.overlay_places) == 1

    no_match = jurisdiction.lookup("XB", place_fips="0000000")
    assert no_match.overlay_market is True  # state-level overlay flag still set
    assert no_match.overlay_places == []


def test_lookup_unknown_state_degrades_to_empty_block(rules):
    answer = jurisdiction.lookup("ZZ")
    assert answer.state == {}
    assert answer.state_name is None


# ── format_compact ────────────────────────────────────────────────────


def test_format_compact_full_state(rules):
    answer = jurisdiction.lookup("XA")
    out = jurisdiction.format_compact(answer, property_label="Garden Row")
    assert out.startswith("Garden Row — Alphaland")
    assert "cap 2 months' rent" in out
    assert "return within 30 days of tenancy_end" in out
    assert "§11-22-33" in out
    assert "demand" in out
    assert "Month-to-month termination: 60 days" in out
    assert "no state protection" in out and "never refuse vouchers" in out
    assert "tier 1" in out
    # quirks capped at 3
    assert "quirk three" in out and "quirk four" not in out
    # footer is ALWAYS the last line
    assert out.splitlines()[-1] == (
        "snapshot last verified test-2026-08; verify the controlling statute "
        "before executing deposit dispositions, termination notices, rent "
        "increases, or screening denials — not legal advice."
    )
    assert len(out.splitlines()) <= 20


def test_format_compact_missing_groups_render_conservative_default(rules):
    answer = jurisdiction.lookup("XC")
    out = jurisdiction.format_compact(answer, property_label="Sparse Manor")
    assert jurisdiction.NOT_ENCODED in out
    assert "most tenant-protective assumption" in out
    # footer still present even when nothing is encoded
    assert "not legal advice." in out.splitlines()[-1]


def test_format_compact_overlay_and_verify_flag(rules):
    answer = jurisdiction.lookup("XB", place_fips="9904000")
    out = jurisdiction.format_compact(answer, property_label="Metro Flats")
    assert "⚠ Overlay market: cities layer their own ordinances" in out
    assert "⚠ Metro City: Metro City has just-cause eviction" in out
    assert "license to operate required" in out
    assert "⚠ verify against current statute" in out  # deposit verify_flag
    assert "PROTECTED — voucher refusal is illegal here" in out


def test_format_compact_marks_config_overrides(rules):
    answer = jurisdiction.lookup("XA", overrides={"deposit.cap_months": 3})
    out = jurisdiction.format_compact(answer, property_label="Garden Row")
    assert "cap 3 months' rent" in out
    assert "Config overrides applied to: deposit.cap_months" in out


# ── module 2: pending changes + rates (lookup and rendering) ──────────


def test_lookup_applies_past_pending_change_and_flags(rules):
    answer = jurisdiction.lookup("XR", today=dt.date(2026, 8, 18))
    assert answer.state["deposit"]["cap_months"] == 1
    assert len(answer.pending_flags) == 1
    assert "cap_months changed to 1 effective 2026 — verify exact date" in answer.pending_flags[0]
    assert "HB 99" in answer.pending_flags[0]
    # The cached JSON is never mutated (deep-copy guard).
    assert _RULES["states"]["XR"]["deposit"]["cap_months"] == 2


def test_lookup_future_pending_change_keeps_current_value(rules):
    answer = jurisdiction.lookup("XR", today=dt.date(2025, 6, 1))
    assert answer.state["deposit"]["cap_months"] == 2
    assert len(answer.pending_flags) == 1
    assert "upcoming change" in answer.pending_flags[0]
    assert "2026-01-01" in answer.pending_flags[0]


def test_lookup_config_override_beats_pending_change(rules):
    answer = jurisdiction.lookup(
        "XR", today=dt.date(2026, 8, 18), overrides={"deposit.cap_months": 3}
    )
    assert answer.state["deposit"]["cap_months"] == 3
    assert answer.pending_flags == []  # config wins; no bundled transition applied


def test_lookup_dotted_override_reaches_nested_rates(rules):
    # jurisdiction_notes params use dotted paths of arbitrary depth — the
    # merge must reach INTO the new rates structure.
    answer = jurisdiction.lookup("XR", overrides={"rates.max_increase.value": 8.8})
    assert answer.state["rates"]["max_increase"]["value"] == 8.8
    assert answer.sources["rates.max_increase.value"] == "config"
    assert _RULES["states"]["XR"]["rates"]["max_increase"]["value"] is None


def test_format_compact_rates_null_renders_formula_never_a_number(rules):
    answer = jurisdiction.lookup("XR", today=dt.date(2026, 8, 18))
    out = jurisdiction.format_compact(answer, property_label="Rates Row")
    # Null value → formula + publisher + honesty line, never an estimate.
    assert "Rate: max increase — 5% + CPI capped at 10% (State DOJ, annually)" in out
    assert "current-year rate not loaded" in out
    # Loaded value → the number with its citation.
    assert "Rate: deposit interest rate — 5% (§47a-21)" in out
    # Capped at 3 lines + an honest more-count.
    assert out.count("Rate: ") == 3
    assert "(+2 more rates not shown)" in out
    assert "fourth rate" not in out and "fifth rate" not in out
    # The applied pending change is surfaced as a ⚠ rule-change line.
    assert "⚠ Rule change: cap_months changed to 1 effective 2026" in out
    # Footer stays the last line; total stays compact.
    assert "not legal advice." in out.splitlines()[-1]
    assert len(out.splitlines()) <= 26


def test_format_compact_rates_lines_render_before_overlay_warnings(rules):
    # Give the overlay state a rates block via override depth (cheap way to
    # combine both features in one render).
    answer = jurisdiction.lookup(
        "XB",
        place_fips="9904000",
        overrides={"rates.board_increase.formula": "60% of CPI"},
    )
    out = jurisdiction.format_compact(answer, property_label="Metro Flats")
    lines = out.splitlines()
    rate_idx = next(i for i, ln in enumerate(lines) if "Rate: board increase" in ln)
    overlay_idx = next(i for i, ln in enumerate(lines) if "⚠ Overlay market" in ln)
    assert rate_idx < overlay_idx


# ── the REAL data file (skips cleanly until the owner lands it) ───────

_REAL_RULES = Path(jurisdiction.__file__).resolve().parent / "data" / "jurisdiction_rules.json"


@pytest.mark.skipif(
    not _REAL_RULES.exists(), reason="jurisdiction_rules.json not landed yet (written separately)"
)
def test_real_rules_file_shape():
    data = json.loads(_REAL_RULES.read_text(encoding="utf-8"))
    assert data.get("version"), "rules file must carry a version"
    states = data.get("states") or {}
    assert len(states) == 51, f"expected 50 states + DC, got {len(states)}"
    for abbr, block in states.items():
        for group in ("deposit", "notice", "screening"):
            assert group in block, f"state {abbr} missing the {group} block"


# ── backfill pass (intel_backfill._backfill_jurisdiction) ─────────────


class _AcquireCM:
    def __init__(self, conn: object) -> None:
        self._conn = conn

    async def __aenter__(self) -> object:
        return self._conn

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _pool(rows: list[dict] | None = None) -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=rows or [])
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _AcquireCM(conn))
    return pool, conn


_STACK = jurisdiction.JurisdictionStack(
    state_fips="13",
    state_abbr="GA",
    county_fips="13121",
    county_name="Fulton County",
    place_fips=None,
    place_name=None,
)


@pytest.mark.asyncio
async def test_backfill_persists_stack_with_null_place() -> None:
    pool, conn = _pool([{"buildium_id": 700, "lat": 33.7, "lng": -84.4}])
    with patch.object(intel_backfill.jurisdiction, "resolve_stack", AsyncMock(return_value=_STACK)):
        n = await intel_backfill._backfill_jurisdiction(
            pool, "co-1", settings=MagicMock(), limit=25
        )
    assert n == 1
    call = conn.execute.await_args
    assert "jurisdiction_resolved_at = now()" in call.args[0]
    # place_fips None (unincorporated) is persisted as resolved, not skipped.
    # ⚠ The NAMES ride along (migration 0099). resolve_stack has always returned
    # them off the same Census response the FIPS codes come from and this UPDATE
    # threw both away — so the snapshot could not say which county it had
    # decided on, and the overlay layer (17 of 17 entries keyed by place NAME)
    # could never match anything. A FIPS code cannot be turned back into a name
    # without a gazetteer this product does not bundle, so dropping them here
    # made the jurisdiction unnameable rather than merely unnamed.
    assert call.args[1:] == ("co-1", 700, "13", "13121", None, "Fulton County", None)


@pytest.mark.asyncio
async def test_backfill_unresolved_writes_nothing_and_failures_isolate() -> None:
    pool, conn = _pool(
        [
            {"buildium_id": 1, "lat": 1.0, "lng": 2.0},
            {"buildium_id": 2, "lat": 3.0, "lng": 4.0},
        ]
    )
    resolve = AsyncMock(side_effect=[RuntimeError("boom"), _STACK])
    with patch.object(intel_backfill.jurisdiction, "resolve_stack", resolve):
        n = await intel_backfill._backfill_jurisdiction(
            pool, "co-1", settings=MagicMock(), limit=25
        )
    # Property 1's crash is isolated; property 2 still lands its stack.
    assert n == 1
    assert conn.execute.await_count == 1
    assert conn.execute.await_args.args[2] == 2


@pytest.mark.asyncio
async def test_backfill_missing_migration_degrades_to_zero() -> None:
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=Exception("column state_fips does not exist"))
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _AcquireCM(conn))
    n = await intel_backfill._backfill_jurisdiction(pool, "co-1", settings=MagicMock(), limit=25)
    assert n == 0


# ── the agent tool (mirror_tools.jurisdiction_lookup) ─────────────────


def _ctx(company_id: str = "co-1") -> MagicMock:
    ctx = MagicMock()
    ctx.company_id = company_id
    return ctx


def _resolved(name: str = "Garden Row") -> ResolvedProperty:
    return ResolvedProperty(
        buildium_id=700, name=name, address="1 Garden Way, Boston, MA", lat=42.3, lng=-71.1
    )


#: Columns the tool's SELECT reads that a caller below may not mention. Filling
#: them with ``None`` is not a convenience — it is the fixture SAYING WHICH
#: STATE IT MEANS (gotchas 67/72/73): every case that passes only FIPS codes is
#: a property resolved BEFORE migration 0099, so its names are genuinely NULL.
#: A case that means "fully resolved" passes the names explicitly.
_UNRESOLVED_NAMES = {"county_name": None, "place_name": None}


def _tool_pool(fips_row: dict | None, notes_rows: list[dict] | None = None) -> MagicMock:
    conn = MagicMock()
    conn.fetchrow = AsyncMock(
        return_value=None if fips_row is None else {**_UNRESOLVED_NAMES, **fips_row}
    )
    conn.fetch = AsyncMock(return_value=notes_rows or [])
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=lambda: _AcquireCM(conn))
    return pool


@pytest.mark.asyncio
async def test_tool_unresolved_property_is_honest(rules) -> None:
    pool = _tool_pool({"state_fips": None, "county_fips": None, "place_fips": None})
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(
            mirror_tools.geo_repo, "resolve_property", AsyncMock(return_value=[_resolved()])
        ),
    ):
        out = await mirror_tools._jurisdiction_lookup_impl("Garden Row")
    assert "Jurisdiction not yet resolved" in out
    assert "after the next sync" in out


@pytest.mark.asyncio
async def test_tool_resolved_property_formats_snapshot(rules, monkeypatch) -> None:
    # Persisted FIPS points at fake state XA via a patched abbr table.
    monkeypatch.setitem(jurisdiction.STATE_FIPS_TO_ABBR, "99", "XA")
    pool = _tool_pool({"state_fips": "99", "county_fips": "99001", "place_fips": None})
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(
            mirror_tools.geo_repo, "resolve_property", AsyncMock(return_value=[_resolved()])
        ),
    ):
        out = await mirror_tools._jurisdiction_lookup_impl("Garden Row")
    assert out.startswith("Garden Row — Alphaland")
    assert "not legal advice." in out


@pytest.mark.asyncio
async def test_tool_config_notes_override_json(rules, monkeypatch) -> None:
    monkeypatch.setitem(jurisdiction.STATE_FIPS_TO_ABBR, "99", "XA")
    notes = [
        # jsonb comes back as a JSON-encoded string on a codec-less pool.
        {"jurisdiction_key": "state:XA", "param": "deposit.cap_months", "value": "3"},
    ]
    pool = _tool_pool({"state_fips": "99", "county_fips": None, "place_fips": None}, notes)
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(
            mirror_tools.geo_repo, "resolve_property", AsyncMock(return_value=[_resolved()])
        ),
    ):
        out = await mirror_tools._jurisdiction_lookup_impl("Garden Row")
    assert "cap 3 months' rent" in out
    assert "Config overrides applied to: deposit.cap_months" in out


@pytest.mark.asyncio
async def test_tool_deadline_params_compute_dates_and_keep_footer_last(rules, monkeypatch) -> None:
    monkeypatch.setitem(jurisdiction.STATE_FIPS_TO_ABBR, "99", "XA")
    pool = _tool_pool({"state_fips": "99", "county_fips": None, "place_fips": None})
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(
            mirror_tools.geo_repo, "resolve_property", AsyncMock(return_value=[_resolved()])
        ),
    ):
        out = await mirror_tools._jurisdiction_lookup_impl(
            "Garden Row", "deposit_return", "2026-06-30"
        )
    # XA: 30 calendar days (return_deadline_unit absent → calendar default).
    assert "return due by 2026-07-30" in out
    # Compliance completion: last business day STRICTLY before the due date.
    assert "schedule completion by 2026-07-29" in out
    assert "never rely on weekend roll-forward" in out
    # The mandatory footer stays the last line even with computed lines.
    assert "not legal advice." in out.splitlines()[-1]


@pytest.mark.asyncio
async def test_tool_invalid_trigger_date_is_honest_no_crash(rules, monkeypatch) -> None:
    monkeypatch.setitem(jurisdiction.STATE_FIPS_TO_ABBR, "99", "XA")
    pool = _tool_pool({"state_fips": "99", "county_fips": None, "place_fips": None})
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(
            mirror_tools.geo_repo, "resolve_property", AsyncMock(return_value=[_resolved()])
        ),
    ):
        out = await mirror_tools._jurisdiction_lookup_impl(
            "Garden Row", "deposit_return", "06/30/2026"
        )
    assert "not a valid YYYY-MM-DD date" in out
    assert "not legal advice." in out.splitlines()[-1]


@pytest.mark.asyncio
async def test_tool_demand_regime_states_no_fixed_period(rules, monkeypatch) -> None:
    # XA's nonpay regime is the string "demand" — no date math, plain words.
    monkeypatch.setitem(jurisdiction.STATE_FIPS_TO_ABBR, "99", "XA")
    pool = _tool_pool({"state_fips": "99", "county_fips": None, "place_fips": None})
    with (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(
            mirror_tools.geo_repo, "resolve_property", AsyncMock(return_value=[_resolved()])
        ),
    ):
        out = await mirror_tools._jurisdiction_lookup_impl(
            "Garden Row", "nonpay_notice", "2026-06-30"
        )
    assert "no fixed statutory period applies" in out
    assert "'demand'" in out
    assert "not legal advice." in out.splitlines()[-1]


@pytest.mark.asyncio
async def test_tool_unknown_kind_and_missing_pair_are_honest(rules, monkeypatch) -> None:
    monkeypatch.setitem(jurisdiction.STATE_FIPS_TO_ABBR, "99", "XA")
    pool = _tool_pool({"state_fips": "99", "county_fips": None, "place_fips": None})
    ctx_patches = (
        patch.object(mirror_tools, "current_memory_context", return_value=_ctx()),
        patch.object(mirror_tools, "get_pool", return_value=pool),
        patch.object(
            mirror_tools.geo_repo, "resolve_property", AsyncMock(return_value=[_resolved()])
        ),
    )
    with ctx_patches[0], ctx_patches[1], ctx_patches[2]:
        out = await mirror_tools._jurisdiction_lookup_impl("Garden Row", "eviction", "2026-06-30")
    assert "unknown deadline_kind 'eviction'" in out
    with ctx_patches[0], ctx_patches[1], ctx_patches[2]:
        out = await mirror_tools._jurisdiction_lookup_impl("Garden Row", "deposit_return", "")
    assert "both deadline_kind and trigger_date" in out


def test_tool_description_pins_safety_lines() -> None:
    # The description the LLM sees must survive the SDK docstring parser in
    # full (short lines ending in ':' truncate — the test_mirror_tools trap)
    # and carry the safety posture verbatim.
    desc = mirror_tools.jurisdiction_lookup.description
    assert "citations" in desc
    assert "NOT legal advice" in desc
    assert "human" in desc and "confirmation" in desc
    assert "never assists refusing a housing voucher" in desc
    assert "screening denials" in desc.lower() or "screening" in desc


def test_jurisdiction_lookup_registered_like_property_hazards() -> None:
    names = [getattr(t, "name", "") for t in mirror_tools.mirror_tools]
    assert "jurisdiction_lookup" in names
    assert "property_hazards" in names  # same registration list


# ── doctrine block in the built Buildium agent ────────────────────────


def test_buildium_agent_instructions_carry_jurisdiction_doctrine() -> None:
    from helixis.agents.buildium_agent import JURISDICTION_DOCTRINE, create_buildium_agent

    settings = Settings(
        env="test",
        llm={"default_model": "openai/gpt-4o", "openai_api_key": "test-key"},
        memory={"enabled": False},
    )
    agent = create_buildium_agent(settings)
    assert JURISDICTION_DOCTRINE in agent.instructions
    # Whitespace-normalized so hard-wrapped prose still matches the pins.
    flat = " ".join(agent.instructions.split())
    for phrase in (
        "Resolve jurisdiction BEFORE substance",
        "Strictest layer wins",
        "never answer from state law alone",
        "Never execute from the snapshot alone",
        "never assist refusing, screening out, or discouraging a housing-voucher holder",
        "most tenant-protective assumption",
        "not legal advice",
        "consulting a local attorney",
    ):
        assert phrase in flat, f"doctrine phrase missing: {phrase!r}"
