"""A gated write reuses its client after a read closed it. That has to work.

`httpx.AsyncClient` cannot be reopened:

    RuntimeError: Cannot send a request, as the client has been closed.

Every confirm-gated Buildium write does at least TWO separate
`async with client:` blocks on the SAME client — one to read the record for
the confirm card (usually inside `_gate_label`, the Whitfield fix that makes
the card derive from the record instead of the model), and one inside the
`execute()` closure `run_guarded` calls after approval.

The first closed the transport. The second raised. `run_guarded` caught it as
ambiguous and reaped the claim `abandoned_unknown`, which is **never retried
by design**. Measured live 2026-08-30: every gated write in the reversible and
additive tiers failed exactly this way (`ambiguous:RuntimeError`). The one
exception is the tell — `create_work_order` writes BEFORE it verifies, so its
write landed and only the read-back raised (`verify_error:RuntimeError`).

⚠ It read as a network fault for two full runs because the agent had a bare
RuntimeError to explain and told users there had been a "connection error".
The reply text was the only signal anyone had, and it was a confabulation.
"""

from __future__ import annotations

import httpx
import pytest

from helixis.buildium.client import BuildiumClient
from helixis.settings import BuildiumSettings


def _settings() -> BuildiumSettings:
    return BuildiumSettings(client_id="cid", client_secret="secret")


def _transport(calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


def _mocked(calls: list[str]) -> BuildiumClient:
    """A client whose transport is mocked INCLUDING on rebuild.

    ⚠ Assigning ``client._client`` directly is not enough: ``_owns_client``
    stays True, so a rebuild calls the real factory and issues a LIVE request.
    My first cut of this file did exactly that and the two rebuild tests failed
    with a real 403 -- which looked like the fix was broken when in fact the
    rebuild had worked perfectly and the test simply could not observe it.
    Patching the factory is what makes the rebuilt transport observable.
    """
    client = BuildiumClient(_settings())
    client._new_http_client = lambda: httpx.AsyncClient(  # type: ignore[method-assign]
        base_url="https://x/v1", transport=_transport(calls)
    )
    client._client = client._new_http_client()
    return client


# ── the defect, at the shape it actually occurred ─────────────────────


@pytest.mark.asyncio
async def test_a_write_after_a_read_closed_the_client_still_works() -> None:
    """THE regression. Read under one `async with`, then write under another —
    which is what every gated write does, and what failed on every one."""
    calls: list[str] = []
    client = _mocked(calls)

    async with client:  # the confirm-card read
        await client.request("GET", "/tasks/1")

    # `execute()` runs here, after the read's context has exited.
    async with client:  # the write
        await client.request("PUT", "/tasks/1")

    assert len(calls) == 2, "the write never reached the transport"


@pytest.mark.asyncio
async def test_a_write_outside_any_context_still_works() -> None:
    """The `execute()` closure may run with no surrounding `async with` at all
    — the nesting count cannot help there, so the transport must rebuild on
    demand at request time."""
    calls: list[str] = []
    client = _mocked(calls)

    async with client:
        await client.request("GET", "/vendors/1")
    await client.aclose()

    await client.request("POST", "/vendors/1/notes")
    assert len(calls) == 2


# ── nesting ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_inner_helper_cannot_close_the_transport_on_its_caller() -> None:
    """`_gate_label` enters the client itself. Before the nesting count, its
    exit closed the transport mid-way through the caller's own block."""
    calls: list[str] = []
    client = _mocked(calls)

    async with client:
        async with client:  # a helper doing its own `async with`
            await client.request("GET", "/vendors/1")
        # The helper has returned. The caller's transport must still be live.
        assert not client._client.is_closed, "an inner exit closed the caller's transport"
        await client.request("GET", "/vendors/2")
    assert client._client.is_closed, "the outermost exit did not close it"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_aclose_is_idempotent() -> None:
    client = _mocked([])
    await client.aclose()
    await client.aclose()  # must not raise


# ── the boundary that must NOT move ───────────────────────────────────


@pytest.mark.asyncio
async def test_an_injected_transport_is_never_rebuilt() -> None:
    """⚠ Tests pass their own client. Silently swapping a closed injected
    transport for a freshly-built real one would turn a unit test into a LIVE
    Buildium call against whatever credentials the settings carry — a far worse
    failure than the one being fixed."""
    calls: list[str] = []
    injected = httpx.AsyncClient(base_url="https://x/v1", transport=_transport(calls))
    client = BuildiumClient(_settings(), http_client=injected)

    async with client:
        await client.request("GET", "/tasks/1")

    assert client._client is injected, "the injected transport was replaced"
    await injected.aclose()
    client._ensure_open()
    assert client._client is injected, "a closed injected transport was rebuilt"


@pytest.mark.asyncio
async def test_an_injected_transport_is_not_closed_by_the_context() -> None:
    """The pre-existing `_owns_client` contract, pinned so the nesting change
    cannot quietly take ownership of somebody else's transport."""
    injected = httpx.AsyncClient(base_url="https://x/v1", transport=_transport([]))
    client = BuildiumClient(_settings(), http_client=injected)
    async with client:
        pass
    assert not injected.is_closed
    await injected.aclose()
