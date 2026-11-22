"""Voyage embedder + mem0 config wiring (the Gemini→Voyage memory migration).

These run WITHOUT the ``[memory]`` extra — ``voyage_embedder`` ships an import
shim for mem0's ``EmbeddingBase``, and ``build_mem0_config`` is pure. The wire
contract pinned here: explicit ``output_dimension`` on EVERY call (server
defaults differ per model and would silently mismatch the pgvector column),
``input_type`` query/document mapping, float-list response parsing, and the
key never appearing anywhere but the Authorization header.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from helixis.memory.manager import build_mem0_config
from helixis.memory.voyage_embedder import (
    VOYAGE_EMBEDDINGS_URL,
    VoyageEmbedder,
)
from helixis.settings import Settings


def _config(**overrides: Any) -> SimpleNamespace:
    base = {"model": "voyage-4-lite", "api_key": "vk-test", "embedding_dims": 1024}
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_post(captured: list[dict], vectors_by_len: bool = True):
    """Return a fake httpx.post recording requests and echoing embeddings."""

    def fake_post(url: str, *, json: dict, headers: dict, timeout: float) -> httpx.Response:
        captured.append({"url": url, "json": json, "headers": headers})
        data = [
            {"index": i, "embedding": [0.1] * json["output_dimension"]}
            for i in range(len(json["input"]))
        ]
        return httpx.Response(
            200,
            json={"data": data},
            request=httpx.Request("POST", url),
        )

    return fake_post


# ── VoyageEmbedder ──────────────────────────────────────────────────


def test_embed_pins_output_dimension_and_maps_input_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(httpx, "post", _fake_post(captured))
    emb = VoyageEmbedder(_config())

    vec = emb.embed("tenant in 4B prefers texting", "search")
    assert len(vec) == 1024

    req = captured[0]
    assert req["url"] == VOYAGE_EMBEDDINGS_URL
    assert req["json"]["model"] == "voyage-4-lite"
    # NEVER rely on the server default dimension.
    assert req["json"]["output_dimension"] == 1024
    # search → query side; add/update → document side.
    assert req["json"]["input_type"] == "query"
    assert req["headers"]["Authorization"] == "Bearer vk-test"


def test_mongodb_issued_key_routes_to_mongodb_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keys minted in the MongoDB/Atlas console (prefix "al-") only work
    against ai.mongodb.com — a native-Voyage host 403s them (re-embed run #3).
    Mirror the official SDK's key-prefix endpoint selection."""
    from helixis.memory.voyage_embedder import MONGODB_EMBEDDINGS_URL

    captured: list[dict] = []
    monkeypatch.setattr(httpx, "post", _fake_post(captured))

    VoyageEmbedder(_config(api_key="al-mongodb-style-key")).embed("x", "search")
    assert captured[-1]["url"] == MONGODB_EMBEDDINGS_URL

    VoyageEmbedder(_config(api_key="pa-native-voyage-key")).embed("x", "search")
    assert captured[-1]["url"] == VOYAGE_EMBEDDINGS_URL


