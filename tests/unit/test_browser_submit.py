"""Browser input primitives: select_option (ungated) + submit_form (GATED).

Asserts at the ACTION boundary (the Playwright call on the page), mirroring
the Buildium write-gate suite:

  * select_option — reversible, NOT gated: the page action runs directly.
  * submit_form   — irreversible, GATED via confirm_or_abort:
      approve            → the submit click fires exactly once
      deny               → the click NEVER fires
      timeout/queue-full → the click NEVER fires (fail closed)
      and confirm_or_abort is invoked BEFORE the click.

CI-safe: no Browserbase, no [browser] extra, no creds. A fake page/locator
stands in for Playwright; the confirm gate is driven through the real
module-level emission/resolve channel in helixis.agents.tools.
"""

from __future__ import annotations

import asyncio

import pytest

from helixis.agents import browser_session as bs
from helixis.agents import browser_tools as bt
from helixis.agents import idempotency as idem
from helixis.agents import tools as agent_tools
from helixis.memory.context import memory_context
from helixis.settings import Settings


def _settings() -> Settings:
    return Settings(env="test", memory={"enabled": False})


class _FakeLocator:
    """Records whether each Playwright action fired (the boundary spy)."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.select_calls = 0
        self.click_calls = 0
        self.fill_calls = 0

    @property
    def first(self) -> _FakeLocator:
        return self

    async def fill(self, value: str, timeout: int | None = None) -> None:
        if self._fail:
            raise RuntimeError("boom")
        self.fill_calls += 1

    async def select_option(self, value: str, timeout: int | None = None) -> None:
        if self._fail:
            raise RuntimeError("boom")
        self.select_calls += 1

    async def click(self, timeout: int | None = None) -> None:
        if self._fail:
            raise RuntimeError("boom")
        self.click_calls += 1


class _FakeAccessibility:
    """Minimal stand-in for ``page.accessibility`` with a configurable tree.

    ``tree`` is the dict ``snapshot()`` returns (None → unreadable page, which
    the Cut-2 verifier treats as needs-review)."""

    def __init__(self, tree: dict | None) -> None:
        self._tree = tree

    async def snapshot(self) -> dict | None:
        return self._tree


class _FakePage:
    def __init__(
        self,
        *,
        fail: bool = False,
        url: str = "https://court.example.gov/evict",
        post_submit_url: str | None = None,
        post_submit_tree: dict | None = None,
    ) -> None:
        self._loc = _FakeLocator(fail=fail)
        self.url = url
        # After the submit click, the verifier reads url + accessibility tree.
        # By default a confirmation page so existing approve-path tests still
        # report ``submitted``; tests override these to model failure/unknown.
        self._post_submit_url = post_submit_url or "https://court.example.gov/evict/confirmation"
        if post_submit_tree is None:
            post_submit_tree = {
                "role": "WebArea",
                "name": "Eviction Filed",
                "children": [
                    {"role": "heading", "name": "Filing confirmation number 12345"},
                ],
            }
        self._post_submit_tree = post_submit_tree
        self.accessibility = _FakeAccessibility(post_submit_tree)
        self._clicked = False

    def locator(self, selector: str) -> _FakeLocator:
        return self._loc

    @property
    def loc(self) -> _FakeLocator:
        return self._loc

    async def wait_for_load_state(self, state: str = "load", timeout: int | None = None) -> None:
        # On settle, advance the URL to the post-submit page (models the
        # redirect a real filing causes).
        self.url = self._post_submit_url


def _bind_session(monkeypatch: pytest.MonkeyPatch, page: _FakePage) -> bs.BrowserSession:
    sess = bs.BrowserSession(
        company_id="co-1",
        session_id="sess-1",
        started_at=bs.time.monotonic(),
        page=page,
    )
    monkeypatch.setattr(bs, "current_session", lambda: sess)
    # browser_tools imported current_session by name — patch there too.
    monkeypatch.setattr("helixis.agents.browser_tools.current_session", lambda: sess)
    return sess


async def _resolve_next(approved: bool) -> None:
    """Yield until submit_form emits its confirm payload, then resolve it."""
    for _ in range(50):
        await asyncio.sleep(0)
        drained = await agent_tools.drain_pending_confirmations()
        if drained:
            await agent_tools.resolve_confirmation(drained[0]["confirm_id"], approved)
            return
    raise AssertionError("submit_form did not emit a confirm payload")


# ── select_option: ungated, runs directly ───────────────────────────────────


@pytest.mark.asyncio
async def test_select_option_runs_without_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage()
    _bind_session(monkeypatch, page)
    await agent_tools.drain_pending_confirmations()

    result = await bt._select_option_impl("select#county", "Henrico", settings=_settings())

    assert result.status == "ok"
    assert page.loc.select_calls == 1
    # Reversible → no confirm payload emitted.
    assert await agent_tools.drain_pending_confirmations() == []


@pytest.mark.asyncio
async def test_select_option_unavailable_without_session() -> None:
    result = await bt._select_option_impl("select#x", "v", settings=_settings())
    assert result.status == "browser_unavailable"


@pytest.mark.asyncio
async def test_select_option_empty_value_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage()
    _bind_session(monkeypatch, page)
    result = await bt._select_option_impl("select#x", "", settings=_settings())
    assert result.status == "invalid_input"
    assert page.loc.select_calls == 0


@pytest.mark.asyncio
async def test_select_option_never_logs_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """The chosen value is sensitive — result reports a length, not the value."""
    page = _FakePage()
    _bind_session(monkeypatch, page)
    secret = "Tenant-Sensitive-Choice"
    result = await bt._select_option_impl("select#x", secret, settings=_settings())
    assert result.status == "ok"
    assert result.value_count == len(secret)
    assert secret not in result.model_dump_json()


# ── submit_form: GATED ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_approve_clicks_once(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage()
    _bind_session(monkeypatch, page)
    await agent_tools.drain_pending_confirmations()

    task = asyncio.create_task(bt._submit_form_impl("button#file", settings=_settings()))
    # The click must NOT have fired before approval resolves.
    await asyncio.sleep(0)
    assert page.loc.click_calls == 0
    await _resolve_next(True)
    result = await asyncio.wait_for(task, timeout=1.0)

    assert result.status == "submitted"
    assert page.loc.click_calls == 1


@pytest.mark.asyncio
async def test_submit_deny_does_not_click(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _FakePage()
    _bind_session(monkeypatch, page)
    await agent_tools.drain_pending_confirmations()

    task = asyncio.create_task(bt._submit_form_impl("button#file", settings=_settings()))
    await _resolve_next(False)
    result = await asyncio.wait_for(task, timeout=1.0)

    assert result.status == "not_submitted"
    assert page.loc.click_calls == 0


@pytest.mark.asyncio
async def test_submit_timeout_does_not_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """No user response before timeout → fail closed, no click. Patch
    confirm_or_abort (now reached via run_guarded → idempotency) to a fast
    timeout-equivalent."""
    page = _FakePage()
    _bind_session(monkeypatch, page)

    async def _timed_out(**_kwargs: object) -> bool:
        return False

    monkeypatch.setattr("helixis.agents.idempotency.confirm_or_abort", _timed_out)
    result = await bt._submit_form_impl("button#file", settings=_settings())

    assert result.status == "not_submitted"
    assert page.loc.click_calls == 0


@pytest.mark.asyncio
async def test_submit_calls_confirm_before_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """confirm_or_abort must be invoked BEFORE the page click — and the click
    only fires when it approves."""
    page = _FakePage()
    _bind_session(monkeypatch, page)
    order: list[str] = []

    async def _spy_confirm(**_kwargs: object) -> bool:
        order.append("confirm")
        # At confirm time, the click must not have happened yet.
        assert page.loc.click_calls == 0
        return True

    monkeypatch.setattr("helixis.agents.idempotency.confirm_or_abort", _spy_confirm)
    result = await bt._submit_form_impl("button#file", settings=_settings())

    assert order == ["confirm"]
    assert result.status == "submitted"
    assert page.loc.click_calls == 1


@pytest.mark.asyncio
async def test_submit_unavailable_without_session() -> None:
    result = await bt._submit_form_impl("button#file", settings=_settings())
    assert result.status == "browser_unavailable"


@pytest.mark.asyncio
async def test_submit_invalid_selector_does_not_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad selector is rejected before the gate — no confirm emitted."""
    page = _FakePage()
    _bind_session(monkeypatch, page)
    await agent_tools.drain_pending_confirmations()

    result = await bt._submit_form_impl("   ", settings=_settings())

    assert result.status == "invalid_input"
    assert page.loc.click_calls == 0
    assert await agent_tools.drain_pending_confirmations() == []


