"""Hardening the one Rentvine write (loop iteration 5, 2026-10-02).

Three defects the gate stack had, each pinned through the REAL idempotency
guard with an in-memory store, so the filing (retryable vs ambiguous) is
measured rather than assumed:

1. A body approved minutes ago was sent without re-reading: an edit made in
   Rentvine in between was overwritten with the old value.
2. A refusal that provably sent nothing (breaker, Retry-After hold, a 4xx) was
   filed ``abandoned_unknown``. That blocks the retry for the whole window and
   then answers "I sent it but couldn't confirm it".
3. The read-back checked only the status, so a full-replace update that
   cleared other fields would have reported success.

All records are synthetic.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any

import pytest

from helixis.agents import idempotency as idem
from helixis.agents import rentvine_tools as rt
from helixis.db.repositories.action_idempotency import (
    STATUS_ABANDONED_UNKNOWN,
    STATUS_FAILED_RETRYABLE,
    STATUS_SUCCEEDED,
)
from helixis.rentvine import client as rvclient
from helixis.rentvine import work_orders as wo
from helixis.rentvine.client import RentvineAPIError
from helixis.rentvine.transport import Failure

from .test_idempotency_site_dedup import FakeStore
from .test_rentvine_write_tool import _LABEL, _WO_ID, _FakeClient, _patches, _record


async def _approve(**_kw: object) -> bool:
    return True


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    s = FakeStore()
    monkeypatch.setattr(idem, "store_from_pool", lambda: s)
    monkeypatch.setattr(idem, "confirm_or_abort", _approve)
    monkeypatch.setattr(rt, "_VERIFY_BACKOFF_SECONDS", 0.0)
    return s


class _Client(_FakeClient):
    """``reads`` are served in order by ``get_object`` (the last one repeats);
    ``after_post`` replaces the record once a write lands."""

    def __init__(
        self,
        *,
        reads: list[dict[str, Any]] | None = None,
        after_post: dict[str, Any] | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self._reads = list(reads or [])
        self._after_post = after_post
        self._posted = False

    async def get_object(self, template: str, **kw: Any) -> dict[str, Any]:
        if self._posted and self._after_post is not None:
            self.get_object_calls += 1
            return dict(self._after_post)
        if self._reads:
            self.get_object_calls += 1
            if self._get_object_error is not None:
                raise self._get_object_error
            return dict(self._reads.pop(0) if len(self._reads) > 1 else self._reads[0])
        return await super().get_object(template, **kw)

    async def post(self, template: str, **kw: Any) -> Any:
        out = await super().post(template, **kw)
        self._posted = True
        if self._after_post is None:
            # The record as the base fake updated it (the new status).
            self._after_post = dict(self._record)
        return out


async def _call(client: _FakeClient) -> str:
    with ExitStack() as stack:
        _patches(stack, client)
        return await rt._update_work_order_status_impl(_WO_ID, "Completed", _LABEL)


def _row(store: FakeStore) -> Any:
    (row,) = store._rows.values()
    return row


# ── 1. the record moved after approval ────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_record_edited_after_the_card_is_not_overwritten(store: FakeStore) -> None:
    edited = {**_record(), "vendorInstructions": "Use the side door now."}
    client = _Client(reads=[_record(), edited])
    reply = await _call(client)
    assert client.posts == []
    assert "changed in Rentvine after you approved" in reply
    assert "Nothing was changed in Rentvine" in reply
    assert _row(store).status == STATUS_FAILED_RETRYABLE


@pytest.mark.asyncio
async def test_after_a_moved_record_refusal_asking_again_works(store: FakeStore) -> None:
    """Retryable means retryable: the same change, asked again, goes through."""
    edited = {**_record(), "vendorInstructions": "Use the side door now."}
    first = _Client(reads=[_record(), edited])
    await _call(first)
    second = _Client(reads=[edited])
    reply = await _call(second)
    assert len(second.posts) == 1
    assert second.posts[0]["body"]["vendorInstructions"] == "Use the side door now."
    assert reply.startswith("Set ")
    assert _row(store).status == STATUS_SUCCEEDED


@pytest.mark.asyncio
async def test_an_unchanged_record_is_sent_after_the_re_read(store: FakeStore) -> None:
    client = _Client(reads=[_record()])
    reply = await _call(client)
    assert len(client.posts) == 1 and reply.startswith("Set ")
    # card read + pre-send re-read + read-back
    assert client.get_object_calls >= 3


@pytest.mark.asyncio
async def test_a_failed_re_read_sends_nothing_and_stays_retryable(store: FakeStore) -> None:
    class _ReReadFails(_Client):
        async def get_object(self, template: str, **kw: Any) -> dict[str, Any]:
            if self.get_object_calls >= 1:
                self.get_object_calls += 1
                raise RentvineAPIError(503, Failure.UNDOCUMENTED, True, "down")
            return await super().get_object(template, **kw)

    client = _ReReadFails(reads=[_record()])
    reply = await _call(client)
    assert client.posts == []
    assert "Nothing was changed in Rentvine" in reply
    assert _row(store).status == STATUS_FAILED_RETRYABLE


# ── 2. what is filed retryable, and what stays ambiguous ─────────────────────


@pytest.mark.parametrize(
    "error",
    [
        rvclient.RentvineUnavailableError(300.0),
        rvclient._held_error(30.0),
        RentvineAPIError(400, Failure.BAD_REQUEST, False, "bad body"),
        RentvineAPIError(403, Failure.PERMISSION, False, "role"),
    ],
    ids=["breaker", "retry-after hold", "400", "403"],
)
@pytest.mark.asyncio
async def test_a_write_that_provably_did_not_happen_is_retryable(
    store: FakeStore, error: RentvineAPIError
) -> None:
    client = _Client(reads=[_record()], post_error=error)
    reply = await _call(client)
    assert "Nothing" in reply or "nothing" in reply
    assert "couldn't confirm" not in reply
    assert _row(store).status == STATUS_FAILED_RETRYABLE


@pytest.mark.parametrize(
    "error",
    [
        RentvineAPIError(500, Failure.UNDOCUMENTED, True, "server"),
        RentvineAPIError(0, Failure.UNDOCUMENTED, True, "transport: reset"),
    ],
    ids=["500", "transport"],
)
@pytest.mark.asyncio
async def test_a_write_that_may_have_landed_stays_ambiguous(
    store: FakeStore, error: RentvineAPIError
) -> None:
    """A 5xx or a dropped connection may have written: never auto-retried."""
    client = _Client(reads=[_record()], post_error=error)
    await _call(client)
    assert _row(store).status == STATUS_ABANDONED_UNKNOWN


def test_only_the_breaker_and_the_hold_claim_not_sent() -> None:
    assert rvclient.RentvineUnavailableError(None).sent is False
    assert rvclient._held_error(5.0).sent is False
    assert RentvineAPIError(500, Failure.UNDOCUMENTED, True, "").sent is True


# ── 3. the read-back checks the rest of the record ───────────────────────────


@pytest.mark.asyncio
async def test_a_field_the_update_cleared_is_named_and_not_reported_as_done(
    store: FakeStore,
) -> None:
    """If the endpoint is a full replace that blanks fields, the first live
    write says so instead of "Set ... to Completed"."""
    after = {**_record(44), "vendorInstructions": None, "vendorContactID": None}
    client = _Client(reads=[_record()], after_post=after)
    reply = await _call(client)
    assert "cleared these fields" in reply
    assert "vendorContactID" in reply and "vendorInstructions" in reply
    assert not reply.startswith("Set ")
    assert _row(store).status == STATUS_ABANDONED_UNKNOWN


@pytest.mark.asyncio
async def test_a_reformatted_value_is_not_erasure(store: FakeStore) -> None:
    after = {**_record(44), "priorityID": "2", "dateDue": "2026-10-01 00:00:00"}
    client = _Client(reads=[_record()], after_post=after)
    reply = await _call(client)
    assert reply.startswith("Set ")
    assert _row(store).status == STATUS_SUCCEEDED


def test_erasure_is_presence_only() -> None:
    sent = {"a": "x", "b": 0, "c": False, "d": "  ", "e": None}
    after = {"a": "", "b": 0, "c": False}
    # "a" emptied; b/c keep falsy-but-present values; d and e carried nothing.
    assert rt._erased_fields(sent, after) == ["a"]


def test_the_status_field_is_still_what_the_read_back_reads() -> None:
    assert wo.current_status_id({wo.STATUS_FIELD: 44}) == 44
