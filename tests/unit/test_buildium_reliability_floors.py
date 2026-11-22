"""Reliability *floor* tests for the Buildium surface.

These are auto-covering regression guards for the two failure classes the
unit suite otherwise mocks away — a read that silently truncates at
Buildium's first page, and a tool that leaks Buildium's raw error English
into the property manager's chat. Both are floors: they discover the
surface by introspection, so a NEW list method or a NEW tool is covered
the moment it lands, without anyone remembering to add a case here.

Two guards:

1. **No-silent-truncation (pagination floor).**  Every collection-returning
   endpoint method (``list_*`` / ``get_all*`` / ``search_*``) must route
   through ``BuildiumClient.list_all`` / ``paginate`` — never a bare
   ``self._client.request("GET", ...)`` that returns only page one. An
   AST scan of ``buildium/endpoints/*.py`` enforces this; a behavioural
   test proves the schemaless (raw-dict) pagination path added for the
   note/history/service-history collections actually collects every page.

2. **No-raw-error-leak (credential-UX floor).**  Every read tool that
   catches ``BuildiumApiError`` must return it through
   ``_sanitize_buildium_error`` — never ``str(exc)``. We drive every such
   ``_*_impl`` with an API whose calls all raise a 401 carrying a unique
   leak sentinel, and assert the sentinel (and the fake settings path, and
   any traceback) never reaches the returned string. This is the exact
   PR #76 Bug-3 regression, made a floor so a new tool can't reintroduce it.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from typing import Any

import httpx
import pytest

from helixis.agents import buildium_tools as bt
from helixis.buildium.client import BuildiumClient
from helixis.buildium.errors import BuildiumApiError
from helixis.memory.context import memory_context
from helixis.settings import BuildiumSettings

_ENDPOINTS_DIR = (
    pathlib.Path(bt.__file__).resolve().parents[2] / "helixis" / "buildium" / "endpoints"
)

# A collection method's name signals it returns a list, so it must paginate.
_COLLECTION_PREFIXES = ("list_", "get_all", "search_")


# --------------------------------------------------------------------------- #
# 1. Pagination floor — no bare collection request may bypass list_all         #
# --------------------------------------------------------------------------- #


def _collection_methods_bypassing_pagination() -> list[str]:
    """Every ``list_*``/``get_all*``/``search_*`` endpoint method whose body
    never references ``list_all`` or ``paginate`` — i.e. a collection read
    that would truncate at Buildium's first page."""
    offenders: list[str] = []
    for path in sorted(_ENDPOINTS_DIR.glob("*.py")):
        src = path.read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if not node.name.startswith(_COLLECTION_PREFIXES):
                continue
            body = ast.get_source_segment(src, node) or ""
            if "list_all" not in body and "paginate" not in body:
                offenders.append(f"{path.stem}.{node.name}")
    return offenders


def test_no_collection_method_bypasses_pagination() -> None:
    """FLOOR: a collection-returning endpoint method that calls the client
    directly (bare ``request``) truncates silently at page one — the agent
    then answers from a partial list with no gate to catch it. Every such
    method must go through ``list_all``/``paginate``.

    If this fails, the named method returns a raw ``list`` via
    ``self._client.request("GET", ...)``; switch it to
    ``self._client.list_all(path, query)`` (``schema`` is optional for
    raw-dict collections). Do NOT add an allowlist — the whole point is
    that there is no bounded-enough exception; a "handful" today is a
    truncated list the day an account grows past the page size.
    """
    offenders = _collection_methods_bypassing_pagination()
    assert offenders == [], (
        "These collection methods bypass pagination and will truncate at "
        f"Buildium's first page: {offenders}. Route them through "
        "BuildiumClient.list_all()."
    )


def _settings() -> BuildiumSettings:
    return BuildiumSettings(
        base_url="https://apisandbox.buildium.com",
        client_id="cid",
        client_secret="csecret",
        webhook_secret="whsec",
        account_id=1,
        concurrency=4,
    )


def _client(transport: httpx.MockTransport) -> BuildiumClient:
    http = httpx.AsyncClient(
        base_url=_settings().base_url.rstrip("/") + "/v1",
        transport=transport,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    return BuildiumClient(_settings(), http_client=http)


@pytest.mark.asyncio
async def test_schemaless_list_all_collects_every_page() -> None:
    """The raw-dict (``schema=None``) pagination path — added so vendor
    notes / task history / applicant applications / file categories page
    instead of truncating — must collect BOTH a full page and the trailing
    short page.

    (Appliance service-history was a fifth caller until 2026-09-22, when that
    surface was retired ahead of Buildium's 2026-10-19 date; the four above
    are why this path still exists.)"""
    page_size = 1000
    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params.get("offset", 0))
        calls.append(offset)
        count = page_size if offset == 0 else 7  # page 1 full, page 2 short
        return httpx.Response(200, json=[{"Note": f"n{offset + i}"} for i in range(count)])

    async with _client(httpx.MockTransport(handler)) as client:
        items = await client.list_all("/vendors/5/notes", None)  # schema omitted

    assert calls == [0, page_size]  # a second page WAS fetched
    assert len(items) == page_size + 7  # nothing dropped
    assert items[0]["Note"] == "n0" and items[-1]["Note"] == f"n{page_size + 6}"


# --------------------------------------------------------------------------- #
# 2. Credential-UX floor — no read tool leaks raw Buildium error text          #
# --------------------------------------------------------------------------- #

# A unique string planted in the raised error; if it surfaces in a tool's
# returned text, the tool skipped _sanitize_buildium_error.
_LEAK_SENTINEL = "RAWLEAK_EntityId_required_when_EntityType_is_provided"
_LEAK_PATH = "/v1/administration/settings/apikeys"


