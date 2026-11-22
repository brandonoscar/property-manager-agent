"""Unit tests for the opt-in Langfuse eval reporter (`evals.langfuse_report`).

No network: a fake Langfuse client captures the calls. Asserts the reporter
(a) no-ops safely when keys are blank, (b) maps pass/fail/latency/failure-kind
to the right Scores, (c) skips SKIPPED cases, and (d) never raises even when the
client throws.
"""

from __future__ import annotations

from typing import Any

import pytest

from helixis.evals import langfuse_report
from helixis.evals.runner import AssertionResult, CaseResult


class _FakeItem:
    def __init__(self, item_id: str) -> None:
        self.id = item_id
        self.links: list[tuple[Any, str]] = []

    def link(self, trace: Any, run_name: str, **_: Any) -> None:
        self.links.append((trace, run_name))


class _FakeDataset:
    def __init__(self, items: list[_FakeItem]) -> None:
        self.items = items


class _FakeTrace:
    def __init__(self, trace_id: str) -> None:
        self.id = trace_id


class _FakeClient:
    """Captures every call the reporter makes."""

    def __init__(self, item_ids: list[str]) -> None:
        self.datasets: list[dict[str, Any]] = []
        self.items: list[dict[str, Any]] = []
        self.traces: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self.flushed = False
        self._dataset = _FakeDataset([_FakeItem(i) for i in item_ids])

    def create_dataset(self, **kwargs: Any) -> None:
        self.datasets.append(kwargs)

    def create_dataset_item(self, **kwargs: Any) -> None:
        self.items.append(kwargs)

    def get_dataset(self, name: str) -> _FakeDataset:
        return self._dataset

    def trace(self, **kwargs: Any) -> _FakeTrace:
        self.traces.append(kwargs)
        return _FakeTrace(kwargs["id"])

    def score(self, **kwargs: Any) -> None:
        self.scores.append(kwargs)

    def flush(self) -> None:
        self.flushed = True


def _case(case_id: str, case_type: str = "planner_summarizer") -> dict[str, Any]:
    return {"id": case_id, "case_type": case_type, "input": {"x": 1}, "expected": {"y": 2}}


def _passed(case_id: str) -> CaseResult:
    r = CaseResult(case_id=case_id, assertions=[AssertionResult(name="a", passed=True)])
    r.latency_ms = 1234.5
    return r


def _quality_fail(case_id: str) -> CaseResult:
    r = CaseResult(
        case_id=case_id,
        assertions=[AssertionResult(name="wording", passed=False, reason="x", critical=False)],
    )
    r.latency_ms = 50.0
    return r


def _safety_fail(case_id: str) -> CaseResult:
    r = CaseResult(
        case_id=case_id,
        assertions=[AssertionResult(name="forbidden", passed=False, reason="x", critical=True)],
    )
    r.latency_ms = 10.0
    return r


def test_noop_when_keys_blank(capsys: pytest.CaptureFixture[str]) -> None:
    """With no Langfuse keys, the reporter builds no client and prints a skip."""
    from helixis.settings import Settings

    settings = Settings()
    settings.langfuse.public_key = ""
    settings.langfuse.secret_key = ""

    langfuse_report.report([_case("001")], [_passed("001")], settings=settings)
    out = capsys.readouterr().out
    assert "skipping" in out.lower()


def test_scores_pass_fail_latency_and_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(item_ids=["001", "002", "003"])
    monkeypatch.setattr(langfuse_report, "_build_client", lambda settings: fake)

    cases = [_case("001"), _case("002"), _case("003")]
    results = [_passed("001"), _quality_fail("002"), _safety_fail("003")]

    langfuse_report.report(cases, results)

    # Dataset upserted with one item per case.
    assert len(fake.items) == 3
    assert {i["id"] for i in fake.items} == {"001", "002", "003"}
    assert fake.flushed

    # eval_pass emitted for every case with the right value.
    passes = {s["trace_id"]: s for s in fake.scores if s["name"] == "eval_pass"}
    assert len(passes) == 3
    by_trace_name = {t["id"]: t["name"] for t in fake.traces}
    pass_vals = {by_trace_name[tid]: s["value"] for tid, s in passes.items()}
    assert pass_vals == {"eval.001": 1.0, "eval.002": 0.0, "eval.003": 0.0}

    # latency scored for every case.
    assert sum(1 for s in fake.scores if s["name"] == "eval_latency_ms") == 3

    # failure_kind only on failures, with the right category.
    kinds = {
        by_trace_name[s["trace_id"]]: s["value"]
        for s in fake.scores
        if s["name"] == "eval_failure_kind"
    }
    assert kinds == {"eval.002": "quality", "eval.003": "safety"}
    assert all(
        s["data_type"] == "CATEGORICAL" for s in fake.scores if s["name"] == "eval_failure_kind"
    )

    # Each case linked to its dataset item as a run.
    linked = {it.id for it in fake._dataset.items if it.links}
    assert linked == {"001", "002", "003"}


def test_skipped_case_not_scored(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(item_ids=["001"])
    monkeypatch.setattr(langfuse_report, "_build_client", lambda settings: fake)

    skipped = CaseResult(case_id="001", skipped=True, skip_reason="no key")
    langfuse_report.report([_case("001")], [skipped])

    assert not fake.scores  # a skip is not a quality signal
    assert not fake.traces


def test_reporting_error_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        def create_dataset(self, **_: Any) -> None:
            raise RuntimeError("boom")

        def create_dataset_item(self, **_: Any) -> None:
            raise RuntimeError("boom")

        def get_dataset(self, name: str) -> Any:
            raise RuntimeError("boom")

        def trace(self, **_: Any) -> Any:
            raise RuntimeError("boom")

        def score(self, **_: Any) -> None:
            raise RuntimeError("boom")

        def flush(self) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(langfuse_report, "_build_client", lambda settings: _Boom())
    # Must not raise despite every client call throwing.
    langfuse_report.report([_case("001")], [_passed("001")])
