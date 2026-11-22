# Occupella's E2B sandbox image — the reporting toolchain, prebaked.
#
# WHY THIS EXISTS
# ---------------
# The sandbox denies ALL outbound network (`allow_internet_access=False`,
# CodeExecSettings.e2b_deny_egress). That is the containment property, not a
# tunable — so a run CANNOT `pip install` anything at execution time. Whatever
# agent-authored Python needs must be in the image before the microVM boots.
#
# Live-probed against the E2B default template (2026-08-22, e2b-code-interpreter
# 2.9.1). Present already: pandas, openpyxl, numpy, matplotlib, httpx.
# ABSENT: reportlab, pypdf, fpdf2, xlsxwriter — which is the whole reason for
# this file. Without them, "draft the owner statement as a PDF" fails inside the
# sandbox with an ImportError the user never asked for.
#
# ⚠ The base runs **Python 3.13** (measured on the first build, 2026-08-23) —
# the backend is 3.11. Agent-authored code is executed by the SANDBOX's
# interpreter, not ours, so a 3.13-only or 3.13-removed API behaves differently
# here than anywhere else in the product. Assume nothing carries over.
#
# ⚠ Versions are PINNED. An unpinned image rebuilds differently every time,
# which turns "the sandbox behaves the same as when we verified containment"
# into an assumption rather than a fact.
FROM e2bdev/code-interpreter:latest

# PDF generation (owner statements, notices) and PDF reading.
#   reportlab  — compose PDFs from scratch
#   pypdf      — read/merge/split existing PDFs
#   fpdf2      — the lighter compose path; imports as `fpdf`
# Spreadsheet WRITING with formatting (openpyxl is already present for reading).
#   xlsxwriter — number formats, column widths, charts
#
# ⚠ `chardet` is pinned because reportlab pulls it as a TRANSITIVE dep and pip
# resolved it unpinned on the first build (chardet-7.6.0). An unpinned
# transitive makes the "pinned" claim above false in exactly the quiet way that
# matters — the image drifts while the Dockerfile looks unchanged.
RUN pip install --no-cache-dir \
      reportlab==4.2.5 \
      chardet==7.6.0 \
      pypdf==5.1.0 \
      fpdf2==2.8.2 \
      xlsxwriter==3.2.0

# Fail the BUILD if any of it is unimportable, rather than discovering it at
# 3am inside a user's request. `fpdf2` installs under the module name `fpdf`.
RUN python -c "import reportlab, pypdf, fpdf, xlsxwriter; print('toolchain ok')"