class _RaiseOnCall:
    """An object whose every attribute access chains to itself and whose
    every call is an awaitable that raises ``exc``. Stands in for a whole
    ``BuildiumApi`` so ``await api.<domain>.<method>(...)`` raises the same
    error no matter which read impl we drive."""

    def __init__(self, exc: BaseException) -> None:
        object.__setattr__(self, "_exc", exc)

    def __getattr__(self, _name: str) -> _RaiseOnCall:
        return self

    async def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        raise object.__getattribute__(self, "_exc")


def _connected_patches(api: Any):
    """Context managers that make ``_open_api`` yield ``api`` for a bound
    company (credentials present, client is a no-op async CM)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return (
        memory_context(company_id="co-1", user_id="u1", session_id="s1"),
        patch.object(
            bt, "load_buildium_settings_for_company", new=AsyncMock(return_value=object())
        ),
        patch.object(bt, "BuildiumClient", return_value=client),
        patch.object(bt, "BuildiumApi", return_value=api),
    )


def _read_impls() -> list[tuple[str, Any]]:
    """Every ``_*_impl`` coroutine that catches Buildium errors as a READ
    (routes through ``_sanitize_buildium_error`` and is NOT a guarded
    write). This is the surface obligated to sanitise — discovered by
    introspection so new read tools are covered automatically."""
    found: list[tuple[str, Any]] = []
    for name, fn in inspect.getmembers(bt, inspect.iscoroutinefunction):
        if not name.endswith("_impl"):
            continue
        try:
            src = inspect.getsource(fn)
        except OSError:  # pragma: no cover
            continue
        if "_sanitize_buildium_error" in src and "run_guarded" not in src:
            found.append((name, fn))
    return found


def _dummy_args(fn: Any) -> list[Any]:
    """Positional stand-ins for a read impl's required params (ids → 1,
    everything else → a short string)."""
    args: list[Any] = []
    for p in inspect.signature(fn).parameters.values():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if p.default is not inspect.Parameter.empty:
            continue
        args.append(1 if p.annotation is int else "1")
    return args


def test_read_impl_discovery_is_nonempty() -> None:
    """Guard the guard: if introspection finds no read impls (a refactor
    renamed them), the leak floor below would vacuously pass."""
    impls = _read_impls()
    assert len(impls) >= 20, f"expected the full Buildium read surface, found {len(impls)}"


@pytest.mark.asyncio
async def test_no_read_tool_leaks_raw_buildium_error() -> None:
    """FLOOR (PR #76 Bug 3): a Buildium 401/403/4xx/5xx must reach the user
    as a sanitised sentence, never Buildium's raw English (field names, api
    -key settings paths) or a Python traceback. Drive every read impl with
    an API that raises a sentinel-bearing 401 and assert the sentinel never
    escapes.
    """
    exc = BuildiumApiError(status=401, message=_LEAK_SENTINEL, path=_LEAK_PATH)
    api = _RaiseOnCall(exc)

    failures: list[str] = []
    exercised = 0  # impls that actually reached the sanitise path and returned text
    for name, fn in _read_impls():
        cm_ctx, cm_creds, cm_client, cm_api = _connected_patches(api)
        with cm_ctx, cm_creds, cm_client, cm_api:
            try:
                result = await fn(*_dummy_args(fn))
            except BuildiumApiError:
                # The impl let the Buildium error propagate instead of routing it
                # through _sanitize_buildium_error — that IS the leak-class bug.
                failures.append(f"{name}: propagated BuildiumApiError raw instead of sanitising")
                continue
            except Exception as other:  # noqa: BLE001
                # Generic stand-in args tripped this impl's pre-API logic (e.g. an
                # enum-status map) before it reached Buildium. Inconclusive, not a
                # leak — UNLESS the raised text itself carries the raw sentinel.
                if _LEAK_SENTINEL in str(other) or _LEAK_PATH in str(other):
                    failures.append(f"{name}: leaked raw text via raise -> {other!r}")
                continue
        if not isinstance(result, str) or not result:
            failures.append(f"{name}: returned non-string/empty {result!r}")
            continue
        exercised += 1
        if _LEAK_SENTINEL in result or _LEAK_PATH in result or "Traceback" in result:
            failures.append(f"{name}: leaked raw error text -> {result!r}")

    assert not failures, "read tools leaked raw Buildium error text:\n" + "\n".join(failures)
    # Guard against a vacuous pass: the harness must have actually driven most of
    # the read surface through the sanitiser, not skipped it all on stand-in args.
    assert exercised >= 20, f"only {exercised} read impls reached the sanitise path"


@pytest.mark.asyncio
async def test_sanitizer_maps_status_classes() -> None:
    """The sanitiser itself: each HTTP class yields its own guidance and
    never echoes the raw message. (Anchors the floor above to concrete
    expected phrasing.)"""
    for status, must_have in (
        (401, "authentication"),
        # ⚠ 403 used to be pinned to "authentication" alongside 401, which is
        # what a shared branch made it look like. A 403 means the credentials
        # were ACCEPTED and the request refused — an API-key permission or a
        # plan limit — so the honest guidance names permissions and never
        # authentication. Re-pointed, not deleted (gotchas 89/93/95).
        (403, "permission"),
        (404, "couldn't find"),
        (400, "rejected"),
        (503, "trouble responding"),
    ):
        msg = bt._sanitize_buildium_error(
            BuildiumApiError(status=status, message=_LEAK_SENTINEL, path=_LEAK_PATH),
            "do the thing",
        )
        assert _LEAK_SENTINEL not in msg and _LEAK_PATH not in msg
        assert must_have in msg.lower(), f"status {status} → {msg!r}"
