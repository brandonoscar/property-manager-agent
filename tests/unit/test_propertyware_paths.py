"""The generated path index: Propertyware's official spec, case and all.

Propertyware's paths are case-sensitive and a wrong case answers 404
(api-notes §4), so the client calls only what ``_paths.py`` lists. These tests
rebuild the index from the spec and fail on any difference, hold it to the
vendor folder's own MANIFEST (both sides, rule 15), and prove that the beta and
total-count detection can fire, so a passing run is never a scan over nothing.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from helixis.propertyware import _paths

REPO = Path(__file__).resolve().parents[2]
VENDOR = REPO / "docs" / "propertyware" / "vendor" / "app.propertyware.com"


def _generator() -> ModuleType:
    """The generator, imported by path: ``scripts/`` is not a package."""
    path = REPO / "scripts" / "propertyware_paths.py"
    spec = importlib.util.spec_from_file_location("_pw_paths_gen", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    return _generator()


@pytest.fixture(scope="module")
def spec(gen: ModuleType) -> dict[str, Any]:
    loaded, digest = gen.load_spec(REPO / gen.SPEC)
    assert digest == gen.EXPECTED_SHA256, "the vendor spec changed: read it before regenerating"
    return loaded


# ── the index is the spec ─────────────────────────────────────────────────


def test_the_committed_index_is_rebuilt_from_the_spec(gen: ModuleType, spec: dict) -> None:
    rebuilt = gen.build_index(spec)
    assert {k: frozenset(v["query"]) for k, v in rebuilt.items()} == dict(_paths.QUERY_PARAMS)
    assert {k for k, v in rebuilt.items() if v["beta"]} == set(_paths.BETA)
    assert {k for k, v in rebuilt.items() if v["total"]} == set(_paths.TOTAL_COUNT)
    permissions = {k: v["permission"] for k, v in rebuilt.items() if v["permission"]}
    assert permissions == dict(_paths.PERMISSIONS)
    sortable = {k: v["sortable"] for k, v in rebuilt.items() if v["sortable"]}
    assert sortable == dict(_paths.SORTABLE)
    assert gen.auth_headers(spec) == _paths.AUTH_HEADERS
    assert _paths.SPEC_SHA256 == gen.EXPECTED_SHA256


def test_the_committed_file_is_what_the_generator_writes(gen: ModuleType) -> None:
    assert (REPO / gen.TARGET).read_text(encoding="utf-8") == gen.generate(REPO / gen.SPEC)


def test_check_passes_on_the_committed_file_and_fails_on_drift(
    gen: ModuleType, tmp_path: Path
) -> None:
    args = ["--check", "--spec", str(REPO / gen.SPEC)]
    assert gen.main([*args, "--target", str(REPO / gen.TARGET)]) == 0
    stale = tmp_path / "_paths.py"
    stale.write_text("# hand-edited\n", encoding="utf-8")
    assert gen.main([*args, "--target", str(stale)]) == 1


def test_a_different_spec_is_refused_not_indexed(gen: ModuleType, tmp_path: Path) -> None:
    changed = tmp_path / "openapi.json"
    shutil.copy(REPO / gen.SPEC, changed)
    changed.write_bytes(changed.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="HASH MISMATCH"):
        gen.generate(changed)


def test_the_index_counts_what_the_vendor_manifest_records() -> None:
    """Both sides: the generated index against the provenance record."""
    manifest = json.loads((VENDOR / "MANIFEST.json").read_text(encoding="utf-8"))
    item = next(i for i in manifest["items"] if i["file"] == "openapi.json")
    assert item["sha256"] == _paths.SPEC_SHA256
    assert len(_paths.QUERY_PARAMS) == item["operations"]
    assert len({key.split(" ", 1)[1] for key in _paths.QUERY_PARAMS}) == item["paths"]


def test_a_path_item_key_the_generator_does_not_know_stops_it(gen: ModuleType) -> None:
    with pytest.raises(ValueError, match="does not understand"):
        gen.build_index({"paths": {"/buildings": {"get": {}, "x-future": {}}}})


# ── what the index records ────────────────────────────────────────────────


def test_paths_keep_the_specs_own_mixed_case() -> None:
    """The spec writes one resource two ways; both are kept exactly."""
    for key in (
        "GET /leases/{leaseId}",
        "DELETE /leases/{leaseID}",
        "GET /units/{unitID}/customFields",
        "PUT /units/customfields",
    ):
        assert key in _paths.QUERY_PARAMS, key
    assert "GET /Buildings" not in _paths.QUERY_PARAMS


def test_every_paged_list_declares_limit_and_offset_in_lower_case() -> None:
    paged = [k for k, names in _paths.QUERY_PARAMS.items() if "limit" in names]
    assert paged, "no paged list found: the parameter walk is pointed at nothing"
    for key in paged:
        names = _paths.QUERY_PARAMS[key]
        assert "offset" in names, key
        assert not {n for n in names if n.lower() in {"limit", "offset"}} - {"limit", "offset"}


def test_the_beta_marker_is_found_where_the_spec_writes_it() -> None:
    """Measured on this spec: every DELETE and the close-work-order PUT carry
    the beta-write note, and no read does (the GL left beta on 2025-09-10)."""
    assert "PUT /workorders/closeworkorder/{workOrderID}" in _paths.BETA
    deletes = {k for k in _paths.QUERY_PARAMS if k.startswith("DELETE ")}
    assert deletes and deletes <= _paths.BETA
    assert not {k for k in _paths.BETA if k.startswith("GET ")}


def test_the_beta_detection_fires_on_a_beta_read(gen: ModuleType) -> None:
    """The guard against a FUTURE beta read must be able to fail: the older
    copies wrote the read note this way."""
    note = "This endpoint is still in beta. Production integrations should be avoided."
    synthetic = {
        "paths": {
            "/ledger": {"get": {"description": note}},
            "/plain": {"get": {"description": "Retrieves a list."}},
            "/marked": {"get": {"summary": "Retrieve things (BETA)"}},
        }
    }
    index = gen.build_index(synthetic)
    assert index["GET /ledger"]["beta"] is True
    assert index["GET /marked"]["beta"] is True
    assert index["GET /plain"]["beta"] is False


def test_the_two_lists_without_a_total_are_the_designs_two() -> None:
    """Design, "Completeness": these two declare no X-Total-Count and are
    not used. A list without the header could never be read as complete."""
    paged_gets = {
        k for k, names in _paths.QUERY_PARAMS.items() if k.startswith("GET ") and "limit" in names
    }
    assert paged_gets - _paths.TOTAL_COUNT == {"GET /leases/autocharges", "GET /leases/creditmemos"}


def test_the_credential_headers_are_the_authentication_sections_three(spec: dict) -> None:
    assert _paths.AUTH_HEADERS == (
        "x-propertyware-client-id",
        "x-propertyware-client-secret",
        "x-propertyware-system-id",
    )
    # Why they come from prose: the spec declares no security scheme at all.
    assert "securitySchemes" not in (spec.get("components") or {})
    assert "security" not in spec


def test_every_operation_but_the_health_check_names_its_permission() -> None:
    """Measured on this spec: one permission per operation, in Propertyware's
    own words, and none on GET /health."""
    assert set(_paths.QUERY_PARAMS) - set(_paths.PERMISSIONS) == {"GET /health"}
    assert _paths.PERMISSIONS["GET /buildings"] == "BUILDINGS - Read"
    assert _paths.PERMISSIONS["POST /workorders"] == "WORK ORDERS - Write"


def test_a_second_permission_on_one_operation_stops_the_generator(gen: ModuleType) -> None:
    block = '<span class="permissionBlock">{}</span> - <code>Read</code>'
    two = block.format("LEASES") + " and " + block.format("UNITS")
    with pytest.raises(ValueError, match="2 permissions"):
        gen.build_index({"paths": {"/leases": {"get": {"description": two}}}})


def test_the_header_extraction_refuses_anything_but_three(gen: ModuleType) -> None:
    two = {"info": {"description": "## Authentication\n- `x-propertyware-client-id`\n"}}
    with pytest.raises(ValueError, match="three"):
        gen.auth_headers(two)


# ── the sort keys each list declares (PW-13) ──────────────────────────────


def test_every_sortable_note_in_the_spec_is_read(spec: dict) -> None:
    """Measured on this spec: every "Sortable by" note is in a description,
    and each one became an entry, so a passing run is not a scan over nothing."""
    notes = sum(
        str(op.get("description") or "").count("Sortable by")
        for item in spec["paths"].values()
        for op in item.values()
        if isinstance(op, dict)
    )
    assert notes and notes == len(_paths.SORTABLE)
    assert _paths.SORTABLE["GET /leases"] >= {"id", "lastmodifieddatetime", "startdate"}


def test_the_gl_accounts_are_the_one_read_that_cannot_sort_by_change_time() -> None:
    """The incremental windows sort by ``lastmodifieddatetime`` where a list
    declares it, else by id (``sync.window_query``). Measured on this spec."""
    from helixis.propertyware import client

    unsortable = {
        path
        for path in client.READS
        if "lastmodifieddatetime" not in _paths.SORTABLE.get(f"GET {path}", frozenset())
    }
    assert unsortable == {"/accounting/glaccounts"}
    assert all("id" in _paths.SORTABLE[f"GET {path}"] for path in client.READS)


@pytest.mark.parametrize(
    "op",
    [
        pytest.param({"description": "<b>Sortable by:</b> id"}, id="no-code"),
        pytest.param(
            {"description": "<b>Sortable by:</b> <code>id</code> " * 2},
            id="two-notes",
        ),
        pytest.param({"summary": "Sortable by id"}, id="in-the-summary"),
        pytest.param({"description": "<b>Sortable by:</b> <code></code>"}, id="empty"),
    ],
)
def test_a_sortable_note_the_generator_cannot_read_stops_it(gen: ModuleType, op: dict) -> None:
    with pytest.raises(ValueError, match="Sortable by"):
        gen.build_index({"paths": {"/leases": {"get": op}}})


def test_a_readable_sortable_note_is_kept_as_written(gen: ModuleType) -> None:
    note = "Lists.<br/><b>Sortable by:</b> <code>name</code>, <code>lastmodifieddatetime</code>"
    index = gen.build_index({"paths": {"/x": {"get": {"description": note}}, "/y": {"get": {}}}})
    assert index["GET /x"]["sortable"] == frozenset({"name", "lastmodifieddatetime"})
    assert index["GET /y"]["sortable"] == frozenset()
