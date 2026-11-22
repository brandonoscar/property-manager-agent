"""Generate ``helixis.doorloop._paths`` from DoorLoop's reference.

    python scripts/doorloop_paths.py            # print what would be written
    python scripts/doorloop_paths.py --write    # write the index
    python scripts/doorloop_paths.py --check    # fail when the index and the spec differ

WHY THE PATHS ARE GENERATED AND NOT TYPED
-----------------------------------------
The client calls only what this index lists, and refuses any other operation or
query parameter before sending (``pms.http``). A vendor that ignores a filter it
does not know answers the UNFILTERED list with a 200 (gotcha 24), so a typed
guess at a name is worse than no call. DoorLoop's own names are not uniform
(``filter_movedOutAt_To`` beside ``filter_movedInAt_to``), which is the case
for copying them, never retyping them.

WHAT IS RECORDED
----------------
* every operation, ``"<METHOD> <path>"`` (the key ``pms.http.operation_key``
  builds), with the query parameters it declares (a ``$ref`` is followed; a
  header parameter is not a query parameter);
* which operations are LISTS: a 2xx JSON answer that is an object with a
  ``total`` and a ``data`` array, as the Pagination page describes;
* the paging and sorting parameters every list takes. The Pagination and
  Sorting pages document them for all lists, while most list operations do
  not declare them, so they are read from those pages and recorded once. The
  generator stops if a page no longer names one;
* the documented root (``servers``) and the largest ``page_size``.

The spec is the union of the OpenAPI block each reference page embeds
(``docs/doorloop/vendor/api.doorloop.com/MANIFEST.json``). The index is
committed so the runtime never depends on ``docs/`` being in the deployed
image. ``tests/unit/test_doorloop_paths.py`` rebuilds it and fails on any
difference, and re-checks the union against the pages.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

VENDOR = Path("docs/doorloop/vendor/api.doorloop.com")
SPEC = VENDOR / "openapi.json"
PAGES = VENDOR / "pages"
TARGET = Path("src/helixis/doorloop/_paths.py")

#: Recorded in the vendor folder's MANIFEST.json. An index generated from a
#: different document than the manifest describes is a silent lie about its
#: source.
EXPECTED_SHA256 = "9f1fba0a59a35270f578c4efcc0f9434a68109b3b2c4c12159aee979b466aaa6"

#: The verbs an OpenAPI path item may hold. Anything else under a path that is
#: not one of :data:`PATH_ITEM_FIELDS` stops the generator: a key it does not
#: understand must never be dropped in silence.
VERBS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
PATH_ITEM_FIELDS = ("parameters", "summary", "description", "servers", "$ref")

#: The parameters every list takes, and the guide page that documents each.
LIST_PARAMS: dict[str, str] = {
    "page_number": "pagination.md",
    "page_size": "pagination.md",
    "sort_by": "sorting.md",
    "sort_descending": "sorting.md",
}


def load_spec(path: Path = SPEC) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def _resolve(node: Any, spec: dict[str, Any]) -> dict[str, Any]:
    """Follow ``$ref`` inside the document until a node holds no ``$ref``."""
    seen: set[str] = set()
    while isinstance(node, dict) and "$ref" in node:
        ref = str(node["$ref"])
        if ref in seen or not ref.startswith("#/"):
            raise ValueError(f"a $ref this generator cannot follow: {ref}")
        seen.add(ref)
        target: Any = spec
        for part in ref[2:].split("/"):
            target = target.get(part) if isinstance(target, dict) else None
        node = target
    if not isinstance(node, dict):
        raise ValueError("a $ref that leads to nothing")
    return node


def _query_names(params: list[Any], spec: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for param in params:
        resolved = _resolve(param, spec)
        if resolved.get("in") == "query":
            name = resolved.get("name")
            if not name:
                raise ValueError(f"a query parameter with no name: {resolved}")
            names.add(str(name))
    return names


def _is_list(op: dict[str, Any], spec: dict[str, Any]) -> bool:
    """A 2xx JSON answer that is an object with a ``total`` and a ``data`` array."""
    for code, response in (op.get("responses") or {}).items():
        if not str(code).startswith("2"):
            continue
        content = _resolve(response, spec).get("content") or {}
        schema = (content.get("application/json") or {}).get("schema")
        if not isinstance(schema, dict):
            continue
        props = _resolve(schema, spec).get("properties") or {}
        data = props.get("data")
        if (
            "total" in props
            and isinstance(data, dict)
            and _resolve(data, spec).get("type") == "array"
        ):
            return True
    return False


def build_index(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``"<METHOD> <path>"`` -> ``{"query": set, "list": bool}``."""
    index: dict[str, dict[str, Any]] = {}
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            raise ValueError(f"{path}: the path item is not an object")
        unknown = sorted(k for k in item if k not in VERBS and k not in PATH_ITEM_FIELDS)
        if unknown:
            raise ValueError(f"{path}: keys this generator does not understand: {unknown}")
        shared = list(item.get("parameters") or [])
        for verb in VERBS:
            op = item.get(verb)
            if not isinstance(op, dict):
                continue
            key = f"{verb.upper()} {path}"
            if key in index:
                raise ValueError(f"{key} is declared twice")
            index[key] = {
                "query": _query_names(shared + list(op.get("parameters") or []), spec),
                "list": verb == "get" and _is_list(op, spec),
            }
    return index


