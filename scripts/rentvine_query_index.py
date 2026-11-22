"""Generate ``helixis.rentvine._query_index`` from the captured Rentvine spec.

    python scripts/rentvine_query_index.py --write
    python scripts/rentvine_query_index.py --check    # CI / guard use

WHY THIS IS GENERATED AND NOT TYPED
-----------------------------------
Rentvine's query-parameter names cannot be derived from a rule. Measured
against the T1 spec: **five filters are declared BOTH with and without a
``[]`` suffix, on different operations** -- ``propertyIDs`` on
``GET /accounting/payables/search`` and ``propertyIDs[]`` on
``GET /accounting/transactions/search``, and likewise for ``portfolioIDs``,
``bankAccountIDs``, ``tagIDs`` and ``assignedToUserIDs``. So "append ``[]`` to
list filters" is wrong on some endpoints and "never append it" is wrong on
others -- and note both examples above are MONEY searches, so getting it
backwards answers a financial question about the whole account.

That matters more than tidiness. A Rentvine filter the server does not
recognise is **ignored**, and the request returns the UNFILTERED collection
with a 200 -- CLAUDE.md gotcha 24's silent whole-account read, which this
product has already been bitten by on Buildium's ``?propertyids``. A wrong
parameter name is therefore not a 400 we would notice; it is a plausible
answer about the wrong scope.

The index is committed so the runtime does not depend on ``docs/`` being
present in the deployed image, and
``tests/unit/test_rentvine_query_contract.py`` regenerates it and fails if it
has drifted from the spec.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SPEC = Path("docs/rentvine/vendor/docs.rentvine.com/openapi.json")
TARGET = Path("src/helixis/rentvine/_query_index.py")

# Recorded in vendor/docs.rentvine.com/MANIFEST.json. An index generated from a
# different document than the notes cite is a silent lie about sourcing.
EXPECTED_SHA256 = "9c3049fea3d3dc2d0b347034c959088b9ff2f486014a451f87974096285e3802"

# The spec declares only these three. It has no PUT and no PATCH -- updates are
# POST. Listing them explicitly means a future verb shows up as a KeyError in
# the generator rather than being silently dropped from the index.
VERBS = ("get", "post", "delete")


def load_spec(path: Path = SPEC) -> tuple[dict, str]:
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def _resolve(param: dict, components: dict) -> dict:
    """Follow a ``$ref`` into ``components.parameters``.

    Load-bearing: 44 operations reference ``#/components/parameters/page`` by
    ref and declare nothing inline. A scan that reads only inline ``name``
    fields sees none of them -- which is exactly how an earlier pass concluded
    Rentvine had no pagination at all (gotcha 63's shape).
    """
    ref = param.get("$ref")
    if not ref:
        return param
    return components[ref.rsplit("/", 1)[-1]]


def build_index(spec: dict) -> dict[str, tuple[str, ...]]:
    """Map ``"<VERB> <path>"`` to the query parameter names it accepts."""
    components = (spec.get("components") or {}).get("parameters") or {}
    index: dict[str, tuple[str, ...]] = {}
    for path, item in (spec.get("paths") or {}).items():
        for verb, op in (item or {}).items():
            if verb not in VERBS:
                continue
            names = {
                resolved["name"]
                for resolved in (_resolve(p, components) for p in (op.get("parameters") or []))
                if resolved.get("in") == "query"
            }
            index[f"{verb.upper()} {path}"] = tuple(sorted(names))
    return index


def _deref(node: dict | None, spec: dict, depth: int = 0) -> dict:
    """Resolve ``$ref`` and flatten ``allOf``.

    ⚠ **BOTH, and this is the trap this repo has now fallen into five times.**
    A scan that reads ``properties`` without following ``$ref`` and merging
    ``allOf`` reports ZERO fields for a composed schema — and zero reads as
    "this endpoint takes no body", which is exactly the wrong conclusion for a
    write. The count that matters is always the resolved one.
    """
    if depth > 8 or not isinstance(node, dict):
        return {}
    if "$ref" in node:
        cur: object = spec
        for part in str(node["$ref"]).lstrip("#/").split("/"):
            cur = cur.get(part, {}) if isinstance(cur, dict) else {}
        return _deref(cur if isinstance(cur, dict) else {}, spec, depth + 1)
    if "allOf" in node:
        merged: dict = {"properties": {}, "required": []}
        for sub in node["allOf"]:
            r = _deref(sub, spec, depth + 1)
            merged["properties"].update(r.get("properties") or {})
            merged["required"] += list(r.get("required") or [])
        return merged
    return node


def build_body_index(spec: dict) -> dict[str, tuple[str, ...]]:
    """Map ``"<VERB> <path>"`` to the JSON body fields it declares.

    Only operations that declare a ``requestBody`` appear. An operation with a
    body but no resolvable properties is emitted as an EMPTY tuple rather than
    omitted, because "declares a body we could not read" and "declares no body"
    are different facts and a write path must be able to tell them apart.
    """
    index: dict[str, tuple[str, ...]] = {}
    for path, item in (spec.get("paths") or {}).items():
        for verb, op in (item or {}).items():
            if verb not in VERBS or not isinstance(op, dict):
                continue
            body = op.get("requestBody")
            if not body:
                continue
            content = (_deref(body, spec).get("content") or {}).get("application/json") or {}
            schema = _deref(content.get("schema"), spec)
            index[f"{verb.upper()} {path}"] = tuple(sorted(schema.get("properties") or {}))
    return index


def render(
    index: dict[str, tuple[str, ...]], digest: str, bodies: dict[str, tuple[str, ...]] | None = None
) -> str:
    lines = [
        '"""Query parameters each Rentvine operation accepts. GENERATED -- do not edit.',
        "",
        "Regenerate with ``python scripts/rentvine_query_index.py --write``.",
        "``tests/unit/test_rentvine_query_contract.py`` fails if this drifts from",
        "the captured spec.",
        "",
        "Source: docs/rentvine/vendor/docs.rentvine.com/openapi.json",
        f"sha256 {digest}",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f'SPEC_SHA256 = "{digest}"',
        "",
        "QUERY_PARAMS: dict[str, frozenset[str]] = {",
    ]
    for op in sorted(index):
        names = index[op]
        if not names:
            lines.append(f'    "{op}": frozenset(),')
            continue
        # Wrap rather than emit one long line: the output is committed source
        # and has to pass the repo's own ruff config (line-length 100) like
        # anything else. A generated file that needs a lint exclude is one more
        # exception for somebody to remember.
        # Built in two steps: a nested same-quote f-string is 3.12+ syntax and
        # this repo targets py311.
        body = ", ".join(f'"{n}"' for n in sorted(names))
        one_line = f'    "{op}": frozenset({{{body}}}),'
        if len(one_line) <= 100:
            lines.append(one_line)
            continue
        lines.append(f'    "{op}": frozenset(')
        lines.append("        {")
        lines.extend(f'            "{n}",' for n in names)
        lines.append("        }")
        lines.append("    ),")
    lines.append("}")

    if bodies is not None:
        lines.append("")
        lines.append("#: JSON body fields each write operation DECLARES.")
        lines.append("#:")
        lines.append("#: ⚠ Declaring a field is not the same as REQUIRING one, and for")
        lines.append("#: ``POST /maintenance/work-orders/{workOrderID}`` the spec declares 38")
        lines.append("#: fields with ``required: []`` while CREATE requires 8. Two readings")
        lines.append("#: the spec cannot separate: a partial update, or a full replace with a")
        lines.append("#: permissive schema. A caller must be correct under BOTH — see")
        lines.append("#: ``rentvine/work_orders.py``.")
        lines.append("BODY_FIELDS: dict[str, frozenset[str]] = {")
        for op in sorted(bodies):
            names = bodies[op]
            if not names:
                lines.append(f'    "{op}": frozenset(),')
                continue
            body = ", ".join(f'"{n}"' for n in sorted(names))
            one_line = f'    "{op}": frozenset({{{body}}}),'
            if len(one_line) <= 100:
                lines.append(one_line)
                continue
            lines.append(f'    "{op}": frozenset(')
            lines.append("        {")
            lines.extend(f'            "{n}",' for n in names)
            lines.append("        }")
            lines.append("    ),")
        lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    spec, digest = load_spec()
    if digest != EXPECTED_SHA256:
        print(f"SPEC HASH MISMATCH\n  expected {EXPECTED_SHA256}\n  got      {digest}")
        return 1

    text = render(build_index(spec), digest, build_body_index(spec))
    if args.write:
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_text(text, encoding="utf-8")
        print(f"wrote {TARGET} ({len(text)} bytes)")
        return 0
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print("DRIFT: _query_index.py does not match the spec. Re-run --write.")
            return 1
        print("index matches the spec")
        return 0
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
