"""Generate ``helixis.rentvine._row_envelopes`` from the captured Rentvine spec.

    python scripts/rentvine_row_envelopes.py --write
    python scripts/rentvine_row_envelopes.py --check
    python scripts/rentvine_row_envelopes.py --audit   # show the reasoning per op

WHAT THIS IS FOR
    Rentvine's list endpoints do NOT all return bare records. Measured against
    the T1 spec, 28 of 70 array-returning GETs wrap each row in an object, and
    five of the seven collections the sync reads are among them:

        GET /owners                        bare      (items is a $ref)
        GET /properties/{propertyID}/units bare      (items is a $ref)
        GET /tenants                       {contact}
        GET /vendors                       {contact}
        GET /properties                    {property, token, image?, portfolio?}
        GET /leases                        {lease, unit}
        GET /maintenance/work-orders       {workOrder, contact}

    A mapper that reads its fields off the top level of such a row finds
    nothing, every record is skipped for want of an id, and the sync reports a
    clean and complete fetch of an empty portfolio. That is a fabricated
    absence (gotchas 26/55/57) with a success verdict attached to it.

⚠ WHY A MAP AND NOT "UNWRAP THE SINGLE KEY"
    Three of the five wrappers carry MORE than one key, and it is the same
    three the sync most needs: ``/properties`` has ``token`` beside it,
    ``/leases`` has ``unit``, ``/maintenance/work-orders`` has ``contact``.
    A generic "if the row has exactly one key, unwrap it" heuristic leaves all
    three wrapped and reproduces the original bug SILENTLY, for exactly the
    endpoints that matter most. The key has to be named per operation.

⚠ AND WHY NOT "$ref MEANS RECORD" — the rule that produced a wrong map first
    Two independent counterexamples, each fatal in a different direction:

    * ``/vendors`` INLINES its contact schema with no ``$ref`` at all, so a
      ``$ref``-keyed rule drops the operation from the map entirely — and an
      absent entry reads exactly like "this one is bare", which reproduces the
      bug with no error anywhere. The silent direction.
    * The eleven ``/accounting/diagnostics/*`` rows carry a ``$ref`` to
      ``ledger_types``, which is an INTEGER ENUM. A ``$ref``-keyed rule reads
      those flat rows as wrapped and would unwrap a whole record down to one
      integer.

    So the test is whether the value RESOLVES to a record — an object with
    named properties — following ``$ref``, ``allOf``, ``oneOf`` and ``anyOf``.
    Following the composition keywords is not optional: ``property`` and
    ``work_order`` are ``allOf`` compositions and read as zero fields without
    it, which is how ``/properties`` first measured as bare and
    ``/maintenance/work-orders`` first measured as ``contact`` (gotcha 63's
    shape — the naive scan does not follow the indirection, and reports the
    wrong answer rather than no answer).

⚠ THE ONE INFERENCE, STATED RATHER THAN BURIED
    Where several properties resolve to records, this takes the FIRST DECLARED
    one. Property order is not semantically meaningful in JSON Schema, so that
    is a T4 inference from a T1 document, and the module it writes carries an
    ``@unverified`` marker saying so. What supports it: it agrees with the
    record on **27 of the 28** ambiguous operations and with the unambiguous 21
    by construction, and where the path names an entity it agrees with the path
    too (``/leases``→lease, ``/properties``→property, ``/invoices``→invoice).
    What does NOT support it: ``required`` and ``nullable`` are too sparse to
    decide — ``/leases`` marks nothing required and nothing nullable, while
    ``/maintenance/work-orders`` marks BOTH keys required.

    ⚠ The 28th is ``GET /properties/units/export``, which declares ``property``
    first and is a UNITS export — measured by probe K, corrected in
    ``SUBJECT_OF``, and the sole exception in the whole document. This
    paragraph read "all 28" until 2026-09-23; a rule that is right 27 times is
    exactly the shape that survives review and is silently wrong on the one
    route somebody is about to start using.

    Because it is an inference, the companions are recorded beside the choice
    rather than discarded. A wrong pick is then one line to correct and the
    alternatives are already written down; and the runtime RAISES on a row that
    does not carry the chosen key, so a wrong pick surfaces on the first live
    call instead of as an empty mirror.

⚠ AND WHY THERE IS AN OVERRIDE TABLE AT ALL — the spec is not the server
    Two operations declare ``items`` as a bare ``$ref`` to the record and
    RETURN a wrapper. The generator read them faithfully and was wrong, because
    the document it reads is wrong. ``LIVE_OVERRIDES`` is where a live
    measurement outranks the capture, and every entry carries the run that
    produced it — see the constant.

The map is committed so the runtime does not depend on ``docs/`` shipping in
the image, and ``tests/unit/test_rentvine_row_envelopes.py`` regenerates it and
fails on drift.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SPEC = Path("docs/rentvine/vendor/docs.rentvine.com/openapi.json")
TARGET = Path("src/helixis/rentvine/_row_envelopes.py")
EXPECTED_SHA256 = "9c3049fea3d3dc2d0b347034c959088b9ff2f486014a451f87974096285e3802"

#: ``$ref`` chains here are shallow (two hops at most), but composition can
#: nest, so the walk is bounded rather than trusting the document's shape.
_MAX_DEPTH = 8

#: Operations where a LIVE response contradicted the captured spec.
#:
#: ⚠ **THE SPEC IS NOT THE SERVER, AND THE GENERATOR CANNOT TELL.** Both
#: entries below declare ``items`` as a bare ``$ref`` to the record —
#: ``{"$ref": "#/components/schemas/contact"}`` and ``.../unit`` — so
#: :func:`build_index`, reading correctly, wrote ``None`` for each. The first
#: Phase 8 sync against a real sandbox (2026-09-23) received a WRAPPER from
#: both: ``{"contact": {...}}`` and ``{"unit": {...}, "token": ...}``.
#:
#: The tell was sitting in the map the whole time: ``GET /owners/search``
#: declares ``{"properties": {"contact": …}}`` two lines above, so the spec
#: contradicts ITSELF about what an owner row looks like — and the half that
#: happens to be wrong is the half the sync reads.
#:
#: ⚠ **WHAT IT COST, because it is the reason this table exists rather than a
#: rule change.** A wrong KEY NAME raises in ``client._rows_from`` and names
#: itself. A wrong ``None`` cannot: there is no declared key to be missing, so
#: the wrapper passes through untouched, ``mapper`` finds no id on it, and
#: ``sync._write_rows`` skips every row. Measured on that run: owners 5 → 0 and
#: units 37 → 0 in the mirror, with ``failed={}`` and the sync reporting
#: success. Five of seven families reconciled; these two read as empty tables.
#:
#: ⚠ **A STALE ENTRY IS A LICENCE NOBODY NEEDS.** If Rentvine corrects its
#: spec, the override stops differing from the walk and must be DELETED rather
#: than left to imply a disagreement that no longer exists —
#: ``test_every_live_override_still_contradicts_the_spec`` fails when that day
#: comes (gotcha 63's stale-allowlist rule).
LIVE_OVERRIDES: dict[str, str | None] = {
    "GET /owners": "contact",
    "GET /properties/{propertyID}/units": "unit",
}


def load_spec(path: Path = SPEC) -> tuple[dict, str]:
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def _resolves_to_record(node: object, schemas: dict, depth: int = 0) -> bool:
    """Does this schema describe an object with named properties?

    ⚠ Follows ``$ref`` AND ``allOf``/``oneOf``/``anyOf``. Stopping at the
    wrapper is what made ``property`` and ``work_order`` — both ``allOf``
    compositions — read as "not a record", which silently moved two of the
    seven collections into the wrong bucket.

    ⚠ An integer enum is NOT a record. ``ledger_types`` is a ``$ref`` to
    ``{"type": "integer", "enum": [...]}``, and reading a ``$ref`` as proof of
    recordhood would have unwrapped eleven flat diagnostic rows down to a
    single integer.
    """
    if depth > _MAX_DEPTH or not isinstance(node, dict):
        return False
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
        return _resolves_to_record(schemas.get(ref.rsplit("/", 1)[-1], {}), schemas, depth + 1)
    for keyword in ("allOf", "oneOf", "anyOf"):
        parts = node.get(keyword)
        if isinstance(parts, list) and any(
            _resolves_to_record(part, schemas, depth + 1) for part in parts
        ):
            return True
    return bool(node.get("properties"))


def _array_item_schema(op: dict) -> dict | None:
    """The ``items`` schema of a 200 that is an array, else ``None``."""
    content = ((op.get("responses") or {}).get("200") or {}).get("content") or {}
    schema = (next(iter(content.values()), {}) or {}).get("schema") or {}
    if schema.get("type") != "array":
        return None
    return schema.get("items") or {}


#: Operations where the row carries SEVERAL records and the first-declared one
#: is not the operation's subject.
#:
#: ⚠ **THIS IS A DIFFERENT CLAIM FROM ``LIVE_OVERRIDES`` AND MUST NOT BE MERGED
#: WITH IT.** There the captured spec is WRONG about the row's shape. Here the
#: spec is RIGHT — ``/properties/units/export`` really does return
#: ``{"property": …, "unit": …}``, exactly as declared — and it is the
#: GENERATOR's tie-break that is wrong, because a row carrying two records has
#: no single envelope and position is not an answer to which one the caller
#: wants. Keeping them separate matters to the guards: every ``LIVE_OVERRIDES``
#: entry must CONTRADICT the spec, and this one does not.
#:
#: ⚠ **THE POSITIONAL RULE IS RIGHT 27 TIMES OUT OF 28, WHICH IS WHY IT SURVIVED
#: THIS LONG.** Measured across the whole captured spec: 70 array-returning
#: GETs, 28 of them carrying more than one record, and in 27 the subject
#: happens to be declared first (``bill`` before ``contact``, ``lease`` before
#: ``unit``, ``workOrder`` before ``contact``, ``application`` before ``unit``).
#: ``/properties/units/export`` is the sole exception in the document, and it is
#: the exact route probe K tells the sync to switch to — so the guess is
#: silently wrong precisely where it was about to become load-bearing.
#:
#: What settles it: probe K, run against the ``demoaccount41`` sandbox
#: 2026-09-23, 25 rows sampled, ``row_keys: ["property", "unit"]``. The subject
#: of a UNITS export is the unit; ``property`` is the join the export carries.
#:
#: ⚠ **CORRECTING THE SUBJECT DOES NOT MAKE THIS ROUTE A DROP-IN FOR THE
#: PER-PROPERTY FETCH, and believing it does is the next silent zero.** The
#: export's unit is ``unit_export``, whose 18 properties do NOT include
#: ``propertyID`` (measured against the captured spec, not recalled) — the link
#: lives on the SIBLING ``property`` record. So an unwrapped export row is a
#: unit with nothing to attach it to, and a caller that wants both records
#: cannot use ``client._rows_from`` at all: one key is all that map can name.
#: ``sync.UNITS_PATH``'s comment says why the N+1 stands.
SUBJECT_OF: dict[str, str] = {
    "GET /properties/units/export": "unit",
}


def _apply_subject_of(
    envelope: dict[str, str | None], companions: dict[str, tuple[str, ...]]
) -> None:
    """Correct the positional tie-break where the subject has been measured.

    ⚠ Refuses THREE ways rather than one. An operation the spec does not declare
    as array-returning cannot have a subject corrected (the ``LIVE_OVERRIDES``
    refusal, for the same reason — a plain assignment would invent a
    collection). And a subject that is not one of the records ON that row is
    refused too: declaring a key the rows do not carry makes
    ``client._unwrap_rows`` raise on every row of a live call, which trades a
    silent wrong record for a loud total failure. And an operation named by
    BOTH tables is refused, because the two make incompatible claims: a live
    override says the spec is wrong about the row's SHAPE, a subject says the
    spec is right and the generator picked the wrong record off it. Resolving
    them by order would make whichever ran second win silently.
    """
    both = sorted(set(SUBJECT_OF) & set(LIVE_OVERRIDES))
    if both:
        raise KeyError(
            f"{both} appear in BOTH SUBJECT_OF and LIVE_OVERRIDES. Those are "
            f"different claims about the same row -- the spec is wrong about "
            f"its shape, or the spec is right and the tie-break is wrong. "
            f"One operation cannot be both; pick the one that is measured."
        )
    for operation, subject in SUBJECT_OF.items():
        if operation not in envelope:
            raise KeyError(
                f"SUBJECT_OF names {operation!r}, which the spec does not declare "
                f"as an array-returning GET. A subject correction picks among the "
                f"records on a row; it cannot create a collection."
            )
        available = {envelope[operation], *companions.get(operation, ())}
        if subject not in available:
            raise KeyError(
                f"SUBJECT_OF names {subject!r} for {operation!r}, but that row "
                f"carries {sorted(k for k in available if k)}. A subject must be "
                f"one of the records actually on the row, or every row raises."
            )
        on_row = (envelope[operation], *companions.get(operation, ()))
        others = tuple(k for k in on_row if k != subject)
        envelope[operation] = subject
        companions[operation] = others


def build_index(spec: dict) -> tuple[dict[str, str | None], dict[str, tuple[str, ...]]]:
    """Map ``"GET <path>"`` to the key holding the record, plus its companions.

    ``None`` means the array's items ARE the record — a caller must NOT unwrap
    those, and the distinction is why the value is three-valued rather than a
    string with an empty sentinel. An operation ABSENT from the map is a third
    state again: not a collection, so the question does not arise.
    """
    schemas = (spec.get("components") or {}).get("schemas") or {}
    envelope: dict[str, str | None] = {}
    companions: dict[str, tuple[str, ...]] = {}
    for path, item in (spec.get("paths") or {}).items():
        op = (item or {}).get("get")
        if not op:
            continue
        items = _array_item_schema(op)
        if items is None:
            continue
        key = f"GET {path}"
        props = items.get("properties") or {}
        records = [k for k, v in props.items() if _resolves_to_record(v, schemas)]
        if not records:
            # Either ``items`` is a $ref to the record itself, or it is a flat
            # object whose fields are scalars. Both are bare rows.
            envelope[key] = None
            continue
        envelope[key] = records[0]
        if len(records) > 1:
            companions[key] = tuple(records[1:])
    _apply_live_overrides(envelope)
    # ⚠ AFTER the overrides and after the companions are complete: a subject
    # correction picks among the records the row actually carries, so it needs
    # the companion list it is about to rewrite.
    _apply_subject_of(envelope, companions)
    return envelope, companions


def _apply_live_overrides(envelope: dict[str, str | None]) -> None:
    """Let a live measurement outrank the capture, for the operations where one
    exists.

    ⚠ An override for an operation the spec does not declare as
    array-returning RAISES rather than being inserted. A plain assignment would
    ADD the key, and an absent key is the map's third state ("not a
    collection") — so a typo would invent a collection out of nothing and the
    runtime would start unwrapping rows of something that is not one.
    """
    for operation, key in LIVE_OVERRIDES.items():
        if operation not in envelope:
            raise KeyError(
                f"LIVE_OVERRIDES names {operation!r}, which the spec does not "
                f"declare as an array-returning GET. An override corrects a "
                f"reading; it cannot create a collection."
            )
        envelope[operation] = key


def audit(spec: dict) -> str:
    """Show the reasoning per operation, so the inference can be checked."""
    envelope, companions = build_index(spec)
    lines = []
    bare = [k for k, v in envelope.items() if v is None]
    lines.append(f"{len(envelope)} array-returning GETs")
    lines.append(f"  {len(bare)} bare (items ARE the record)")
    lines.append(f"  {len(envelope) - len(bare) - len(companions)} wrapped, one record-shaped key")
    lines.append(
        f"  {len(companions)} wrapped, SEVERAL record-shaped keys -> first-declared wins, "
        f"except the {len(SUBJECT_OF)} in SUBJECT_OF"
    )
    lines.append("")
    for key in sorted(envelope):
        chosen = envelope[key]
        # An audit that showed an overridden value without saying so would read
        # as the spec agreeing with us, which is the one thing it does not do.
        live = "   [LIVE — the spec says bare]" if key in LIVE_OVERRIDES else ""
        if key in SUBJECT_OF:
            # Same reasoning as the LIVE marker, different claim: an audit that
            # showed a corrected tie-break silently would read as the
            # first-declared rule having got this one right.
            live += "   [MEASURED SUBJECT — the spec declares another record first]"
        if chosen is None:
            lines.append(f"  {key:<56} bare{live}")
        elif key in companions:
            lines.append(f"  {key:<56} {chosen}  (over {', '.join(companions[key])}){live}")
        else:
            lines.append(f"  {key:<56} {chosen}{live}")
    return "\n".join(lines)


_HEADER = '''"""Where each Rentvine collection puts the record. GENERATED -- do not edit.

