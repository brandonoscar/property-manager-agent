"""Tests for MemoryManager with a mocked Mem0 AsyncMemory."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from helixis.memory.manager import MemoryManager
from helixis.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        memory={
            "enabled": True,
            "embedding_model": "text-embedding-3-small",
            "search_top_k": 10,
            "auto_inject": True,
            "auto_extract": True,
            "inject_top_k": 5,
        },
        api={"host": "127.0.0.1", "port": 8000},
    )


@pytest.fixture
def manager(settings: Settings) -> MemoryManager:
    mgr = MemoryManager(settings)
    mgr._mem0 = AsyncMock()
    mgr._initialized = True
    return mgr


async def test_add_stores_with_metadata(manager: MemoryManager) -> None:
    manager._mem0.add = AsyncMock(return_value={"results": [{"event": "ADD", "memory": "test"}]})

    await manager.add(
        "Unit 4B tenant is Sarah Chen",
        company_id="comp_1",
        stored_by="user_brandon",
        category="tenant",
    )

    manager._mem0.add.assert_called_once()
    kwargs = manager._mem0.add.call_args.kwargs
    assert kwargs["user_id"] == "comp_1"
    metadata = kwargs["metadata"]
    assert metadata["stored_by"] == "user_brandon"
    assert metadata["category"] == "tenant"
    assert "stored_at" in metadata


async def test_add_strips_never_store_pii_keeps_phone_email(manager: MemoryManager) -> None:
    """add() redacts SSN/card/IBAN before anything reaches the vector store,
    but preserves email + phone (the operational facts memory needs)."""
    manager._mem0.add = AsyncMock(return_value={"results": []})

    await manager.add(
        "Sarah Chen sarah@oak.com 415-555-0199 SSN 078-05-1120 card 4111 1111 1111 1111",
        company_id="comp_1",
        stored_by="user_brandon",
    )

    stored = manager._mem0.add.call_args.args[0]  # content is the first positional arg
    assert "078-05-1120" not in stored  # SSN stripped
    assert "4111 1111 1111 1111" not in stored  # card stripped
    assert "sarah@oak.com" in stored  # email kept
    assert "415-555-0199" in stored  # phone kept
    assert "Sarah Chen" in stored  # name kept


async def test_add_stamps_embedding_model(manager: MemoryManager) -> None:
    """Every stored row records WHICH model embedded it — the audit's one
    durable finding: a same-dimension embedder change would otherwise corrupt
    retrieval with zero errors (today's mismatch is loud only because
    1536 ≠ 1024)."""
    manager._mem0.add = AsyncMock(return_value={"results": []})

    await manager.add("a fact", company_id="comp_1")

    metadata = manager._mem0.add.call_args.kwargs["metadata"]
    assert metadata["embedding_model"] == manager.settings.memory.embedding_model
    assert metadata["embedding_dims"] == manager.settings.memory.embedding_dims


async def test_extract_and_store_stamps_embedding_model(manager: MemoryManager) -> None:
    manager._mem0.add = AsyncMock(return_value={"results": []})

    await manager.extract_and_store(
        [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
        company_id="comp_1",
    )

    metadata = manager._mem0.add.call_args.kwargs["metadata"]
    assert metadata["embedding_model"] == manager.settings.memory.embedding_model
    assert metadata["embedding_dims"] == manager.settings.memory.embedding_dims


async def test_add_defaults_to_system_and_skips_empty_category(
    manager: MemoryManager,
) -> None:
    manager._mem0.add = AsyncMock(return_value={"results": []})

    await manager.add("a fact", company_id="comp_1")

    metadata = manager._mem0.add.call_args.kwargs["metadata"]
    assert metadata["stored_by"] == "system"
    assert "category" not in metadata


async def test_search_uses_v3_filters(manager: MemoryManager) -> None:
    manager._mem0.search = AsyncMock(return_value={"results": []})

    await manager.search("tenant info", company_id="comp_1")

    kwargs = manager._mem0.search.call_args.kwargs
    assert "filters" in kwargs
    assert kwargs["filters"]["user_id"] == "comp_1"
    assert kwargs["top_k"] == 10
    # Importantly: no top-level user_id kwarg (v3 would reject it).
    assert "user_id" not in kwargs


async def test_get_all_uses_v3_filters(manager: MemoryManager) -> None:
    manager._mem0.get_all = AsyncMock(return_value={"results": []})

    await manager.get_all(company_id="comp_1")

    kwargs = manager._mem0.get_all.call_args.kwargs
    assert kwargs["filters"]["user_id"] == "comp_1"
    assert "user_id" not in kwargs


async def test_search_for_context_formats_categories_and_authors(
    manager: MemoryManager,
) -> None:
    manager._mem0.search = AsyncMock(
        return_value={
            "results": [
                {
                    "memory": "Unit 4B tenant is Sarah Chen",
                    "metadata": {
                        "category": "tenant",
                        "stored_by": "user_brandon",
                    },
                },
                {
                    "memory": "Plumber: Joe's Plumbing, 555-0123",
                    "metadata": {
                        "category": "vendor",
                        "stored_by": "user_ryan",
                    },
                },
                {
                    "memory": "Orphan note without metadata",
                    "metadata": None,
                },
            ]
        }
    )

    result = await manager.search_for_context("Unit 4B", company_id="comp_1")

    assert "[tenant]" in result
    assert "user_brandon" in result
    assert "Sarah Chen" in result
    assert "[vendor]" in result
    assert "user_ryan" in result
    # Unannotated memories still render, just without prefix decorations.
    assert "Orphan note without metadata" in result


async def test_search_for_context_returns_empty_when_no_hits(
    manager: MemoryManager,
) -> None:
    manager._mem0.search = AsyncMock(return_value={"results": []})
    assert await manager.search_for_context("x", company_id="c") == ""


async def test_extract_and_store_sends_full_conversation(
    manager: MemoryManager,
) -> None:
    manager._mem0.add = AsyncMock(
        return_value={"results": [{"event": "ADD", "memory": "User prefers email over phone"}]}
    )

    conversation = [
        {"role": "user", "content": "Always email tenants, never call"},
        {
            "role": "assistant",
            "content": "Noted — I'll default to email for tenant comms.",
        },
    ]

    stored = await manager.extract_and_store(
        conversation, company_id="comp_1", stored_by="user_brandon"
    )

    assert stored == ["User prefers email over phone"]
    call = manager._mem0.add.call_args
    # Conversation passed through positionally.
    assert call.args[0] == conversation
    assert call.kwargs["user_id"] == "comp_1"
    metadata = call.kwargs["metadata"]
    assert metadata["source"] == "auto_extraction"
    assert metadata["stored_by"] == "user_brandon"


async def test_extract_and_store_ignores_non_add_events(
    manager: MemoryManager,
) -> None:
    manager._mem0.add = AsyncMock(
        return_value={
            "results": [
                {"event": "UPDATE", "memory": "updated"},
                {"event": "ADD", "memory": "new"},
                {"event": "DELETE", "memory": "gone"},
            ]
        }
    )
    conversation = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "y"},
    ]

    stored = await manager.extract_and_store(conversation, company_id="c")
    assert stored == ["new"]


async def test_user_b_retrieves_what_user_a_wrote_same_company(
    manager: MemoryManager,
) -> None:
    """Cross-user, same-company recall — the core company-scoping guarantee.

    User A (Brandon) stores a fact; User B (Ryan) at the *same* company_id
    later searches and gets it back. The two are different humans
    (``stored_by`` vs ``searched_by``) but share one Mem0 partition, so the
    fact is visible to B. Both calls must scope by the same company key.
    """
    company = "company_acme"
    fact = "Unit 4B tenant is Sarah Chen, lease expires 2026-09-30"

    # User A writes.
    manager._mem0.add = AsyncMock(return_value={"results": [{"event": "ADD", "memory": fact}]})
    await manager.add(fact, company_id=company, stored_by="user_brandon", category="tenant")

    # Mem0 partitions by the company key, so a search in the same company
    # returns A's memory regardless of which user issues it.
    manager._mem0.search = AsyncMock(
        return_value={
            "results": [
                {"memory": fact, "metadata": {"category": "tenant", "stored_by": "user_brandon"}}
            ]
        }
    )

    # User B reads.
    hits = await manager.search(
        "Who is the tenant in 4B?", company_id=company, searched_by="user_ryan"
    )

    assert any(fact in h.get("memory", "") for h in hits)
    # Both the write and the read scoped to the same company partition.
    assert manager._mem0.add.call_args.kwargs["user_id"] == company
    assert manager._mem0.search.call_args.kwargs["filters"]["user_id"] == company

    # And it renders into B's injected context string.
    manager._mem0.search = AsyncMock(
        return_value={
            "results": [
                {"memory": fact, "metadata": {"category": "tenant", "stored_by": "user_brandon"}}
            ]
        }
    )
    context = await manager.search_for_context(
        "tenant 4B", company_id=company, searched_by="user_ryan"
    )
    assert "Sarah Chen" in context
    assert "(via user_brandon)" in context


async def test_cross_company_isolation(manager: MemoryManager) -> None:
    """A user in company X must NOT see company Y's memories.

    Unlike the same-company test, this one backs the Mem0 mock with a real
    partitioned store keyed on the scope filter the production code passes,
    so isolation is *genuinely* exercised: ``add`` files the memory under the
    ``user_id`` partition it receives, and ``search`` only returns the
    partition named in ``filters["user_id"]``. If the production code ever
    issued the wrong company scope, company Y's search would surface company
    X's fact and this test would fail — it is not a trivially-green test.
    """
    company_x = "company_x"
    company_y = "company_y"
    x_fact = "Unit 4B tenant is Sarah Chen"

    # Partitioned backing store: {mem0_scope_key: [memories]}.
    store: dict[str, list[dict[str, Any]]] = {}

    async def fake_add(content: Any, *, user_id: str, metadata: dict | None = None) -> dict:
        store.setdefault(user_id, []).append({"memory": content, "metadata": metadata or {}})
        return {"results": [{"event": "ADD", "memory": content}]}

    async def fake_search(query: str, *, filters: dict, top_k: int = 10) -> dict:
        scope = filters["user_id"]
        return {"results": list(store.get(scope, []))}

    manager._mem0.add = AsyncMock(side_effect=fake_add)
    manager._mem0.search = AsyncMock(side_effect=fake_search)

    # Company X stores a fact.
    await manager.add(x_fact, company_id=company_x, stored_by="user_xavier", category="tenant")

    # Company Y searches — must get nothing.
    y_hits = await manager.search(x_fact, company_id=company_y, searched_by="user_yolanda")
    assert y_hits == [], "company Y must not receive company X's memories"

    # The search was genuinely issued under company Y's scope (not X's).
    assert manager._mem0.search.call_args.kwargs["filters"]["user_id"] == company_y

    # And company Y's injected context is empty — nothing leaks across the boundary.
    context = await manager.search_for_context(
        x_fact, company_id=company_y, searched_by="user_yolanda"
    )
    assert context == ""
    assert "Sarah Chen" not in context

    # Sanity: company X still sees its own fact, proving the store isn't just
    # empty for everyone (which would make the isolation assertion vacuous).
    x_hits = await manager.search(x_fact, company_id=company_x, searched_by="user_xavier")
    assert any(x_fact in h.get("memory", "") for h in x_hits)


async def test_search_returns_empty_when_unavailable(
    settings: Settings,
) -> None:
    mgr = MemoryManager(settings)
    # _initialized stays False — simulating the "Mem0 not installed" path.
    assert mgr.available is False
    assert await mgr.search("q", company_id="c") == []
    assert await mgr.add("f", company_id="c") is None
    assert await mgr.get_all(company_id="c") == []
    assert await mgr.search_for_context("q", company_id="c") == ""
    assert await mgr.extract_and_store([{"role": "user", "content": "x"}], company_id="c") == []


async def test_search_handles_list_response_shape(
    manager: MemoryManager,
) -> None:
    # Some Mem0 versions return a bare list instead of {"results": [...]}.
    manager._mem0.search = AsyncMock(return_value=[{"memory": "x"}])
    out = await manager.search("q", company_id="c")
    assert out == [{"memory": "x"}]


async def test_search_swallows_mem0_exceptions(manager: MemoryManager) -> None:
    manager._mem0.search = AsyncMock(side_effect=RuntimeError("boom"))
    assert await manager.search("q", company_id="c") == []


async def test_search_times_out_fast_instead_of_stalling(
    manager: MemoryManager, settings: Settings
) -> None:
    """A hung embedder must degrade to "no memory" within the timeout — the
    ~80s pre-turn stall the 2026-07-25 stress test caught on prod. With a
    tiny timeout, a search that would take longer returns [] quickly, never
    blocking the chat turn."""
    import asyncio
    import time

    settings.memory.search_timeout_s = 0.05

    async def _hang(*_a: Any, **_k: Any) -> dict:
        await asyncio.sleep(5)
        return {"results": [{"memory": "never reached"}]}

    manager._mem0.search = _hang
    start = time.monotonic()
    result = await manager.search("q", company_id="c")
    elapsed = time.monotonic() - start
    assert result == []
    assert elapsed < 1.0  # bailed at the 0.05s timeout, not the 5s hang


def _install_fake_mem0(captured: dict) -> tuple:
    """Inject a fake ``mem0`` module into sys.modules.

    The real mem0ai is heavy (Qdrant, Gemini, sentence-transformers) and
    not part of the dev dependency set. The manager imports ``AsyncMemory``
    inside its initialize() method, so swapping the module at sys.modules
    is enough to intercept ``from mem0 import AsyncMemory``.

    Returns (module, AsyncMemory class) so the caller can clean up.
    """
    import sys
    import types

    fake_module = types.ModuleType("mem0")
    instance = AsyncMock()

    class _FakeEmbedderConfig:
        # Mirrors pydantic's no-validation constructor — the manager builds
        # the embedder sub-config with model_construct to bypass mem0's
        # hardcoded provider allowlist (re-embed run #4).
        def __init__(self, provider: str, config: dict):
            self.provider = provider
            self.config = config

        @classmethod
        def model_construct(cls, *, provider: str, config: dict) -> _FakeEmbedderConfig:
            return cls(provider, config)

    class _FakeMemoryConfig:
        def __init__(self, **kwargs: object):
            self.kwargs = kwargs

    class _FakeAsyncMemory:
        # Mirrors real mem0 2.x: SYNC constructor taking config=MemoryConfig
        # (the async surface is add()/search()/get_all()). Flattens back to
        # the dict shape the assertions read.
        def __new__(cls, config: _FakeMemoryConfig):  # noqa: ANN204
            emb = config.kwargs["embedder"]
            captured["embedder"] = {"provider": emb.provider, "config": emb.config}
            captured["llm"] = config.kwargs["llm"]
            if "vector_store" in config.kwargs:
                captured["vector_store"] = config.kwargs["vector_store"]
            return instance

    fake_module.AsyncMemory = _FakeAsyncMemory  # type: ignore[attr-defined]

    # initialize() imports the factory + config seams — mirror them all.
    fake_utils = types.ModuleType("mem0.utils")
    fake_factory = types.ModuleType("mem0.utils.factory")
    fake_configs = types.ModuleType("mem0.configs")
    fake_configs_base = types.ModuleType("mem0.configs.base")
    fake_embeddings = types.ModuleType("mem0.embeddings")
    fake_embeddings_configs = types.ModuleType("mem0.embeddings.configs")

    class _FakeEmbedderFactory:
        provider_to_class: dict = {}

    fake_factory.EmbedderFactory = _FakeEmbedderFactory  # type: ignore[attr-defined]
    fake_configs_base.MemoryConfig = _FakeMemoryConfig  # type: ignore[attr-defined]
    fake_embeddings_configs.EmbedderConfig = _FakeEmbedderConfig  # type: ignore[attr-defined]
    fake_utils.factory = fake_factory  # type: ignore[attr-defined]
    fake_module.utils = fake_utils  # type: ignore[attr-defined]
    fake_module.configs = fake_configs  # type: ignore[attr-defined]
    fake_configs.base = fake_configs_base  # type: ignore[attr-defined]
    fake_module.embeddings = fake_embeddings  # type: ignore[attr-defined]
    fake_embeddings.configs = fake_embeddings_configs  # type: ignore[attr-defined]
    for name, mod in {
        "mem0": fake_module,
        "mem0.utils": fake_utils,
        "mem0.utils.factory": fake_factory,
        "mem0.configs": fake_configs,
        "mem0.configs.base": fake_configs_base,
        "mem0.embeddings": fake_embeddings,
        "mem0.embeddings.configs": fake_embeddings_configs,
    }.items():
        sys.modules[name] = mod
    return fake_module, _FakeAsyncMemory


def _pop_fake_mem0() -> None:
    import sys

    for name in (
        "mem0",
        "mem0.utils",
        "mem0.utils.factory",
        "mem0.configs",
        "mem0.configs.base",
        "mem0.embeddings",
        "mem0.embeddings.configs",
    ):
        sys.modules.pop(name, None)


async def test_initialize_uses_voyage_embedder_and_anthropic_llm() -> None:
    """Mem0 must be wired to Voyage for embeddings + Anthropic for the
    extraction LLM — never OpenAI, never (dead) Gemini (gotcha 18)."""
    settings = Settings(
        env="test",
        memory={"enabled": True},
        llm={"voyage_api_key": "test-voyage-key", "anthropic_api_key": "test-anthropic-key"},
    )
    mgr = MemoryManager(settings)

    captured: dict = {}
    _install_fake_mem0(captured)
    try:
        await mgr.initialize()
    finally:
        _pop_fake_mem0()

    assert mgr.available is True
    assert captured["embedder"]["provider"] == "voyage"
    assert captured["embedder"]["config"]["model"] == "voyage-4-lite"
    assert captured["embedder"]["config"]["api_key"] == "test-voyage-key"
    # The dims are pinned explicitly — the embedder and pgvector column can
    # never drift apart.
    assert captured["embedder"]["config"]["embedding_dims"] == 1024
    assert captured["llm"]["provider"] == "anthropic"
    assert captured["llm"]["config"]["api_key"] == "test-anthropic-key"
    # Crucially: no openai and no gemini anywhere.
    import json

    dumped = json.dumps(captured).lower()
    assert "openai" not in dumped
    assert "gemini" not in dumped


async def test_initialize_registers_voyage_provider() -> None:
    """from_config resolves ``provider: "voyage"`` through mem0's factory —
    initialize() must register our class there first or init throws
    "Unsupported Embedder provider"."""
    settings = Settings(
        env="test",
        memory={"enabled": True},
        llm={"voyage_api_key": "vk", "anthropic_api_key": "ak"},
    )
    mgr = MemoryManager(settings)

    captured: dict = {}
    _install_fake_mem0(captured)
    try:
        await mgr.initialize()
        import sys

        factory = sys.modules["mem0.utils.factory"].EmbedderFactory
        assert (
            factory.provider_to_class["voyage"] == "helixis.memory.voyage_embedder.VoyageEmbedder"
        )
    finally:
        _pop_fake_mem0()


async def test_initialize_skips_when_voyage_key_missing() -> None:
    """Without the Voyage key we'd silently fail at first embed call —
    refuse to initialise so the startup self-check log line shows
    ``memory: false`` instead of a half-broken manager."""
    settings = Settings(
        env="test",
        memory={"enabled": True},
        llm={"voyage_api_key": "", "anthropic_api_key": "ak"},
    )
    mgr = MemoryManager(settings)
    await mgr.initialize()
    assert mgr.available is False


async def test_initialize_skips_when_anthropic_key_missing() -> None:
    """Mem0's fact-extraction LLM is Anthropic — no key, no memory."""
    settings = Settings(
        env="test",
        memory={"enabled": True},
        llm={"voyage_api_key": "vk", "anthropic_api_key": ""},
    )
    mgr = MemoryManager(settings)
    await mgr.initialize()
    assert mgr.available is False


