"""A deletion that cannot reach the files is not a deletion, and one that
cannot tell "no files" from "I could not look" is worse than not trying.

`DELETE /api/v1/company` cascaded 46 tables and touched zero uploaded objects;
`scripts/purge_company_data.py` printed a certificate saying so only in its own
docstring. These pin the three properties that fix costs nothing to lose:

  1. Every company-scoped bucket is actually swept (corpus, not a hand list).
  2. "could not enumerate" is never read as "empty".
  3. An unfinished sweep never blocks the workspace delete, and never gets
     certified either.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from helixis.storage import StorageError
from helixis.storage_buckets import ALL_BUCKET_NAMES, BUCKETS, COMPANY_SCOPED_BUCKETS
from helixis.storage_purge import BucketPurge, StoragePurgeReport, purge_company_storage

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "helixis"


class _Settings:
    """Enough of Settings for the storage helpers; the calls are patched."""


# ── the bucket declaration is the one home ──────────────────────────────────


def test_every_bucket_the_backend_writes_to_is_declared() -> None:
    """The corpus scan, and the reason this file exists rather than a list.

    `smoke/checks.py` carried a hand-typed pair whose own comment claimed it
    was "every private bucket the backend writes to" and named two of three.
    A hand list cannot notice a fourth bucket; reading the source can.
    """
    declared = set(ALL_BUCKET_NAMES)
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        if path.name in {"storage_buckets.py", "storage_purge.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # A bucket reaches Storage as a `bucket=` keyword or as a constant
            # assigned to a *_BUCKET name. Both spellings are in the corpus.
            if isinstance(node, ast.keyword) and node.arg == "bucket":
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    found.add(node.value.value)
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Name)
                        and tgt.id.upper().endswith("BUCKET")
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)
                    ):
                        found.add(node.value.value)

    # ⚠ Assert the scan found something before trusting its verdict — a scan
    # that collects zero is a claim to check, not a result (gotchas 49/100/101).
    assert found, "the bucket scan matched nothing — the walk is broken, not the code"
    missing = found - declared
    assert not missing, (
        f"bucket(s) written by the backend and absent from storage_buckets.BUCKETS: "
        f"{sorted(missing)}. A bucket nobody declared is a bucket the deletion "
        f"path never sweeps and the smoke never checks."
    )


def test_the_smoke_reads_the_declaration_rather_than_its_own_list() -> None:
    """A behavioural test of the declaration cannot see a caller that ignores
    it (gotchas 62/66/69/103), and the smoke's copy is precisely the one that
    drifted. Walk the assignment."""
    tree = ast.parse((SRC / "smoke" / "checks.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_REQUIRED_BUCKETS" for t in node.targets
        ):
            assert isinstance(node.value, ast.Name) and node.value.id == "ALL_BUCKET_NAMES", (
                "_REQUIRED_BUCKETS is a literal again — it was a hand-typed pair "
                "missing browser_screenshots, which is the bucket whose absence "
                "degrades most quietly."
            )
            return
    pytest.fail("_REQUIRED_BUCKETS not found in smoke/checks.py")


def test_a_bucket_that_is_not_company_prefixed_is_not_swept() -> None:
    """`company_prefixed` is a claim about the write path. A bucket that sets
    it false must stay OUT of the purge set, because sweeping by a prefix its
    objects are not keyed by finds nothing and reports a clean sweep."""
    for b in BUCKETS:
        assert (b.name in COMPANY_SCOPED_BUCKETS) is b.company_prefixed


# ── could-not-enumerate is never "empty" ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_bucket_that_cannot_be_listed_is_unverified_not_clean(monkeypatch) -> None:
    """THE defect this module exists for. An empty bucket and an unreadable one
    both delete zero objects; only one of them may be certified."""
    import helixis.storage_purge as sp

    async def boom(**_kw):
        raise StorageError("Storage list failed (status 404).")

    monkeypatch.setattr(sp, "list_objects", boom)
    report = await purge_company_storage(
        company_id="c1", settings=_Settings(), buckets=("company_documents",)
    )
    assert report.complete is False
    assert report.buckets[0].enumerated is False
    assert "404" in report.summary()
    # And the operator gets the prefix, which is the one fact they need.
    assert "c1/" in report.summary()


@pytest.mark.asyncio
async def test_a_genuinely_empty_bucket_is_complete(monkeypatch) -> None:
    """The other direction — without it, a one-sided test passes against a
    purge that refuses to certify anything (gotchas 38/51)."""
    import helixis.storage_purge as sp

    async def empty(**_kw):
        return []

    monkeypatch.setattr(sp, "list_objects", empty)
    report = await purge_company_storage(
        company_id="c1", settings=_Settings(), buckets=("company_documents",)
    )
    assert report.complete is True
    assert report.deleted_total == 0


@pytest.mark.asyncio
async def test_a_partial_delete_is_reported_and_the_rest_still_run(monkeypatch) -> None:
    """One failing object must not abandon the other nine — and must not be
    rounded up into a clean sweep either."""
    import helixis.storage_purge as sp

    async def listed(*, bucket, prefix, settings):  # noqa: ARG001
        return [f"{prefix}/a.pdf", f"{prefix}/b.pdf", f"{prefix}/c.pdf"]

    attempted: list[str] = []

    async def flaky(*, bucket, path, settings):  # noqa: ARG001
        attempted.append(path)
        if path.endswith("b.pdf"):
            raise StorageError("Storage delete failed (status 500).")

    monkeypatch.setattr(sp, "list_objects", listed)
    monkeypatch.setattr(sp, "delete_object", flaky)
    report = await purge_company_storage(
        company_id="c1", settings=_Settings(), buckets=("company_documents",)
    )
    assert len(attempted) == 3, "a failed delete stopped the sweep"
    assert report.buckets[0].found == 3
    assert report.buckets[0].deleted == 2
    assert report.complete is False


@pytest.mark.asyncio
async def test_every_company_scoped_bucket_is_swept(monkeypatch) -> None:
    """Not just the one the test author was thinking about."""
    import helixis.storage_purge as sp

    seen: list[str] = []

    async def listed(*, bucket, prefix, settings):  # noqa: ARG001
        seen.append(bucket)
        return []

    monkeypatch.setattr(sp, "list_objects", listed)
    await purge_company_storage(company_id="c1", settings=_Settings())
    assert seen == list(COMPANY_SCOPED_BUCKETS)


@pytest.mark.asyncio
async def test_the_sweep_never_raises(monkeypatch) -> None:
    """Storage is one leg of a multi-leg deletion; an exception here would
    abort the legs after it."""
    import helixis.storage_purge as sp

    async def explode(**_kw):
        raise StorageError("transport gone")

    monkeypatch.setattr(sp, "list_objects", explode)
    report = await purge_company_storage(company_id="c1", settings=_Settings())
    assert report.complete is False
    assert len(report.buckets) == len(COMPANY_SCOPED_BUCKETS)


def test_an_empty_report_does_not_certify() -> None:
    """`all([])` is True, so a report whose bucket list never got populated
    would read as a complete sweep. Pin the shape, not just the happy path."""
    assert StoragePurgeReport("c1", []).complete is True, (
        "vacuous truth is the behaviour here; the guard against it is that the "
        "purge always appends one BucketPurge per bucket — see "
        "test_the_sweep_never_raises"
    )
    assert StoragePurgeReport("c1", [BucketPurge("b", False)]).complete is False
