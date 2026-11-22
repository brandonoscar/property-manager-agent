"""Document-grounded company knowledge — extract a digest from an uploaded PDF,
cache it, and fold it into the distiller facts.

The extraction leg (Storage sign + Gemini multimodal) is a live seam, so it's
mocked; the deterministic pieces — digest composition, the ingest skip/cache
guard, and the facts→prompt rendering — are tested directly.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from helixis.company_knowledge import documents as ck_docs
from helixis.company_knowledge.sources import CompanyFacts, facts_to_prompt_block
from helixis.crm.draft import _build_context
from helixis.db.repositories.docs_links import KnowledgeDoc
from helixis.db.repositories.leads import LeadRow

# ── digest composition (pure) ───────────────────────────────────────


def test_compose_digest_summary_plus_fields_sorted() -> None:
    digest = ck_docs._compose_digest(
        "Standard residential lease.",
        {"monthly_rent": "$1,800", "pet_policy": "cats OK, $25/mo", "blank": ""},
    )
    assert digest.startswith("Standard residential lease.")
    assert "monthly rent: $1,800" in digest
    assert "pet policy: cats OK, $25/mo" in digest
    assert "blank" not in digest  # empty values dropped


def test_compose_digest_empty_when_nothing() -> None:
    assert ck_docs._compose_digest("", {}) == ""


def test_compose_digest_keeps_full_policy_figures() -> None:
    """Regression (#461 dogfood, bug 3): a verbose pet field must NOT be sliced
    mid-number. The old 120-char cap turned "$300 … $35/mo" into a wrong "$30"."""
    pet = (
        "Pet-friendly (cats & dogs), max 2 pets. $300 refundable deposit per pet, "
        "plus $35/mo pet rent per pet. Restricted breeds excluded."
    )
    digest = ck_docs._compose_digest("Resident policies.", {"pet_policy": pet})
    assert "$300" in digest  # not corrupted to $30
    assert "$35/mo" in digest  # the pet rent survives
    assert "$30 " not in digest and not digest.endswith("$30")


def test_clip_never_slices_mid_token() -> None:
    # Over the limit → cut at a word boundary + ellipsis, keeping "$300" whole.
    out = ck_docs._clip("deposit is $300 per pet and more text here", 14)
    assert out.endswith("…")
    assert "$30" not in out.replace("…", "")  # the number wasn't split
    # Under the limit → returned unchanged, no ellipsis.
    assert ck_docs._clip("short", 100) == "short"


# ── extract_document_digest (mocked live seam) ──────────────────────


async def test_extract_digest_success() -> None:
    result = SimpleNamespace(
        summary="Pet policy handbook.", fields={"pet_deposit": "$300"}, warnings=[]
    )
    with (
        patch.object(ck_docs, "sign_object_url", AsyncMock(return_value="https://signed")),
        patch.object(ck_docs, "extract_document_fields_impl", AsyncMock(return_value=result)),
    ):
        digest = await ck_docs.extract_document_digest(
            _settings(), storage_path="c/1.pdf", content_type="application/pdf"
        )
    assert digest is not None
    assert "Pet policy handbook." in digest
    assert "pet deposit: $300" in digest


async def test_extract_digest_none_on_failure_warning() -> None:
    result = SimpleNamespace(summary="", fields={}, warnings=["attachment_unavailable"])
    with (
        patch.object(ck_docs, "sign_object_url", AsyncMock(return_value="https://signed")),
        patch.object(ck_docs, "extract_document_fields_impl", AsyncMock(return_value=result)),
    ):
        digest = await ck_docs.extract_document_digest(
            _settings(), storage_path="c/1.pdf", content_type="application/pdf"
        )
    assert digest is None


async def test_extract_digest_none_on_extractor_exception() -> None:
    with (
        patch.object(ck_docs, "sign_object_url", AsyncMock(return_value="https://signed")),
        patch.object(
            ck_docs, "extract_document_fields_impl", AsyncMock(side_effect=RuntimeError("boom"))
        ),
    ):
        digest = await ck_docs.extract_document_digest(
            _settings(), storage_path="c/1.pdf", content_type="application/pdf"
        )
    assert digest is None


# ── ingest: skip already-digested, cache new ────────────────────────


async def test_ingest_skips_digested_and_caches_new() -> None:
    docs = [
        KnowledgeDoc(
            id="d1",
            name="Pet Policy.pdf",
            kind="other",
            storage_path="c/d1.pdf",
            content_type="application/pdf",
            text_digest=None,  # needs extraction
        ),
        KnowledgeDoc(
            id="d2",
            name="Lease.pdf",
            kind="lease",
            storage_path="c/d2.pdf",
            content_type="application/pdf",
            text_digest="already have it",  # skip
        ),
    ]
    set_digest = AsyncMock()
    with (
        patch.object(ck_docs.docs_repo, "documents_for_knowledge", AsyncMock(return_value=docs)),
        patch.object(ck_docs.docs_repo, "set_document_digest", set_digest),
        patch.object(ck_docs, "extract_document_digest", AsyncMock(return_value="cats OK")),
    ):
        n = await ck_docs.ingest_company_document_digests(object(), _settings(), company_id="c")
    assert n == 1
    set_digest.assert_awaited_once()
    assert set_digest.await_args.kwargs["document_id"] == "d1"
    assert set_digest.await_args.kwargs["text_digest"] == "cats OK"


async def test_ingest_returns_zero_when_table_missing() -> None:
    with patch.object(
        ck_docs.docs_repo,
        "documents_for_knowledge",
        AsyncMock(side_effect=Exception("relation does not exist")),
    ):
        n = await ck_docs.ingest_company_document_digests(object(), _settings(), company_id="c")
    assert n == 0


# ── facts rendering + CRM draft context ─────────────────────────────


def test_facts_block_renders_document_contents_with_source() -> None:
    facts = CompanyFacts(
        documents=[("Pet Policy.pdf", "other")],
        document_digests=[("Pet Policy.pdf", "Cats & dogs under 40lbs, $25/mo.")],
    )
    block = facts_to_prompt_block(facts)
    assert "Document contents" in block
    assert "Pet Policy.pdf" in block
    assert "Cats & dogs under 40lbs, $25/mo." in block
    assert facts.has_any_signal() is True


def test_draft_context_includes_company_knowledge() -> None:
    lead = LeadRow(
        company_id="c",
        name="Jordan",
        kind="renter",
        stage="new",
        source="zillow",
        unit_label="Maple 4B",
        note="Is 4B available?",
    )
    ctx = _build_context(lead, [], "Application fee: $45. Pets: cats OK.")
    assert "Company facts" in ctx
    assert "Application fee: $45." in ctx
    assert "Maple 4B" in ctx


def _settings() -> object:
    # generate_lead_draft / extract only read attributes we don't touch in these
    # tests (the LLM/extractor calls are mocked), so a bare object suffices.
    return SimpleNamespace()