async def test_initialize_adds_pgvector_when_db_url_set() -> None:
    settings = Settings(
        env="test",
        memory={"enabled": True},
        llm={"voyage_api_key": "vk", "anthropic_api_key": "ak"},
        supabase={"db_url": "postgresql://u:p@h:5432/d"},
    )
    mgr = MemoryManager(settings)

    captured: dict = {}
    _install_fake_mem0(captured)
    try:
        await mgr.initialize()
    finally:
        _pop_fake_mem0()

    assert captured["vector_store"]["provider"] == "pgvector"
    assert captured["vector_store"]["config"]["connection_string"] == "postgresql://u:p@h:5432/d"
    # Column dims always equal embedder dims — single source of truth.
    assert (
        captured["vector_store"]["config"]["embedding_model_dims"]
        == captured["embedder"]["config"]["embedding_dims"]
    )


async def test_search_contains_spacy_systemexit(manager: MemoryManager) -> None:
    """mem0 2.x runs spaCy-backed BM25 lemmatization on the query; when the
    model can't load it sys.exit(1)s. SystemExit is NOT an Exception, so a plain
    `except Exception` would let it slip through and kill run_agent_streaming
    (the live sev-1 after #322). search() must contain it and degrade to []."""
    manager._mem0.search = AsyncMock(side_effect=SystemExit(1))
    hits = await manager.search("gate code", company_id="comp_1", searched_by="u")
    assert hits == []


