"""``helixis.doorloop._paths`` is DoorLoop's reference, regenerated, never typed.

The index is built from ``docs/doorloop/vendor/api.doorloop.com/openapi.json``,
which is the union of the OpenAPI block each reference page embeds. These
tests rebuild the index from the spec, re-check the union against the pages
and both against the vendor folder's MANIFEST (rule 15), and pin what the
client relies on: which operations are lists, the paging and sorting
parameters the guide pages document, and names copied exactly.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

from helixis.doorloop import _paths

REPO = Path(__file__).resolve().parents[2]
VENDOR = REPO / "docs" / "doorloop" / "vendor" / "api.doorloop.com"
_BLOCK = re.compile(r"# OpenAPI definition\s*```json\s*(\{.*?\})\s*```", re.S)
_VERBS = ("get", "post", "put", "patch", "delete")


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    path = REPO / "scripts" / "doorloop_paths.py"
    spec = importlib.util.spec_from_file_location("_dl_paths_gen", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_dl_paths_gen"] = mod
    spec.loader.exec_module(mod)
    return mod


def _manifest() -> dict:
    return json.loads((VENDOR / "MANIFEST.json").read_text(encoding="utf-8"))


def _spec() -> dict:
    return json.loads((VENDOR / "openapi.json").read_text(encoding="utf-8"))


# ── the index is the spec, regenerated ────────────────────────────────────


def test_the_committed_index_is_what_the_spec_generates(
    gen: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO)
    committed = (REPO / gen.TARGET).read_text(encoding="utf-8")
    assert gen.generate() == committed, "run: python scripts/doorloop_paths.py --write"


def test_a_changed_spec_stops_the_generator(
    gen: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO)
    changed = tmp_path / "openapi.json"
    shutil.copy(VENDOR / "openapi.json", changed)
    changed.write_bytes(changed.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="HASH MISMATCH"):
        gen.generate(changed)


def test_a_guide_page_that_drops_a_list_parameter_stops_the_generator(
    gen: ModuleType, tmp_path: Path
) -> None:
    pages = tmp_path / "pages"
    shutil.copytree(VENDOR / "pages", pages)
    text = (pages / "sorting.md").read_text(encoding="utf-8")
    (pages / "sorting.md").write_text(
        text.replace("sort\\_descending", "x").replace("sort_descending", "x"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="sort_descending"):
        gen.check_list_params(pages)


def test_a_path_item_key_the_generator_does_not_know_stops_it(gen: ModuleType) -> None:
    with pytest.raises(ValueError, match="does not understand"):
        gen.build_index({"paths": {"/tasks": {"get": {}, "x-future": {}}}})


def test_a_header_parameter_is_never_a_query_parameter(gen: ModuleType) -> None:
    """``GET /leases`` declares the Authorization header as a parameter."""
    spec = _spec()
    declared = [p.get("$ref", "") for p in spec["paths"]["/leases"]["get"]["parameters"]]
    assert "#/components/parameters/Authorization" in declared
    assert "Authorization" not in _paths.QUERY_PARAMS["GET /leases"]


# ── both sides: the spec, the pages and the manifest ──────────────────────


def test_the_index_counts_what_the_vendor_manifest_records() -> None:
    item = next(i for i in _manifest()["items"] if i["file"] == "openapi.json")
    assert item["sha256"] == _paths.SPEC_SHA256
    assert hashlib.sha256((VENDOR / "openapi.json").read_bytes()).hexdigest() == item["sha256"]
    assert len(_paths.QUERY_PARAMS) == item["operations"]
    assert len({key.split(" ", 1)[1] for key in _paths.QUERY_PARAMS}) == item["paths"]


def test_every_page_is_the_byte_for_byte_capture_the_manifest_records() -> None:
    pages = next(i for i in _manifest()["items"] if i["file"] == "pages/*.md")
    on_disk = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in VENDOR.glob("pages/*")}
    assert on_disk == pages["sha256"]
    assert (VENDOR / ".gitattributes").read_text(encoding="utf-8").splitlines()[-1] == "* -text"


def test_the_spec_is_the_union_of_the_pages_definitions() -> None:
    """The manifest's provenance check, re-run: every operation in the spec is
    the one its own page defines, and every page's operation is in the spec."""
    spec = _spec()
    from_pages: dict[str, dict] = {}
    for page in sorted(VENDOR.glob("pages/*.md")):
        found = _BLOCK.search(page.read_text(encoding="utf-8"))
        if not found:
            continue
        block = json.loads(found.group(1))
        for path, item in block.get("paths", {}).items():
            for verb in _VERBS:
                if verb in item:
                    from_pages[f"{verb.upper()} {path}"] = item[verb]
        for name, schema in block.get("components", {}).get("schemas", {}).items():
            assert spec["components"]["schemas"][name] == schema, f"{page.name}: {name}"
    in_spec = {
        f"{verb.upper()} {path}": item[verb]
        for path, item in spec["paths"].items()
        for verb in _VERBS
        if verb in item
    }
    assert in_spec == from_pages


# ── what the client relies on ─────────────────────────────────────────────


def test_the_sync_reads_are_lists_and_single_records_are_not() -> None:
    for key in (
        "GET /properties",
        "GET /units",
        "GET /leases",
        "GET /leases/tenants",
        "GET /tenants",
        "GET /owners",
        "GET /vendors",
        "GET /tasks",
        "GET /lease-charges",
        "GET /lease-payments",
        "GET /lease-credits",
    ):
        assert key in _paths.LISTS, key
    for key in ("GET /units/{unitId}", "GET /properties/{propertyId}", "GET /reports/rent-roll"):
        assert key not in _paths.LISTS, key


def test_the_paging_and_sorting_parameters_are_the_documented_ones() -> None:
    assert _paths.LIST_PARAMS == {"page_number", "page_size", "sort_by", "sort_descending"}
    page_size = _spec()["components"]["parameters"]["page_size"]["schema"]
    assert _paths.MAX_PAGE_SIZE == page_size["maximum"] == 1000
    assert _paths.BASE_URL == "https://app.doorloop.com/api"


def test_names_are_copied_exactly_never_tidied() -> None:
    """DoorLoop writes ``filter_movedOutAt_To`` beside ``filter_movedInAt_to``.
    A tidied name would be a filter DoorLoop ignores (gotcha 24)."""
    tenants = _paths.QUERY_PARAMS["GET /leases/tenants"]
    assert {"filter_movedOutAt_To", "filter_movedInAt_to"} <= tenants
    assert "filter_movedOutAt_to" not in tenants
