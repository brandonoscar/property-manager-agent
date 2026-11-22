"""Pure-core tests for scripts/purge_synthetic_rows.py (#7 F5).

The live delete path needs a DB; these cover the SQL builders + the identifier
guard so a bad target or an injection attempt fails loudly, not silently.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PATH = os.path.join(_ROOT, "scripts", "purge_synthetic_rows.py")
_spec = importlib.util.spec_from_file_location("purge_synthetic_rows", _PATH)
assert _spec and _spec.loader
purge = importlib.util.module_from_spec(_spec)
# Register before exec so the module's @dataclass can resolve its (stringized,
# via `from __future__ import annotations`) field types against the module dict.
sys.modules["purge_synthetic_rows"] = purge
_spec.loader.exec_module(purge)


def test_like_patterns_wraps_each_in_wildcards():
    assert purge.like_patterns(["Write-probe", "CRM Sync Test"]) == [
        "%Write-probe%",
        "%CRM Sync Test%",
    ]


def test_build_where_scopes_to_company_when_asked():
    without = purge.build_where("name", has_company=False)
    assert without == "name ILIKE ANY($1::text[])"
    with_co = purge.build_where("name", has_company=True)
    assert with_co == "name ILIKE ANY($1::text[]) AND company_id = $2::uuid"


def test_count_and_delete_target_the_public_schema():
    count = purge.build_count_sql("crm_leads", "name", has_company=False)
    assert count.startswith("SELECT count(*) FROM public.crm_leads WHERE ")
    delete = purge.build_delete_sql("buildium_events", "payload::text", has_company=True)
    assert delete.startswith("DELETE FROM public.buildium_events WHERE ")
    assert "company_id = $2::uuid" in delete


def test_payload_cast_expression_is_allowed():
    # buildium_events matches the marker inside the JSON payload.
    assert "payload::text ILIKE" in purge.build_where("payload::text", has_company=False)


@pytest.mark.parametrize(
    "bad",
    ["name; drop table crm_leads", "name)--", "1=1", "payload::jsonb; delete"],
)
def test_validate_rejects_injection(bad):
    with pytest.raises(ValueError):
        purge.build_where(bad, has_company=False)


def test_targets_cover_the_three_pm_surfaces():
    tables = {t.table for t in purge.TARGETS}
    assert tables == {"buildium_events", "helixis_activity_log", "crm_leads"}


def test_args_include_company_only_when_scoped():
    assert purge._args_for(["x"], None) == [["%x%"]]
    assert purge._args_for(["x"], "co-1") == [["%x%"], "co-1"]