async def test_add_contains_spacy_systemexit(manager: MemoryManager) -> None:
    """Same containment on the write path — a mem0 SystemExit must not crash
    the caller; add() returns None."""
    manager._mem0.add = AsyncMock(side_effect=SystemExit(1))
    result = await manager.add("a fact", company_id="comp_1", stored_by="u")
    assert result is None


async def test_delete_requires_company_and_verifies_owner(manager: MemoryManager) -> None:
    """#35 hardening: delete() was the ONE memory op without a company scope
    check. It must refuse an empty company, refuse a cross-company id, and
    delete only when the memory's partition key matches."""
    manager._mem0.get = AsyncMock(return_value={"id": "m1", "user_id": "comp_A"})
    manager._mem0.delete = AsyncMock()

    # No company → refused, nothing fetched or deleted.
    assert await manager.delete("m1", "") is False
    manager._mem0.delete.assert_not_called()

    # Wrong company → refused after ownership check.
    assert await manager.delete("m1", "comp_B") is False
    manager._mem0.delete.assert_not_called()

    # Right company → deleted.
    assert await manager.delete("m1", "comp_A") is True
    manager._mem0.delete.assert_called_once_with(memory_id="m1")

    # Unknown id → refused.
    manager._mem0.get = AsyncMock(return_value=None)
    manager._mem0.delete.reset_mock()
    assert await manager.delete("ghost", "comp_A") is False
    manager._mem0.delete.assert_not_called()


