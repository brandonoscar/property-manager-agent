"""Generate ``helixis.propertyware._paths`` from Propertyware's official spec.

    python scripts/propertyware_paths.py            # print what would be written
    python scripts/propertyware_paths.py --write    # write the index
    python scripts/propertyware_paths.py --check    # fail when the index and the spec differ

WHY THE PATHS ARE GENERATED AND NOT TYPED
-----------------------------------------
Propertyware's paths are case-sensitive, and a wrong case answers **404**
(api-notes §4). So does a wrong-case ``limit`` or ``offset``. The spec itself
mixes cases on one resource: ``GET /leases/{leaseId}`` beside
``DELETE /leases/{leaseID}``, and ``GET /units/{unitID}/customFields`` beside
``PUT /units/customfields``. A hand-typed path is one keystroke from a 404, and
a 404 on a list call would read as "no rows" if anything ever treated it as
empty. Here it is an error, and the client calls only what this index lists.

WHAT IS RECORDED, PER OPERATION
-------------------------------
* the method and the path, case exact, as the key ``"<METHOD> <path>"``
  (the same key ``helixis.pms.http.operation_key`` builds);
* the query parameters it declares (a ``$ref`` is followed);
* whether its summary or description carries the beta marker. Terms rule 2:
  a beta read is never called by sync or chat, and the client refuses one;
* whether its 2xx response declares ``X-Total-Count``. A list without it can
  never be read as complete (design, "What built correctly covers here");
* the API-key permission its description names ("BUILDINGS - Read"). A 403
  tells the person which permission the key lacks in Propertyware's own words;
* the fields its description says it can be sorted by ("Sortable by: ..."),
  as the spec writes them. The incremental reads sort a window by
  ``lastmodifieddatetime`` only where the list declares it (PW-13).

Plus the three credential headers, named as the spec's Authentication section
names them. The spec declares no ``securitySchemes`` (api-notes §11.2), so its
prose is the only place the names are written.

The index is committed so the runtime never depends on ``docs/`` being in the
deployed image. ``tests/unit/test_propertyware_paths.py`` rebuilds it from the
spec and fails on any difference.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SPEC = Path("docs/propertyware/vendor/app.propertyware.com/openapi.json")
TARGET = Path("src/helixis/propertyware/_paths.py")

#: Recorded in the vendor folder's MANIFEST.json. An index generated from a
#: different document than the notes cite is a silent lie about its source.
EXPECTED_SHA256 = "4ab5e2c85731fb56a6305be387f3669a8c8e029f8023d5015920ad77c0577936"

#: The verbs an OpenAPI path item may hold. Anything else under a path that
#: is not one of :data:`PATH_ITEM_FIELDS` stops the generator: a key it does
#: not understand must never be dropped in silence.
VERBS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
PATH_ITEM_FIELDS = ("parameters", "summary", "description", "servers", "$ref")

#: The marker. A word match, case-insensitive, in the summary or the
#: description: today's spec writes "(BETA)" in summaries and "opted in to
#: our beta program" in descriptions; the older copies wrote "still in beta".
#: It over-matches on purpose: a false "beta" only keeps an operation out of
#: production, a missed one would call it.
BETA_RE = re.compile(r"\bbeta\b", re.IGNORECASE)

TOTAL_HEADER = "x-total-count"

#: The permission an operation's description names:
#: ``<span class="permissionBlock">BUILDINGS</span> - <code>Read</code>``.
PERMISSION_RE = re.compile(
    r'permissionBlock">\s*([^<]+?)\s*</span>\s*-\s*<code>\s*([^<]+?)\s*</code>'
)

#: The sort keys a list's description declares:
#: ``<b>Sortable by:</b> <code>createddate</code>, <code>id</code>``.
SORTABLE_RE = re.compile(r"<b>\s*Sortable by:\s*</b>\s*((?:<code>[^<]*</code>\s*,?\s*)+)")
SORTABLE_WORDS_RE = re.compile(r"Sortable by", re.IGNORECASE)
CODE_RE = re.compile(r"<code>\s*([^<]*?)\s*</code>")

#: A credential header as the Authentication section writes it.
AUTH_HEADER_RE = re.compile(r"`(x-propertyware-[a-z-]+)`")
AUTH_SECTION_RE = re.compile(r"^## Authentication\s*$(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)


def load_spec(path: Path = SPEC) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def _resolve(node: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Follow one ``$ref`` inside the document (parameters, responses)."""
    ref = node.get("$ref")
    if not ref:
        return node
    target: Any = spec
    for part in str(ref).lstrip("#/").split("/"):
        target = target.get(part, {}) if isinstance(target, dict) else {}
    return target if isinstance(target, dict) else {}


