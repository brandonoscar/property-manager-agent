"""Individual real-service checks. Each returns a :class:`CheckResult`.

All checks are read-only EXCEPT :func:`check_langfuse`, which emits
one idempotent ``smoke_test`` trace as the only way to validate that
ingestion is live end-to-end.

Heavy imports (``litellm``, ``composio``, the Langfuse SDK) live
inside each function so ``python -m helixis.smoke --help`` stays
sub-second even when those packages are slow to load.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from helixis.billing.plans import PLANS
from helixis.settings import Settings
from helixis.storage_buckets import ALL_BUCKET_NAMES


@dataclass
class CheckResult:
    name: str
    status: str  # "pass" | "fail" | "skipped"
    latency_ms: int
    detail: str

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    @property
    def failed(self) -> bool:
        return self.status == "fail"

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


# Accepted one-word acknowledgements from the LLM. Anything past 20
# chars (single token) or containing whitespace beyond a trailing
# punctuation isn't a one-word ack — fail those as "didn't follow the
# prompt" rather than just "wrong word".
_EXPECTED_ACK_WORDS = frozenset({"ok", "okay", "yes", "sure", "hi", "hello", "affirmative"})


def _is_coherent_one_word_ack(text: str) -> bool:
    """True when ``text`` looks like a single-word ack from the LLM.

    Strip trailing punctuation (``ok.``, ``OK!``) and outer whitespace
    before comparing. The expected-word set covers Gemini's typical
    rephrasing; the length+space fallback accepts any other one-word
    reply so the check fails only on genuinely broken / verbose output.
    """
    normalised = text.strip().rstrip(".!?,;: ").lower()
    if not normalised:
        return False
    if normalised in _EXPECTED_ACK_WORDS:
        return True
    if len(normalised) < 20 and " " not in normalised and "\n" not in normalised:
        return True
    return False


# ── Buildium ────────────────────────────────────────────────────────


async def check_buildium(settings: Settings) -> CheckResult:
    """Cheapest authenticated Buildium call: ``GET /rentals?limit=1``.

    ⚠ THIS PROBES THE DEPLOYMENT-GLOBAL CREDENTIAL, WHICH PRODUCTION NO LONGER
    USES. Measured 2026-09-05: every production Buildium path — the agent
    tools, the properties grid, admin, staff sync, the task enricher, the
    webhook handlers — opens its client through
    ``load_buildium_settings_for_company``, i.e. the per-company Fernet-
    encrypted row. The only readers of ``settings.buildium.client_id`` are this
    check, ``worker.process_one`` (a replay helper with zero callers), a
    boolean on the health payload, and the eval harness.

    So the pair is LEGACY SINGLE-TENANT CONFIG — gotcha 69's shape, a
    deployment-global setting that is really a per-company fact. On a correct
    white-label install it is absent by design, and failing on that would red
    the cron every morning for a credential the product does not use, which is
    how the one signal that matters gets ignored (gotcha 23).

    Hence the split, and it is the fixture lesson of gotchas 72/73 — the state
    has to say what it MEANS:
    * absent  → SKIP. Correctly unset on multi-tenant; nothing is degraded.
    * present → probe it, and a 401 still FAILS. A single-tenant operator who
      configured the pair has a genuinely broken credential.

    ⚠ WHAT THIS LEAVES UNCOVERED, stated rather than implied: nothing in the
    smoke checks that production can reach Buildium FOR A REAL COMPANY. That
    needs ``HELIXIS_DATA_KEY`` to decrypt a per-company row, which must not be
    handed to CI (gotcha 11 — it wraps every at-rest secret and has no
    rotation path). The available proxy is the mirror freshness stamp (gotcha
    57: a stamp is evidence a stage did not error), but staleness is normal for
    a quiet account, so a naive stale-means-broken check would cry wolf and get
    switched off (gotcha 52's lesson). Left as a known gap on purpose.
    """
    from helixis.buildium.client import BuildiumClient

    started = time.perf_counter()
    if not settings.buildium.client_id or not settings.buildium.client_secret:
        return CheckResult(
            "buildium_api",
            "skipped",
            _elapsed_ms(started),
            "no deployment-global Buildium credential — correct on a "
            "multi-tenant install (production uses per-company encrypted "
            "credentials); this check covers NOTHING about production reach",
        )
    try:
        async with BuildiumClient(settings.buildium) as client:
            await client.request("GET", "/rentals", query={"limit": 1})
        return CheckResult("buildium_api", "pass", _elapsed_ms(started), "200 OK")
    except Exception as exc:  # noqa: BLE001 — smoke surfaces any failure mode
        return CheckResult(
            "buildium_api",
            "fail",
            _elapsed_ms(started),
            f"{type(exc).__name__}: {exc}",
        )


# ── Composio (Gmail / Calendar / Drive) ─────────────────────────────


_ACTIVE_STATUSES = frozenset({"active", "connected", "ok"})


async def check_composio_toolkit(
    settings: Settings, name: str, toolkit_slug: str, user_id: str
) -> CheckResult:
    """Verify ``user_id`` has an active Composio connection for ``toolkit_slug``.

    Reports diagnostic detail when failing so it's clear whether the
    user has zero connections at all (typo in user_id, OAuth never
    completed) vs. has connections but all are expired / dropped
    (Composio incident-style revocation — see issue #183 for the
    incident that motivated the richer detail).

    ``list_connections`` is sync; wrapped via ``asyncio.to_thread``
    so the parallel ``asyncio.gather`` in the runner stays honest
    (no blocked event loop).
    """
    from helixis.connectors.composio_client import ComposioClient

    started = time.perf_counter()
    if not settings.composio.api_key:
        return CheckResult(name, "fail", _elapsed_ms(started), "HELIXIS_COMPOSIO__API_KEY not set")
    try:
        client = ComposioClient(settings)
        await client.initialize()
        if not client.available:
            return CheckResult(name, "fail", _elapsed_ms(started), "Composio SDK init failed")

        connections = await asyncio.to_thread(client.list_connections, user_id)
        matching = [
            c for c in connections if (c.get("toolkit") or "").lower() == toolkit_slug.lower()
        ]

        if not matching:
            # Reaching ``list_connections`` and getting a response means the
            # Composio SERVICE is healthy — a zero-connection result for the
            # smoke account is a user-state/config issue (the account never
            # OAuth'd, or HELIXIS_SMOKE_COMPANY_ID points at a non-connected
            # user), NOT a service outage. Skip rather than fail so a stale
            # smoke account can't permanently red-gate the daily healthcheck.
            # A genuine degradation of a CONNECTED account still fails below
            # (the expired/dropped branch — the #183 signal).
            return CheckResult(
                name,
                "skipped",
                _elapsed_ms(started),
                f"Composio reachable; user {user_id} has no {toolkit_slug} connection "
                "(set HELIXIS_SMOKE_COMPANY_ID to a connected account for coverage)",
            )

        active = [c for c in matching if str(c.get("status") or "").lower() in _ACTIVE_STATUSES]
        if active:
            chosen = active[0]
            connection_id = chosen.get("connection_id") or "unknown"
            connected_at = chosen.get("connected_at")
            relative = _relative_time(connected_at)
            return CheckResult(
                name,
                "pass",
                _elapsed_ms(started),
                f"active ({connection_id} last refreshed {relative})",
            )

        # Has connections but none active — surface the breakdown so the
        # smoke output makes the cause obvious (expired vs dropped).
        n_expired = sum(1 for c in matching if str(c.get("status") or "").lower() == "expired")
        n_dropped = sum(1 for c in matching if str(c.get("status") or "").lower() == "dropped")
        return CheckResult(
            name,
            "fail",
            _elapsed_ms(started),
            f"no active connections — {n_expired} expired, {n_dropped} dropped",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name, "fail", _elapsed_ms(started), f"{type(exc).__name__}: {exc}")


def _relative_time(when: Any) -> str:
    """Best-effort "X minutes/hours/days ago" for a timestamp string.

    Composio's ``created_at`` is typically ISO 8601 but shape varies
    by SDK build. Returns ``"unknown"`` when nothing parseable is
    given so the diagnostic detail never raises.
    """
    if not when:
        return "unknown"
    if not isinstance(when, str):
        return str(when)
    try:
        # Tolerate trailing Z (UTC) — fromisoformat doesn't accept it pre-3.11.
        parsed = datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - parsed
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "in the future"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


# ── Supabase Postgres ───────────────────────────────────────────────


async def check_postgres(settings: Settings) -> CheckResult:
    """``SELECT 1`` against the configured Supabase DB.

    Uses :func:`helixis.db.engine.create_pool` (not the process-global
    pool) so the smoke check has its own short-lived pool and tears it
    down on exit — running smoke from a long-lived process never
    contends with the app's pool.
    """
    from helixis.db.engine import create_pool

    started = time.perf_counter()
    dsn = settings.supabase.db_url
    if not dsn:
        return CheckResult(
            "supabase_postgres",
            "fail",
            _elapsed_ms(started),
            "HELIXIS_SUPABASE__DB_URL not set",
        )
    pool = None
    try:
        pool = await create_pool(dsn, min_size=1, max_size=1)
        async with pool.acquire() as conn:
            result = await conn.fetchval("SELECT 1")
        if result == 1:
            return CheckResult("supabase_postgres", "pass", _elapsed_ms(started), "connected")
        return CheckResult(
            "supabase_postgres",
            "fail",
            _elapsed_ms(started),
            f"unexpected SELECT 1 result: {result!r}",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "supabase_postgres",
            "fail",
            _elapsed_ms(started),
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        if pool is not None:
            await pool.close()


# ── Webhook queue depth (LED-019) ────────────────────────────────────
#
# A wedged or crash-looping worker doesn't fire an alarm by itself — its per-
# event failures are individual Sentry events, never an aggregate "the queue is
# N hours behind" signal. This check surfaces that: how many events are READY to
# process (status='pending' AND available_at <= now(), i.e. not a future-dated
# retry) and how old the oldest ready one is. The worker polls every ~2s, so a
# ready event minutes old means it isn't keeping up.

#: A ready event older than this (seconds) means the worker isn't draining.
_QUEUE_MAX_READY_AGE_S = 900  # 15 min
#: More than this many ready events is a backlog even if none is old yet.
_QUEUE_MAX_READY_DEPTH = 500


def _queue_health(ready_count: int, oldest_ready_age_s: float | None) -> tuple[str, str]:
    """Classify webhook-queue backlog. Pure — unit-tested.

    ``ready_count`` = pending events whose ``available_at`` has passed (a
    future-dated retry is NOT counted — it's waiting on purpose).
    ``oldest_ready_age_s`` = age of the oldest such event, or None when the
    queue is empty. Returns ``(status, detail)``.
    """
    if ready_count == 0:
        return ("pass", "queue empty (0 ready events)")
    age = oldest_ready_age_s or 0.0
    if age > _QUEUE_MAX_READY_AGE_S:
        return (
            "fail",
            f"queue backed up: oldest ready event {int(age // 60)}m old "
            f"({ready_count} ready) — worker not draining",
        )
    if ready_count > _QUEUE_MAX_READY_DEPTH:
        return ("fail", f"queue depth {ready_count} ready events (oldest {int(age)}s)")
    return ("pass", f"{ready_count} ready, oldest {int(age)}s")


async def check_webhook_queue(settings: Settings) -> CheckResult:
    """Flag a backed-up ``buildium_events`` queue (a wedged/stopped worker).

    Best-effort: a missing table or DB error degrades to ``skipped`` (the
    postgres check already gates real DB health), never a false fail.
    """
    from helixis.db.engine import create_pool

    started = time.perf_counter()
    dsn = settings.supabase.db_url
    if not dsn:
        return CheckResult(
            "webhook_queue", "skipped", _elapsed_ms(started), "HELIXIS_SUPABASE__DB_URL not set"
        )
    pool = None
    try:
        pool = await create_pool(dsn, min_size=1, max_size=1)
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                  count(*) FILTER (
                    WHERE status = 'pending' AND available_at <= now()
                  ) AS ready,
                  extract(epoch FROM (now() - min(received_at) FILTER (
                    WHERE status = 'pending' AND available_at <= now()
                  ))) AS oldest_age_s
                FROM public.buildium_events
                """
            )
        ready = int(row["ready"] or 0)
        oldest = float(row["oldest_age_s"]) if row["oldest_age_s"] is not None else None
        status, detail = _queue_health(ready, oldest)
        return CheckResult("webhook_queue", status, _elapsed_ms(started), detail)
    except Exception as exc:  # noqa: BLE001 — best-effort; postgres check owns DB health
        return CheckResult(
            "webhook_queue",
            "skipped",
            _elapsed_ms(started),
            f"could not read queue ({type(exc).__name__})",
        )
    finally:
        if pool is not None:
            await pool.close()


# ── LiteLLM router ──────────────────────────────────────────────────


def _provider_key(settings: Settings, model_name: str) -> tuple[str, str]:
    """Resolve ``(api_key, key_var_name)`` for a LiteLLM model name.

    Same provider-prefix resolution as :mod:`helixis.llm.router` (Gemini-only
    in prod — PR #69's ``assert_no_openai_default`` guard). An empty api_key
    means "no Helixis key configured for this provider".
    """
    lowered = model_name.lower()
    llm = settings.llm
    if lowered.startswith(("gemini/", "google/", "vertex_ai/")):
        return (llm.google_api_key or "", "HELIXIS_LLM__GOOGLE_API_KEY")
    if lowered.startswith("openai/"):
        return (llm.openai_api_key or "", "HELIXIS_LLM__OPENAI_API_KEY")
    if lowered.startswith(("anthropic/", "claude/")):
        return (llm.anthropic_api_key or "", "HELIXIS_LLM__ANTHROPIC_API_KEY")
    return ("", "(unknown provider)")


async def check_llm_router(settings: Settings) -> CheckResult:
    """Send a deterministic OK prompt to the configured default model.

    Uses ``litellm.acompletion`` directly with the same key resolution
    pattern as :mod:`helixis.llm.router` (Gemini-only in prod — see
    PR #69's ``assert_no_openai_default`` guard).
    """
    import litellm

    started = time.perf_counter()
    model_name = settings.llm.default_model
    api_key, key_var = _provider_key(settings, model_name)

    if not api_key:
        return CheckResult(
            "llm_router",
            "fail",
            _elapsed_ms(started),
            f"{key_var} not set for model {model_name!r}",
        )

    try:
        response = await litellm.acompletion(
            model=model_name,
            api_key=api_key,
            messages=[{"role": "user", "content": "Reply with one word: ok"}],
            # No temperature override: Sonnet 5 (the prod default since the
            # Anthropic migration) REJECTS temperature=0 with an
            # UnsupportedParamsError ("Only temperature=1 is supported"), which
            # red-gated this check daily. Omitting it defaults to 1, accepted by
            # every provider — determinism doesn't matter for a one-word ack.
            # The default model is a *thinking* model: reasoning
            # tokens count against max_tokens, so a tiny cap (was 10) gets
            # consumed by thinking before any visible text is emitted and
            # ``content`` comes back empty — which read as "LLM returned
            # empty response" and red-gated this check every day while the
            # LLM was in fact healthy. 2048 leaves ample headroom for the
            # trivial prompt's (small, dynamic) thinking budget plus the
            # one-word ack — a once-daily call, so the cost is negligible.
            max_tokens=2048,
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            return CheckResult(
                "llm_router",
                "fail",
                _elapsed_ms(started),
                "LLM returned empty response",
            )
        # "Did the LLM respond at all, with something coherent?" not
        # "Did the LLM produce exact text?". Gemini may say "Ok", "Okay",
        # "Sure", "OK!"; the previous strict substring match flagged
        # those as failures and made the smoke noisy. Now we fail only
        # when the LLM is genuinely broken/unreachable (empty response)
        # or it returned something that doesn't look like a one-word ack.
        if _is_coherent_one_word_ack(text):
            return CheckResult("llm_router", "pass", _elapsed_ms(started), text)
        snippet = text[:100]
        return CheckResult(
            "llm_router",
            "fail",
            _elapsed_ms(started),
            f"LLM response did not match expected pattern: '{snippet}'",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "llm_router",
            "fail",
            _elapsed_ms(started),
            f"{type(exc).__name__}: {exc}",
        )


# ── Model slots (retired-slug guard, #537) ──────────────────────────


async def _probe_model_slug(settings: Settings, model_name: str) -> str | None:
    """Minimal "does this slug resolve?" probe. Returns ``None`` when the
    model answers (any content), else a short error string. Never raises.

    A retired/renamed slug raises ``NotFoundError`` (404) before any
    generation, so a tiny ``max_tokens`` still surfaces it cheaply — we
    only care that the call didn't 404, not what it said.
    """
    import litellm

    api_key, key_var = _provider_key(settings, model_name)
    if not api_key:
        return f"{key_var} not set"
    try:
        await litellm.acompletion(
            model=model_name,
            api_key=api_key,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=16,
            # No temperature: Sonnet 5 rejects temperature=0 (see check_llm_router).
        )
    except Exception as exc:  # noqa: BLE001 — smoke surfaces any failure mode
        return f"{type(exc).__name__}: {exc}"
    return None


async def check_model_slots(settings: Settings) -> CheckResult:
    """Verify every configured model slug BEYOND ``default_model`` resolves.

    ``check_llm_router`` already exercises ``default_model`` with a real
    completion, so a retired default slug fails there. But the other
    ``llm.models.*`` slots are never validated — notably the reasoning
    model's 503 **fallback SKU** (the ``browser`` slot, e.g.
    ``gemini/gemini-3.5-flash``) and ``analysis``. If one of those is a
    *preview* slug that Google retires (the #531 outage class), the daily
    default-model check stays green while a runtime 503-failover would 404.

    This probes each DISTINCT non-default configured slug concurrently and
    fails (naming the dead slug) if any 404s — turning a silent
    fallback-rot into a ``[smoke]`` alert. Skips cleanly when no extra slots
    are declared or no provider key is set (nothing to validate).
    """
    started = time.perf_counter()
    default = settings.llm.default_model
    models = settings.llm.models or {}
    slugs = sorted(
        {m for cfg in models.values() if (m := (cfg or {}).get("model")) and m != default}
    )
    if not slugs:
        return CheckResult(
            "model_slots",
            "skipped",
            _elapsed_ms(started),
            "no non-default model slots configured (default covered by llm_router)",
        )
    if not any(_provider_key(settings, s)[0] for s in slugs):
        return CheckResult(
            "model_slots",
            "skipped",
            _elapsed_ms(started),
            "no provider API key set for configured slots",
        )

    errors = await asyncio.gather(*[_probe_model_slug(settings, s) for s in slugs])
    dead = [(s, e) for s, e in zip(slugs, errors, strict=True) if e]
    if dead:
        detail = "; ".join(f"{s} → {e}" for s, e in dead)
        return CheckResult("model_slots", "fail", _elapsed_ms(started), f"unreachable: {detail}")
    return CheckResult(
        "model_slots",
        "pass",
        _elapsed_ms(started),
        f"{len(slugs)} slot(s) resolve: {', '.join(slugs)}",
    )


# ── Memory (mem0 embedder + vector search) ──────────────────────────


async def check_memory(settings: Settings) -> CheckResult:
    """Probe the mem0 memory READ path (Voyage embedder + pgvector search).

    Memory's request-path :meth:`search` swallows every failure and returns
    ``[]`` by design (it is an enhancement, never load-bearing) — so a broken
    embedder degrades silently and can stay broken for weeks unnoticed (a 403
    ``PERMISSION_DENIED`` on Gemini's ``BatchEmbedContents`` ran ~19 days
    undetected, found in the 2026-07-12 audit). This check calls
    :meth:`MemoryManager.health_probe`, which runs the same query-embed +
    vector-search WITHOUT swallowing, turning that silent rot into a daily
    ``[smoke]`` alert. Skips cleanly when memory is disabled or no Voyage key
    is set (nothing to probe).
    """
    started = time.perf_counter()
    if not settings.memory.enabled:
        return CheckResult("memory", "skipped", _elapsed_ms(started), "memory disabled in settings")
    if not settings.llm.voyage_api_key:
        # ⚠ FAIL, not skip. Memory is ENABLED here — ``config/default.yaml``
        # and ``config/production.yaml`` both say ``enabled: true`` — so the
        # deployment believes it has recall, and without the embedder key
        # every search returns ``[]`` through the by-design swallow in
        # ``manager.search``. That is precisely the silent rot this check was
        # written to catch (gotcha 17: a 403 on the embedder ran 19 days
        # undetected); a missing key is the same dead memory with a cheaper
        # cause, and skipping it made the check green in exactly the state it
        # exists to alarm on. The skip above is honest — memory switched OFF
        # has nothing to probe — but "on and unprovisioned" is a
        # misconfiguration, not an absence.
        return CheckResult(
            "memory",
            "fail",
            _elapsed_ms(started),
            "memory is enabled but HELIXIS_LLM__VOYAGE_API_KEY is not set — "
            "every recall silently returns nothing",
        )
    try:
        from helixis.memory.manager import MemoryManager

        manager = MemoryManager(settings)
        await manager.initialize()
    except Exception as exc:  # noqa: BLE001 — smoke surfaces any failure mode
        return CheckResult(
            "memory", "fail", _elapsed_ms(started), f"init: {type(exc).__name__}: {exc}"
        )

    if not manager.available:
        return CheckResult(
            "memory",
            "fail",
            _elapsed_ms(started),
            "mem0 init failed — memory unavailable (see logs)",
        )

    ok, detail = await manager.health_probe()
    return CheckResult("memory", "pass" if ok else "fail", _elapsed_ms(started), detail)


# ── LLM-as-judge liveness (the quality metric that can silently die) ─


async def check_judge(settings: Settings) -> CheckResult:
    """Probe the escalation LLM-judge end-to-end — the one live quality judge.

    ``check_llm_router`` proves the model *answers*, but it deliberately omits
    ``temperature`` and uses a plain prompt, so it does NOT exercise the judge's
    own call (its ``response_format`` + params on the reasoning model). When the
    judge's params are rejected by the prod model, ``judge_escalation_reply``
    throws, its best-effort ``except`` swallows it to ``None``, and NO score
    prints — the judge silently scores nothing (exactly what a hardcoded
    ``temperature=0`` did on claude-5). Same silent-degradation class as
    ``check_memory`` (gotcha 17): the swallow is correct, the silence is the
    bug. This runs a real escalation reply through the judge and FAILS when no
    score comes back, turning that silent death into a daily ``[smoke]`` alert.
    Skips cleanly when the judge model's key is unset (nothing to probe).
    """
    started = time.perf_counter()
    models = settings.llm.models or {}
    reasoning = models.get("reasoning") or {}
    model_name = reasoning.get("model") or settings.llm.default_model
    api_key, key_var = _provider_key(settings, model_name)
    if not api_key:
        return CheckResult(
            "judge", "skipped", _elapsed_ms(started), f"{key_var} not set for judge model"
        )

    try:
        from helixis.evals.escalation_judge import judge_escalation_reply

        score = await judge_escalation_reply(
            user_message="What's the status of my building permit?",
            reply=(
                "I hit a login wall on the city's permit portal and couldn't get "
                "past it — the status page needs an account I don't have. Next "
                "step: share the portal login, or the permit number so I can try "
                "the automated status line."
            ),
            scenario="smoke: escalation-judge liveness probe",
        )
    except Exception as exc:  # noqa: BLE001 — smoke surfaces any failure mode
        return CheckResult("judge", "fail", _elapsed_ms(started), f"{type(exc).__name__}: {exc}")

    if score is None:
        return CheckResult(
            "judge",
            "fail",
            _elapsed_ms(started),
            "judge returned no score — live call/parse failed (see logs); metric silently dead",
        )
    return CheckResult("judge", "pass", _elapsed_ms(started), f"overall={score.overall:.2f}")


# ── Langfuse ────────────────────────────────────────────────────────


async def check_langfuse(settings: Settings) -> CheckResult:
    """Emit one ``smoke_test``-tagged trace and flush.

    When Langfuse isn't configured (no public/secret key), returns
    ``skipped`` — Langfuse is optional observability infra; missing
    keys mustn't fail the overall smoke.
    """
    started = time.perf_counter()
    cfg = settings.langfuse
    if not cfg.enabled or not cfg.public_key or not cfg.secret_key:
        return CheckResult("langfuse", "skipped", _elapsed_ms(started), "not configured")

    try:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=cfg.public_key,
            secret_key=cfg.secret_key,
            host=cfg.host,
        )
        await asyncio.to_thread(_emit_smoke_trace, client)
        return CheckResult("langfuse", "pass", _elapsed_ms(started), "ingested")
    except AttributeError:
        # langfuse 3.x dropped the v2 ``client.trace()`` API this check uses
        # ("'Langfuse' object has no attribute 'trace'"). Prod pins langfuse<3
        # (#713); a 3.x in the smoke env is a dependency-pin drift, NOT a
        # service outage — degrade to a clear SKIP so optional observability
        # can't red-gate the smoke. Fix by pinning langfuse<3 in the smoke env.
        return CheckResult(
            "langfuse",
            "skipped",
            _elapsed_ms(started),
            "langfuse SDK is 3.x but the smoke uses the v2 trace() API — pin langfuse<3",
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("langfuse", "fail", _elapsed_ms(started), f"{type(exc).__name__}: {exc}")


def _emit_smoke_trace(client: Any) -> None:
    """Sync helper for :func:`asyncio.to_thread` — emit + flush.

    ``timestamp`` MUST be passed as a top-level trace field. Putting the
    time only in ``metadata`` (as this did originally) leaves the trace's
    own timestamp unset, so Langfuse defaulted it to the epoch and every
    smoke trace landed dated 1970-01-01 (found in the Langfuse audit,
    docs/audits/langfuse-render-audit.md). Set it explicitly so the daily
    smoke traces sort correctly on the timeline.
    """
    client.trace(
        name="smoke_test",
        tags=["smoke_test"],
        timestamp=datetime.now(UTC),
        metadata={"timestamp": datetime.now(UTC).isoformat()},
    )
    client.flush()


# ── Storage buckets (the missing-bucket class: code green, first upload 404s) ─

# Every private bucket the backend writes to. A bucket referenced in code but
# absent in Supabase fails ONLY at the first real upload — unit tests mock the
# storage client, so nothing pre-merge can catch it (that is exactly how
# codeexec_artifacts shipped missing). This check makes the daily smoke the
# tripwire.
#
# ⚠ This was a HAND-TYPED PAIR whose own comment claimed "every private bucket
# the backend writes to" and named two of three. `browser_screenshots` was
# missing, and it is the one where absence degrades most quietly:
# `_upload_screenshot_to_storage` returns None on any failure and the agent
# reports `screenshot_failed`, so a missing bucket looks like a flaky page
# forever. It reads the one declaration now, so a fourth bucket cannot be added
# to the product and forgotten here.
_REQUIRED_BUCKETS = ALL_BUCKET_NAMES


async def check_storage_buckets(settings: Settings) -> CheckResult:
    """Verify every backend-referenced storage bucket exists (read-only).

    ``GET /storage/v1/bucket/{id}`` with the service key — proves both the
    bucket's existence and that the service-key storage path authenticates
    (the same credential ``upload_object`` / ``sign_object_url`` use).
    """
    import httpx

    started = time.perf_counter()
    base = (settings.supabase.url or "").rstrip("/")
    key = settings.supabase.service_key or ""
    if not base or not key:
        return CheckResult(
            "storage_buckets",
            "fail",
            _elapsed_ms(started),
            "Supabase URL or service key not set",
        )
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    missing: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            for bucket in _REQUIRED_BUCKETS:
                resp = await client.get(f"{base}/storage/v1/bucket/{bucket}", headers=headers)
                if resp.status_code != 200:
                    missing.append(f"{bucket} ({resp.status_code})")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "storage_buckets",
            "fail",
            _elapsed_ms(started),
            f"{type(exc).__name__}: {exc}",
        )
    if missing:
        return CheckResult(
            "storage_buckets",
            "fail",
            _elapsed_ms(started),
            f"missing/unreadable: {', '.join(missing)}",
        )
    return CheckResult(
        "storage_buckets",
        "pass",
        _elapsed_ms(started),
        f"all {len(_REQUIRED_BUCKETS)} buckets present",
    )


# ── Twilio (outbound customer-care SMS — dark until A2P approval) ────


async def check_twilio(settings: Settings) -> CheckResult:
    """Read-only Twilio account probe for the outbound-SMS channel.

    Skips while the feature is dark (``HELIXIS_TWILIO__ENABLED=false``, the
    pre-A2P-approval default) or creds are missing — an unconfigured optional
    channel must never red-gate the daily smoke. When enabled, ``GET`` the
    account resource (no message is ever sent) and pass only on a 200 with
    account status ``active``/``trial`` — a suspended account would otherwise
    silently fail every rent reminder (the gotcha-17 silent-degradation class).
    """
    import httpx

    started = time.perf_counter()
    twilio = settings.twilio
    if not twilio.enabled or not twilio.account_sid or not twilio.auth_token:
        return CheckResult(
            "twilio_sms",
            "skipped",
            _elapsed_ms(started),
            "not configured (outbound SMS dark until A2P approval)",
        )
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.twilio.com/2010-04-01/Accounts/{twilio.account_sid}.json",
                auth=(twilio.account_sid, twilio.auth_token),
            )
    except Exception as exc:  # noqa: BLE001 — smoke surfaces any failure mode
        return CheckResult(
            "twilio_sms", "fail", _elapsed_ms(started), f"{type(exc).__name__}: {exc}"
        )
    if resp.status_code != 200:
        return CheckResult(
            "twilio_sms",
            "fail",
            _elapsed_ms(started),
            f"account fetch returned HTTP {resp.status_code}",
        )
    try:
        status = str(resp.json().get("status") or "")
    except Exception:  # noqa: BLE001 — a 200 with an unparseable body is broken
        status = ""
    if status.lower() in {"active", "trial"}:
        return CheckResult("twilio_sms", "pass", _elapsed_ms(started), f"account {status}")
    return CheckResult(
        "twilio_sms",
        "fail",
        _elapsed_ms(started),
        f"account status {status or 'unknown'!r} (expected active/trial)",
    )


