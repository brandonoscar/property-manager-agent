"""The one Rentvine write — its gates, its body, and its read-back verify.

TWO SECTIONS, AND THEY MEASURE DIFFERENT THINGS.

``the merge contract`` covers ``rentvine/work_orders.py``: what body a one-field
change actually sends. That module exists because
``POST /maintenance/work-orders/{workOrderID}`` declares 38 fields and requires
none, which is either a partial update or a full replace and the captured spec
cannot say which — so the body has to be correct under both readings.

``the tool`` covers ``agents/rentvine_tools.py``: the gate stack, in order.

⚠ **THE LOAD-BEARING TEST IS ``test_a_2xx_with_an_unchanged_status_is_not_a
_success``.** Rentvine's update semantics are unprobed, and gotcha 20 is this
repo's standing record of an endpoint that returns 200 and silently ignores the
field you sent — Buildium's ``PUT /workorders`` does exactly that with a Status.
A tool that trusted the 2xx would tell a manager their work order was closed
when it was not. That test reads BOTH sides: what the tool WRITES
(``STATUS_FIELD`` in the POST body) and what the verifier READS
(``current_status_id`` on the re-GET). A verify pointed at the wrong field is a
silent false negative going one way (gotcha 20) and a silent false POSITIVE
going the other (gotcha 70), and only reading both sides can see either.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.agents import rentvine_tools as rt
from helixis.agents import tools as agent_tools
from helixis.memory.context import memory_context
from helixis.rentvine import work_orders as wo
from helixis.rentvine.client import RentvineAPIError
from helixis.rentvine.transport import Failure

#: The captured vendor spec (``scripts/rentvine_query_index.py`` reads the same file).
SPEC = Path(__file__).resolve().parents[2] / "docs/rentvine/vendor/docs.rentvine.com/openapi.json"
_WO_ID = 4471
_LABEL = "Replace the hallway smoke detector"

# The account's own status vocabulary — deliberately NOT the ids a reader would
# guess, because the whole point of resolving a NAME is that the numbers are the
# customer's and not ours.
_STATUS_ROWS = [
    {"workOrderStatusID": 91, "name": "New"},
    {"workOrderStatusID": 7, "name": "In Progress"},
    {"workOrderStatusID": 44, "name": "Completed"},
]


#: A record shaped like the GET response: real field names from the generated
#: index, including one the update surface does NOT declare (``workOrderID``)
#: and one that is null.
def _record(status_id: int = 91) -> dict[str, Any]:
    return {
        "workOrderID": _WO_ID,
        "workOrderStatusID": status_id,
        "description": _LABEL,
        "propertyID": 812,
        "unitID": 1904,
        "vendorContactID": 55,
        "vendorInstructions": "Ring the bell; the dog is friendly.",
        "priorityID": 2,
        "isInternal": False,
        "dateDue": "2026-10-01",
        "closingDescription": None,
    }


class _FakeClient:
    """Records what the tool sends; serves what the tool reads.

    ``statuses_after_post`` is what the re-GET returns once a write has
    happened — the knob the 200-but-lie test turns.
    """

    def __init__(
        self,
        *,
        record: dict[str, Any] | None = None,
        status_after_post: int | None = 44,
        rows: list[dict[str, Any]] | None = None,
        get_object_error: RentvineAPIError | None = None,
        post_error: RentvineAPIError | None = None,
    ) -> None:
        self._record = dict(record or _record())
        self._status_after_post = status_after_post
        self._rows = _STATUS_ROWS if rows is None else rows
        self._get_object_error = get_object_error
        self._post_error = post_error
        self.posts: list[dict[str, Any]] = []
        self.closed = False
        self.get_object_calls = 0

    async def get_rows(self, template: str, **_kw: Any) -> list[dict[str, Any]]:
        assert template == wo.STATUSES_PATH
        return list(self._rows)

    async def get_object(self, template: str, **_kw: Any) -> dict[str, Any]:
        assert template == wo.GET_PATH
        self.get_object_calls += 1
        if self._get_object_error is not None:
            raise self._get_object_error
        return dict(self._record)

    async def post(
        self, template: str, *, json_body: dict[str, Any], path_params: dict[str, Any] | None = None
    ) -> Any:
        if self._post_error is not None:
            raise self._post_error
        self.posts.append({"template": template, "body": json_body, "path": path_params})
        if self._status_after_post is not None:
            self._record[wo.STATUS_FIELD] = self._status_after_post
        return {"ok": True}

    async def aclose(self) -> None:
        self.closed = True


def _settings(*, enabled: bool = True) -> Any:
    s = MagicMock()
    s.rentvine.enabled = enabled
    return s


def _patches(
    stack: ExitStack,
    client: _FakeClient | None,
    *,
    enabled: bool = True,
    connected: bool = True,
    role: str = "admin",
) -> None:
    stack.enter_context(memory_context(company_id="company-1", user_id="u1", session_id="s1"))
    stack.enter_context(patch.object(rt, "get_settings", return_value=_settings(enabled=enabled)))
    stack.enter_context(
        patch.object(
            rt,
            "load_rentvine_settings_for_company",
            new=AsyncMock(return_value=object() if connected else None),
        )
    )
    stack.enter_context(patch.object(rt, "RentvineClient", return_value=client))
    # No pool in unit tests, so the Manager+ floor's role lookup is stubbed.
    from helixis.agents import buildium_tools as bt

    stack.enter_context(patch.object(bt, "_caller_role", new=AsyncMock(return_value=role)))


async def _next_confirm_payload() -> dict[str, Any]:
    for _ in range(50):
        await asyncio.sleep(0)
        drained = await agent_tools.drain_pending_confirmations()
        if drained:
            return drained[0]
    raise AssertionError("no confirm payload was emitted by the write tool")


async def _run(client: _FakeClient, *, status: str = "Completed", **kw: Any) -> str:
    """Drive the tool to completion, approving at the gate."""
    await agent_tools.drain_pending_confirmations()
    with ExitStack() as stack:
        _patches(stack, client, **kw)
        task = asyncio.create_task(
            rt._update_work_order_status_impl(_WO_ID, status, work_order_label=_LABEL)
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        return await asyncio.wait_for(task, timeout=2.0)


async def _refuses_before_the_gate(
    client: _FakeClient, *, status: str = "Completed", **kw: Any
) -> str:
    """Drive the tool expecting it to refuse BEFORE any confirm gate.

    ⚠ **BOUNDED, and the bound is the point.** ``confirm_or_abort``'s default
    timeout is 300 seconds, so a refusal that stops working does not fail here —
    it HANGS, for five minutes, per test. The first mutation battery discovered
    that the hard way: half a dozen mutants each parked for 300s and the run had
    to be killed, which turns a battery into something nobody runs again. A
    two-second ceiling turns every one of those into a fast failure that names
    the cause, and asserting on the DRAINED QUEUE afterwards is what proves the
    gate was never reached rather than merely never answered.
    """
    await agent_tools.drain_pending_confirmations()
    with ExitStack() as stack:
        _patches(stack, client, **kw)
        try:
            result = await asyncio.wait_for(
                rt._update_work_order_status_impl(_WO_ID, status, work_order_label=_LABEL),
                timeout=2.0,
            )
        except TimeoutError:  # pragma: no cover — only a regression reaches this
            raise AssertionError(
                "the tool did not refuse; it blocked, which means it reached the confirm gate"
            ) from None
    assert await agent_tools.drain_pending_confirmations() == [], (
        "a confirm card was emitted by a path that must refuse before the gate"
    )
    return result


# ---- the merge contract ------------------------------------------------------


def test_the_body_echoes_every_readable_writable_field() -> None:
    """Correct under BOTH readings of ``required: []`` — see the module docstring."""
    body = wo.merge_update(_record(), {wo.STATUS_FIELD: 44})
    # The change landed.
    assert body[wo.STATUS_FIELD] == 44
    # Everything else the record carried AND the endpoint declares came back.
    for field in ("propertyID", "unitID", "vendorContactID", "vendorInstructions", "dateDue"):
        assert body[field] == _record()[field], field


def test_a_field_the_update_does_not_declare_is_not_echoed() -> None:
    """``workOrderID`` is on the record and not on the write surface.

    Echoing an undeclared field is at best ignored and at worst a 400 — and a
    body built by copying a GET response wholesale is how that happens.
    """
    body = wo.merge_update(_record(), {wo.STATUS_FIELD: 44})
    assert "workOrderID" not in body


def test_the_two_side_effect_triggers_are_never_echoed() -> None:
    """⚠ ``sendVendorNotification`` mails somebody else's contractor.

    They are writable fields, so a merge built from ``BODY_FIELDS`` alone would
    include them the moment a GET ever returns them. Both directions are pinned:
    the constant names exactly these two, and a record carrying them cannot get
    them into the body.
    """
    assert wo.NEVER_ECHOED == {"sendVendorNotification", "sendReview"}
    noisy = _record()
    noisy["sendVendorNotification"] = True
    noisy["sendReview"] = True
    body = wo.merge_update(noisy, {wo.STATUS_FIELD: 44})
    # ⚠ Re-pointed 2026-10-03, not relaxed: these asserted the keys were
    # ABSENT. The spec declares sendVendorNotification ``"default": true``, so an
    # absent one asks Rentvine to mail the vendor (gotcha 191). Sent FALSE is
    # the only safe body: an echo (True) and an omission (None) both fail.
    assert body.get("sendVendorNotification") is False
    assert body.get("sendReview") is False


def test_the_spec_is_why_the_triggers_are_sent_false() -> None:
    """The other side of ``wo.QUIET`` (rule 15): the captured spec declares the
    update's ``sendVendorNotification`` with ``"default": true``, so a body
    without it asks Rentvine to mail the vendor (gotcha 191)."""
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    body = spec["paths"][wo.UPDATE_PATH]["post"]["requestBody"]["content"]["application/json"]
    props = body["schema"]["properties"]
    assert props["sendVendorNotification"].get("default") is True
    assert wo.QUIET == {"sendVendorNotification": False, "sendReview": False}


@pytest.mark.asyncio
async def test_the_status_change_tells_rentvine_not_to_mail_the_vendor() -> None:
    """What Rentvine RECEIVES, not what the merge returns, and the card says it.

    ⚠ And the change still SUCCEEDS. The triggers are not state, so the re-read
    never returns them; the first cut read that absence as "cleared by the
    update" and held every status change for review. The reply is pinned
    exactly, because "Completed" alone also appears in that review reply.
    """
    await agent_tools.drain_pending_confirmations()
    client = _FakeClient()
    with ExitStack() as stack:
        _patches(stack, client)
        task = asyncio.create_task(rt._update_work_order_status_impl(_WO_ID, "Completed", _LABEL))
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        result = await asyncio.wait_for(task, timeout=2.0)
    (post,) = client.posts
    assert post["body"]["sendVendorNotification"] is False
    assert post["body"]["sendReview"] is False
    assert "Rentvine's vendor notification: off" in payload["items"]
    assert result == f"Set {_LABEL} to Completed in Rentvine.", result


def test_a_trigger_the_read_back_cannot_return_is_not_erasure() -> None:
    """``_erased_fields`` skips the triggers, and still catches a real clearing."""
    sent = {**wo.QUIET, "vendorInstructions": "Use the side door", "dateDue": "2026-10-09"}
    kept = {"vendorInstructions": "Use the side door", "dateDue": "x"}
    assert rt._erased_fields(sent, kept) == []
    cleared = {"vendorInstructions": "", "dateDue": "x"}
    assert rt._erased_fields(sent, cleared) == ["vendorInstructions"]


def test_a_null_field_is_omitted_rather_than_sent_as_null() -> None:
    """An explicit null blanks the field under BOTH readings; omission only under one."""
    body = wo.merge_update(_record(), {wo.STATUS_FIELD: 44})
    assert "closingDescription" not in body


def test_a_change_the_endpoint_does_not_declare_raises() -> None:
    """Dropping it silently would report success for an edit that never happened."""
    with pytest.raises(wo.RentvineWorkOrderError) as exc:
        wo.merge_update(_record(), {"totallyMadeUpField": 1})
    assert "totallyMadeUpField" in str(exc.value)


def test_an_empty_body_index_refuses_rather_than_sending_nothing() -> None:
    """⚠ An empty index entry means "we could not read one", not "no body".

    That is the ``$ref``/``allOf`` trap; a merge built from an empty field list
    would send ``{}`` and silently blank a whole work order under reading (b).
    """
    with patch.object(wo, "BODY_FIELDS", {}):
        with pytest.raises(wo.RentvineWorkOrderError):
            wo.writable_fields()


def test_a_status_name_resolves_against_the_accounts_own_vocabulary() -> None:
    assert wo.resolve_status(_STATUS_ROWS, "completed")[0] == 44
    assert wo.resolve_status(_STATUS_ROWS, "  In Progress  ")[0] == 7
    ident, names = wo.resolve_status(_STATUS_ROWS, "Closed")
    assert ident is None
    assert names == ["completed", "in progress", "new"]


# ---- the tool ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_writes_the_status_and_verifies_it() -> None:
    client = _FakeClient()
    result = await _run(client)
    assert len(client.posts) == 1
    post = client.posts[0]
    assert post["template"] == wo.UPDATE_PATH
    assert post["path"] == {"workOrderID": _WO_ID}
    assert post["body"][wo.STATUS_FIELD] == 44
    assert "Completed" in result
    assert str(_WO_ID) not in result  # never leak a raw id into the reply
    assert client.closed


@pytest.mark.asyncio
async def test_a_2xx_with_an_unchanged_status_is_not_a_success() -> None:
    """THE one that matters. ⚠ A 2xx is not the write (gotchas 20/70).

    ``status_after_post=None`` is an endpoint that accepts the body, answers
    200, and ignores the field — exactly what Buildium's work-order PUT does.
    The tool must say it could not confirm, and must NOT say it set anything.
    """
    client = _FakeClient(status_after_post=None)
    result = await _run(client)
    assert len(client.posts) == 1  # it did send
    assert "couldn't confirm" in result.lower()
    assert "flagging it for review" in result.lower()
    assert "set " not in result.lower()  # never the success sentence


@pytest.mark.asyncio
async def test_deny_writes_nothing() -> None:
    await agent_tools.drain_pending_confirmations()
    client = _FakeClient()
    with ExitStack() as stack:
        _patches(stack, client)
        task = asyncio.create_task(rt._update_work_order_status_impl(_WO_ID, "Completed", _LABEL))
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], False)
        result = await asyncio.wait_for(task, timeout=2.0)
    assert client.posts == []
    assert "nothing was changed" in result.lower()


@pytest.mark.asyncio
async def test_the_confirm_card_shows_the_record_label_and_the_real_body() -> None:
    """⚠ The card carries the ACTUAL body, not a summary of it.

    This endpoint may be a full replace, so an approver entitled to see "the
    status changes" is entitled to see that 9 other fields are being re-sent.
    """
    await agent_tools.drain_pending_confirmations()
    client = _FakeClient()
    with ExitStack() as stack:
        _patches(stack, client)
        task = asyncio.create_task(rt._update_work_order_status_impl(_WO_ID, "Completed", _LABEL))
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        await asyncio.wait_for(task, timeout=2.0)
    items = " | ".join(payload["items"])
    assert _LABEL in items
    assert str(_WO_ID) not in items
    body = payload["request_body"]
    assert body[wo.STATUS_FIELD] == 44
    assert body["vendorInstructions"] == _record()["vendorInstructions"]


@pytest.mark.asyncio
async def test_the_label_comes_from_the_record_not_the_models_string() -> None:
    """The record is already in hand, so the card describes what is changing.

    A card showing the model's own words instead would be a card about
    something other than the thing about to change.
    """
    await agent_tools.drain_pending_confirmations()
    client = _FakeClient()
    with ExitStack() as stack:
        _patches(stack, client)
        task = asyncio.create_task(
            rt._update_work_order_status_impl(_WO_ID, "Completed", "the model's guess")
        )
        payload = await _next_confirm_payload()
        await agent_tools.resolve_confirmation(payload["confirm_id"], True)
        await asyncio.wait_for(task, timeout=2.0)
    assert _LABEL in " | ".join(payload["items"])
    assert "the model's guess" not in " | ".join(payload["items"])


@pytest.mark.asyncio
async def test_the_dark_flag_refuses_before_anything_else() -> None:
    """⚠ Nobody approves a write that cannot happen (gotcha 67).

    Checked FIRST, so no credential is loaded, no client is built and no confirm
    card is shown — a card shown here would teach a manager that their approval
    means something it does not.
    """
    client = _FakeClient()
    result = await _refuses_before_the_gate(client, enabled=False)
    assert "isn't switched on" in result
    assert client.posts == []
    assert client.get_object_calls == 0


@pytest.mark.asyncio
async def test_a_company_without_rentvine_credentials_is_refused() -> None:
    client = _FakeClient()
    result = await _refuses_before_the_gate(client, connected=False)
    assert "isn't connected" in result
    assert client.posts == []


@pytest.mark.asyncio
async def test_the_role_floor_denies_a_member_before_any_read() -> None:
    """Stricter than the Buildium equivalent ON PURPOSE.

    Whether a closed Rentvine work order can be reopened is ``@unverified``, and
    unverified reversibility is treated as hard-to-undo. Phase 8 may relax this
    WITH EVIDENCE; it must not be relaxed for symmetry.
    """
    client = _FakeClient()
    result = await _refuses_before_the_gate(client, role="member")
    assert "Manager or Admin" in result
    assert client.posts == []
    assert client.get_object_calls == 0
    assert client.closed


@pytest.mark.asyncio
async def test_an_unverifiable_role_fails_closed() -> None:
    client = _FakeClient()
    result = await _refuses_before_the_gate(client, role=None)  # type: ignore[arg-type]
    assert "Manager or Admin" in result
    assert client.posts == []


@pytest.mark.asyncio
async def test_an_unknown_status_names_the_real_options_and_writes_nothing() -> None:
    """Pointing only at a prohibition is what makes a model improvise."""
    client = _FakeClient()
    result = await _refuses_before_the_gate(client, status="Closed")
    assert "no work-order status called 'Closed'" in result
    assert "completed" in result and "in progress" in result
    assert client.posts == []
    assert client.closed


@pytest.mark.asyncio
async def test_a_work_order_already_in_that_status_is_a_noop() -> None:
    client = _FakeClient(record=_record(status_id=44))
    result = await _refuses_before_the_gate(client)
    assert "already Completed" in result
    assert client.posts == []


#: The INSTRUCTION, not the noun.
#:
#: ⚠ Anchoring the negative on the bare word "reconnect" is the 414/416
#: false-positive class (gotchas 52/65/86/102/113): the permission message NAMES
#: reconnecting in order to rule it out — "that's fixed on the key's
#: permissions in Rentvine, not by reconnecting" — which is the best sentence in
#: the set and the one a noun-anchored assertion flags. My first cut did exactly
#: that. What separates the two messages is whether the reader is TOLD to
#: reconnect, so that is what the test reads.
_RECONNECT_INSTRUCTION = "Ask the user to reconnect Rentvine"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "must_say", "instructs_reconnect"),
    [
        (Failure.CREDENTIALS, "rejected the credentials", True),
        (Failure.PERMISSION, "key's role doesn't allow it", False),
        (Failure.NOT_FOUND, "no record with that id", False),
    ],
)
async def test_credentials_and_permission_failures_give_different_advice(
    failure: Failure, must_say: str, instructs_reconnect: bool
) -> None:
    """⚠ 401 and 403 are different questions (gotcha 102).

    A 403 means the credential was read, accepted and refused for THIS
    operation — telling somebody to reconnect sends them to redo a connection
    that works. Rentvine makes it sharper: its API keys carry a role the
    customer picks at creation, so a restricted key is an expected state.
    """
    err = RentvineAPIError(403, failure, False, "nope")
    client = _FakeClient(get_object_error=err)
    result = await _refuses_before_the_gate(client)
    assert must_say in result
    assert (_RECONNECT_INSTRUCTION in result) is instructs_reconnect
    assert client.posts == []
    assert client.closed


@pytest.mark.asyncio
async def test_the_client_is_closed_even_when_the_write_raises() -> None:
    err = RentvineAPIError(500, Failure.UNDOCUMENTED, True, "boom")
    client = _FakeClient(post_error=err)
    result = await _run(client)
    assert "Nothing was changed in Rentvine" in result
    assert client.closed


# ---- the registry ------------------------------------------------------------


def test_the_registry_holds_exactly_the_reviewed_writes() -> None:
    """Re-pointed 2026-10-03 from "exactly one tool": the founder added four
    (docs/decisions.md). Still the EXACT set, so a sixth is a deliberate edit,
    and every name is one each negative case forbids. The status write stays
    first: the description test below reads ``rentvine_tools()[0]``."""
    names = [_tool_name(t) for t in rt.rentvine_tools()]
    assert names == [
        "rentvine_update_work_order_status",
        "rentvine_create_work_order",
        "rentvine_upload_file",
        "rentvine_post_charge",
        "rentvine_record_payment",
    ]


def test_the_tool_description_tells_the_model_it_is_confirm_gated() -> None:
    """The description is a contract with the model, not a comment.

    ⚠ Read off ``.description`` — under the real SDK ``@function_tool`` returns a
    ``FunctionTool`` and the docstring is no longer on ``__doc__`` (gotcha 130).
    No fallback: a container whose SDK cannot answer this must fail loudly
    rather than pass vacuously.
    """
    tool = rt.rentvine_tools()[0]
    raw = getattr(tool, "description", None)
    assert isinstance(raw, str) and raw, "the SDK did not expose a tool description"
    # ⚠ Whitespace is COLLAPSED first. A docstring is hard-wrapped, so "Manager
    # or Admin" arrives as "Manager or\nAdmin" and a literal substring test
    # reports the correct description as missing the phrase — gotcha 77's rule
    # (a required phrase split by markup is invisible to a substring scan), and
    # the model reads the rendered text, not the line breaks.
    text = " ".join(raw.split())
    assert "confirm" in text.lower()
    assert "Manager or Admin" in text
    # The status is a NAME, and the description has to say so — a model reading
    # "status" alone hands over an id and moves the work order to whatever
    # status holds that number on this account.
    assert "not a number" in text


def _tool_name(tool: Any) -> str:
    for attr in ("name", "__name__"):
        value = getattr(tool, attr, None)
        if isinstance(value, str) and value:
            return value
    raise AssertionError(f"cannot read a name off {tool!r}")