def base_url(spec: dict[str, Any]) -> str:
    """The one documented root. Two would be a choice this index cannot make."""
    servers = [s.get("url") for s in spec.get("servers") or [] if isinstance(s, dict)]
    if len(servers) != 1 or not str(servers[0]).startswith("https://"):
        raise ValueError(f"expected one https:// server, got {servers}")
    return str(servers[0])


def max_page_size(spec: dict[str, Any]) -> int:
    schema = _resolve(spec["components"]["parameters"]["page_size"], spec).get("schema") or {}
    maximum = schema.get("maximum")
    if not isinstance(maximum, int) or maximum < 1:
        raise ValueError(f"page_size declares no usable maximum: {maximum!r}")
    return maximum


def check_list_params(pages: Path = PAGES) -> None:
    """Each paging and sorting parameter is still named on its guide page."""
    for name, page in LIST_PARAMS.items():
        text = (pages / page).read_text(encoding="utf-8").replace("\\_", "_")
        if name not in text:
            raise ValueError(f"{page} no longer names {name}: read it before regenerating")


def _set_literal(names: list[str]) -> str:
    return "frozenset()" if not names else "frozenset({" + ", ".join(f'"{n}"' for n in names) + "})"


def render(index: dict[str, dict[str, Any]], root: str, page_max: int, digest: str) -> str:
    lines = [
        '"""Every operation in DoorLoop\'s reference. GENERATED -- do not edit.',
        "",
        "Regenerate with ``python scripts/doorloop_paths.py --write``. Its ``--check``",
        "fails when this file and the spec differ, and ``tests/unit/test_doorloop_paths.py``",
        "rebuilds it from the spec.",
        "",
        "Source: docs/doorloop/vendor/api.doorloop.com/openapi.json",
        f"sha256 {digest}",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f'SPEC_SHA256 = "{digest}"',
        "",
        "#: The documented root (the spec's one server).",
        f'BASE_URL = "{root}"',
        "",
        "#: The largest ``page_size`` (``components.parameters.page_size``).",
        f"MAX_PAGE_SIZE = {page_max}",
        "",
        "#: The paging and sorting parameters every list takes (the Pagination and",
        "#: Sorting pages). Most list operations do not declare them.",
        f"LIST_PARAMS: frozenset[str] = {_set_literal(sorted(LIST_PARAMS))}",
        "",
        '#: ``"<METHOD> <path>"`` -> the query parameters it declares.',
        "QUERY_PARAMS: dict[str, frozenset[str]] = {",
    ]
    for key in sorted(index):
        names = sorted(index[key]["query"])
        one_line = f'    "{key}": {_set_literal(names)},'
        if len(one_line) <= 100:
            lines.append(one_line)
            continue
        lines.append(f'    "{key}": frozenset(')
        lines.append("        {")
        lines.extend(f'            "{n}",' for n in names)
        lines.append("        }")
        lines.append("    ),")
    lines.append("}")
    lines.append("")
    lines.append('#: The lists: a 2xx answer of ``{"total": ..., "data": [...]}``.')
    lines.append("LISTS: frozenset[str] = frozenset(")
    lines.append("    {")
    lines.extend(f'        "{key}",' for key in sorted(k for k, v in index.items() if v["list"]))
    lines.append("    }")
    lines.append(")")
    lines.append("")
    return "\n".join(lines)


def generate(spec_path: Path = SPEC, pages: Path = PAGES) -> str:
    """The index text for the spec at ``spec_path``. Stops on a hash mismatch."""
    spec, digest = load_spec(spec_path)
    if digest != EXPECTED_SHA256:
        raise ValueError(
            f"SPEC HASH MISMATCH: expected {EXPECTED_SHA256}, got {digest}. Read the new "
            "spec and update MANIFEST.json before regenerating."
        )
    check_list_params(pages)
    return render(build_index(spec), base_url(spec), max_page_size(spec), digest)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the DoorLoop path index.")
    ap.add_argument("--write", action="store_true", help="write the index")
    ap.add_argument("--check", action="store_true", help="fail if the index differs")
    ap.add_argument("--spec", default=str(SPEC))
    ap.add_argument("--target", default=str(TARGET))
    args = ap.parse_args(argv)

    try:
        text = generate(Path(args.spec))
    except ValueError as exc:
        print(exc)
        return 1
    target = Path(args.target)
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"wrote {target}")
        return 0
    if args.check:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != text:
            print(f"DRIFT: {target} does not match the spec. Run --write.")
            return 1
        print("the index matches the spec")
        return 0
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