async def test_search_swallow_reports_to_sentry(
    manager: MemoryManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P2 (#34): the fail-open degrade stays (returns []), but the swallowed
    exception is forwarded to Sentry so degradation is visible BETWEEN daily
    smoke runs instead of rotting silently for up to ~24h."""
    from helixis.memory import manager as mgr_module

    captured: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mgr_module,
        "capture_swallowed",
        lambda exc, *, op: captured.append((type(exc).__name__, op)),
    )
    manager._mem0.search = AsyncMock(side_effect=RuntimeError("embedder down"))

    assert await manager.search("q", company_id="c") == []  # degrade unchanged
    assert captured == [("RuntimeError", "memory.search")]


async def test_delete_company_memory_atomic(manager: MemoryManager) -> None:
    """Right-to-erasure: prefers Mem0's atomic delete_all on the company
    partition; returns the count and does NOT fall back to per-id deletes."""
    manager._mem0.get_all = AsyncMock(return_value={"results": [{"id": "m1"}, {"id": "m2"}]})
    manager._mem0.delete_all = AsyncMock()

    n = await manager.delete_company_memory("comp_1", requested_by="admin-uuid")

    assert n == 2
    manager._mem0.delete_all.assert_called_once_with(user_id="comp_1")
    manager._mem0.delete.assert_not_called()


async def test_delete_company_memory_fallback_iterates(settings: Settings) -> None:
    """On a Mem0 without delete_all, erasure enumerates and deletes each row."""
    mgr = MemoryManager(settings)
    mgr._mem0 = AsyncMock(spec=["get_all", "delete"])  # no delete_all attribute
    mgr._initialized = True
    mgr._mem0.get_all = AsyncMock(return_value={"results": [{"id": "m1"}, {"id": "m2"}]})
    mgr._mem0.delete = AsyncMock()  # list-spec children are MagicMock; make it awaitable

    n = await mgr.delete_company_memory("comp_1")

    assert n == 2
    assert mgr._mem0.delete.await_count == 2


async def test_delete_company_memory_requires_company(manager: MemoryManager) -> None:
    """An empty company_id is a no-op (never erase the 'default' partition)."""
    assert await manager.delete_company_memory("") == 0


async def test_delete_company_memory_can_raise_for_an_erasure(manager: MemoryManager) -> None:
    """A failure returns 0 by default (right for a chat turn). The workspace
    delete asks it to raise, because there a silent 0 reads as "nothing to
    delete"."""
    manager._mem0.get_all = AsyncMock(return_value={"results": [{"id": "m1"}]})
    manager._mem0.delete_all = AsyncMock(side_effect=RuntimeError("vector store down"))

    assert await manager.delete_company_memory("comp_1") == 0
    with pytest.raises(RuntimeError, match="vector store down"):
        await manager.delete_company_memory("comp_1", raise_on_error=True)
