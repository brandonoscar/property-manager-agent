"""Unit tests for the runner's ``--tag`` case selector (release / quality).

The tag filter lets a CI lane run only the release-gating cases (or only the
quality cases) without touching how individual assertions are classified
safety-vs-quality at run time. A case is release-gating unless it explicitly
sets ``critical: false`` (default True — fail-closed).
"""

from __future__ import annotations

from helixis.evals.runner import _case_matches_tag


def _case(critical: object = "absent") -> dict:
    c: dict = {"id": "x"}
    if critical != "absent":
        c["critical"] = critical
    return c


def test_tag_all_includes_everything() -> None:
    assert _case_matches_tag(_case(), "all")
    assert _case_matches_tag(_case(critical=True), "all")
    assert _case_matches_tag(_case(critical=False), "all")


def test_tag_release_excludes_only_explicit_quality() -> None:
    # No `critical` field → defaults to release-gating (fail-closed).
    assert _case_matches_tag(_case(), "release") is True
    assert _case_matches_tag(_case(critical=True), "release") is True
    # Explicitly quality → excluded from the release lane.
    assert _case_matches_tag(_case(critical=False), "release") is False


def test_tag_quality_includes_only_explicit_quality() -> None:
    assert _case_matches_tag(_case(critical=False), "quality") is True
    assert _case_matches_tag(_case(), "quality") is False
    assert _case_matches_tag(_case(critical=True), "quality") is False
