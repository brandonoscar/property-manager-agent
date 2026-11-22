"""Cut 1 — Buildium read-back verification for create_to_do_task.

The create call returns the new task Id; the verifier reads it back via
``api.tasks.get`` (bounded retries) and only then reports success. PR #75: the
id never reaches the user-facing string, the model-visible ``guard.result``
(fresh OR replay), or the activity log — asserted here on both paths.

CI-safe: no real Buildium; ``api`` / ``client`` are mocks. The idempotency
store is forced absent (passthrough) so two calls in one test re-run the
create+verify deterministically; dedup-across-calls is covered separately in
test_idempotency_site_dedup.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from helixis.agents import buildium_tools as bt
from helixis.agents import idempotency as idem
from helixis.buildium.errors import BuildiumApiError
from helixis.buildium.schemas import Task
from helixis.memory.context import memory_context


def _ctx() -> Any:
    return memory_context(company_id="co-ver", user_id="u1", session_id="s1")


async def _auto_approve(**_kwargs: object) -> bool:
    return True


def _client() -> Any:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto-approve the gate; force passthrough (no store) so create+verify run
    on each call without dedup collapsing the second one."""
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    monkeypatch.setattr(idem, "store_from_pool", lambda: None)
    monkeypatch.setattr(idem, "_idempotency_required", lambda: False)
    monkeypatch.setattr(bt, "load_buildium_settings_for_company", AsyncMock(return_value=object()))


def _api(create_return: Any) -> Any:
    api = MagicMock()
    api.tasks = MagicMock()
    api.tasks.create = AsyncMock(return_value=create_return)
    return api


# ── happy path: read-back confirms ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_verified_reports_success_no_id_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api({"Id": 555, "Title": "Fix sink"})
    api.tasks.get = AsyncMock(return_value=Task(Id=555, Title="Fix sink"))
    monkeypatch.setattr(bt, "BuildiumClient", lambda *a, **k: _client())
    monkeypatch.setattr(bt, "BuildiumApi", lambda *a, **k: api)

    with _ctx():
        result = await bt._create_to_do_task_impl(
            title="Fix sink", unit_id=4, assigned_to_user_id=1
        )

    assert result == 'Task created in Buildium: "Fix sink".'
    api.tasks.create.assert_awaited_once()
    api.tasks.get.assert_awaited()  # read-back happened
    # PR #75: the id must NOT appear in the user-facing string.
    assert "555" not in result


@pytest.mark.asyncio
async def test_id_not_leaked_on_fresh_or_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    """On the passthrough path each call re-runs; the id is never in the
    returned (model-visible) string on the first OR a subsequent call."""
    api = _api({"Id": 99887, "Title": "Send lease"})
    api.tasks.get = AsyncMock(return_value=Task(Id=99887, Title="Send lease"))
    monkeypatch.setattr(bt, "BuildiumClient", lambda *a, **k: _client())
    monkeypatch.setattr(bt, "BuildiumApi", lambda *a, **k: api)

    with _ctx():
        first = await bt._create_to_do_task_impl(title="Send lease", assigned_to_user_id=1)
        second = await bt._create_to_do_task_impl(title="Send lease", assigned_to_user_id=1)

    for r in (first, second):
        assert r == 'Task created in Buildium: "Send lease".'
        assert "99887" not in r
        assert "Id" not in r  # no dict-dump


