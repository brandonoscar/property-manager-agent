"""Voyage AI embedder for Mem0 — the post-Gemini memory embedder (gotcha 18).

Anthropic has no embeddings API, so memory pairs Claude with Voyage
(``voyage-4-lite``: 200M free tokens per account, then $0.02/MTok — effectively
$0 at our per-turn volumes). mem0 2.x has no native Voyage provider (its
``langchain`` provider would drag the full ``langchain`` package into the prod
image just for an isinstance check), so this module is a ~60-line embedder that
POSTs Voyage's REST API directly with ``httpx`` (already a core dep — zero new
packages) and registers itself into mem0's ``EmbedderFactory`` under provider
name ``"voyage"``.

Dimension discipline: the output dimension is ALWAYS pinned explicitly via
``output_dimension`` (never the server default — voyage-4-nano's default is
2048 while our column is vector(1024), so trusting defaults would silently
mismatch the pgvector column). ``settings.memory.embedding_dims`` is the single
source of truth; changing it requires the re-embed migration
(``scripts/reembed_memories.py``).

Import shim: CI's unit-test env installs only ``[dev]`` (no mem0), so this
module must import without mem0 — the shim base class mirrors mem0 2.x
``EmbeddingBase.__init__`` (stores config). The factory registration path
always runs with real mem0 present.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

try:  # pragma: no cover — exercised implicitly by which base ends up used
    from mem0.embeddings.base import EmbeddingBase  # type: ignore[import-not-found]
except ImportError:  # mem0 not installed (CI unit-test env) — minimal mirror

    class EmbeddingBase:  # type: ignore[no-redef]
        def __init__(self, config: Any = None):
            self.config = config


VOYAGE_EMBEDDINGS_URL = "https://api.voyageai.com/v1/embeddings"

# Voyage is MongoDB-owned, and keys minted through the MongoDB/Atlas console
# (prefix "al-") only work against MongoDB's host — a native-Voyage host
# rejects them with 403 "Voyage AI API keys work with Voyage AI endpoints,
# and MongoDB Atlas API keys work with MongoDB endpoints" (re-embed run #3).
# Same API contract on both hosts; the official voyageai SDK selects the
# base URL by key prefix exactly like this.
MONGODB_EMBEDDINGS_URL = "https://ai.mongodb.com/v1/embeddings"


def _embeddings_url_for_key(api_key: str) -> str:
    return MONGODB_EMBEDDINGS_URL if api_key.startswith("al-") else VOYAGE_EMBEDDINGS_URL


# Voyage rejects batches over 1000 inputs; stay well under it. Larger batches
# also mean FEWER requests — which is what matters under the keyless free
# tier's 3-requests-per-minute limit.
_BATCH_SIZE = 128

_TIMEOUT_SECONDS = 30.0

# 429 handling: a Voyage account with no payment method is capped at 3 RPM
# (bit re-embed run #6). Honor Retry-After when present, else wait out the
# RPM window. Bounded so a sustained 429 on the CHAT path can't stall a turn
# forever — memory degrades to "no recall" rather than blocking (gotcha 7).
_RATE_LIMIT_MAX_RETRIES = 5
_RATE_LIMIT_DEFAULT_WAIT_SECONDS = 25.0
_RATE_LIMIT_MAX_WAIT_SECONDS = 60.0

# mem0 memory_action -> Voyage input_type. Voyage embeds queries and documents
# with different instruction prefixes; using the right one measurably improves
# retrieval. "search" is the query side; everything else stores a document.
_INPUT_TYPE = {"search": "query", "add": "document", "update": "document"}


class VoyageEmbedder(EmbeddingBase):
    """mem0 embedder calling Voyage's REST API via httpx.

    Reads ``model`` / ``api_key`` / ``embedding_dims`` from the standard
    ``BaseEmbedderConfig``. Raises on failure — mem0/manager paths already
    swallow-and-degrade (memory is an enhancement, never load-bearing), and
    the smoke ``health_probe`` needs the raw error to surface rot loudly.
    """

    def __init__(self, config: Any = None):
        super().__init__(config)
        if not getattr(self.config, "model", None):
            raise ValueError("VoyageEmbedder requires `model` (e.g. voyage-4-lite)")
        if not getattr(self.config, "api_key", None):
            raise ValueError("VoyageEmbedder requires `api_key` (HELIXIS_LLM__VOYAGE_API_KEY)")
        if not getattr(self.config, "embedding_dims", None):
            # Never fall back to the server default — see module docstring.
            raise ValueError("VoyageEmbedder requires `embedding_dims` pinned explicitly")
        self._url = _embeddings_url_for_key(self.config.api_key)

    def _request(self, texts: list[str], input_type: str | None) -> list[list[float]]:
        payload: dict[str, Any] = {
            "input": texts,
            "model": self.config.model,
            "output_dimension": self.config.embedding_dims,
            "truncation": True,
        }
        if input_type:
            payload["input_type"] = input_type
        # No encoding_format key → the API returns plain float lists.
        for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
            response = httpx.post(
                self._url,
                json=payload,
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                timeout=_TIMEOUT_SECONDS,
            )
            if response.status_code != 429:
                break
            if attempt == _RATE_LIMIT_MAX_RETRIES:
                break  # fall through to the error below with the 429 body
            wait = _RATE_LIMIT_DEFAULT_WAIT_SECONDS
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.strip().isdigit():
                wait = min(float(retry_after), _RATE_LIMIT_MAX_WAIT_SECONDS)
            logger.warning(
                "Voyage embeddings rate-limited (429) — waiting %.0fs "
                "(attempt %d/%d; a keyless free-tier account is capped at 3 RPM)",
                wait,
                attempt + 1,
                _RATE_LIMIT_MAX_RETRIES,
            )
            time.sleep(wait)
        if response.status_code >= 400:
            # Include the response BODY — a bare "403 Forbidden" is
            # undiagnosable (invalid key? unknown model? quota?), and Voyage
            # puts the reason in the body. Never echoes the request/key.
            raise RuntimeError(
                f"Voyage embeddings HTTP {response.status_code} for model "
                f"{self.config.model!r}: {response.text[:300]}"
            )
        data = response.json()["data"]
        # Voyage returns items with an ``index`` field; sort defensively so
        # vectors line up with input order even if the API reorders.
        data.sort(key=lambda item: item.get("index", 0))
        return [item["embedding"] for item in data]

    def embed(
        self,
        text: str,
        memory_action: Literal["add", "search", "update"] | None = None,
    ) -> list[float]:
        return self._request([text], _INPUT_TYPE.get(memory_action or "add"))[0]

    def embed_batch(self, texts: list[str], memory_action: str = "add") -> list[list[float]]:
        """Native batch support — one API call per ``_BATCH_SIZE`` chunk
        instead of mem0's default per-text loop."""
        input_type = _INPUT_TYPE.get(memory_action or "add")
        out: list[list[float]] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            out.extend(self._request(texts[start : start + _BATCH_SIZE], input_type))
        return out


def register_voyage_provider() -> None:
    """Idempotently register ``provider: "voyage"`` in mem0's EmbedderFactory.

    mem0's factory resolves providers from a class-level dict of import paths,
    so adding a key is the supported extension seam (no subclassing, no fork).
    Must run before ``AsyncMemory.from_config`` sees the config. Raises if mem0
    is missing — callers (manager.initialize) already degrade gracefully.
    """
    from mem0.utils.factory import EmbedderFactory

    EmbedderFactory.provider_to_class.setdefault(
        "voyage", "helixis.memory.voyage_embedder.VoyageEmbedder"
    )