def test_embed_defaults_to_document_input_type(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(httpx, "post", _fake_post(captured))
    emb = VoyageEmbedder(_config())

    emb.embed("fact", "add")
    emb.embed("fact", None)
    assert [c["json"]["input_type"] for c in captured] == ["document", "document"]


def test_embed_batch_chunks_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(httpx, "post", _fake_post(captured))
    emb = VoyageEmbedder(_config())

    out = emb.embed_batch([f"m{i}" for i in range(300)], "add")
    assert len(out) == 300
    # 300 texts @ batch 128 → 3 requests (128 + 128 + 44).
    assert [len(c["json"]["input"]) for c in captured] == [128, 128, 44]


def test_embed_sorts_response_by_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vectors must line up with input order even if the API reorders."""

    def scrambled_post(url: str, *, json: dict, headers: dict, timeout: float) -> httpx.Response:
        data = [
            {"index": 1, "embedding": [1.0] * json["output_dimension"]},
            {"index": 0, "embedding": [0.0] * json["output_dimension"]},
        ]
        return httpx.Response(200, json={"data": data}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", scrambled_post)
    emb = VoyageEmbedder(_config())
    out = emb.embed_batch(["a", "b"], "add")
    assert out[0][0] == 0.0 and out[1][0] == 1.0


def test_embed_raises_on_http_error_with_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failures must PROPAGATE — the manager/health-probe layers decide
    whether to swallow (request path) or surface (smoke). The error carries
    the response BODY (a bare "403 Forbidden" is undiagnosable — bit re-embed
    run #1) but never the API key."""

    def failing_post(url: str, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(
            403, json={"detail": "Provided API key is invalid"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", failing_post)
    emb = VoyageEmbedder(_config())
    with pytest.raises(RuntimeError, match="403") as exc_info:
        emb.embed("x", "search")
    assert "API key is invalid" in str(exc_info.value)
    assert "vk-test" not in str(exc_info.value)


def test_embed_retries_through_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A keyless free-tier Voyage account is capped at 3 RPM (bit re-embed
    run #6) — 429s must be waited out (honoring Retry-After) and retried,
    not surfaced as instant failure."""
    from helixis.memory import voyage_embedder as ve

    sleeps: list[float] = []
    monkeypatch.setattr(ve.time, "sleep", sleeps.append)

    calls = {"n": 0}

    def flaky_post(url: str, *, json: dict, headers: dict, timeout: float) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(
                429,
                json={"detail": "rate limited"},
                headers={"Retry-After": "7"},
                request=httpx.Request("POST", url),
            )
        data = [
            {"index": i, "embedding": [0.1] * json["output_dimension"]}
            for i in range(len(json["input"]))
        ]
        return httpx.Response(200, json={"data": data}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", flaky_post)
    emb = VoyageEmbedder(_config())
    vec = emb.embed("fact", "add")
    assert len(vec) == 1024
    assert calls["n"] == 3
    # Retry-After honored (7s, twice), never the blind default.
    assert sleeps == [7.0, 7.0]


def test_embed_gives_up_after_bounded_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sustained 429 must eventually surface (bounded stall — memory
    degrades rather than blocking a chat turn forever)."""
    from helixis.memory import voyage_embedder as ve

    sleeps: list[float] = []
    monkeypatch.setattr(ve.time, "sleep", sleeps.append)

    def always_429(url: str, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(
            429, json={"detail": "rate limited"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", always_429)
    emb = VoyageEmbedder(_config())
    with pytest.raises(RuntimeError, match="429"):
        emb.embed("fact", "add")
    # Retried the bounded number of times with the default wait.
    assert len(sleeps) == ve._RATE_LIMIT_MAX_RETRIES
    assert all(s == ve._RATE_LIMIT_DEFAULT_WAIT_SECONDS for s in sleeps)


def test_constructor_refuses_missing_dims() -> None:
    with pytest.raises(ValueError, match="embedding_dims"):
        VoyageEmbedder(_config(embedding_dims=None))
    with pytest.raises(ValueError, match="api_key"):
        VoyageEmbedder(_config(api_key=""))
    with pytest.raises(ValueError, match="model"):
        VoyageEmbedder(_config(model=""))


# ── build_mem0_config ───────────────────────────────────────────────


def _settings(voyage: str = "vk", anthropic: str = "ak", db_url: str = "") -> Settings:
    return Settings(
        env="test",
        memory={"enabled": True},
        llm={"voyage_api_key": voyage, "anthropic_api_key": anthropic},
        supabase={"db_url": db_url},
    )


def test_build_config_wires_voyage_and_anthropic() -> None:
    config, reason = build_mem0_config(_settings())
    assert config is not None and reason == ""
    assert config["embedder"]["provider"] == "voyage"
    assert config["llm"]["provider"] == "anthropic"
    dumped = json.dumps(config).lower()
    assert "gemini" not in dumped
    assert "openai" not in dumped


def test_build_config_dims_single_source_of_truth() -> None:
    config, _ = build_mem0_config(_settings(db_url="postgresql://u:p@h/d"))
    assert config is not None
    assert (
        config["embedder"]["config"]["embedding_dims"]
        == config["vector_store"]["config"]["embedding_model_dims"]
        == 1024
    )


def test_build_config_refuses_missing_keys() -> None:
    config, reason = build_mem0_config(_settings(voyage=""))
    assert config is None and "VOYAGE" in reason
    config, reason = build_mem0_config(_settings(anthropic=""))
    assert config is None and "ANTHROPIC" in reason


def test_default_yaml_resolves_voyage_not_gemini() -> None:
    """config/default.yaml OUTRANKS the settings.py class defaults (gotcha 6
    mechanics) — re-embed run #1 failed because #692 updated settings.py while
    the YAML still pinned the dead Gemini embedder. A default-constructed
    Settings (which loads the YAML) must resolve the Voyage values."""
    s = Settings()
    assert s.memory.embedding_model == "voyage-4-lite"
    assert s.memory.embedding_dims == 1024
    assert "gemini" not in s.memory.embedding_model.lower()
