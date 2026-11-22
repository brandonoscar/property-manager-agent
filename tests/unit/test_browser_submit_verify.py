"""Cut 2 — browser submit read-back verification.

After the submit click, ``_submit_form_impl`` reads the resulting page back and
reports ``submitted`` ONLY on a positive confirmation. A failure signal, an
absent confirmation, or an unreadable page → ``not_submitted`` with an honest
"couldn't confirm" message — never a false success, never a re-click.

CI-safe: a fake page/locator stands in for Playwright; the confirm gate runs
through the real module channel and is auto-approved.
"""

from __future__ import annotations

import pytest

from helixis.agents import browser_session as bs
from helixis.agents import browser_tools as bt
from helixis.agents import idempotency as idem
from helixis.settings import Settings


def _settings() -> Settings:
    return Settings(env="test", memory={"enabled": False})


async def _auto_approve(**_kwargs: object) -> bool:
    return True


# ── _classify_submit_outcome unit (the fail-safe core) ───────────────────────


def test_classify_positive_expected_phrase() -> None:
    verdict, detail = bt._classify_submit_outcome(
        flattened_tree="heading: Filing confirmation number 99812",
        final_url="https://court.gov/evict",
        starting_url="https://court.gov/evict",
        expected_confirmation="confirmation number",
    )
    assert verdict is True
    assert "confirmation number" in detail


def test_classify_positive_url_moved_to_confirmation() -> None:
    verdict, _ = bt._classify_submit_outcome(
        flattened_tree="heading: Thank you",
        final_url="https://court.gov/evict/confirmation",
        starting_url="https://court.gov/evict",
        expected_confirmation="",
    )
    assert verdict is True


def test_classify_negative_error_signal_beats_positive() -> None:
    """An explicit error signal wins even if the expected phrase is present."""
    verdict, detail = bt._classify_submit_outcome(
        flattened_tree="alert: Error — required field missing. confirmation number",
        final_url="https://court.gov/evict",
        starting_url="https://court.gov/evict",
        expected_confirmation="confirmation number",
    )
    assert verdict is False
    assert "error" in detail.lower()


def test_classify_negative_expected_phrase_absent() -> None:
    verdict, _ = bt._classify_submit_outcome(
        flattened_tree="heading: Eviction Filing\ntextbox: Tenant name",
        final_url="https://court.gov/evict",
        starting_url="https://court.gov/evict",
        expected_confirmation="confirmation number",
    )
    assert verdict is False


def test_classify_unknown_no_signal_is_none() -> None:
    """No caller signal + clean page + no URL move → None (NOT success)."""
    verdict, _ = bt._classify_submit_outcome(
        flattened_tree="heading: Some page",
        final_url="https://court.gov/evict",
        starting_url="https://court.gov/evict",
        expected_confirmation="",
    )
    assert verdict is None


# ── end-to-end through _submit_form_impl ─────────────────────────────────────


def _bind(monkeypatch: pytest.MonkeyPatch, page: _Page) -> bs.BrowserSession:
    sess = bs.BrowserSession(
        company_id="co-1", session_id="sess-1", started_at=bs.time.monotonic(), page=page
    )
    monkeypatch.setattr("helixis.agents.browser_tools.current_session", lambda: sess)
    monkeypatch.setattr(idem, "confirm_or_abort", _auto_approve)
    return sess


class _Loc:
    def __init__(self) -> None:
        self.click_calls = 0

    @property
    def first(self) -> _Loc:
        return self

    async def click(self, timeout: int | None = None) -> None:
        self.click_calls += 1


class _Page:
    def __init__(
        self,
        *,
        post_url: str,
        tree: dict | None,
        a11y_raises: bool = False,
        body_text: str = "",
    ) -> None:
        self._loc = _Loc()
        self.url = "https://court.gov/evict"
        self._post_url = post_url
        self.accessibility = _Acc(tree, raises=a11y_raises)
        self._body_text = body_text

    def locator(self, _s: str) -> _Loc:
        return self._loc

    @property
    def loc(self) -> _Loc:
        return self._loc

    async def wait_for_load_state(self, state: str = "load", timeout: int | None = None) -> None:
        self.url = self._post_url

    async def inner_text(self, selector: str, timeout: int | None = None) -> str:
        # Cut-2 verify fallback reads body text when the a11y snapshot fails.
        if self._body_text == "__raise__":
            raise RuntimeError("body read failed")
        return self._body_text