# ── confirm-card content: masked field values ───────────────────────────────


def test_mask_value_reveals_only_first_and_last() -> None:
    """Masking reveals first + last char, hides the interior AND the length."""
    assert bt._mask_value("Maria Lopez") == "M•••z"
    assert bt._mask_value("Orange") == "O•••e"
    # Length is masked too: different-length values mask to the same width.
    assert len(bt._mask_value("Orange")) == len(bt._mask_value("Maria Lopez"))
    # Short values are fully masked; nothing of the raw value survives.
    assert bt._mask_value("AB") == "••"
    assert bt._mask_value("X") == "•"
    assert bt._mask_value("") == ""


def test_submit_confirm_preview_includes_masked_field_values() -> None:
    """The confirm card shows form identity AND one masked row per filled
    field — raw values never appear, masked values do."""
    filled = [("textbox: Tenant name", bt._mask_value("Maria Lopez"))]
    details, items = bt._submit_confirm_details(
        "https://court.example.gov/evict", "button#file", filled
    )
    assert "court.example.gov" in details
    assert "1 field" in details
    assert any("Site:" in i for i in items)
    assert any("button#file" in i for i in items)
    # The field row is present, masked, and leaks no raw value.
    assert any("textbox: Tenant name" in i for i in items)
    assert any("M•••z" in i for i in items)
    assert "Maria Lopez" not in "\n".join(items)
    assert "Maria" not in "\n".join(items)