# ── Census geocoder (keyless, no SLA — the daily canary for the geo layer) ─


async def check_geocoder(settings: Settings) -> CheckResult:
    """Geocode a fixed, stable address through the configured provider.

    The census provider is public-domain and keyless but carries NO SLA, and
    the request path swallows failures by design (``geocode`` returns ``None``
    — geo is never load-bearing). Same silent-degradation class as memory
    (gotcha 17): without this check, a Census outage or response-shape change
    would just quietly null every geocode. The probe address is the Census
    Bureau's own HQ, which their geocoder will always know.
    """
    from helixis.geo.geocode import geocode

    started = time.perf_counter()
    point = await geocode("4600 Silver Hill Rd, Washington, DC 20233", settings=settings)
    if point is None:
        return CheckResult(
            "geocoder",
            "fail",
            _elapsed_ms(started),
            f"no result from provider={settings.maps.provider!r} (outage or shape change)",
        )
    # Suitland, MD sanity box — catches x/y (lng/lat) transposition too.
    if not (38.0 < point.lat < 39.5 and -77.5 < point.lng < -76.0):
        return CheckResult(
            "geocoder",
            "fail",
            _elapsed_ms(started),
            f"implausible coords ({point.lat:.4f}, {point.lng:.4f}) — lat/lng swap?",
        )
    return CheckResult(
        "geocoder", "pass", _elapsed_ms(started), f"({point.lat:.4f}, {point.lng:.4f})"
    )