Regenerate with ``python scripts/rentvine_row_envelopes.py --write``.
``tests/unit/test_rentvine_row_envelopes.py`` fails if this drifts from the
captured spec.

``ROW_ENVELOPE[key]`` is the property holding the record, or ``None`` when the
array's items ARE the record and must NOT be unwrapped. A key ABSENT from the
map is a third state: that operation does not return a collection.

⚠ THE VALUES IN ``LIVE_OVERRIDDEN`` DO NOT COME FROM THE SPEC. For those
operations the captured document declares a bare record and the server returns
a wrapper, so a live measurement outranks the capture. The generator's
``LIVE_OVERRIDES`` carries the run that settled each one; do not "correct" them
back by reading the spec.

⚠ @unverified — where a row carries several record-shaped properties the
FIRST-DECLARED one is taken as the record, and JSON Schema does not make
property order meaningful, so that is inferred rather than documented. It
agrees with the entity the path names wherever the path names one, and
``ROW_COMPANIONS`` records the alternatives so a wrong pick is one line to
correct. Phase 8 settles it in one call per collection: read a row and check
the chosen key carries the id the mapper reads.

⚠ THAT RULE IS RIGHT 27 TIMES OUT OF 28 AND ``MEASURED_SUBJECT`` IS THE 28th.
Being right almost always is what let it ship; the one exception is
``/properties/units/export``, a UNITS export that declares ``property`` first.
Those entries are NOT ``LIVE_OVERRIDDEN``: the spec describes that row
correctly and the tie-break picked the wrong record off it.

