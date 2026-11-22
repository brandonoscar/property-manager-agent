"""A Rentvine contact's email and phone reach the mirror.

Measured 2026-10-03 on the sandbox mirror (key names and fill counts only, no
values): all 53 stored contacts (35 tenants, 5 owners, 13 vendors) carry
``email`` and ``phone``, a plain string or null, filled on 30 of each, and none
carries the spec's plural ``emails`` / ``phones``. The mappers read only the
plural, so 0 of 53 had either stored (gotcha 186): contact lookup said "no
email on file" and an inbound text could never match its tenant.

Every row here is synthetic (terms rule 18).
"""

from __future__ import annotations

from typing import Any

import pytest

from helixis.rentvine import mapper

MAPPERS = (mapper.map_tenant, mapper.map_owner, mapper.map_vendor)


def _contact(**fields: Any) -> dict[str, Any]:
    return {"contactID": 990000901, "firstName": "Avery", "lastName": "Quill", **fields}


@pytest.mark.parametrize("map_fn", MAPPERS)
def test_the_key_the_server_sends_is_stored(map_fn: Any) -> None:
    row = map_fn(_contact(email="avery@example.com", phone="+15550100101"))
    assert row["email"] == "avery@example.com"
    assert row["phone"] == "+15550100101"


@pytest.mark.parametrize("map_fn", MAPPERS)
def test_the_spec_shaped_plural_still_maps(map_fn: Any) -> None:
    """The spec declares the plural, and a route nobody has measured may send it."""
    row = map_fn(_contact(emails="a@example.com,b@example.com", phones="555-0101"))
    assert row["email"] == "a@example.com"
    assert row["phone"] == "555-0101"


@pytest.mark.parametrize("map_fn", MAPPERS)
def test_the_measured_key_wins_and_an_empty_one_falls_back(map_fn: Any) -> None:
    both = map_fn(_contact(email="new@example.com", emails="old@example.com"))
    assert both["email"] == "new@example.com"
    empty = map_fn(_contact(email=None, emails="old@example.com", phone="", phones="555-0102"))
    assert empty["email"] == "old@example.com"
    assert empty["phone"] == "555-0102"


@pytest.mark.parametrize("map_fn", MAPPERS)
def test_nothing_sent_is_none_never_invented(map_fn: Any) -> None:
    row = map_fn(_contact(email=None, phone=None))
    assert row["email"] is None and row["phone"] is None
    assert map_fn(_contact())["email"] is None


def test_the_e2e_fixture_guard_reads_both_spellings() -> None:
    """The committed AI-test fixture must hold no real address in EITHER key,
    now that the mapper stores the singular one (terms rule 18)."""
    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "rentvine_ai_e2e.py"
    spec = importlib.util.spec_from_file_location("_rv_e2e_contacts", path)
    assert spec and spec.loader
    e2e = importlib.util.module_from_spec(spec)
    sys.modules["_rv_e2e_contacts"] = e2e
    spec.loader.exec_module(e2e)

    singular = {
        "tenants": [{"contactID": 990000301, "email": "a@corp.test", "phone": "303-555-1234"}]
    }
    problems = e2e.fixture_problems(singular)
    assert any("a@corp.test" in p for p in problems), problems
    assert any("303-555-1234" in p for p in problems), problems
    clean = {"tenants": [{"contactID": 990000301, "email": "a@example.com", "phone": "555-0101"}]}
    assert e2e.fixture_problems(clean) == []