# ── Tavily public-web search (the canary the web layer never had) ──────────


async def check_web_search(settings: Settings) -> CheckResult:
    """Run one cheap public-web search and require it to come back with sources.

    ⚠ WHY THIS IS A FAIL AND NOT A SKIP, unlike ``check_twilio``. Outbound SMS
    is dark on purpose until carriers approve it; public-web search is a LIVE
    shipped capability on two surfaces — the ``web_agent`` specialist (always
    registered) and the Electron browser's ``helixis://search`` page. A blank
    or dead key is not an unconfigured optional feature, it is every user's
    web search silently returning nothing.

    And silently is the word. ``web_search_impl`` never raises by design
    (gotcha 7's rule: research is an enhancement, never load-bearing), so a
    revoked key comes back as ``warnings: [search_unauthorized]``, the
    specialist says "public-web context is temporarily unavailable", and
    nobody is told. That is the same class as the embedder 403 that ran ~19
    days undetected (gotcha 17) and the reason ``check_geocoder`` exists.
    Tavily was the last paid dependency on a request path with no canary.

    ⚠ Assert on SOURCES, not on the absence of a warning. A key that
    authenticates but returns nothing is the failure that matters, and a
    warnings-only check would pass it — the same trap gotcha 54 hit when a
    keepalive was documented and never reached the wire.
    """
    from helixis.agents.web_tools import web_search_impl

    started = time.perf_counter()
    if not settings.web.tavily_api_key:
        return CheckResult(
            "web_search",
            "fail",
            _elapsed_ms(started),
            "HELIXIS_WEB__TAVILY_API_KEY is not set — every web search returns "
            "search_unavailable, on the specialist AND the browser results page",
        )

    # A query with a stable, unambiguous public answer. Deliberately NOT a
    # tenant/property string: this runs unauthenticated in CI and the query is
    # persisted to the activity log by web_search_impl.
    result = await web_search_impl(
        "what is the federal Fair Housing Act", 3, "basic", settings=settings
    )
    warnings = list(result.warnings)
    if "search_unauthorized" in warnings:
        return CheckResult(
            "web_search",
            "fail",
            _elapsed_ms(started),
            "Tavily rejected the key (401/403) — expired, revoked, or out of quota",
        )
    if warnings:
        return CheckResult("web_search", "fail", _elapsed_ms(started), f"warnings={warnings}")
    usable = [s for s in result.sources if s.url]
    if not usable:
        return CheckResult(
            "web_search",
            "fail",
            _elapsed_ms(started),
            "authenticated but returned 0 usable sources (quota exhausted, or a "
            "response-shape change _parse_tavily_payload no longer reads)",
        )
    return CheckResult("web_search", "pass", _elapsed_ms(started), f"{len(usable)} sources")