def _query_names(params: list[Any], spec: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for param in params:
        resolved = _resolve(param, spec) if isinstance(param, dict) else {}
        if resolved.get("in") == "query" and resolved.get("name"):
            names.add(str(resolved["name"]))
    return names


def _declares_total(op: dict[str, Any], spec: dict[str, Any]) -> bool:
    for code, response in (op.get("responses") or {}).items():
        if not str(code).startswith("2") or not isinstance(response, dict):
            continue
        headers = _resolve(response, spec).get("headers") or {}
        if any(str(name).lower() == TOTAL_HEADER for name in headers):
            return True
    return False


def _is_beta(op: dict[str, Any]) -> bool:
    text = f"{op.get('summary') or ''}\n{op.get('description') or ''}"
    return bool(BETA_RE.search(text))


def _permission(key: str, op: dict[str, Any]) -> str | None:
    """The one permission the description names, as "AREA - Level", or
    ``None`` when it names none. Two would be a shape this index does not
    know, so the generator stops rather than keep one of them."""
    found = PERMISSION_RE.findall(str(op.get("description") or ""))
    if len(found) > 1:
        raise ValueError(f"{key} names {len(found)} permissions: {found}")
    return f"{found[0][0]} - {found[0][1]}" if found else None


def _sortable(key: str, op: dict[str, Any]) -> frozenset[str]:
    """The sort keys the description declares, as written, or none. A
    "Sortable by" this cannot read, or two of them, stops the generator: a
    list of keys must never be dropped or merged in silence."""
    description = str(op.get("description") or "")
    if SORTABLE_WORDS_RE.search(str(op.get("summary") or "")):
        raise ValueError(f"{key}: a 'Sortable by' note in the summary, a shape not read here")
    found = SORTABLE_RE.findall(description)
    mentioned = len(SORTABLE_WORDS_RE.findall(description))
    if mentioned != len(found) or len(found) > 1:
        raise ValueError(f"{key}: {mentioned} 'Sortable by' note(s), {len(found)} readable")
    if not found:
        return frozenset()
    keys = [k for k in CODE_RE.findall(found[0]) if k]
    if not keys:
        raise ValueError(f"{key}: a 'Sortable by' note that names no field")
    return frozenset(keys)


def build_index(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``"<METHOD> <path>"`` -> ``{"query": set, "beta": bool, "total": bool,
    "permission": str | None, "sortable": frozenset}``."""
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
                "beta": _is_beta(op),
                "total": _declares_total(op, spec),
                "permission": _permission(key, op),
                "sortable": _sortable(key, op),
            }
    return index


def auth_headers(spec: dict[str, Any]) -> tuple[str, ...]:
    """The credential headers the Authentication section lists, in its order.

    Exactly three, or the generator stops: a fourth would be a credential the
    client does not send, and fewer would mean the section moved.
    """
    description = str((spec.get("info") or {}).get("description") or "")
    found: list[str] = []
    for section in AUTH_SECTION_RE.findall(description):
        for name in AUTH_HEADER_RE.findall(section):
            if name not in found:
                found.append(name)
    if len(found) != 3:
        raise ValueError(f"expected three credential headers in Authentication, got {found}")
    return tuple(found)


def _frozenset_lines(name: str, keys: list[str]) -> list[str]:
    lines = [f"{name}: frozenset[str] = frozenset("]
    lines.append("    {")
    lines.extend(f'        "{key}",' for key in sorted(keys))
    lines.append("    }")
    lines.append(")")
    return lines


def render(index: dict[str, dict[str, Any]], headers: tuple[str, ...], digest: str) -> str:
    lines = [
        '"""Every operation in Propertyware\'s official spec. GENERATED -- do not edit.',
        "",
        "Regenerate with ``python scripts/propertyware_paths.py --write``. Its",
        "``--check`` fails when this file and the spec differ, and",
        "``tests/unit/test_propertyware_paths.py`` rebuilds it from the spec.",
        "",
        "Source: docs/propertyware/vendor/app.propertyware.com/openapi.json",
        f"sha256 {digest}",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f'SPEC_SHA256 = "{digest}"',
        "",
        "#: The credential headers, in the order the spec's Authentication section",
        "#: lists them. The spec declares no securitySchemes; its prose names them.",
        "AUTH_HEADERS: tuple[str, ...] = (",
    ]
    lines.extend(f'    "{header}",' for header in headers)
    lines.append(")")
    lines.append("")
    lines.append('#: ``"<METHOD> <path>"``, path case exact -> the query parameters it declares.')
    lines.append("QUERY_PARAMS: dict[str, frozenset[str]] = {")
    for key in sorted(index):
        names = sorted(index[key]["query"])
        if not names:
            lines.append(f'    "{key}": frozenset(),')
            continue
        body = ", ".join(f'"{n}"' for n in names)
        one_line = f'    "{key}": frozenset({{{body}}}),'
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
    lines.append("#: Operations whose summary or description carries the beta marker.")
    lines.extend(_frozenset_lines("BETA", [k for k, v in index.items() if v["beta"]]))
    lines.append("")
    lines.append("#: Operations whose 2xx response declares the ``X-Total-Count`` header.")
    lines.extend(_frozenset_lines("TOTAL_COUNT", [k for k, v in index.items() if v["total"]]))
    lines.append("")
    lines.append("#: The API-key permission each operation's description names. An")
    lines.append("#: operation that names none is absent.")
    lines.append("PERMISSIONS: dict[str, str] = {")
    for key in sorted(k for k, v in index.items() if v["permission"]):
        lines.append(f'    "{key}": "{index[key]["permission"]}",')
    lines.append("}")
    lines.append("")
    lines.append("#: The fields each operation's description says it can be sorted by, as")
    lines.append("#: written. An operation that declares none is absent.")
    lines.append("SORTABLE: dict[str, frozenset[str]] = {")
    for key in sorted(k for k, v in index.items() if v["sortable"]):
        body = ", ".join(f'"{n}"' for n in sorted(index[key]["sortable"]))
        one_line = f'    "{key}": frozenset({{{body}}}),'
        if len(one_line) <= 100:
            lines.append(one_line)
            continue
        lines.append(f'    "{key}": frozenset(')
        lines.append("        {")
        lines.extend(f'            "{n}",' for n in sorted(index[key]["sortable"]))
        lines.append("        }")
        lines.append("    ),")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def generate(spec_path: Path = SPEC) -> str:
    """The index text for the spec at ``spec_path``. Stops on a hash mismatch."""
    spec, digest = load_spec(spec_path)
    if digest != EXPECTED_SHA256:
        raise ValueError(
            f"SPEC HASH MISMATCH: expected {EXPECTED_SHA256}, got {digest}. Read the new "
            "spec and update MANIFEST.json before regenerating."
        )
    return render(build_index(spec), auth_headers(spec), digest)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the Propertyware path index.")
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
