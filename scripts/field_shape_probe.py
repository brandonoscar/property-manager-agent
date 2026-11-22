"""Does the field the code reads actually carry a VALUE in production?

`buildium/field_shapes.py` is the codified defence against this repo's most
expensive recurring bug — a Buildium field present as a KEY and null as a
VALUE, read as if populated. It records four instances in one month
(gotchas 24/25/26/53/55), every one of which reported a plausible wrong number
rather than crashing, and every one of which survived unit tests because the
tests supplied the field the code wanted.

Until this script existed the module had **no production or CI caller** — it
was imported by exactly one file, its own unit test. So it was a static table
of expectations that had never been compared against a production row, which
means it could not detect the NEXT null-valued field. That is gotcha 53's
"check population, not presence" pointed back at the very module written to
enforce it.

    python scripts/field_shape_probe.py                     # needs Buildium creds
    python scripts/field_shape_probe.py --json --limit 400

Exit 0 = every declared field behaves as the registry expects. Exit 1 = at
least one finding. Exit 2 = could not probe (missing credentials) — deliberately
NOT 0: a shape check that could not run is not a shape check that passed, the
same skip-floor rule the eval harness uses (gotcha 35).

⚠ **Read-only.** It lists rows and counts nulls. It writes nothing, to Buildium
or to us.

⚠ **The registry covers `bill` and `task` only**, because it was written from
four historical incidents rather than measured. A clean run therefore means
"the fields we already got burned on still behave", NOT "no field is null" —
the first broad reconciliation pass is expected to find drift, and widening
`EXPECTATIONS` to more entities is the follow-up this script makes possible.

⚠ **A `never_trust` field that starts working is reported too.** That is not
noise: it means code which correctly stopped depending on the field could now
start again, and nobody would otherwise notice the constraint had lifted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from helixis.buildium.api import BuildiumApi  # noqa: E402
from helixis.buildium.client import BuildiumClient  # noqa: E402
from helixis.buildium.field_shapes import probe_live  # noqa: E402
from helixis.settings import get_settings  # noqa: E402


async def _run(limit: int) -> dict[str, list]:
    settings = get_settings()
    async with BuildiumClient(settings.buildium) as client:
        return await probe_live(BuildiumApi(client), limit=limit)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=200, help="rows per entity (default 200)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    settings = get_settings()
    creds = settings.buildium
    if not (getattr(creds, "client_id", "") and getattr(creds, "client_secret", "")):
        # NOT exit 0 — see the module docstring's skip-floor note.
        print(
            "Buildium credentials are not configured — cannot probe field shapes.\n"
            "Set HELIXIS_BUILDIUM__CLIENT_ID and HELIXIS_BUILDIUM__CLIENT_SECRET.",
            file=sys.stderr,
        )
        return 2

    try:
        findings = asyncio.run(_run(args.limit))
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — a probe failure is exit 2, not 0
        print(f"field-shape probe could not complete: {exc!r}", file=sys.stderr)
        return 2

    total = sum(len(v) for v in findings.values())

    if args.json:
        print(
            json.dumps(
                {
                    "limit": args.limit,
                    "entities": sorted(findings),
                    "findings": {k: [str(f) for f in v] for k, v in findings.items()},
                    "total": total,
                },
                indent=2,
            )
        )
    else:
        for entity in sorted(findings):
            rows = findings[entity]
            if not rows:
                print(f"{entity}: OK")
                continue
            print(f"{entity}: {len(rows)} finding(s)")
            for f in rows:
                print(f"  - {f}")
        if not total:
            print(
                "\nEvery declared field behaves as the registry expects. Note the registry "
                "covers only the entities in EXPECTATIONS — this is not 'no field is null'."
            )

    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