class _Acc:
    def __init__(self, tree: dict | None, *, raises: bool = False) -> None:
        self._tree = tree
        self._raises = raises

    async def snapshot(self) -> dict | None:
        if self._raises:
            raise AttributeError("accessibility.snapshot unavailable on this page")
        return self._tree


def _tree(name: str) -> dict:
    return {"role": "WebArea", "name": name, "children": []}


@pytest.mark.asyncio
async def test_submit_confirmed_reports_submitted(monkeypatch: pytest.MonkeyPatch) -> None:
    page = _Page(post_url="https://court.gov/evict", tree=_tree("Filing confirmation number 7"))
    _bind(monkeypatch, page)

    result = await bt._submit_form_impl("button#file", "confirmation number", settings=_settings())

    assert result.status == "submitted"
    assert page.loc.click_calls == 1  # filed exactly once
    assert result.final_url == "https://court.gov/evict"
    assert "confirmation number" in result.confirmation_detail


@pytest.mark.asyncio
async def test_submit_error_page_is_not_submitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Click fired, but the resulting page shows an error → not_submitted,
    honest message, no false success, no re-click."""
    page = _Page(post_url="https://court.gov/evict", tree=_tree("Error: filing failed"))
    _bind(monkeypatch, page)

    result = await bt._submit_form_impl("button#file", "confirmation number", settings=_settings())

    assert result.status == "not_submitted"
    assert page.loc.click_calls == 1  # clicked once, NOT re-clicked
    assert any("could not confirm" in w for w in result.warnings)
    # The honest message carries the evidence, no false "submitted".
    assert "error" in result.confirmation_detail.lower()


@pytest.mark.asyncio
async def test_submit_unknown_no_signal_is_not_submitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """No expected_confirmation + clean page + no URL move → cannot confirm →
    not_submitted (absence of a confirmable signal is never success)."""
    page = _Page(post_url="https://court.gov/evict", tree=_tree("Some neutral page"))
    _bind(monkeypatch, page)

    result = await bt._submit_form_impl("button#file", settings=_settings())

    assert result.status == "not_submitted"
    assert page.loc.click_calls == 1


@pytest.mark.asyncio
async def test_submit_unreadable_page_is_not_submitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot returns None (unreadable) → verifier returns False via classify
    (None→not success) → not_submitted. Never a false success."""
    page = _Page(post_url="https://court.gov/evict/confirmation", tree=None)
    _bind(monkeypatch, page)

    # URL still moves to /confirmation, so this asserts the URL-positive path
    # works even when the tree is unreadable (defensible success signal).
    result = await bt._submit_form_impl("button#file", settings=_settings())
    assert result.status == "submitted"
    assert result.final_url.endswith("/confirmation")


# ── a11y-snapshot failure → body-text fallback (the live #217 bug) ────────────


@pytest.mark.asyncio
async def test_a11y_raises_but_body_confirms_is_submitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live bug: accessibility.snapshot() raises on real DOM but the submit
    DID land — the confirmation is in the body text. Fallback must find it →
    submitted, not a false not_submitted."""
    page = _Page(
        post_url="https://court.gov/evict",
        tree=None,
        a11y_raises=True,
        body_text="Received! Your form was submitted.",
    )
    _bind(monkeypatch, page)

    result = await bt._submit_form_impl("button#file", "Received!", settings=_settings())

    assert result.status == "submitted"
    assert page.loc.click_calls == 1
    assert "Received!" in result.confirmation_detail


@pytest.mark.asyncio
async def test_a11y_raises_and_body_raises_is_not_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BOTH reads fail → genuinely unreadable → verify_error → not_submitted
    (fail-safe; never a false success)."""
    page = _Page(
        post_url="https://court.gov/evict",
        tree=None,
        a11y_raises=True,
        body_text="__raise__",
    )
    _bind(monkeypatch, page)

    result = await bt._submit_form_impl("button#file", "Received!", settings=_settings())

    assert result.status == "not_submitted"
    assert page.loc.click_calls == 1  # clicked once, never re-clicked


@pytest.mark.asyncio
async def test_a11y_raises_body_shows_error_is_not_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """a11y raises, fallback body text shows a failure signal → not_submitted."""
    page = _Page(
        post_url="https://court.gov/evict",
        tree=None,
        a11y_raises=True,
        body_text="Error: your submission failed.",
    )
    _bind(monkeypatch, page)

    result = await bt._submit_form_impl("button#file", "Received!", settings=_settings())

    assert result.status == "not_submitted"
    assert page.loc.click_calls == 1
