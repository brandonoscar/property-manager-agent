"""The four "multimodal document" cases exercise the one branch production refuses.

Measured 2026-08-29, both sides:

* All four 02x cases pass ``mime_type="text/plain"`` with a ``.txt`` fixture.
* ``api/routes/agent.py`` builds its upload allowlist as
  ``frozenset(m for m in _SUPPORTED_MIME_TYPES if m != "text/plain")`` — the
  comment beside it already says "text/plain is eval-only fixtures".
* ``document_tools._build_messages`` branches on exactly that value: text is
  inlined as a text block, everything else is base64'd into an ``image_url``.
* ``use_docling`` defaults **False**, so a production PDF is not converted to
  text either — it takes the base64 branch too.

So production can only ever take the multimodal branch and no golden case ever
does. Confirmed in the database as well (cross-tenant on purpose, because the
claim is "never, by anyone"): ``company_documents`` holds 0 rows for all time,
and 0 ``orchestrator_runs`` have ever called ``extract_document_fields`` or
``assess_image``.

⚠ WHAT THIS IS **NOT**. The audit called it "the real path has only ever run
against a fake response", and that overstates it: the multimodal branch IS
covered structurally — ``test_document_tools`` feeds real PDF bytes and asserts
an ``image_url`` block comes out. What has never happened is that branch
meeting a live model. Keeping the distinction matters, because the fix for
"never structurally tested" is a unit test and the fix for "never met a model"
is spending money on a live multimodal case in release week.

So this file does the cheap half properly: it makes the gap **declared and
counted** rather than accidental, and reads BOTH sides so it cannot rot. A test
that only read the golden files would pass forever after someone widened the
upload allowlist, and one that only read the route would pass forever after
someone switched a fixture to a real PNG.
"""

from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
GOLDEN = _ROOT / "src" / "helixis" / "evals" / "golden"

#: The 02x document family. Named rather than globbed so adding a fifth case is
#: a deliberate act that lands in this list.
DOC_CASES = (
    "020_extract_lease",
    "021_extract_invoice",
    "022_extract_illegible",
    "023_assess_maintenance_photo",
)


def _case(case_id: str) -> dict:
    return json.loads((GOLDEN / f"{case_id}.json").read_text())


def test_the_document_family_is_exactly_what_this_file_thinks_it_is() -> None:
    """A tripwire. A new 02x case must be classified here rather than quietly
    inheriting whatever the others do."""
    on_disk = {p.stem for p in GOLDEN.glob("02*.json")}
    assert on_disk == set(DOC_CASES), f"02x family changed: {sorted(on_disk)}"


def test_production_still_refuses_the_mime_type_every_case_uses() -> None:
    """The core measurement, read from both sides at once.

    If this ever fails it is GOOD NEWS in one direction (production started
    accepting text/plain, so the cases now mean something) and a regression in
    the other (a case switched to a real image and the note below is stale).
    Either way it needs a human to look, which is the point.
    """
    from helixis.agents.document_tools import _SUPPORTED_MIME_TYPES
    from helixis.api.routes.agent import _UPLOAD_MIME_TYPES

    eval_only = set(_SUPPORTED_MIME_TYPES) - set(_UPLOAD_MIME_TYPES)
    assert eval_only == {"text/plain"}, (
        f"the eval-only mime set changed to {sorted(eval_only)} — the 02x "
        "cases' notes describe the old split"
    )
    used = {_case(c)["input"]["kwargs"].get("mime_type") for c in DOC_CASES}
    assert used == {"text/plain"}, (
        f"02x cases now use {sorted(used)} — if one reaches production's set, "
        "it is the first case ever to exercise the branch a real upload takes"
    )


def test_every_document_case_says_which_branch_it_exercises() -> None:
    """⚠ The honesty half, and the thing that was actually broken.

    ``023_assess_maintenance_photo`` is named for assessing a photo and is fed
    a ``.txt`` file; ``020``'s note called itself "Gemini multimodal
    extraction" while Gemini has been dead since 2026-07-21 (gotcha 18) and the
    case is not multimodal. A description that cannot fail goes on being wrong
    (gotcha 48), and these are the descriptions a future session reads before
    deciding the document path is covered.
    """
    for case_id in DOC_CASES:
        blob = json.dumps(_case(case_id))
        assert "text branch only" in blob.lower(), (
            f"{case_id} does not say which branch it exercises — it reads as "
            "coverage of the production document path and is not"
        )
        assert "Gemini" not in blob, (
            f"{case_id} still credits Gemini, which no longer serves any "
            "request (gotcha 18) — a stale note is how a stale gate survives"
        )


def test_the_multimodal_branch_is_covered_structurally_somewhere() -> None:
    """The refutation, pinned so it stays true.

    The audit's "only ever run against a fake response" is too strong: real
    bytes DO reach ``_build_messages`` and produce an ``image_url`` block. If
    that coverage is ever deleted, the audit's stronger claim becomes true and
    this file should stop citing the refutation.
    """
    src = (_ROOT / "tests" / "unit" / "test_document_tools.py").read_text()
    assert "image_url" in src and "_pdf_b64" in src, (
        "nothing feeds real document bytes through _build_messages any more — "
        "the multimodal branch now has NO coverage of any kind"
    )