@pytest.mark.asyncio
async def test_replay_result_carries_no_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a durable store, the SECOND identical call replays the stored
    result — assert that stored/replayed value is the id-free string."""

    from helixis.db.repositories.action_idempotency import (
        STATUS_IN_PROGRESS,
        STATUS_SUCCEEDED,
        ClaimResult,
        ClaimRow,
    )

    class _Store:
        def __init__(self) -> None:
            self._rows: dict[tuple[str, str], ClaimRow] = {}

        async def claim(
            self, *, company_id: str, idem_key: str, action_type: str, window: Any
        ) -> ClaimResult:
            k = (company_id, idem_key)
            if k in self._rows:
                return ClaimResult(won=False, row=self._rows[k])
            row = ClaimRow(
                company_id=company_id,
                idem_key=idem_key,
                action_type=action_type,
                status=STATUS_IN_PROGRESS,
                result=None,
                error=None,
                attempts=1,
                expires_at=None,
            )
            self._rows[k] = row
            return ClaimResult(won=True, row=row)

        async def finalize_success(self, *, company_id: str, idem_key: str, result: Any) -> None:
            self._rows[(company_id, idem_key)].status = STATUS_SUCCEEDED
            self._rows[(company_id, idem_key)].result = result

        async def finalize_retryable(self, **_k: Any) -> None: ...
        async def finalize_abandoned(self, **_k: Any) -> None: ...
        async def finalize_aborted(self, **_k: Any) -> None: ...
        async def reclaim(self, **_k: Any) -> bool:
            return False

        async def get(self, *, company_id: str, idem_key: str) -> ClaimRow | None:
            return self._rows.get((company_id, idem_key))

        async def reap_stale(self) -> int:
            return 0

    store = _Store()
    monkeypatch.setattr(idem, "store_from_pool", lambda: store)
    api = _api({"Id": 4242, "Title": "Anything"})
    api.tasks.get = AsyncMock(return_value=Task(Id=4242, Title="Anything"))
    monkeypatch.setattr(bt, "BuildiumClient", lambda *a, **k: _client())
    monkeypatch.setattr(bt, "BuildiumApi", lambda *a, **k: api)

    with _ctx():
        first = await bt._create_to_do_task_impl(title="Anything", assigned_to_user_id=1)
        second = await bt._create_to_do_task_impl(title="Anything", assigned_to_user_id=1)

    api.tasks.create.assert_awaited_once()  # second was a replay, no 2nd create
    assert first == second == 'Task created in Buildium: "Anything".'
    # The stored (replayed, model-visible) result carries no id.
    stored = await store.get(company_id="co-ver", idem_key=next(iter(store._rows))[1])
    assert stored is not None
    assert "4242" not in str(stored.result)


# ── not-found after retries → needs-review, no false success, one create ──────


@pytest.mark.asyncio
async def test_not_found_is_needs_review_single_create(monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api({"Id": 1, "Title": "Ghost"})
    api.tasks.get = AsyncMock(side_effect=BuildiumApiError(404, "not found", path="/tasks/1"))
    monkeypatch.setattr(bt, "BuildiumClient", lambda *a, **k: _client())
    monkeypatch.setattr(bt, "BuildiumApi", lambda *a, **k: api)
    monkeypatch.setattr(bt, "_VERIFY_BACKOFF_SECONDS", 0.0)  # no real sleep

    with _ctx():
        result = await bt._create_to_do_task_impl(title="Ghost", assigned_to_user_id=1)

    # Honest needs-review message — NOT the success line, NOT the id.
    assert "flagging it for review" in result
    assert result != 'Task created in Buildium: "Ghost".'  # not a false success
    assert not any(ch.isdigit() for ch in result)  # no id (or any digit) leaks
    api.tasks.create.assert_awaited_once()  # NEVER re-created


@pytest.mark.asyncio
async def test_get_raises_through_exhaustion_is_needs_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = _api({"Id": 2, "Title": "Flaky"})
    api.tasks.get = AsyncMock(side_effect=TimeoutError("read-back timeout"))
    monkeypatch.setattr(bt, "BuildiumClient", lambda *a, **k: _client())
    monkeypatch.setattr(bt, "BuildiumApi", lambda *a, **k: api)
    monkeypatch.setattr(bt, "_VERIFY_BACKOFF_SECONDS", 0.0)

    with _ctx():
        result = await bt._create_to_do_task_impl(title="Flaky", assigned_to_user_id=1)

    assert "flagging it for review" in result
    api.tasks.create.assert_awaited_once()  # one create, no re-create


@pytest.mark.asyncio
async def test_read_back_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The verifier retries at most _VERIFY_MAX_ATTEMPTS times."""
    api = _api({"Id": 3, "Title": "Bound"})
    get = AsyncMock(side_effect=TimeoutError("timeout"))
    api.tasks.get = get
    monkeypatch.setattr(bt, "BuildiumClient", lambda *a, **k: _client())
    monkeypatch.setattr(bt, "BuildiumApi", lambda *a, **k: api)
    monkeypatch.setattr(bt, "_VERIFY_BACKOFF_SECONDS", 0.0)

    with _ctx():
        await bt._create_to_do_task_impl(title="Bound", assigned_to_user_id=1)

    assert get.await_count == bt._VERIFY_MAX_ATTEMPTS


# ── #227: store-unavailable fail-closed → honest "nothing created" message ────


@pytest.mark.asyncio
async def test_store_unavailable_fail_closed_says_nothing_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """required=True + no store → fail closed BEFORE the create. The user-facing
    message must NOT claim the task was created (it never ran), and create must
    not be called."""
    monkeypatch.setattr(idem, "_idempotency_required", lambda: True)  # override autouse fixture
    create = AsyncMock(return_value={"Id": 9, "Title": "Nope"})
    api = MagicMock()
    api.tasks = MagicMock()
    api.tasks.create = create
    api.tasks.get = AsyncMock()
    monkeypatch.setattr(bt, "BuildiumClient", lambda *a, **k: _client())
    monkeypatch.setattr(bt, "BuildiumApi", lambda *a, **k: api)

    with _ctx():
        result = await bt._create_to_do_task_impl(title="Nope", assigned_to_user_id=1)

    create.assert_not_awaited()  # fail-closed BEFORE execute
    api.tasks.get.assert_not_awaited()
    # Honest: did NOT create, nothing changed — and never the "created" wording.
    assert "did not create" in result.lower()
    assert "nothing was changed" in result.lower()
    assert "I created the task" not in result