def test_submit_confirm_preview_identity_only_when_no_fields() -> None:
    """With no recorded fields, the card falls back to identity-only."""
    details, items = bt._submit_confirm_details("https://court.example.gov/evict", "button#file")
    assert items == ["Site: https://court.example.gov/evict", "Submit control: button#file"]
    assert "field" not in details


@pytest.mark.asyncio
async def test_type_into_records_masked_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """type_into populates the ephemeral confirm buffer with a MASKED value;
    the raw text is never retained on the session."""
    page = _FakePage()
    sess = _bind_session(monkeypatch, page)

    result = await bt._type_into_impl("textbox#tenant", "Maria Lopez", settings=_settings())

    assert result.status == "ok"
    assert page.loc.fill_calls == 1
    assert sess.filled_fields == [("textbox#tenant", "M•••z")]
    # Raw value is nowhere in the ephemeral record.
    assert "Maria Lopez" not in repr(sess.filled_fields)


@pytest.mark.asyncio
async def test_type_into_correction_overwrites_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-filling the same field overwrites (latest wins), not appends."""
    page = _FakePage()
    sess = _bind_session(monkeypatch, page)

    await bt._type_into_impl("textbox#tenant", "Maria Lopez", settings=_settings())
    await bt._type_into_impl("textbox#tenant", "Bob Smith", settings=_settings())

    assert sess.filled_fields == [("textbox#tenant", "B•••h")]


@pytest.mark.asyncio
async def test_submit_confirm_payload_masks_filled_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through the real confirm channel: after filling a field,
    the emitted confirm payload carries the MASKED value and never the raw."""
    page = _FakePage()
    _bind_session(monkeypatch, page)
    await agent_tools.drain_pending_confirmations()

    await bt._type_into_impl("textbox#tenant", "Maria Lopez", settings=_settings())

    task = asyncio.create_task(bt._submit_form_impl("button#file", settings=_settings()))
    # Capture the emitted payload, then approve so the task completes.
    payload = None
    for _ in range(50):
        await asyncio.sleep(0)
        drained = await agent_tools.drain_pending_confirmations()
        if drained:
            payload = drained[0]
            await agent_tools.resolve_confirmation(payload["confirm_id"], True)
            break
    result = await asyncio.wait_for(task, timeout=1.0)

    assert payload is not None
    blob = "\n".join(payload["items"]) + payload["details"]
    assert "M•••z" in blob
    assert "Maria Lopez" not in blob
    assert result.status == "submitted"


@pytest.mark.asyncio
async def test_submit_store_unavailable_does_not_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    """#227: required=True + no store → fail closed BEFORE the click. The form
    is NOT submitted (status not_submitted), and the click never fires. Guards
    the new blocked_store_unavailable outcome — without the caller arm it would
    fall through to the success/'submitted' path."""
    page = _FakePage()
    _bind_session(monkeypatch, page)
    await agent_tools.drain_pending_confirmations()
    monkeypatch.setattr(idem, "store_from_pool", lambda: None)
    monkeypatch.setattr(idem, "_idempotency_required", lambda: True)

    with memory_context(company_id="co-1", user_id="u", session_id="sess-1"):
        result = await bt._submit_form_impl("button#file", settings=_settings())

    assert result.status == "not_submitted"
    assert page.loc.click_calls == 0  # fail-closed before the click