⚠ AND AN ENVELOPE CANNOT EXPRESS A JOIN. ``/properties/units/export`` carries
two records and this map names one, so unwrapping it yields a unit with no
``propertyID`` -- ``unit_export`` does not carry the column, the link is on the
sibling ``property``. A caller that needs both must read the row whole rather
than through ``client._rows_from``, which is why ``sync`` still fetches units
per property.

⚠ A WRONG KEY NAME IS LOUD AND A WRONG ``None`` IS SILENT — they are not the
same defect and this file used to claim they were. ``client._rows_from``
RAISES on a row missing its DECLARED key, so a mis-picked name names itself on
the first live call. A wrong ``None`` has no declared key to be missing: the
wrapper passes through untouched, ``mapper`` finds no id on it, and
``sync._write_rows`` skips every row — a complete, successful sync of an empty
table. That is what ``LIVE_OVERRIDES`` exists because of, and it is why
``sync``'s skip warning prints the row's KEYS.

Source: docs/rentvine/vendor/docs.rentvine.com/openapi.json
sha256 {digest}
"""

from __future__ import annotations

SPEC_SHA256 = "{digest}"

'''


def render(envelope: dict[str, str | None], companions: dict[str, tuple[str, ...]], digest: str):
    quote = chr(34)
    lines = [_HEADER.format(digest=digest), "ROW_ENVELOPE: dict[str, str | None] = {"]
    for op in sorted(envelope):
        key = envelope[op]
        value = "None" if key is None else f"{quote}{key}{quote}"
        # ⚠ Marked INLINE as well as collected below, because the line is where
        # somebody reading this map against the spec meets the disagreement —
        # and without the marker the obvious next move is to "fix" it back.
        mark = ""
        if op in LIVE_OVERRIDES:
            mark = "  # live-measured; the spec says bare"
        elif op in SUBJECT_OF:
            # ⚠ Short on purpose: this lands on the map's own line and the
            # generated file is lint-checked at 100 columns, so a comment that
            # reads well here can make `--check` fail permanently once an
            # operation with a longer path is measured.
            mark = "  # measured subject, not first-declared"
        lines.append(f"    {quote}{op}{quote}: {value},{mark}")
    lines.append("}")
    lines.append("")
    lines.append("#: The operations above whose value was MEASURED on a live call rather")
    lines.append("#: than read from the spec. See ``LIVE_OVERRIDES`` in the generator for")
    lines.append("#: the run that settled each one.")
    lines.append("LIVE_OVERRIDDEN: frozenset[str] = frozenset(")
    lines.append("    {")
    for op in sorted(LIVE_OVERRIDES):
        lines.append(f"        {quote}{op}{quote},")
    lines.append("    }")
    lines.append(")")
    lines.append("")
    lines.append("#: The operations whose value is the MEASURED subject rather than the")
    lines.append("#: first-declared record. The spec is right about the row's shape here and")
    lines.append("#: the generator's tie-break was wrong, which is a different claim from")
    lines.append("#: ``LIVE_OVERRIDDEN`` -- see ``SUBJECT_OF`` in the generator.")
    lines.append("MEASURED_SUBJECT: frozenset[str] = frozenset(")
    lines.append("    {")
    for op in sorted(SUBJECT_OF):
        lines.append(f"        {quote}{op}{quote},")
    lines.append("    }")
    lines.append(")")
    lines.append("")
    lines.append("#: The other record-shaped properties on the same row, in declared order.")
    lines.append("#: Not noise -- ``/leases`` carries ``unit`` and ``/properties`` carries")
    lines.append("#: ``portfolio``, which is relationship material a later phase may want.")
    lines.append("ROW_COMPANIONS: dict[str, tuple[str, ...]] = {")
    for op in sorted(companions):
        names = companions[op]
        # ⚠ Emitted in ``ruff format``'s own shape, MEASURED by formatting the
        # file and reading the result back, not guessed. A one-element tuple
        # keeps its comma inline because that comma is required syntax rather
        # than a magic trailing comma; anything longer is exploded one per
        # line. Getting this wrong does not merely look untidy — the drift
        # check compares the committed file byte for byte, so a generator that
        # emits what the formatter would rewrite makes `--check` fail
        # permanently the moment anyone runs `make format`.
        if len(names) == 1:
            lines.append(f"    {quote}{op}{quote}: ({quote}{names[0]}{quote},),")
            continue
        lines.append(f"    {quote}{op}{quote}: (")
        for name in names:
            lines.append(f"        {quote}{name}{quote},")
        lines.append("    ),")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--audit", action="store_true")
    args = ap.parse_args(argv)

    # ⚠ SPEC is read here rather than left to ``load_spec``'s default, which
    # binds at def time and so cannot be redirected — a test pointing the
    # generator at a different spec would silently keep reading the real one
    # and report a pass.
    spec, digest = load_spec(SPEC)
    if digest != EXPECTED_SHA256:
        print(f"SPEC HASH MISMATCH\n  expected {EXPECTED_SHA256}\n  got      {digest}")
        return 1
    if args.audit:
        print(audit(spec))
        return 0
    envelope, companions = build_index(spec)
    text = render(envelope, companions, digest)
    if args.write:
        TARGET.write_text(text, encoding="utf-8")
        print(f"wrote {TARGET} ({len(text)} bytes, {len(envelope)} operations)")
        return 0
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print("DRIFT: _row_envelopes.py does not match the spec. Re-run --write.")
            return 1
        print(f"row-envelope map matches the spec ({len(envelope)} operations)")
        return 0
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
