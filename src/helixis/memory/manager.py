"""Persistent memory manager using Mem0 AsyncMemory.

Stores property details, tenant histories, management preferences,
and workflow patterns across sessions. Scoped per property management
company — every user in the same company shares one memory pool, so
a fact stored by one team member's agent is retrievable by any other.

All Mem0 calls go through AsyncMemory for true non-blocking I/O.
v3 API note: entity IDs go inside `filters` for search() and get_all();
add() still accepts them as top-level kwargs.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, NamedTuple, cast

from helixis.memory.audit import write_audit
from helixis.observability.sentry_setup import capture_swallowed
from helixis.security.pii import redact_for_memory
from helixis.settings import Settings

logger = logging.getLogger(__name__)

_memory_manager: MemoryManager | None = None

# Tables Mem0 creates at runtime via the service-role db_url. They are never in
# a migration, so RLS must be enforced here (and in 0025) — otherwise they land
# in the ``public`` schema RLS-OFF and are readable cross-tenant via PostgREST
# by anyone with the anon key (risk #1 / Supabase lint 0013).
_MEM0_RUNTIME_TABLES = (
    "helixis_memories",
    "helixis_memories_entities",
    "mem0migrations",
)

# Category taxonomy used in metadata and surfaced to the agent via prompt.
MEMORY_CATEGORIES = {
    "tenant": "Tenant names, contact info, lease terms, payment history, complaints",
    "property": "Property addresses, unit details, amenities, condition notes",
    "maintenance": "Work orders, vendor contacts, repair history, recurring issues",
    "financial": "Rent amounts, fee schedules, budget items, delinquency status",
    "preference": "Company workflows, communication preferences, escalation rules",
    "vendor": "Vendor names, contact info, service areas, pricing, reliability notes",
    "lease": "Lease terms, renewal dates, special clauses, move-in/move-out dates",
    "compliance": "Inspection schedules, code violations, insurance requirements",
}


# Mem0's fact-extraction model. Haiku: extraction is a cheap structured task
# and this keeps memory writes off the Sonnet spend line. Anthropic API alias
# (mem0's anthropic provider talks to the raw SDK, not LiteLLM — no
# ``anthropic/`` prefix here).
MEM0_LLM_MODEL = "claude-haiku-4-5"


def build_mem0_config(settings: Settings) -> tuple[dict[str, Any] | None, str]:
    """Build the Mem0 ``from_config`` dict, or ``(None, reason)`` when a
    required key is missing.

    Pure — no mem0 import, unit-testable without the ``[memory]`` extra.
    Dimension discipline: ``settings.memory.embedding_dims`` feeds BOTH the
    embedder (explicit ``output_dimension`` on every Voyage call) and the
    pgvector column, so they can never drift apart.
    """
    voyage_key = settings.llm.voyage_api_key
    if not voyage_key:
        return (
            None,
            "HELIXIS_LLM__VOYAGE_API_KEY missing — the memory embedder is "
            "Voyage (Anthropic has no embeddings API; gotcha 18).",
        )
    anthropic_key = settings.llm.anthropic_api_key
    if not anthropic_key:
        return (
            None,
            "HELIXIS_LLM__ANTHROPIC_API_KEY missing — Mem0's fact-extraction "
            "LLM runs on Anthropic.",
        )

    config: dict[str, Any] = {
        "embedder": {
            # Registered by ``voyage_embedder.register_voyage_provider``.
            "provider": "voyage",
            "config": {
                "model": settings.memory.embedding_model,
                "api_key": voyage_key,
                "embedding_dims": settings.memory.embedding_dims,
            },
        },
        "llm": {
            "provider": "anthropic",
            "config": {
                "model": MEM0_LLM_MODEL,
                "api_key": anthropic_key,
                # ⚠ Turns OFF mem0's sampling params. Omitting ``temperature``
                # here does NOT stop mem0 sending one — that was the #878 fix
                # and it did not work.
                #
                # The failure: ``Messages.create() got an unexpected keyword
                # argument 'temperature'`` on EVERY post-turn extraction
                # (Sentry PYTHON-FASTAPI-B/-3B/-3C, 2026-08-23). #878 removed
                # the key from this dict at 00:53 UTC; the daily smoke failed
                # with the SAME error at 11:32 UTC, eleven hours later. The
                # omission was inert because mem0 supplies its own default:
                # ``BaseLlmConfig.temperature`` is ``0.1``, and
                # ``AnthropicLLM._get_common_params`` injects it whenever
                # ``_enable_sampling_parameters()`` is true — which, for a
                # model whose family parses as ``haiku``, is unconditionally
                # true. Our config never had a vote.
                #
                # ``enable_sampling_parameters`` IS the vote: the method reads
                # it FIRST and returns it verbatim when not None, so False
                # sends neither temperature nor top_p. A first-class mem0 knob,
                # not a monkeypatch.
                #
                # Root cause is dependency drift (issue #535): ``anthropic`` is
                # unpinned at ``>=0.40`` and **1.0.0 removed temperature, top_p
                # AND top_k** from ``Messages.create`` — verified against the
                # 1.0.0 sdist, which also introduces an ``httpx2`` dependency.
                # ``Messages.create`` takes no ``**kwargs``, so a dropped param
                # is a hard TypeError rather than an ignored key.
                #
                # Deliberately NOT fixed by pinning ``anthropic<1.0``, and #878
                # was right about why: LiteLLM drives the PRODUCTION chat path
                # through the same package, so a ceiling there risks live chat
                # to stabilise a best-effort enhancement. This knob is version-
                # agnostic — a future SDK that drops the param again cannot
                # break extraction, because we no longer send it.
                #
                # Cost: fact extraction runs at Anthropic's default sampling
                # instead of 0.1. Determinism on a best-effort enhancement,
                # traded for it running at all.
                "enable_sampling_parameters": False,
                # Mem0's BaseLlmConfig defaults max_tokens to 2000, which the
                # fact-extraction call can exceed on long conversations — the
                # response gets cut off mid-JSON ("Unterminated string" parse
                # errors, PYTHON-FASTAPI-2N / -E). Mem0 degrades gracefully on
                # a parse failure (skips extraction for that turn), but a
                # higher cap avoids truncating the extraction JSON at all.
                "max_tokens": 4000,
            },
        },
    }

    db_url = settings.supabase.db_url
    if db_url:
        # When the Supabase pooler URL is set, pin Mem0's vector store to
        # pgvector on that database. Otherwise let Mem0 fall back to its
        # default (Qdrant in-memory) so the backend still boots cleanly with
        # memory enabled in dev.
        config["vector_store"] = {
            "provider": "pgvector",
            "config": {
                "connection_string": db_url,
                "collection_name": "helixis_memories",
                # Must equal the embedder's output dimension AND the live
                # ``helixis_memories`` vector(N) column.
                "embedding_model_dims": settings.memory.embedding_dims,
            },
        }

    return (config, "")


class MemoryRead(NamedTuple):
    """What a memory search actually established.

    ``ok=False`` means the search DID NOT RUN — never that the company has no
    matching memories. Callers that put words in front of a user must keep the
    two apart; the whole point of this type is that ``[]`` cannot express it.
    """

    hits: tuple[dict[str, Any], ...]
    ok: bool
    reason: str


def recorded_age(stored_at: Any, *, now: datetime | None = None) -> str:
    """How long ago a memory was written down, or ``""`` if it cannot be dated.

    ⚠ **AN UNDATED MEMORY IS PRESENTED TO THE MODEL AS A CURRENT FACT.**
    ``search_for_context`` read ``category`` and ``stored_by`` off the metadata
    and dropped ``stored_at``, which every row carries — measured on
    helixis-test, **585 of 585 rows, spanning 27 Jun to 7 Sep**. So a note
    written in June ("waive the late fee for this month") was injected in
    September looking exactly like one written yesterday, under a header that
    calls the block *"facts previously stored by your team"*. The field was
    already in hand and thrown away — the same shape as gotchas 41/50/51/88/
    92/93/94/98, and here it makes a TIME-RELATIVE instruction unanchored.

    ⚠ **NO COMPANY CLOCK, and that is the point rather than an oversight.**
    Gotchas 91/97 make "did you resolve the customer's timezone?" the right
    question to ask of anything date-shaped in this product. A DURATION has no
    timezone — 8 weeks is 8 weeks in Honolulu — so introducing one here would
    add a per-company read that cannot change the answer.

    ⚠ **A default ``now`` is correct HERE and was refused for the reminder
    sweep** (gotcha 91). There the parameter carried a per-company fact, so a
    default silently used the wrong company's date; this one is a UTC instant
    with no tenant meaning, and the default exists only so tests can pin it.

    ⚠ **A naive timestamp is read as UTC via ``replace``, never
    ``astimezone``** — gotcha 91 records that distinction, and comparing a
    naive value against an aware ``now`` raises ``TypeError`` outright. Every
    live row carries an offset today; a future writer might not.

    ⚠ **A FUTURE timestamp clamps to "today".** Clock skew must not render
    "recorded in 3 days", which reads as a bug in front of a user and tells a
    model something impossible.
    """
    if not isinstance(stored_at, str) or not stored_at.strip():
        return ""
    try:
        when = datetime.fromisoformat(stored_at.strip())
    except ValueError:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    days = ((now or datetime.now(UTC)) - when).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    if days < 60:
        return f"{days // 7} weeks ago"
    return f"{days // 30} months ago"


class MemoryManager:
    """Facade over Mem0 AsyncMemory for property-management memory."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._mem0: Any = None  # AsyncMemory instance once initialized
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize Mem0 with the Voyage embedder + Anthropic LLM, pgvector
        when available.

        Post-Gemini-exit wiring (gotcha 18): Anthropic has no embeddings API,
        so embeddings go to Voyage via our own registered provider
        (``memory/voyage_embedder.py`` — direct httpx, no langchain), and
        mem0's fact-extraction LLM runs on Anthropic (its native provider,
        ``anthropic`` package via the ``[memory]`` extra). Both keys are
        required — refusing to init beats a half-broken manager whose first
        embed call fails.
        """
        if not self.settings.memory.enabled:
            logger.info("Memory disabled in settings")
            return

        try:
            from mem0 import AsyncMemory
            from mem0.configs.base import MemoryConfig
            from mem0.embeddings.configs import EmbedderConfig

            config, disabled_reason = build_mem0_config(self.settings)
            if config is None:
                logger.warning("Memory disabled: %s", disabled_reason)
                return

            # Register our "voyage" embedder provider in mem0's factory
            # BEFORE the config resolves the embedder. Idempotent.
            from helixis.memory.voyage_embedder import register_voyage_provider

            register_voyage_provider()

            db_url = self.settings.supabase.db_url

            # ⚠ Do NOT use ``AsyncMemory.from_config`` here: mem0's
            # EmbedderConfig carries a pydantic validator with a HARDCODED
            # provider allowlist (both 2.0.12 and 2.0.13) that rejects our
            # registered "voyage" provider before the factory ever runs —
            # this red-flagged re-embed run #4 with "Unsupported embedding
            # provider: voyage". The embedder sub-config is therefore built
            # with pydantic's official no-validation constructor
            # (``model_construct``); llm / vector_store keep full validation,
            # and the factory still resolves "voyage" through the registered
            # seam. (mem0 2.x constructors are SYNC — the async surface is
            # add()/search()/get_all(), which we await.)
            embedder_cfg = EmbedderConfig.model_construct(
                provider="voyage", config=config["embedder"]["config"]
            )
            mc_kwargs: dict[str, Any] = {"embedder": embedder_cfg, "llm": config["llm"]}
            if "vector_store" in config:
                mc_kwargs["vector_store"] = config["vector_store"]
            self._mem0 = AsyncMemory(config=MemoryConfig(**mc_kwargs))
            self._initialized = True
            logger.info(
                "Mem0 initialized with embedder=voyage/%s llm=anthropic vector_store=%s",
                self.settings.memory.embedding_model,
                "pgvector" if db_url else "default",
            )

            # Mem0's pgvector store creates its tables in the ``public`` schema
            # via the raw db_url, with RLS OFF. Lock them down (deny-all) right
            # after init so a fresh environment never leaves tenant memory
            # exposed to anon/authenticated through PostgREST. No-op when Mem0
            # runs on its in-memory default (no db_url).
            if db_url:
                await self._enable_rls_on_mem0_tables(db_url)

        except ModuleNotFoundError:
            logger.warning("mem0ai not installed — memory disabled")
        except ImportError as e:
            # mem0 IS installed but importing AsyncMemory failed — almost
            # always a version mismatch from an unpinned dep. Surface the real
            # error rather than the misleading "not installed".
            logger.error("Memory disabled — mem0 import failed (version mismatch?): %s", e)
        except (Exception, SystemExit) as e:  # noqa: BLE001 — never let memory break the app
            logger.error("Failed to initialize memory: %s", e)
            capture_swallowed(e, op="memory.initialize")

    @staticmethod
    async def _enable_rls_on_mem0_tables(db_url: str) -> None:
        """Idempotently enable RLS (deny-all) on Mem0's runtime tables.

        Mem0 creates ``helixis_memories`` / ``helixis_memories_entities`` /
        ``mem0migrations`` in the ``public`` schema with RLS off. Enabling RLS
        with no policy denies all anon/authenticated access while leaving the
        backend's privileged pool (which bypasses RLS) untouched — the same
        deny-all pattern as ``helixis_gmail_seen``.

        Best-effort: a failure here must NEVER break boot. Per gotcha #7 we
        catch ``SystemExit`` too (a ``BaseException``) so an asyncpg/spaCy-style
        ``sys.exit`` can't slip past and kill the lifespan.
        """
        try:
            import asyncpg

            conn = await asyncpg.connect(db_url, statement_cache_size=0)
            try:
                for table in _MEM0_RUNTIME_TABLES:
                    # Identifiers are a fixed module constant, never user input.
                    await conn.execute(
                        f"ALTER TABLE IF EXISTS public.{table} ENABLE ROW LEVEL SECURITY"
                    )
                logger.info(
                    "RLS enabled on Mem0 runtime tables: %s",
                    ", ".join(_MEM0_RUNTIME_TABLES),
                )
            finally:
                await conn.close()
        except (Exception, SystemExit) as e:  # noqa: BLE001 — never let this break boot
            logger.warning("Could not enable RLS on Mem0 runtime tables (non-fatal): %s", e)

    @property
    def available(self) -> bool:
        return self._initialized and self._mem0 is not None

    async def health_probe(self) -> tuple[bool, str]:
        """Actively exercise the memory READ path so a silent degradation
        surfaces as a hard signal.

        The everyday :meth:`search` deliberately swallows every failure and
        returns ``[]`` — memory is an enhancement, never load-bearing — which
        is correct for a request path but means a broken embedder (e.g. a 403
        ``PERMISSION_DENIED`` on Gemini's ``BatchEmbedContents``, which the
        query-embed step hits) degrades to "no memory" INVISIBLY and can stay
        broken for weeks (it did: ~19 days, found in the 2026-07-12 audit).

        This probe runs the same query-embed + vector-search against a
        throwaway partition (no tenant data touched, no write) but does NOT
        swallow — it returns ``(ok, detail)`` so the smoke healthcheck
        (:func:`helixis.smoke.checks.check_memory`) can fail loudly and
        auto-file a ``[smoke]`` issue. Never called on a request path.
        """
        if not self.available:
            return (False, "memory not initialized")
        try:
            # Query embed fires regardless of whether the partition has rows,
            # so an embedder 403 surfaces even against an empty pool.
            await self._mem0.search(
                "helixis smoke health probe",
                filters={"user_id": "__smoke_health__"},
                top_k=1,
            )
        except (Exception, SystemExit) as e:  # noqa: BLE001 — surface, don't swallow
            return (False, f"{type(e).__name__}: {e}")

        # Model-drift guard: today's Gemini→Voyage mismatch is loud only
        # because the DIMENSIONS differ (1536 vs 1024, pgvector errors). A
        # future same-dimension embedder change would corrupt retrieval with
        # zero errors — vectors from different models aren't comparable. Every
        # row is stamped with the model that embedded it (add/extract/re-embed
        # paths); any stamp that isn't the configured model fails the daily
        # smoke until the re-embed workflow is run.
        stored = await self._stored_embedding_models()
        foreign = stored - {self.settings.memory.embedding_model}
        if foreign:
            return (
                False,
                f"stored vectors embedded with {sorted(foreign)} but config is "
                f"{self.settings.memory.embedding_model!r} — mixed models silently "
                "break retrieval; run the 'Memory re-embed' workflow",
            )
        # EXTRACTION is a third failure surface, and until 2026-08-24 nothing
        # watched it. The probe above covers the embedder (query-embed) and
        # the store (vector search) — both READ-side. The post-turn write path
        # runs mem0's fact-extraction LLM, and on 2026-08-23 that LLM raised
        # ``Messages.create() got an unexpected keyword argument
        # 'temperature'`` on EVERY turn: the assistant silently stopped
        # learning while recall kept working, so every signal here stayed
        # green. Same silent-degradation class as the embedder 403 this probe
        # was built for (gotcha 17) — one path further along.
        #
        # Costs one LLM call a day. The write lands in the throwaway partition
        # and is removed immediately; a delete failure is NOT a probe failure
        # (the row is inert test text in a partition no tenant reads), so it
        # only downgrades the detail line.
        try:
            await self._mem0.add(
                "Smoke health probe: the office plant is named Kevin.",
                user_id="__smoke_health__",
                metadata={"helixis_smoke": True},
            )
        except (Exception, SystemExit) as e:  # noqa: BLE001 — surface, don't swallow
            return (False, f"extraction failed: {type(e).__name__}: {e}")

        swept = "swept"
        try:
            await self._mem0.delete_all(user_id="__smoke_health__")
        except (Exception, SystemExit):  # noqa: BLE001
            swept = "probe row left behind"

        return (True, f"embedder + vector search + extraction reachable ({swept})")

    async def _stored_embedding_models(self) -> set[str]:
        """Distinct ``embedding_model`` stamps present in the live collection.

        Best-effort (returns empty on any failure or when no db_url is set —
        the probe's job is drift DETECTION, not availability, and rows without
        a stamp are treated as unknown rather than foreign so pre-stamp
        history can't false-alarm)."""
        db_url = self.settings.supabase.db_url
        if not db_url:
            return set()
        try:
            import asyncpg

            conn = await asyncpg.connect(db_url, statement_cache_size=0)
            try:
                rows = await conn.fetch(
                    "SELECT DISTINCT payload->>'embedding_model' AS model "
                    "FROM helixis_memories WHERE payload ? 'embedding_model' LIMIT 5"
                )
                return {r["model"] for r in rows if r["model"]}
            finally:
                await conn.close()
        except (Exception, SystemExit):  # noqa: BLE001 — detection is best-effort
            return set()

    @staticmethod
    def _require_company(company_id: str, action: str) -> bool:
        """Refuse memory operations that have no company scope.

        Historically an empty ``company_id`` fell back to a single shared
        ``"default"`` Mem0 partition — which meant every caller without a
        company (mis-scoped run, missing context) READ AND WROTE the same
        pool across tenants. That's a cross-tenant leak vector, so the
        fallback is gone: no company, no memory op. Memory is an
        enhancement, never load-bearing — skipping is always safe.
        """
        if company_id:
            return True
        logger.warning(
            "memory %s refused: no company_id (shared-partition fallback removed)", action
        )
        return False

    # ── Store ─────────────────────────────────────────────────────────

    async def add(
        self,
        content: str,
        company_id: str = "",
        *,
        stored_by: str = "",
        category: str = "",
        extra_metadata: dict[str, Any] | None = None,
    ) -> str | None:
        """Store a memory scoped to a company.

        Args:
            content: The fact to store.
            company_id: Company scope key (becomes Mem0 user_id). REQUIRED —
                an empty value is refused rather than falling back to a
                shared partition (see ``_require_company``).
            stored_by: The user_id of whoever triggered this storage.
            category: One of MEMORY_CATEGORIES keys, or empty.
            extra_metadata: Any additional metadata to attach.
        """
        if not self.available:
            return None
        if not self._require_company(company_id, "add"):
            return None

        # Strip the never-store PII classes (SSN / credit card / IBAN) BEFORE
        # anything touches the vector store or the audit row. Email / phone /
        # names are intentionally preserved — the agent needs to recall them.
        content = redact_for_memory(content)

        try:
            metadata: dict[str, Any] = {
                "stored_by": stored_by or "system",
                "stored_at": datetime.now(UTC).isoformat(),
                # Which model embedded this row — makes a future embedder
                # change DETECTABLE (health_probe fails on a foreign stamp)
                # instead of silently corrupting retrieval.
                "embedding_model": self.settings.memory.embedding_model,
                "embedding_dims": self.settings.memory.embedding_dims,
            }
            if category:
                metadata["category"] = category
            if extra_metadata:
                metadata.update(extra_metadata)

            # add() accepts user_id at the top level in both v2 and v3.
            result = await self._mem0.add(
                content,
                user_id=company_id,
                metadata=metadata,
            )
            # Best-effort audit row. Never raises — audit failures must
            # not regress a successful memory write.
            await write_audit(
                company_id=company_id,
                user_id=stored_by,
                action="add",
                content=content,
                metadata=metadata,
            )
            return str(result)
        except (Exception, SystemExit) as e:  # noqa: BLE001
            logger.error("Failed to store memory: %s", e)
            capture_swallowed(e, op="memory.add")
            return None

    # ── Search ────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        company_id: str = "",
        top_k: int | None = None,
        *,
        searched_by: str = "",
    ) -> list[dict[str, Any]]:
        """Search memories by semantic similarity.

        Degrades to ``[]`` on any failure. :meth:`search_checked` is the
        implementation — prefer it whenever the result will be turned into
        words in front of a person, because ``[]`` here cannot tell you which
        of the two things happened.

        ``searched_by`` is the auth.users(id) UUID of whoever triggered the
        search. Surfaced into the memory_audit row; not used by Mem0 itself.
        """
        read = await self.search_checked(
            query, company_id=company_id, top_k=top_k, searched_by=searched_by
        )
        return list(read.hits)

    async def search_checked(
        self,
        query: str,
        company_id: str = "",
        top_k: int | None = None,
        *,
        searched_by: str = "",
    ) -> MemoryRead:
        """Search, and report whether the search ACTUALLY RAN.

        ⚠ This is the implementation; :meth:`search` wraps it and drops the
        flag. It exists because ``[]`` cannot distinguish "searched, found
        nothing" from "could not reach the embedder", and those need opposite
        words in front of a user. The agent tool rendered both as *"No relevant
        memories found"* — a confident false absence, which is this product's
        worst failure mode (gotchas 26/42).

        Measured 2026-09-02: memory search failed on **72% of calls under
        burst** — a keyless embedder account is capped at 3 requests per minute
        ACCOUNT-WIDE, shared across every tenant — so the agent has been
        asserting an emptiness it never established.

        The degradation is unchanged and deliberate: memory is an enhancement,
        never load-bearing (gotchas 7/17). Nothing here raises. What changed is
        only what the caller is TOLD.
        """
        if not self.available:
            return MemoryRead((), ok=False, reason="memory_unavailable")
        if not self._require_company(company_id, "search"):
            return MemoryRead((), ok=False, reason="no_company_scope")

        try:
            # v3: entity IDs go inside filters for search(). Hard-bounded:
            # a slow/hung embedder must degrade to "no memory" in a few
            # seconds, never stall the chat turn (the ~80s pre-turn stall the
            # 2026-07-25 stress test measured on prod). ``asyncio.timeout``
            # (not ``wait_for``) runs the coro in THIS task, so a spaCy
            # SystemExit still propagates into the except below (gotcha 7) —
            # wait_for's Task wrapper would re-raise it at the loop level and
            # break that containment. TimeoutError is an Exception, caught here.
            async with asyncio.timeout(self.settings.memory.search_timeout_s):
                results = await self._mem0.search(
                    query,
                    filters={"user_id": company_id},
                    top_k=top_k or self.settings.memory.search_top_k,
                )
            if isinstance(results, dict):
                hits = results.get("results", [])
            elif isinstance(results, list):
                hits = results
            else:
                hits = []
            # Best-effort audit. Skipped silently if no searched_by given
            # (system/cron contexts where there is no human user).
            await write_audit(
                company_id=company_id,
                user_id=searched_by,
                action="search",
                query=query,
                metadata={"hit_count": len(hits)},
            )
            return MemoryRead(tuple(cast(list[dict[str, Any]], hits)), ok=True, reason="")
        except (Exception, SystemExit) as e:  # noqa: BLE001
            # SystemExit is deliberately caught (it is NOT an Exception): mem0
            # 2.x runs spaCy-backed BM25 lemmatization on the query, and when
            # en_core_web_sm isn't baked into the image it tries to pip-download
            # it at runtime → spaCy's run_command sys.exit(1)s. Memory is an
            # enhancement, never load-bearing — a failure here MUST degrade,
            # never crash the agent run (the live sev-1: #322 made the embedder
            # work, so search reached this spaCy step and SystemExit killed
            # run_agent_streaming on every turn). Same class as the #313
            # presidio fix in security/pii.py.
            logger.error("Failed to search memory: %s", e)
            capture_swallowed(e, op="memory.search")
            return MemoryRead((), ok=False, reason="backend_error")

    # ── Auto-context injection ────────────────────────────────────────

    async def search_for_context(
        self,
        query: str,
        company_id: str = "",
        top_k: int | None = None,
        *,
        searched_by: str = "",
    ) -> str:
        """Search memories and return a formatted string for LLM injection.

        Called by the orchestrator before each run so the agent starts
        with relevant company memory already in its context — without
        having to decide to call search_memory itself.

        ``searched_by`` flows through to memory_audit. Defaults to "" so
        existing call sites keep working; the orchestrator passes the
        real auth user id once on every run.
        """
        results = await self.search(
            query,
            company_id=company_id,
            top_k=top_k or 5,
            searched_by=searched_by,
        )
        if not results:
            return ""

        lines: list[str] = []
        now = datetime.now(UTC)
        for mem in results:
            text = mem.get("memory", mem.get("content", ""))
            meta = mem.get("metadata") or {}
            category = meta.get("category", "")
            stored_by = meta.get("stored_by", "")

            prefix = ""
            if category:
                prefix = f"[{category}] "
            if stored_by and stored_by != "system":
                prefix += f"(via {stored_by}) "

            # ⚠ TRAILING, not leading. The age is provenance on every line; put
            # it in the prefix and it pushes the actual fact off the start of
            # the row, where a skimming model reads it last. An undateable row
            # renders with no age rather than a guessed one (gotchas 55/57) —
            # measured at 0 of 585 live rows today, so this is the future
            # writer's case, not a present one.
            age = recorded_age(meta.get("stored_at"), now=now)
            suffix = f" (recorded {age})" if age else ""

            lines.append(f"- {prefix}{text}{suffix}")

        return "\n".join(lines)

    # ── Post-run extraction ───────────────────────────────────────────

    async def extract_and_store(
        self,
        conversation: list[dict[str, str]],
        company_id: str = "",
        stored_by: str = "",
    ) -> list[str]:
        """Extract memorable facts from a completed conversation and store them.

        Relies on Mem0's built-in inference — pass the raw conversation
        and Mem0 extracts salient facts, handling deduplication and
        contradiction resolution internally.
        """
        if not self.available or not conversation:
            return []
        if not self._require_company(company_id, "extract_and_store"):
            return []

        # Redact never-store PII from each message before Mem0's extractor sees
        # the conversation, so SSN/CC/IBAN can't be inferred into a stored fact.
        conversation = [
            {**m, "content": redact_for_memory(str(m.get("content", "")))} if "content" in m else m
            for m in conversation
        ]

        try:
            metadata = {
                "stored_by": stored_by or "system",
                "stored_at": datetime.now(UTC).isoformat(),
                "source": "auto_extraction",
                # See add(): per-row model stamp for drift detection.
                "embedding_model": self.settings.memory.embedding_model,
                "embedding_dims": self.settings.memory.embedding_dims,
            }

            result = await self._mem0.add(
                conversation,
                user_id=company_id,
                metadata=metadata,
            )

            stored: list[str] = []
            if isinstance(result, dict):
                for event in result.get("results", []):
                    if event.get("event") == "ADD":
                        stored.append(event.get("memory", ""))

            # Best-effort audit row for the auto-extraction write, mirroring
            # add(). Records the facts Mem0 actually extracted (not the raw
            # conversation) so the audit trail shows what entered the pool.
            # Never raises — audit failures must not regress a successful write.
            await write_audit(
                company_id=company_id,
                user_id=stored_by,
                action="add",
                content="\n".join(stored),
                metadata={**metadata, "extracted_count": len(stored)},
            )
            return stored

        except (Exception, SystemExit) as e:  # noqa: BLE001
            logger.error("Failed to extract memories: %s", e)
            capture_swallowed(e, op="memory.extract_and_store")
            return []

    # ── Retrieval ─────────────────────────────────────────────────────

    async def get_all(self, company_id: str = "") -> list[dict[str, Any]]:
        """Get all memories for a company."""
        if not self.available:
            return []
        if not self._require_company(company_id, "get_all"):
            return []

        try:
            # v3: entity IDs go inside filters for get_all()
            results = await self._mem0.get_all(filters={"user_id": company_id})
            if isinstance(results, dict):
                return cast(list[dict[str, Any]], results.get("results", []))
            return results if isinstance(results, list) else []
        except (Exception, SystemExit) as e:  # noqa: BLE001
            logger.error("Failed to get memories: %s", e)
            capture_swallowed(e, op="memory.get_all")
            return []

    # ── Deletion ──────────────────────────────────────────────────────

    async def delete(self, memory_id: str, company_id: str) -> bool:
        """Delete a specific memory — only if it belongs to ``company_id``.

        This was the ONE memory operation without a company scope check
        (2026-07-22 platform audit): a bare memory_id could delete another
        tenant's row. Now the memory is fetched first and its ``user_id``
        (the company partition key) must match the requesting company, or
        the delete is refused. No callers existed at hardening time, so any
        future caller starts scoped.
        """
        if not self.available:
            return False
        if not self._require_company(company_id, "delete"):
            return False

        try:
            existing = await self._mem0.get(memory_id)
            if not existing:
                logger.warning("memory delete refused: %s not found", memory_id)
                return False
            owner = str(existing.get("user_id") or "")
            if owner != company_id:
                logger.warning(
                    "memory delete REFUSED: %s belongs to a different company", memory_id
                )
                return False
            await self._mem0.delete(memory_id=memory_id)
            await write_audit(
                company_id=company_id,
                user_id="",
                action="delete",
                metadata={"memory_id": memory_id},
            )
            return True
        except (Exception, SystemExit) as e:  # noqa: BLE001
            logger.error("Failed to delete memory: %s", e)
            capture_swallowed(e, op="memory.delete")
            return False

    async def delete_company_memory(
        self, company_id: str, *, requested_by: str = "", raise_on_error: bool = False
    ) -> int:
        """Erase ALL memory for a company — right-to-erasure / tenant offboarding.

        SOC2/GDPR/CCPA: a company must be able to have its entire memory pool
        deleted. Returns the number of memories removed. Prefers Mem0's atomic
        ``delete_all(user_id=...)`` (``company_id`` IS the partition key); falls
        back to enumerate-then-delete on a Mem0 that lacks it. Writes one audit
        row recording the bulk erasure (attributed to ``requested_by`` when an
        admin UUID is given; skipped silently otherwise, like all audit writes).

        ⚠ ``raise_on_error``: a failure returns 0 by default, which is right for
        a chat turn and wrong for an erasure, where a silent 0 reads as "there
        was nothing to delete". The workspace delete route passes True so it can
        report the memory leg as unfinished instead.
        """
        if not self.available or not company_id:
            return 0

        try:
            # Snapshot the count first so we can report + audit how much was erased.
            existing = await self.get_all(company_id=company_id)
            count = len(existing)

            delete_all = getattr(self._mem0, "delete_all", None)
            if callable(delete_all):
                await delete_all(user_id=company_id)
            else:
                for mem in existing:
                    mem_id = mem.get("id")
                    if mem_id:
                        await self._mem0.delete(memory_id=str(mem_id))

            await write_audit(
                company_id=company_id,
                user_id=requested_by,
                action="delete_company",
                metadata={"erased_count": count},
            )
            logger.info("Erased %d memories for company %s (right-to-erasure)", count, company_id)
            return count
        except (Exception, SystemExit) as e:  # noqa: BLE001
            logger.error("Failed to erase company memory: %s", e)
            if raise_on_error:
                raise
            capture_swallowed(e, op="memory.delete_company")
            return 0

    async def delete_subject_memory(
        self,
        *,
        company_id: str,
        entity_type: str,
        buildium_id: int | None = None,
        email: str | None = None,
        requested_by: str = "",
    ) -> int:
        """Erase memories about ONE person within a company's pool.

        The company-level eraser above is all-or-nothing, which cannot serve an
        individual's deletion request without destroying everyone else's data.
        Mem0 partitions on ``company_id``, so there is no subject partition to
        drop — the pool is enumerated and matched.

        ⚠ **Matches only on real IDENTIFIERS — email and Buildium id — and
        never on a name.** Two tenants share a surname more often than not, and
        this codebase has already shipped one confident cross-person mistake
        from exactly that kind of loose join (the Whitfield misattribution).
        Deleting on a name would silently erase a DIFFERENT person's records to
        satisfy this one's request: an irreversible privacy harm committed in
        the name of privacy. Name-only references therefore survive, and the
        erasure report says so rather than implying the pool is clean.

        ⚠ **Raises on failure, unlike every other method here.** Memory is an
        enhancement on a chat turn and degrading quietly is right there
        (gotcha 17) — but a silent 0 from an erasure reads as "there was
        nothing to delete", which is the one wrong answer that looks like
        success. The caller records the failure instead.
        """
        if not self.available or not company_id:
            return 0
        needles = [n.lower() for n in (email, str(buildium_id) if buildium_id else None) if n]
        if not needles:
            return 0

        existing = await self.get_all(company_id=company_id)
        deleted = 0
        for mem in existing:
            haystack = f"{mem.get('memory', '')} {mem.get('metadata', '')}".lower()
            if not any(n in haystack for n in needles):
                continue
            mem_id = mem.get("id")
            if mem_id and await self.delete(str(mem_id), company_id):
                deleted += 1

        # ⚠ ``action="delete"``, NOT a new ``"delete_subject"`` value, and the
        # subject detail rides in ``metadata`` instead.
        #
        # ``memory_audit.action`` carries a CHECK constraint pinned to exactly
        # six values (live-verified 2026-08-29:
        # ``memory_audit_action_check``), so a seventh would need a migration —
        # and migrations here are applied BY HAND (gotcha 8), meaning a
        # committed one is not an applied one. Worse, ``write_audit`` swallows
        # its own failures by design, so between merge and apply every subject
        # erasure would have written NO audit row and said nothing about it: a
        # silently missing legal audit trail, which is the one place this
        # product's degrade-quietly habit is least affordable.
        #
        # ``delete`` is also the honest verb. ``delete_company`` exists because
        # it is one bulk all-or-nothing wipe; a subject erasure is a set of
        # ordinary per-memory deletes, which is what this loop just did.
        await write_audit(
            company_id=company_id,
            user_id=requested_by,
            action="delete",
            metadata={
                "scope": "subject",
                "entity_type": entity_type,
                "erased_count": deleted,
            },
        )
        logger.info(
            "Erased %d memories for %s in company %s (subject erasure)",
            deleted,
            entity_type,
            company_id,
        )
        return deleted


def get_memory_manager() -> MemoryManager | None:
    return _memory_manager


def set_memory_manager(manager: MemoryManager) -> None:
    global _memory_manager
    _memory_manager = manager
