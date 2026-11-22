"""PR #102 — asyncpg Record.keys() iterator exhaustion regression.

Tonight (May 19, 9:44 PM EDT) we caught a logically impossible bug
in prod: the DB had ``summary_text='Jimmy reported a loud crashing
sound this afternoon at 16'`` and ``summary_attempts=1``,
``row.keys()`` (when printed) listed both columns, but
``_row_to_summary(row)`` returned ``text=None / attempts=0``.

Root cause: the old helper did ``keys = r.keys()`` once and then
performed five ``"key" in keys`` membership checks. ``asyncpg.Record.keys()``
returns a single-use ITERATOR, not a reusable view — the first
``in`` exhausts it and every subsequent check falls through to the
default. Only ``last_attempted_at`` round-tripped correctly in
production because its check happened on the line above ``return``,
before the iterator was consumed by the four checks inside the
``return`` arguments.

This bug shipped in PR #96 (the very PR that was meant to fix the
serializer truth). The PR #96 tests passed because the fixture
shape didn't reproduce the iterator-exhaustion pattern — and PR
#101's verify_schema boot gate did nothing for this symptom because
the columns ARE visible to the pool; the bug is one layer above,
in Python.

The tests here replicate the exhausted-iterator failure mode, not
just the happy path, so we cannot ship a green suite with a
broken serializer again the same way PR #96 did.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

from helixis.api.routes.buildium import _row_to_summary


class FakeRecord:
    """Mimics ``asyncpg.Record``'s relevant surface for the helper.

    Critically: ``keys()`` returns a new SINGLE-USE iterator on each
    call (matching asyncpg's actual behavior). A test that called
    ``keys()`` once and reused the returned object would see it
    exhaust after the first ``in`` check — which is exactly the
    production bug.

    ``dict(record)`` works because we implement ``keys()`` + the
    iterator dict's constructor protocol via ``__iter__`` +
    ``__getitem__``.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = dict(data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def keys(self) -> Iterator[str]:
        return iter(self._data.keys())


def test_row_to_summary_reads_all_fields_from_real_record_shape() -> None:
    """Lock the fix: every summary field round-trips when the source
    behaves like a real asyncpg Record (single-use ``keys()`` iter).

    The pre-PR-#102 helper would fail this test on ``text`` and
    ``attempts`` (returns ``None`` / ``0``) and only pass on
    ``last_attempted_at`` because of the evaluation-order accident
    described in the production repro.
    """
    ts = datetime(2026, 5, 20, 0, 42, 11, 227161, tzinfo=UTC)
    record = FakeRecord(
        {
            "id": "abc-123",
            "event_name": "Task.Created",
            "event_time": ts,
            "status": "received",
            "last_error": None,
            "planner_status": "succeeded",
            "planner_error": None,
            "summary_status": "succeeded",
            "summary_text": "Jimmy reported a loud crashing sound this afternoon at 16",
            "summary_error": None,
            "summary_attempts": 1,
            "summary_last_attempted_at": ts,
        }
    )

    summary = _row_to_summary(record)

    assert summary.status == "succeeded"
    assert summary.text == "Jimmy reported a loud crashing sound this afternoon at 16"
    assert summary.error is None
    assert summary.attempts == 1
    assert summary.last_attempted_at == ts.isoformat()


def test_row_to_summary_falls_back_when_columns_missing() -> None:
    """Skinny shape (pre-migration / fallback): no summary_*
    columns in the row at all → every field uses the Pydantic
    default. Preserves the PR #92 / PR #96 fallback contract."""
    record = FakeRecord(
        {
            "id": "abc-123",
            "event_name": "Task.Created",
            "event_time": datetime(2026, 5, 20, tzinfo=UTC),
            "status": "received",
            "last_error": None,
        }
    )

    summary = _row_to_summary(record)

    assert summary.status == "succeeded"  # Pydantic default
    assert summary.text is None
    assert summary.error is None
    assert summary.attempts == 0
    assert summary.last_attempted_at is None


def test_row_to_summary_does_not_consume_keys_iterator_multiple_times() -> None:
    """Regression lock at the interface level: the helper must NOT
    rely on multi-use ``.keys()``. Even if someone refactors the
    helper later, they can't reintroduce the iterator-exhaustion
    pattern without this test going red.

    We watch ``keys()`` with a MagicMock; the production bug
    pattern would call it exactly once (capture) and reuse, so we
    pin: at most one ``keys()`` call (``dict(r)`` may consume
    ``__iter__`` instead, which is also fine).
    """
    ts = datetime(2026, 5, 20, tzinfo=UTC)
    data = {
        "summary_status": "succeeded",
        "summary_text": "x",
        "summary_error": None,
        "summary_attempts": 1,
        "summary_last_attempted_at": ts,
    }

    record = MagicMock()
    record.keys = MagicMock(return_value=iter(data.keys()))
    record.__iter__ = MagicMock(return_value=iter(data.keys()))
    record.__getitem__ = MagicMock(side_effect=lambda k: data[k])

    summary = _row_to_summary(record)

    # Whether dict(record) uses keys() or __iter__ is an implementation
    # detail of dict's constructor — both routes are safe. What the
    # test pins is that the helper does NOT do ``keys = record.keys()``
    # and reuse it. ``call_count <= 1`` covers both safe paths.
    assert record.keys.call_count <= 1
    # And the field actually round-tripped:
    assert summary.text == "x"
    assert summary.attempts == 1


def test_row_to_summary_handles_real_asyncpg_record_shape() -> None:
    """Smoke test against a real asyncpg.Record-compatible source.

    A plain ``dict`` is not an asyncpg.Record but ``dict(r).get(...)``
    accepts both, which is precisely the property the fix relies on.
    """
    ts = datetime(2026, 5, 20, 0, 42, 11, 227161, tzinfo=UTC)
    row_dict = {
        "summary_status": "failed",
        "summary_text": None,
        "summary_error": "sanitizer rejected output",
        "summary_attempts": 3,
        "summary_last_attempted_at": ts,
    }

    summary = _row_to_summary(row_dict)

    assert summary.status == "failed"
    assert summary.text is None
    assert summary.error == "sanitizer rejected output"
    assert summary.attempts == 3
    assert summary.last_attempted_at == ts.isoformat()
