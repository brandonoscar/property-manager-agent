"""Render the curated PM-workflow coverage matrix (Gate-2, priority b).

    python scripts/coverage_matrix.py            # print markdown
    python scripts/coverage_matrix.py --out docs/audits/workflow-coverage.md

Pure/offline — reads the curated mapping + the live golden/ dir, no Buildium key
and no LLM. The matrix is the sales-facing "does Occupella do the valuable things,
proven?" artifact; see ``helixis.evals.workflow_coverage``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from helixis.evals.workflow_coverage import compute_coverage, render_markdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the PM-workflow coverage matrix.")
    parser.add_argument("--out", help="Write markdown to this path instead of stdout.")
    args = parser.parse_args(argv)

    report = compute_coverage()
    md = render_markdown(report)

    if args.out:
        Path(args.out).write_text(md)
        print(f"wrote {args.out} ({report.with_case}/{report.total} workflows covered)")
    else:
        print(md)

    # Non-zero exit if a cited case has drifted out of golden/ (a real defect).
    if report.missing_case_refs:
        print("ERROR: cited cases missing from golden/:", file=sys.stderr)
        for ref in report.missing_case_refs:
            print(f"  - {ref}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
