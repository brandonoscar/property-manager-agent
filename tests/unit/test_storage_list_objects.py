"""The one call in `storage.py` that was never live-probed, and the one whose
failure mode is silence.

`supabase.co` is egress-blocked from the container this was written in (gotcha
5), so the list endpoint's response shape could not be confirmed the way
upload / sign / download / delete were. Gotcha 79 is the standing record of
what guessing costs: an SDK class name read as a REST route 404'd the whole
A2P chain for as long as it existed.

So the walker is built to be correct under EITHER documented shape, and these
are what make that claim checkable rather than a docstring — the keepalive in
`sse.py` is the precedent (documented for months, never written to the wire,
no test asserted a byte).

⚠ The shape that matters is the nested one. `browser_screenshots` keys objects
``{company_id}/{session_id}/{step}.png`` — two levels below the prefix the
purge sweeps. A flat single call against a folder-returning API finds three
folder names, deletes nothing, and reports a clean sweep: the 200-but-lie
class (gotchas 20/70/75) arriving inside an erasure proof.
"""

from __future__ import annotations

import json

import httpx
import pytest

from helixis import storage
from helixis.storage import StorageError, list_objects


class _Settings:
    class supabase:  # noqa: N801
        url = "https://example.supabase.co"
        service_key = "svc"


def _install(monkeypatch, handler) -> list[dict]:
    """Route the module's client at a MockTransport and record every request.

    ⚠ The real class is captured BEFORE the patch. Building the replacement out
    of `httpx.AsyncClient` after `setattr` makes the factory call itself — a
    RecursionError far from anything the test is about, which reads as "this
    cannot be tested" rather than "one line is wrong".
    """
    real_client = httpx.AsyncClient
    seen: list[dict] = []

    def _factory(*_a, **_kw):
        def _h(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode() or "{}")
            seen.append({"url": str(request.url), "body": body})
            return handler(request, body)

        return real_client(transport=httpx.MockTransport(_h))

    monkeypatch.setattr(storage.httpx, "AsyncClient", _factory)
    return seen


def _file(name: str) -> dict:
    return {"name": name, "id": "obj-" + name, "metadata": {"size": 1}}


def _folder(name: str) -> dict:
    """What Supabase returns for a nested path: no id, no metadata."""
    return {"name": name, "id": None, "metadata": None}


@pytest.mark.asyncio
async def test_a_flat_listing_is_one_pass(monkeypatch) -> None:
    def handler(_req, body):
        if body.get("offset", 0) == 0:
            return httpx.Response(200, json=[_file("a.pdf"), _file("b.pdf")])
        return httpx.Response(200, json=[])

    _install(monkeypatch, handler)
    got = await list_objects(bucket="company_documents", prefix="c1", settings=_Settings())
    assert sorted(got) == ["c1/a.pdf", "c1/b.pdf"]


@pytest.mark.asyncio
async def test_a_nested_listing_is_walked_rather_than_reported_empty(monkeypatch) -> None:
    """THE property. Under the folder-returning shape a single flat call finds
    two session folders and zero files — and a purge built on it deletes
    nothing while reporting success."""

    def handler(_req, body):
        prefix = body.get("prefix", "")
        if body.get("offset", 0):
            return httpx.Response(200, json=[])
        if prefix == "c1/":
            return httpx.Response(200, json=[_folder("sess-a"), _folder("sess-b")])
        if prefix == "c1/sess-a/":
            return httpx.Response(200, json=[_file("1.png"), _file("2.png")])
        if prefix == "c1/sess-b/":
            return httpx.Response(200, json=[_file("1.png")])
        return httpx.Response(200, json=[])

    _install(monkeypatch, handler)
    got = await list_objects(bucket="browser_screenshots", prefix="c1", settings=_Settings())
    assert sorted(got) == ["c1/sess-a/1.png", "c1/sess-a/2.png", "c1/sess-b/1.png"]


@pytest.mark.asyncio
async def test_a_file_whose_id_is_empty_is_still_a_file(monkeypatch) -> None:
    """⚠ The distinguishing input, and a mutation battery is what found it.

    `not entry.get("id")` reads as the obvious folder test and is wrong: an API
    that returns an empty-string id for files reclassifies EVERY file as a
    folder, the walk descends into paths that do not exist, and the sweep
    returns nothing — while still reporting a successful enumeration. Testing
    for the absence of BOTH id and metadata separates them.
    """

    def handler(_req, body):
        if body.get("offset", 0) or body.get("prefix") != "c1/":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[{"name": "a.pdf", "id": "", "metadata": {"size": 1}}])

    _install(monkeypatch, handler)
    got = await list_objects(bucket="company_documents", prefix="c1", settings=_Settings())
    assert got == ["c1/a.pdf"], "an empty id made a real file invisible to the purge"


@pytest.mark.asyncio
async def test_pagination_keeps_going_past_the_first_page(monkeypatch) -> None:
    page = storage._LIST_PAGE

    def handler(_req, body):
        off = body.get("offset", 0)
        if off == 0:
            return httpx.Response(200, json=[_file(f"f{i}.pdf") for i in range(page)])
        if off == page:
            return httpx.Response(200, json=[_file("last.pdf")])
        return httpx.Response(200, json=[])

    _install(monkeypatch, handler)
    got = await list_objects(bucket="company_documents", prefix="c1", settings=_Settings())
    assert len(got) == page + 1
    assert "c1/last.pdf" in got


@pytest.mark.asyncio
async def test_a_404_raises_rather_than_returning_empty(monkeypatch) -> None:
    """The single most important direction. If the endpoint spelling is wrong —
    which is the acknowledged risk — the caller must see an error it can report
    as UNVERIFIED, never an empty list it reports as a clean sweep."""

    def handler(_req, _body):
        return httpx.Response(404, json={"error": "not found"})

    _install(monkeypatch, handler)
    with pytest.raises(StorageError) as exc:
        await list_objects(bucket="company_documents", prefix="c1", settings=_Settings())
    assert "404" in str(exc.value)


@pytest.mark.asyncio
async def test_a_non_list_body_raises(monkeypatch) -> None:
    """A proxy error page or a changed envelope must not iterate as zero
    entries — that reads as empty, which is the failure being guarded."""

    def handler(_req, _body):
        return httpx.Response(200, json={"objects": []})

    _install(monkeypatch, handler)
    with pytest.raises(StorageError):
        await list_objects(bucket="company_documents", prefix="c1", settings=_Settings())


@pytest.mark.asyncio
async def test_the_request_goes_to_the_list_endpoint_with_the_prefix(monkeypatch) -> None:
    """Assert the bytes on the wire, not the intent (the a2p `MessageSamples`
    lesson: producer and consumer both right, the encoding between them wrong)."""

    def handler(_req, _body):
        return httpx.Response(200, json=[])

    seen = _install(monkeypatch, handler)
    await list_objects(bucket="company_documents", prefix="c1", settings=_Settings())
    assert seen, "no request was made"
    assert seen[0]["url"].endswith("/storage/v1/object/list/company_documents")
    assert seen[0]["body"]["prefix"] == "c1/"


@pytest.mark.asyncio
async def test_unconfigured_storage_raises_rather_than_reporting_empty(monkeypatch) -> None:
    class _Bare:
        class supabase:  # noqa: N801
            url = ""
            service_key = ""

    with pytest.raises(StorageError):
        await list_objects(bucket="company_documents", prefix="c1", settings=_Bare())