async def check_stripe(settings: Settings) -> CheckResult:
    """Is the money path actually wired — key, prices, and a webhook endpoint.

    ⚠ WHY THIS IS THE ONE DEPENDENCY THAT MOST NEEDED A CANARY, and had none.
    Every other paid integration here fails in front of somebody: a dead
    Buildium key 401s a page, a dead Tavily key empties a search. Billing fails
    in SILENCE and in OUR favour-shaped direction — a wrong
    ``stripe_webhook_secret`` makes `/billing/webhook` 403 every delivery, so
    Stripe charges the customer, retries for days, eventually disables the
    endpoint, and this product never learns that anybody paid. The customer is
    billed and locked out at `trial_expired`. Nothing in the app says a word.

    Three things are checked, and each one has a distinct failure it catches:

    1. **The secret key works.** A revoked or test/live-swapped key makes every
       checkout 502 at the moment somebody tries to pay.
    2. **Every configured price id resolves.** A typo'd or deleted price 503s
       checkout for that plan alone — the ladder looks complete on screen and
       one rung does nothing. It is also the ONLY automated way to notice that
       a price was archived in the dashboard.
    3. **A webhook endpoint points at this deployment.** Without one, Stripe
       never calls us: the subscription exists and is charged, our row stays on
       `trial`, and the customer is locked out on day 14 holding a receipt.

    ⚠ READ-ONLY. Three GETs, no object is created, and nothing here can charge.

    ⚠ SKIPS when billing is unconfigured rather than failing, and states what
    it did not cover. Gotcha 84's rule: a canary pointed at something the
    deployment does not have is one people stop reading. But it FAILS the
    moment a key IS present and does not work — a half-configured deployment is
    the dangerous state, not the empty one.

    ⚠ It CANNOT verify the webhook SECRET. That value only ever appears inside
    an HMAC over a real delivery, so no API call can confirm it; the endpoint's
    existence is the closest read-only proxy. A wrong secret with a correct
    endpoint URL is still silent, and that gap is stated here rather than
    papered over — the first real webhook is what settles it.
    """
    started = time.perf_counter()
    billing = settings.billing
    if not billing.stripe_secret_key:
        return CheckResult(
            "stripe",
            "skipped",
            _elapsed_ms(started),
            "HELIXIS_BILLING__STRIPE_SECRET_KEY not set — nothing checked: no "
            "key, no price ids, no webhook endpoint. Billing cannot take money "
            "on this deployment.",
        )

    headers = {"Authorization": f"Bearer {billing.stripe_secret_key}"}
    timeout = httpx.Timeout(15.0, connect=8.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # 1. The key itself.
            probe = await client.get(
                "https://api.stripe.com/v1/customers", params={"limit": 1}, headers=headers
            )
            if probe.status_code in (401, 403):
                return CheckResult(
                    "stripe",
                    "fail",
                    _elapsed_ms(started),
                    "Stripe rejected the secret key — revoked, or a test key on a "
                    "live deployment. Every checkout 502s.",
                )
            if probe.status_code >= 400:
                return CheckResult(
                    "stripe", "fail", _elapsed_ms(started), f"http {probe.status_code}"
                )

            # 2. Every price a plan is sold at. A missing id is a config gap
            #    rather than an outage, so it is named separately below.
            missing = [k for k, v in billing.price_ids.items() if not v]
            broken: list[str] = []
            for key, price_id in billing.price_ids.items():
                if not price_id:
                    continue
                resp = await client.get(
                    f"https://api.stripe.com/v1/prices/{price_id}", headers=headers
                )
                if resp.status_code >= 400:
                    broken.append(f"{key} ({price_id}: http {resp.status_code})")
                    continue
                price = resp.json()
                if price.get("active") is False:
                    # Archived in the dashboard. Checkout still 400s on it, and
                    # nothing else would ever notice.
                    broken.append(f"{key} ({price_id}: archived)")
                    continue
                # ⚠ THE NUMBER ON THE CARD vs THE NUMBER ON THE CARD. `plans.py`
                # is what the customer READS and Stripe is what CHARGES them,
                # and nothing has ever compared the two. A price id pasted into
                # the wrong plan's slot, or an amount edited in the dashboard,
                # makes the page say $199 while $500 leaves their account — with
                # no exception, no log line, and a chargeback as the first
                # signal. Both halves are already in this process; only the
                # comparison was missing.
                plan = PLANS.get(key)
                if plan is not None:
                    expected = round(plan.price_usd * 100)
                    actual = price.get("unit_amount")
                    if actual is not None and actual != expected:
                        broken.append(
                            f"{key} ({price_id}: Stripe charges "
                            f"{actual / 100:.2f} but the plan card says "
                            f"{plan.price_usd:.2f})"
                        )
                        continue
                # USD is assumed everywhere a price is rendered — `plans.py`
                # calls the field `price_usd` and the UI writes a `$`. A price
                # created in another currency renders as dollars and bills as
                # something else.
                if (price.get("currency") or "usd").lower() != "usd":
                    broken.append(f"{key} ({price_id}: currency {price.get('currency')}, not usd)")
                    continue
                # A one-off price in a `mode=subscription` Checkout Session is
                # rejected by Stripe at the moment somebody tries to pay.
                recurring = price.get("recurring")
                if not recurring:
                    broken.append(f"{key} ({price_id}: one-time price, not recurring)")
                    continue
                # ⚠ MONTHLY, and the check is about more than the "/ month" the
                # card prints. This product has no annual plan and no advance
                # renewal notice — `notices.py` carries `trial_ending` and
                # `payment_failed` and nothing else, because a monthly renewal
                # announced in advance every single month is a banner nobody
                # reads. A yearly price makes that reasoning FALSE the moment it
                # exists: a large charge lands unannounced twelve months after
                # anybody last thought about it, and the allowance figures in
                # `plans.py` are all stated per month.
                #
                # So the interval is a fact the app never learns — nothing in
                # the codebase reads it — and this is the one place it is
                # already in hand. A yearly price is not a typo to fix in the
                # dashboard; it means a renewal notice has to be built first.
                interval = (recurring or {}).get("interval")
                if interval and interval != "month":
                    broken.append(
                        f"{key} ({price_id}: bills every {interval}, not monthly — the "
                        "plan card says '/ month', the allowance is stated per month, "
                        "and there is no advance renewal notice for a yearly charge)"
                    )
            if broken:
                return CheckResult(
                    "stripe",
                    "fail",
                    _elapsed_ms(started),
                    f"price id unusable for {', '.join(broken)} — checkout 503s for that plan",
                )

            # 3. Does Stripe know where to call us back?
            endpoint_note = "webhook endpoint not checked (no webhook_base_url configured)"
            base = settings.twilio.webhook_base_url or billing.portal_return_url
            if base:
                hooks = await client.get(
                    "https://api.stripe.com/v1/webhook_endpoints",
                    params={"limit": 100},
                    headers=headers,
                )
                if hooks.status_code >= 400:
                    endpoint_note = f"could not list webhook endpoints (http {hooks.status_code})"
                else:
                    urls = [str(e.get("url") or "") for e in hooks.json().get("data", [])]
                    if not any("/billing/webhook" in u for u in urls):
                        return CheckResult(
                            "stripe",
                            "fail",
                            _elapsed_ms(started),
                            "no Stripe webhook endpoint points at /billing/webhook — "
                            "subscriptions are charged and this product never hears "
                            "about it, so the customer pays and stays locked out",
                        )
                    endpoint_note = "webhook endpoint present"
    except Exception as exc:  # network, DNS, TLS
        return CheckResult("stripe", "fail", _elapsed_ms(started), f"{type(exc).__name__}: {exc}")

    detail = f"key ok, {len(billing.price_ids) - len(missing)} price(s) live, {endpoint_note}"
    if missing:
        # ⚠ NOT a pass-with-a-shrug. A plan on the ladder with no price id 503s
        # at checkout, which the customer meets as "that plan is not available
        # yet" after choosing it.
        return CheckResult(
            "stripe",
            "fail",
            _elapsed_ms(started),
            f"{detail}; NO price id for {', '.join(sorted(missing))} — those plans 503 at checkout",
        )
    return CheckResult("stripe", "pass", _elapsed_ms(started), detail)


async def check_billing_rows(settings: Settings) -> CheckResult:
    """Does every company have a `helixis_billing` row?

    ⚠ THIS INVARIANT EXISTED AND NOTHING RAN IT. Gotcha 81 is the record of
    why it matters: two individually-correct fail-open decisions compose into a
    free unlimited account. Migration 0087 backfilled the companies alive that
    morning, 0088 added a trigger so new ones get a row, and `gate.py` reads a
    missing row as ALLOW — deliberately, because a database blip must not lock
    a paying customer out. Each half is right; together, a company with no row
    is served forever and nothing anywhere says so.

    The `helixis_billing_missing` view was created to be that alarm and was
    queried by hand, twice, by whoever happened to remember. The trigger it
    watches is `security definer` and SWALLOWS its own exception by design — a
    signup that 500s is worse than a missing billing row — so when it fails the
    entire signal is one Postgres `raise warning` nobody reads.

    ⚠ A COUNT, not a sample. The failure is per company and permanent, so the
    interesting number is how many, and naming a few ids makes it actionable
    without dumping a tenant list into a log.

    ⚠ Deliberately cross-tenant (gotcha 47): the question IS "is any company
    missing one", which cannot be asked company-scoped.
    """
    from helixis.db.engine import create_pool

    started = time.perf_counter()
    dsn = settings.supabase.db_url
    if not dsn:
        # Same posture as gotcha 84: a check pointed at something this
        # deployment does not have must say what it did not cover rather than
        # fail forever and bury the lines that matter.
        return CheckResult(
            "billing_rows",
            "skipped",
            _elapsed_ms(started),
            "HELIXIS_SUPABASE__DB_URL not set — the every-company-has-a-billing-row "
            "invariant was not checked",
        )
    pool = None
    try:
        pool = await create_pool(dsn, min_size=1, max_size=1)
        async with pool.acquire() as conn:
            missing = await conn.fetchval("select count(*) from public.helixis_billing_missing")
            # ⚠ The view's column is ``company_id`` (0088). This selected ``id``,
            # so the check raised UndefinedColumnError on every run and failed
            # daily without ever measuring the invariant (smoke issue #1068).
            sample = await conn.fetch(
                "select company_id::text as id from public.helixis_billing_missing limit 3"
            )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "billing_rows", "fail", _elapsed_ms(started), f"{type(exc).__name__}: {exc}"
        )
    finally:
        if pool is not None:
            await pool.close()

    if missing:
        ids = ", ".join(r["id"] for r in sample)
        return CheckResult(
            "billing_rows",
            "fail",
            _elapsed_ms(started),
            f"{missing} company(ies) have no billing row (e.g. {ids}) — each one is "
            "served unmetered forever, because gate.py reads a missing row as allow",
        )
    return CheckResult(
        "billing_rows", "pass", _elapsed_ms(started), "every company has a billing row"
    )
