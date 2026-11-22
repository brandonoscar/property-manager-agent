"""Layered settings: config/default.yaml -> config/{env}.yaml -> env vars."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"

logger = logging.getLogger(__name__)

# ⚠ EVERY CREDENTIAL FIELD IN THIS FILE IS ``Field(default="", repr=False)``.
# A settings object ends up in log lines, exception messages and Sentry stack
# frames, and a plain ``str`` field prints its value in all three. ``repr=False``
# changes only what prints; the attribute still holds the value. Not
# ``SecretStr``, which would change the type every reader gets back.
# ``tests/unit/test_no_secrets_in_repr.py`` fails on a new credential field
# that leaves it out.


@lru_cache(maxsize=8)
def _parse_legacy_prices(raw: str) -> dict[str, tuple[str, ...]]:
    """`"pro:price_a,scale:price_b"` -> `{"pro": ("price_a",), ...}`.

    ⚠ **CACHED, and that is about the LOG rather than the parse.** This is read
    through a `@property`, so it re-evaluates on every access —
    `_plan_for_price` calls it once per subscription webhook. Splitting three
    strings is free; re-emitting the same warning every time is not, because a
    line that repeats forever is one people filter out, and that is how the
    next real finding gets scrolled past (gotcha 84). Keyed on the raw string,
    so the warning fires once per distinct configuration.

    ⚠ Malformed entries are DROPPED with a warning rather than raised on. A
    typo in an env var must not stop the app booting, and what it degrades to
    is exactly today's behaviour — the retired id resolves to nothing, a known
    handled state. The warning is the load-bearing half: a typo dropped in
    silence is how a grandfathered customer stays broken with nobody looking.
    """
    legacy: dict[str, list[str]] = {}
    for item in raw.split(","):
        entry = item.strip()
        if not entry:
            continue
        plan, _, price = entry.partition(":")
        plan, price = plan.strip(), price.strip()
        if not plan or not price:
            logger.warning(
                "billing: ignoring malformed legacy price entry %r — expected 'plan:price_id'",
                entry,
            )
            continue
        legacy.setdefault(plan, []).append(price)
    return {k: tuple(v) for k, v in legacy.items()}


class LLMSettings(BaseModel):
    # Anthropic-only in prod (Gemini exit, 2026-07-21 — the Google account
    # is gone; every Gemini call 403s). A bare ``Settings()`` with no YAML /
    # env overrides must NOT resolve to an OpenAI model — see PR #69's
    # ``assert_no_openai_default`` startup guard. ``config/default.yaml``
    # points at this exact model under ``llm.models.reasoning``; the
    # class-level default below is the safety net for environments that
    # bypass YAML loading (tests, scripts).
    default_model: str = "anthropic/claude-sonnet-5"
    models: dict[str, dict[str, str]] = Field(default_factory=dict)
    max_retries: int = 3
    request_timeout: int = 120
    # Per-company daily token ceiling enforced at /agent/run (0 disables).
    # Counting covers the whole fleet; webhook processing is never blocked.
    # 5M tokens/day is far above legitimate use but caps a leaked-JWT
    # runaway at roughly single-digit dollars (pre-launch audit).
    daily_token_budget: int = 5_000_000
    #: Anthropic prompt-cache lifetime — ``"1h"`` or ``"5m"``.
    #:
    #: Measured on the first real turn after the 2026-08-26 boot fix ("what's
    #: the rent on unit 4B", 13.3s, $0.146): **76% of the cost was cache
    #: WRITES** — 44,148 write tokens at $2.50/M against 65,906 reads at
    #: $0.20/M. The default TTL is five minutes, so a manager asking a
    #: question every twenty minutes never gets a hit and re-pays the write
    #: every single turn.
    #:
    #: A 1-hour write costs $4.00/M — 1.6x a five-minute write for a cache
    #: that lives twelve times longer, so it pays for itself on the SECOND
    #: turn within the hour (2 turns: 16% cheaper; 6 turns: 69%). The only
    #: losing shape is one question with no follow-up inside an hour.
    #:
    #: Nothing about the answers changes — same model, same tools, same
    #: prompt. Set ``"5m"`` to revert without a deploy.
    cache_ttl: str = "1h"
    openai_api_key: str = Field(default="", repr=False)
    anthropic_api_key: str = Field(default="", repr=False)
    # Dead since the 2026-07-21 Gemini exit (gotcha 18) — field kept only so a
    # stale env var can't crash Settings; nothing should read it for new work.
    google_api_key: str = Field(default="", repr=False)
    # Voyage AI — the memory embedder (Anthropic has no embeddings API; Voyage
    # is the standard Claude pairing). Used ONLY by mem0 via
    # ``memory/voyage_embedder.py``. HELIXIS_LLM__VOYAGE_API_KEY.
    voyage_api_key: str = Field(default="", repr=False)


class BrowserSettings(BaseModel):
    """Browser_agent v1 — Browserbase-backed read-only specialist.

    When ``browserbase_api_key`` OR ``browserbase_project_id`` is
    blank, the six browser_tools register normally but every call
    returns a structured ``browser_unavailable`` result so the agent
    can degrade gracefully instead of crashing. PR #153 replaced the
    browser_agent runtime; the four legacy CDP fields below have had no
    reader since ``browser/automation.py`` was deleted (2026-10-02). They
    stay because ``config/*.yaml`` still set them and ``test_settings`` uses
    ``headless`` to exercise YAML layering.

    Budgets are intentionally tighter than the original spec (was
    20/90/10, now 15/75/8). v1 bar: reliable on simple lookups,
    fast to escalate on anything complex.
    """

    # PR #153 — new fields driving the Browserbase-backed runtime.
    browserbase_api_key: str = Field(default="", repr=False)
    browserbase_project_id: str = ""
    max_steps_per_task: int = 15
    max_wall_clock_seconds: int = 75
    max_screenshots_per_task: int = 8

    # Legacy CDP fields — no reader (see the class docstring).
    cdp_url: str = "http://localhost:9222"
    headless: bool = False
    default_timeout: int = 30
    screenshot_on_action: bool = False


class MemorySettings(BaseModel):
    enabled: bool = True
    # Voyage embedder (post-Gemini-exit, gotcha 18 — Anthropic has no
    # embeddings API). ``voyage-4-lite``: 200M free tokens/account then
    # $0.02/MTok; Matryoshka output dims pinned via ``embedding_dims``.
    # Wired through ``memory/voyage_embedder.py`` (custom mem0 provider).
    embedding_model: str = "voyage-4-lite"
    # Output dimension for the embedder AND the pgvector column — pinned
    # EXPLICITLY on every embed call (Voyage server defaults differ per model;
    # trusting them would silently mismatch the column). MUST match the live
    # ``helixis_memories`` / ``helixis_memories_entities`` tables. Changing
    # this requires the re-embed migration: ``scripts/reembed_memories.py``
    # (one-click via the "Memory re-embed" workflow_dispatch).
    embedding_dims: int = 1024
    search_top_k: int = 10
    # Auto-context injection — the orchestrator pulls top-K memories
    # into the prompt before the LLM call instead of relying on the
    # agent to decide to search.
    auto_inject: bool = True
    # Post-run extraction — after the agent finishes, feed the
    # conversation to Mem0 so it can extract salient facts.
    auto_extract: bool = True
    # Lower than search_top_k so the injected context stays tight.
    inject_top_k: int = 5
    # Hard ceiling on a single memory search (embed query + vector search).
    # Memory is an enhancement, never load-bearing (gotcha 7/17) — a slow or
    # hung embedder must degrade to "no memory" FAST, not stall the whole
    # chat turn. The 2026-07-25 stress test measured an ~80s pre-turn stall
    # on prod when the embedder hung; this caps that at a few seconds.
    search_timeout_s: float = 6.0


#: Supabase's SESSION-mode pooler caps the whole PROJECT at this many clients,
#: and every process shares that one number — the web service, the worker,
#: mem0's own pool, CI, and whoever has a psql open.
#:
#: ⚠ THESE ARE CONSTANTS RATHER THAN PROSE BECAUSE THE PROSE COULD NOT FAIL.
#: The budget below was derived from a real outage and then lived only in a
#: comment, so raising ``pool_max_web`` back to asyncpg's default of 10 broke
#: no test and silently re-oversubscribed the pooler (gotcha 48's shape,
#: pointed at an operational limit). ``test_connection_budget.py`` reads these
#: against the settings so the arithmetic is checkable.
POOLER_CLIENT_CAP = 15

#: Clients this deployment does NOT control and cannot take back. mem0 opens
#: its own pool for the memory store; ops covers the daily smoke, a CI run and
#: a human's psql session. Subtracted from the cap before we spend any of it.
RESERVED_FOR_MEM0 = 2
RESERVED_FOR_OPS = 2


class SupabaseSettings(BaseModel):
    url: str = ""
    anon_key: str = Field(default="", repr=False)
    service_key: str = Field(default="", repr=False)
    # The connection string carries the database password.
    db_url: str = Field(default="", repr=False)
    # Connection budget — see POOLER_CLIENT_CAP above. The Jul-25 dogfood
    # burst hit the cap and 500'd /agent/run with EMAXCONNSESSION (Sentry
    # PYTHON-FASTAPI-31): web(10 default) + worker(10 default) + mem0 + CI
    # already oversubscribed it. Raise only alongside the pooler's own
    # pool_size (or a move to transaction mode) — the guard reads both.
    pool_max_web: int = 6
    pool_max_worker: int = 4
    # PR #101 — when true (default), boot fails loudly if the
    # asyncpg pool can't see the columns the recent-events route
    # depends on. Set ``HELIXIS_SUPABASE__VERIFY_SCHEMA_ON_BOOT=false``
    # in local dev where migrations haven't been applied yet.
    verify_schema_on_boot: bool = True


class AuthSettings(BaseModel):
    jwt_secret: str = Field(default="", repr=False)


class ComposioSettings(BaseModel):
    api_key: str = Field(default="", repr=False)


class BillingSettings(BaseModel):
    """Stripe subscriptions and the free trial.

    ⚠ `enforce` is the LOCKOUT, and it is a separate switch from having keys.
    Credentials existing means we can take a payment; `enforce` means an
    unpaid company is refused. They are different questions and gotcha 67 is
    this repo's record of what happens when one flag is read as answering
    both — the CRM texted real leads because `configured` was checked where
    `enabled` was meant.

    It defaults **False** so billing can be wired, tested against Stripe's
    test mode, and watched in the usage meter before it starts turning
    customers away. Flip it deliberately.

    ⚠ Per gotcha 29 the live value of this flag is NOT what this file says —
    Render's env wins and is invisible here. Confirm from behaviour or from
    the Render Environment tab, never from the default.

    Price ids come from the Stripe dashboard and differ between test and live
    mode; an empty one makes that plan unbuyable, which is why
    `/billing/checkout` refuses rather than sending Stripe a blank price.
    """

    stripe_secret_key: str = Field(default="", repr=False)
    stripe_webhook_secret: str = Field(default="", repr=False)
    price_starter: str = ""
    price_pro: str = ""
    price_scale: str = ""
    legacy_price_ids: str = ""
    """Retired price ids, `plan:price_id` comma-separated — GRANDFATHERING.

    ⚠ Raising a price means creating a NEW Stripe price, because Stripe cannot
    edit an existing one's amount. Existing subscriptions keep the OLD price id
    forever — that IS the grandfathering, and it is Stripe's behaviour rather
    than a choice. Repoint `price_pro` at the new id and from that moment
    `_plan_for_price` recognises the new one and NOTHING ELSE, so every
    grandfathered customer's subscription resolves to no plan at all.

    Two consequences, and the second is the one that costs money:
      * every `customer.subscription.*` event for them logs at ERROR, forever —
        the canary that cannot pass, which is how the next real finding gets
        scrolled past (gotcha 84).
      * `subscription_reconcile` cannot repair them. Its whole job is restoring
        a company whose row lost its plan to a dropped webhook; with the price
        unresolvable it can see the id and status drift and still has no plan
        to write, so a grandfathered customer stuck on the trial allowance
        STAYS there while their card is charged.

    Format: `pro:price_old123,scale:price_old456`. Unparseable entries are
    dropped with a log rather than raising — a typo in an env var must not stop
    the app booting, and the failure it degrades to is the one that already
    exists today.
    """
    enforce: bool = False
    portal_return_url: str = ""

    @property
    def price_ids_by_plan(self) -> dict[str, tuple[str, ...]]:
        """Plan key -> EVERY price id that means this plan, current first.

        ⚠ The current id leads, and `price_ids` above stays the single source
        for CHECKOUT. A retired price must never be sellable: it exists only so
        an EXISTING subscription can be read back to its plan. Two directions,
        one map each — collapsing them would make a legacy id purchasable,
        which is a customer buying a price we withdrew.
        """
        legacy = _parse_legacy_prices(self.legacy_price_ids)
        return {
            key: (current, *legacy.get(key, [])) if current else tuple(legacy.get(key, ()))
            for key, current in self.price_ids.items()
        }

    @property
    def price_ids(self) -> dict[str, str]:
        """Plan key -> Stripe price id, for every purchasable plan.

        ⚠ This exists so `/billing/checkout` never NAMES a plan. It used to
        carry `price_starter if key == "starter" else price_pro`, which is a
        branch that silently mis-charges the moment a third plan exists — the
        new plan falls to the `else` and buys Pro. Same fan-out shape as
        gotcha 25, pointed at somebody's card.

        A plan missing from this map is unbuyable rather than mispriced, and
        `test_billing_plans.py` compares these keys against `PAID_PLANS` so
        the omission fails a test instead of a checkout.
        """
        return {
            "starter": self.price_starter,
            "pro": self.price_pro,
            "scale": self.price_scale,
        }

    @property
    def configured(self) -> bool:
        return bool(self.stripe_secret_key and self.stripe_webhook_secret)


class TwilioSettings(BaseModel):
    """Twilio SMS — the texting channel for CRM leads.

    A lead can arrive as (or reply by) SMS to a Twilio number Helixis owns
    on the company's behalf. Inbound texts hit ``POST /crm/sms/inbound``
    (Twilio webhook, signature-verified with ``auth_token``); outbound
    replies go through the REST API with ``account_sid`` + ``auth_token``,
    sent from ``from_number``.

    All blank by default → :meth:`configured` is False and the SMS paths
    stay dark (the inbound webhook 404s, a reply call 503s) exactly like
    the other optional connectors. Secrets come from env
    (``HELIXIS_TWILIO__ACCOUNT_SID`` / ``__AUTH_TOKEN`` / ``__FROM_NUMBER``),
    never YAML.

    ``default_company_id`` is the v1 single-number → company mapping: an
    inbound text has no auth, so its company is resolved from the number it
    was sent TO. Until per-company numbers are stored, one shared number
    routes to this company. Leave blank in multi-company deployments.

    ``enabled`` gates the OUTBOUND customer-care SMS channel
    (``comms/sms.py`` — templated rent reminders / maintenance updates /
    appointment confirmations under the registered A2P 10DLC campaign).
    Default OFF so the feature ships DARK and lights up only when carriers
    approve the campaign and a deploy flips ``HELIXIS_TWILIO__ENABLED=true``
    (same safe-off shape as CRM/email ingest). ``messaging_service_sid`` is
    the optional alternative sender to ``from_number`` — when set, sends go
    out via the Messaging Service (Twilio picks the number from its pool).
    """

    enabled: bool = False
    # ``account_sid`` stays printable: it is in the path of every Twilio API
    # URL, so hiding it here would hide nothing.
    account_sid: str = ""
    auth_token: str = Field(default="", repr=False)
    from_number: str = ""
    messaging_service_sid: str = ""
    default_company_id: str = ""
    # The ISV PRIMARY customer profile bundle (``BU...``) — Occupella's own
    # approved business on this Twilio account. Every customer's SECONDARY
    # profile must be assigned to it or the carrier rejects the bundle with
    # "Primary customer profile bundle is null"; it is ONE value for the whole
    # deployment, never per-customer, which is why it lives here and not on a
    # row. Config and not a constant: staging and production are different
    # Twilio accounts with different primaries, so hardcoding it would send
    # staging's registrations at production's business.
    # ⚠ Blank is NOT a soft default — ``advance_submission`` refuses to run.
    # Building a bundle guaranteed to be rejected is the same silent failure
    # the evaluation step was added to end, one layer up.
    primary_profile_sid: str = ""
    # The PUBLIC backend origin Twilio calls back for the customer-care SMS
    # webhooks (e.g. ``https://your-api.onrender.com`` — scheme + host,
    # no trailing slash, no path). Non-empty → outbound sends request
    # delivery receipts (``StatusCallback`` → ``{base}/api/v1/twilio/status``)
    # and the inbound/status endpoints verify ``X-Twilio-Signature`` against
    # ``{base}{route path}``. ⚠ It must match EXACTLY what is configured in
    # the Twilio console (the full URL participates in the HMAC-SHA1
    # signature — a scheme/host/path mismatch fails every verification).
    # Empty (the default) → the twilio.py endpoints 403 everything
    # (fail closed) and no StatusCallback is requested.
    webhook_base_url: str = ""
    # Voice (call intake). When a lead calls the Twilio number, Helixis logs
    # the lead then forwards the call to ``forward_to_number`` (the PM's real
    # phone). If the PM doesn't answer within ``dial_timeout_seconds``, the
    # caller hears ``voice_greeting`` and can leave a voicemail (recorded +
    # attached to the lead). Blank ``forward_to_number`` → straight to
    # voicemail (no forward). Blank greeting → a sensible default is used.
    forward_to_number: str = ""
    voice_greeting: str = ""
    dial_timeout_seconds: int = 20
    # Skip inbound signature verification. TESTING ONLY — lets the
    # ``/crm/sms/simulate`` + ``/crm/voice/simulate`` paths and local
    # Twilio-console tests work without a real signed request. NEVER true in
    # production (an unsigned inbound could then spoof leads). Mirrors the
    # email-ingest ALLOW_ALL flag.
    skip_signature_check: bool = False

    @property
    def configured(self) -> bool:
        """True when the outbound REST path can authenticate + send."""
        return bool(self.account_sid and self.auth_token and self.from_number)

    # ⚠ ``greeting_or_default`` USED TO LIVE HERE AND WAS DELETED. It returned
    # the deployment-global ``voice_greeting``, which on a white-label install
    # belongs to at most ONE customer — so every caller who dialled a different
    # company heard that customer's greeting. The per-company greeting lives on
    # ``helixis_twilio_numbers`` (0083) and is read through
    # ``number_routing.telephony_for_company``, which returns the global only
    # when the mapping table is empty (the single-tenant install, where it IS
    # theirs). Do not reintroduce a convenient global accessor: its only effect
    # is to make the leak one attribute away.


class LangfuseSettings(BaseModel):
    """Langfuse observability — traces every LLM call via LiteLLM's callback.

    When keys are blank, tracing is silently disabled — the rest of the
    backend keeps working unchanged.
    """

    enabled: bool = True
    # Both halves: Langfuse authenticates with public_key:secret_key.
    public_key: str = Field(default="", repr=False)
    secret_key: str = Field(default="", repr=False)
    host: str = "http://localhost:3000"
    # Default tags applied to every trace. Per-request tags are appended.
    default_tags: list[str] = Field(default_factory=lambda: ["helixis"])
    # Redact LLM prompt/completion CONTENT before it leaves for Langfuse Cloud
    # (a third party). Tool spans + app logs are already redacted (security/
    # pii.py, #355/#508); this covers the LiteLLM→Langfuse GENERATION
    # input/output, which otherwise ships raw prompts — tenant names,
    # addresses, rent amounts, full inbound email bodies. Trace structure,
    # latency, token counts, and metadata are unaffected, so run-level
    # debugging still works; only the message text is masked.
    #
    # Default ON (safe-by-default) — the moment a real property manager connects,
    # raw tenant PII (names, addresses, rent, full inbound email bodies) would
    # otherwise ship to Langfuse Cloud. Masking is the correct release posture;
    # trace structure, latency, token counts, and metadata are unaffected, so
    # run-level debugging still works — only the message text is masked. Set
    # HELIXIS_LANGFUSE__MASK_GENERATION_CONTENT=false ONLY in a trusted dev env
    # (synthetic/founder data) when full prompt visibility aids quality debugging.
    # Flipped from False→True for release. Audit finding Q25 (2026-07-12).
    mask_generation_content: bool = True


class LatencySettings(BaseModel):
    """Latency budgets (SLOs) + over-budget alerting.

    Timing already exists across the run path (the orchestrator computes a
    per-run ``latency_ms`` and a per-tool ``duration_ms``). These are the
    *budgets* + the *alert*: when a monitored operation blows past its
    threshold, a signal is emitted through the existing observability seams
    (structured WARNING log + a Langfuse WARNING event/score + a PostHog
    capture) so a slow path surfaces on the dashboards instead of dissolving
    into an average.

    Budgets are advisory — exceeding one NEVER fails or slows the request; it
    only emits telemetry. When disabled, the whole subsystem is a no-op.
    """

    enabled: bool = True
    # Full orchestrator run, wall-clock, milliseconds. Gemini + tool round
    # trips; generous so only genuinely slow turns trip it.
    agent_run_ms: int = 20000
    # A single tool call, milliseconds. Buildium/Gmail/web reads sit well
    # under this; a breach means a hung external call or a pathological page.
    tool_call_ms: int = 10000


class PostHogSettings(BaseModel):
    """PostHog product analytics.

    When project_api_key is blank, analytics is silently disabled — the
    rest of the backend keeps working unchanged.
    """

    enabled: bool = True
    project_api_key: str = Field(default="", repr=False)
    host: str = "https://us.i.posthog.com"
    # Flush after every request via middleware (recommended for web
    # servers). Set False only for batch workers that flush manually.
    flush_per_request: bool = True


class SentrySettings(BaseModel):
    """Sentry error tracking.

    When dsn is blank, the whole subsystem is a no-op — the rest of the
    backend keeps working unchanged.
    """

    enabled: bool = True
    # The DSN embeds the project's ingest key.
    dsn: str = Field(default="", repr=False)
    # Fraction of transactions to capture for performance monitoring.
    # 1.0 captures everything (fine pre-launch); drop to 0.1 at scale.
    traces_sample_rate: float = 0.2
    # Send PII (user id, email, IP) along with events.
    send_default_pii: bool = False


class BuildiumSettings(BaseModel):
    base_url: str = "https://apisandbox.buildium.com"
    # ⚠ repr=False: a settings object in a log line, an exception or a Sentry
    # frame printed the working credential. `client_id` too: it is the other
    # half of the header pair, and it is stored encrypted like the secret.
    client_id: str = Field(default="", repr=False)
    client_secret: str = Field(default="", repr=False)
    webhook_secret: str = Field(default="", repr=False)
    account_id: int = 0
    # Max concurrent in-flight requests (connection-pool bound). NOTE: a
    # concurrency cap alone does NOT bound the request RATE — that is what
    # ``requests_per_sec`` is for. Keep both.
    concurrency: int = 8
    # Hard request-rate ceiling. Buildium throttles at 10 req/sec PER ACCOUNT
    # (429 + ~200ms Retry-After). The client enforces this with a process-wide
    # token bucket keyed by client_id, so it holds across the per-call client
    # instances. Slightly under 10 for headroom against clock jitter.
    requests_per_sec: float = 9.0
    # Webhook freshness window in seconds. 3900s = 1 hour + 5 min buffer
    # to cover Buildium's third retry interval (1 minute → 10 minutes →
    # 1 hour) without 401-ing our own redeliveries.
    signature_freshness_seconds: int = 3900
    # Worker loop settings
    worker_batch_size: int = 10
    worker_poll_interval_seconds: float = 2.0
    worker_max_attempts: int = 10
    # Automatic entity-mirror re-sync (cockpit freshness backstop). The
    # worker ticks hourly; each connected company is fully backfilled at
    # most once per interval. Webhooks + the connector card's "Sync data"
    # button remain the on-demand paths.
    mirror_sync_enabled: bool = True
    mirror_sync_interval_hours: float = 24.0


class RentManagerSettings(BaseModel):
    """Rent Manager connector (design/rentmanager-connector.md). Every switch
    is a DEPLOYMENT kill switch, off by default (design P4), and none is the
    per-company gate: whether THIS company is on Rent Manager is whether a
    `company_rentmanager_credentials` row exists (gotchas 72/73). Read through
    `pms.registry`.

    * `enabled`: connect, sync and reads. Read by `pms.sync_cycle`.
    * `writes_enabled`: the gated write tools are visible to the model. Off
      means reads only, the state Brandon's plan ships a connector in. Read by
      `pms.gated_write` and `agents.run_pms.pms_write_tool_enabled`.
    * `sync_interval_hours`: how often the worker re-reads each company
      (design R6: every 4 hours). Read by `pms.sync_cycle`.

    No credential fields: credentials are per company, with no env fallback
    (gotcha 70).
    """

    enabled: bool = False
    writes_enabled: bool = False
    sync_interval_hours: float = 4.0


class PropertywareSettings(BaseModel):
    """Propertyware connector (design/propertyware-connector.md). The same
    three switches as `RentManagerSettings`, for the same reasons, off by
    default (design W6: every 4 hours)."""

    enabled: bool = False
    writes_enabled: bool = False
    sync_interval_hours: float = 4.0


class DoorLoopSettings(BaseModel):
    """DoorLoop connector (design/doorloop-connector.md). The same switches as
    `PropertywareSettings`, off by default, for the same reasons.
    `sync_interval_hours` is how often `pms.sync_cycle` runs `doorloop.sync`
    for each connected company while `enabled` is on."""

    enabled: bool = False
    writes_enabled: bool = False
    # DoorLoop documents no webhooks, so this poll is the only way changes arrive.
    # Every pass reads every list whole (no list filters by modified time), and
    # a key may make 500 requests an hour (the Rate Limiting page): every six
    # hours keeps a pass's call budget (`doorloop.sync.CALL_BUDGET`) clear of
    # the next one.
    sync_interval_hours: float = 6.0


class RentvineSettings(BaseModel):
    """Rentvine connector — the second PMS. ADR 0010.

    ⚠ **`enabled` IS A DEPLOYMENT KILL SWITCH, NOT THE PER-COMPANY GATE, and
    conflating the two is the mistake gotchas 72/73 record.** Twilio shipped one
    global `enabled` for a fact that was really per-company, and the first
    customer's approval flipped it for everybody. So, explicitly:

    * **Is the Rentvine connector live in this deployment at all?** → this flag.
      Default **False**: the code ships dark and a deploy turns it on.
    * **Is THIS company on Rentvine?** → whether a `company_rentvine_credentials`
      row exists for them. That is per-company by construction, the same way
      Buildium has always worked, and it is the check every product path must
      make. Never answer it from this flag.

    A company with credentials is still dark while `enabled` is False — that
    ordering is deliberate, so a deployment can be rolled back without
    disconnecting anybody.

    The credential fields below exist for LOCAL DEVELOPMENT and the read-only
    probe only. ⚠ Production loads credentials per company through
    `rentvine/credentials.py` and has **no env fallback** — gotcha 70 is the
    standing record of what an env fallback costs: it pointed at the wrong
    account and verified the wrong books.
    """

    enabled: bool = False
    # Local/probe use only — never a production fallback (see the docstring).
    account_code: str = ""
    # ⚠ repr=False: a settings object in a log line, an exception or a Sentry
    # frame printed both halves of the credential (terms rule 11).
    api_key: str = Field(default="", repr=False)
    api_secret: str = Field(default="", repr=False)
    # Mirrors BuildiumSettings' transport knobs. ⚠ `requests_per_sec` is a
    # GUESS: Rentvine's OpenAPI spec contains zero occurrences of `429`,
    # `ratelimit` or `retry-after`, so there is no documented ceiling to aim
    # at. Phase 8 measures it; until then this is deliberately conservative
    # rather than tuned.
    concurrency: int = 4
    requests_per_sec: float = 4.0
    timeout_seconds: float = 30.0
    # How often the worker re-mirrors one company (`rentvine/sync_cycle.py`).
    # ⚠ The POLL IS PRIMARY here, unlike Buildium's, where the same-named knob
    # is a freshness backstop on top of webhooks. Rentvine's webhooks cover four
    # object types, so this interval refreshes everything else, which is the
    # reason the worker ticks it hourly rather than daily and lets this guard
    # decide, the same shape as `mirror_sync_interval_hours`. (Until 2026-10-04
    # the receiver refused every delivery and this was the ONLY feed; the scheme
    # was measured then, #1080/#1081, and production accepts deliveries now.)
    sync_interval_hours: float = 24.0

    # ── the webhook signature scheme ──────────────────────────────────
    #
    # ⚠ BLANK BY DEFAULT, and blank means REFUSE EVERY DELIVERY. The scheme
    # itself is no longer unknown: measured 2026-10-04 (Mailgun's, inside the
    # body; gotchas 201/203), and production accepts deliveries (Lease.Updated,
    # Render logs 2026-10-23), so these are set there. The paragraph below is
    # why the default stays blank.
    # Rentvine's OpenAPI spec contains zero occurrences of `webhook`,
    # `signature` or `hmac`, so the scheme is unknown — and a receiver that
    # fails OPEN on an unknown algorithm accepts forged events into a
    # landlord's books. `rentvine/signature.py` refuses until these are set.
    #
    # They are DEPLOYMENT-level, unlike the signing KEY: how Rentvine signs is
    # a property of the vendor, while the key is per-account and lives
    # encrypted in `company_rentvine_credentials` (migration 0102).
    #
    # ⚠ Fill them from `scripts/rentvine_webhook_capture.py`'s output and
    # nothing else. Its five result fields are these five fields, so settling
    # the unknown is a copy rather than a translation — and a value typed from
    # memory is the guess the seam exists to avoid.
    webhook_signature_header: str = ""
    webhook_signature_algorithm: str = ""
    webhook_signature_payload: str = ""
    webhook_signature_key_encoding: str = ""
    webhook_signature_digest_encoding: str = ""
    #: Only needed when the payload construction mixes a timestamp in.
    webhook_signature_timestamp_header: str = ""


class RateLimitSettings(BaseModel):
    """Per-route rate limits (requests per minute)."""

    agent_run: int = 20
    agent_action: int = 60
    connectors: int = 30
    # Buildium bursts webhook delivery to ~600/min and SUSPENDS the whole
    # subscription after 20 consecutive failed deliveries (2026-07 release
    # audit) — so a 120/min cap that 429s a real burst is a self-inflicted
    # outage. The signature check is the true gate on this route; the rate
    # limit is only abuse insurance, so give it comfortable headroom over
    # Buildium's real burst rate.
    webhooks: int = 1200


class DevSettings(BaseModel):
    """Staff-facing developer affordances.

    Each flag here gates a route or UI surface that exists to help
    debug Helixis itself, not to serve product features. Defaults
    follow the same shape as the rest of the codebase: safe-off for
    production-shaped configs (``HELIXIS_ENV=production`` will pin
    the value to ``False`` via ``config/production.yaml``), opt-in
    elsewhere.
    """

    # PR #141 — gates ``GET /dev/runs`` and the corresponding
    # frontend page. When ``False`` (production default) the route
    # returns 404 even for authenticated users, so the page is
    # invisible until explicitly enabled.
    show_runs_page: bool = False

    # Dev-only local model override (2026-07-26). When set to a LiteLLM
    # model string (e.g. ``ollama/llama3.3``) AND the env is NOT production,
    # the reasoning model resolves to this instead of the Anthropic default —
    # lets a developer run the agent MACHINERY against a free local Ollama
    # model (does fan-out fire? does the loop complete? does the salvage
    # deliver?) without spending Anthropic tokens. ⚠ PLUMBING ONLY — a local
    # model's ANSWER quality says nothing about prod Sonnet. HARD-GATED to
    # non-production: ``router.get_model`` ignores it (with a loud warning)
    # when ``env == "production"`` so it can never accidentally ship.
    # Set via HELIXIS_DEV__LOCAL_MODEL. Empty = off (the default everywhere).
    local_model: str = ""


class CRMSettings(BaseModel):
    """Lead-pipeline (CRM) feature flag.

    The CRM section ships behind a "Coming soon" gate: the frontend
    renders a design preview, and the backend ``/crm/*`` routes 404 while
    ``enabled`` is ``False`` (the default) so the API surface isn't live
    until lead ingestion is wired. Flip with ``HELIXIS_CRM__ENABLED=true``.
    Mirrors the safe-off shape of :class:`DevSettings`.
    """

    enabled: bool = False
    # Worker flag: turn the Gmail→CRM lead poll on (default OFF, dark until a
    # deploy flips it). When on, the webhook worker polls each Gmail-connected
    # user, keeps emails that look like a NEW lead (listing-site sender or an
    # inquiry-shaped message, and NOT an existing Buildium contact — those go
    # to the Inbox), and inserts them as crm_leads. Also gates the keyless
    # ``POST /crm/email/simulate`` test route.
    email_ingest_enabled: bool = False


class AnalysisSettings(BaseModel):
    """Analyst capability (ADR 0007 slice 1) — agent-written read-only SQL
    over the Buildium mirror.

    Dark by default: the analysis tools return ``analysis_unavailable``
    until a deploy flips ``HELIXIS_ANALYSIS__ENABLED=true`` (same safe-off
    shape as CRM/email ingest). The handoff itself registers
    unconditionally — gating lives at the tool layer, per the web/browser
    pattern.
    """

    enabled: bool = False


class NotesSettings(BaseModel):
    """Autonomous Buildium notes — Occupella recording a durable fact in the
    customer's system of record WITHOUT being asked.

    Dark by default, and the flag is the only thing standing between the two
    behaviours. The confirm-gated ``buildium_add_note`` tool is unaffected and
    stays live: the difference this flag makes is whether a human saw the note
    before Buildium did.

    Flipping it also narrows by entity kind — see
    ``notes/writer.py::AUTONOMOUS_KINDS``, which starts at the one Buildium
    endpoint that can only ever append.
    """

    autonomous_enabled: bool = False


class CodeExecSettings(BaseModel):
    """Agent code-execution — the "compute, never act" capability.

    Dark by default (``HELIXIS_CODEEXEC__ENABLED``). ⚠ Do NOT turn it on while
    ``substrate`` is ``"subprocess"``: that path runs agent-authored Python on
    the SAME container and uid as the API process, and its "scrubbed env" is
    not a boundary — a probe read ``HELIXIS_DATA_KEY`` out of
    ``/proc/<ppid>/environ`` (gotcha 33, pinned in
    ``test_codeexec_containment``).

    ``substrate="e2b"`` moves execution into a Firecracker microVM. Live-probed
    2026-08-22: ``/proc/1/environ`` holds 18 bytes (``HOME``/``TERM``), no
    secret is reachable, and a detached grandchild cannot survive because the
    whole VM is destroyed. That is the configuration the flag is allowed to go
    true under.

    The resource ceilings stay tight — a reporting/compute task is short and
    small — and ``wall_clock_s`` is enforced by US, not by the provider (see
    ``e2b_substrate``: E2B's own timeout raises, fires at 2x the requested
    value, and destroys the sandbox).
    """

    enabled: bool = False
    #: "subprocess" (local, NOT isolated — dev/test only) or "e2b".
    substrate: str = "subprocess"

    @model_validator(mode="after")
    def _refuse_live_on_an_unsafe_substrate(self) -> CodeExecSettings:
        """Boot-time fail-closed: the capability cannot be live on the local
        substrate.

        This is the guarantee the executor's fallback leans on. Without it,
        anything that made ``substrate`` unreadable would quietly route agent
        code back onto the same-uid path that leaks ``HELIXIS_DATA_KEY`` — and
        it would look like it was working. A config that could do that now
        refuses to construct, so the deployment fails loudly at boot instead of
        succeeding unsafely.
        """
        if self.enabled and (self.substrate or "").strip().lower() != "e2b":
            raise ValueError(
                "HELIXIS_CODEEXEC__ENABLED=true requires "
                "HELIXIS_CODEEXEC__SUBSTRATE=e2b — the local subprocess "
                "substrate is not isolated (gotcha 33: a run can read "
                "HELIXIS_DATA_KEY out of /proc) and must never serve live "
                "code execution."
            )
        return self

    #: E2B API key. Empty + substrate="e2b" fails closed rather than silently
    #: falling back to the unsafe local path.
    e2b_api_key: str = Field(default="", repr=False)
    #: Custom E2B template with the reporting toolchain prebaked. Empty uses
    #: E2B's default (pandas/openpyxl/numpy/matplotlib present; reportlab,
    #: pypdf, fpdf, xlsxwriter absent — live-probed).
    e2b_template: str = ""
    #: Deny ALL outbound network from the sandbox. Keep true: with the
    #: toolchain baked into a template nothing legitimately needs egress.
    e2b_deny_egress: bool = True
    wall_clock_s: float = 10.0
    cpu_s: int = 8
    memory_mb: int = 512
    fsize_mb: int = 32


class ReconSettings(BaseModel):
    """Reconciliation (roadmap §7 rank 4) — Phase 2 analyst flags.

    Phase 1 (deterministic rules) is always on: pure SQL, no LLM, no
    flag needed. ``analyst_enabled`` gates the Phase-2 nightly LLM
    analyst that cross-examines the company dossier and writes
    ``source='analyst'`` risk flags — default OFF (dark until a deploy
    flips ``HELIXIS_RECON__ANALYST_ENABLED=true``; dogfood on our own
    books first, same safe-off shape as CRM/email ingest).
    """

    analyst_enabled: bool = False
    # Hard cap on analyst flags accepted per company per run — the model
    # is asked for the top findings, not a firehose.
    analyst_max_flags: int = 5
    # Optional model override; empty = the planner's reasoning model.
    analyst_model: str = ""


class CompanyKnowledgeSettings(BaseModel):
    """Company knowledge MD (ADR 0006) — a short standing company profile,
    generated in the background at most once per day and injected as agent
    context.

    ``enabled`` gates the daily background generator. ON by default (#67): it is
    a READ-ONLY background summarizer (one cheap LLM call/company/day, idempotent
    per day) — unlike the send-capable CRM/email ingest, there's no safe-off
    reason to keep it dark, and a populated profile is what makes the agent feel
    like it works *here*. Set ``HELIXIS_COMPANY_KNOWLEDGE__ENABLED=false`` to
    disable. ``inject`` gates whether an existing profile (the auto-learned
    ``content_md``) is added to each agent turn's context; when a company has no
    row yet (or the migration isn't applied), injection is a no-op regardless.
    """

    enabled: bool = True
    inject: bool = True
    # Recent-webhook-activity rollup window for the "what's happening" section.
    activity_days: int = 30
    # Google Drive as a knowledge source (#67 PR-2). When on, the daily cycle
    # enumerates each Drive-connected user's files ONCE and caches a static index
    # (drive_index) so the agent knows what documents exist without a live Drive
    # API call per question. ``drive_max_files`` caps the per-user listing.
    drive_source: bool = True
    drive_max_files: int = 200


class APISettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"]
    )
    # Regex matched against the Origin header. Used for Vercel preview
    # deployments where the URL changes per commit
    # (your-app-<hash>-your-team.vercel.app). Empty
    # string disables the regex check entirely.
    cors_origin_regex: str = ""
    # Public URL of the web app teammates land on. Used as GoTrue's
    # ``redirect_to`` for team-invite emails so the invite link returns to the
    # app (→ set-password screen) instead of Supabase's dashboard Site URL,
    # which has drifted to retired domains before (gotcha 3) — the "invite email
    # link is broken" bug. When blank, the invite falls back to the first
    # https:// entry in ``cors_origins``. ⚠ Whatever this resolves to MUST be in
    # Supabase → Authentication → URL Configuration → Redirect URLs, or GoTrue
    # ignores it.
    public_app_url: str = ""
    rate_limits: RateLimitSettings = Field(default_factory=RateLimitSettings)


class WebSettings(BaseModel):
    """Public-web research via Tavily Search API.

    When ``tavily_api_key`` is blank, the ``web_search`` tool registers
    normally but every call returns a structured ``search_unavailable``
    result so the agent can degrade gracefully instead of crashing.
    """

    tavily_api_key: str = Field(default="", repr=False)
    default_max_results: int = 5
    request_timeout_seconds: float = 6.0


class MapsSettings(BaseModel):
    """Maps / places / geolocation.

    Powers the geo features: geocoding Buildium property addresses (stored on
    the ``helixis_properties`` mirror as a PostGIS point) and external
    places/directions lookups. When ``api_key`` is blank the geocoder + places
    tools register normally but every call returns a structured
    ``maps_unavailable`` result, so the agent degrades gracefully instead of
    crashing — same pattern as ``web_search`` / ``browser``.

    ``provider`` is pluggable: ``census`` (DEFAULT since 2026-08-17 — the US
    Census Bureau geocoder: public domain, KEYLESS, US-only, no billing
    account to lose; static chat maps self-render over OSM tiles via
    geo/static_render.py), ``google`` (adds Places POI search, needs a key),
    or ``mapbox``. On ``census``, geocoding + static maps work with ZERO
    configuration and ``places_nearby`` degrades to ``[]`` — the geo layer
    no longer has a required key, which retires gotcha 18's Maps release
    gate. Only the geocode + places clients read it.
    """

    provider: str = "census"
    api_key: str = Field(default="", repr=False)
    request_timeout_seconds: float = 6.0
    # Default radius for "properties/places near X" when the caller gives none.
    default_radius_meters: int = 8000  # ~5 miles


class IntelSettings(BaseModel):
    """Property intelligence (geo/intel.py) — free geo-data sources keyed off
    the stored property coordinates.

    Four of the five sources (FEMA flood, USFS wildfire, EPA radon, EPA
    Superfund) are keyless. ``hud_api_key`` backs the fifth — HUD Fair Market
    Rents. The key is FREE (register at huduser.gov → "Create an account" →
    request an API token); without it the FMR source simply degrades dark
    (``fmr_for_zip`` returns ``None``, no row is written, and the agent tool
    reports "no data yet") while the keyless sources keep working. Env:
    ``HELIXIS_INTEL__HUD_API_KEY``.
    """

    hud_api_key: str = Field(default="", repr=False)


class DocumentSettings(BaseModel):
    """Document extraction (lease / invoice / inspection / work order).

    The extractor uses the same Gemini model as the planner — no separate
    key. ``max_bytes`` caps the size of any single document the tool will
    even attempt; oversize inputs return ``attachment_too_large`` instead
    of being silently forwarded to the LLM (cost + latency control).
    ``min_confidence_for_pass`` is what the agent's prompt treats as the
    ``low_confidence`` threshold; below this the schema-level validator
    requires a ``low_confidence`` warning.
    """

    request_timeout_seconds: float = 30.0
    max_bytes: int = 8 * 1024 * 1024  # 8 MiB cap on any single document
    min_confidence_for_pass: float = 0.6
    # When True, PDFs are pre-parsed to text with docling-parse (the
    # ``[document]`` extra) before extraction, instead of being sent to Gemini
    # as a base64 image. Default False so base/test/eval behavior is identical
    # to today; prod opts in. Falls back to the multimodal path when the extra
    # is absent or the parse yields no usable text.
    use_docling: bool = False


class AgentSettings(BaseModel):
    # Agent-loop turn cap. LIVE (LED-029): passed as ``max_turns`` to
    # ``Runner.run_streamed`` in orchestrator.run_agent_streaming. Raising it
    # gives a multi-tool/handoff turn more room; the stall watchdog + token
    # budget bound cost independently.
    max_iterations: int = 25
    # ⚠ DEAD CONFIG (LED-029): nothing reads ``confirmation_required``. It looks
    # like the write-gate allowlist but governs NOTHING — confirmation is driven
    # per-tool by ``confirm_or_abort`` / ``run_guarded`` (agents/tools.py,
    # connectors/tool_gate.py), not this list. Editing it does not gate or
    # un-gate any tool. Kept only so a stale env/YAML that sets it doesn't error;
    # do not trust it as a safety control.
    # PR #153 — the ``browser_enabled`` flag was removed. The
    # browser_agent now always registers as a handoff target; the
    # tool layer self-gates on Browserbase credentials being set
    # (``settings.browser.browserbase_api_key`` and
    # ``browserbase_project_id``). When missing, every tool returns
    # ``browser_unavailable`` and the agent's prompt teaches it to
    # surface that gracefully — same shape as web_agent and
    # document_agent.
    confirmation_required: list[str] = Field(
        default_factory=lambda: [
            "send_email",
            "update_buildium",
            "delete_record",
            "create_task",
            "modify_lease",
            "send_sms",
        ]
    )
    # REVERSE_REPORT contract (pre-GTM reliability gate). Off by default
    # in prod; CI turns it on. Slice 1 only wires the parser/validator +
    # eval assertions behind this flag — no prompt or enforcer behavior
    # is gated yet, so flipping it today is a no-op in production.
    reverse_report: bool = False
    # Cross-source reasoning loop (W1). OFF by default so the live path stays
    # the proven terminal-handoff router. When ON, create_orchestrator wires
    # each specialist as a callable ``.as_tool()`` instead of a handoff target,
    # so the orchestrator can consult several sources in ONE turn and synthesize
    # (e.g. cross-reference a delinquent tenant against their lease doc + the
    # owner's last email). Flipping the default to ON is gated on later slices:
    # confirm-gate-survives-.as_tool() verification, telemetry/routing-eval
    # re-keying (handoff spans -> tool-call spans), and runaway protection.
    reasoning_loop: bool = False
    # Runaway protection for the reasoning loop (only consulted when
    # reasoning_loop is ON). A cross-source turn can now call several specialist
    # tools, each a NESTED sub-run — so the worst case is multiplicative. Two
    # independent bounds contain it:
    #   * per-specialist sub-run turn cap — passed as ``.as_tool(max_turns=…)``
    #     so one specialist can't loop unboundedly inside a single tool call
    #     (the orchestrator's own ``max_iterations`` still bounds its rounds).
    #   * absolute per-turn wall-clock deadline — the stall watchdog only catches
    #     SILENCE; a loop that keeps emitting events but runs long would never
    #     trip it, so this caps total elapsed regardless of activity.
    # Bumped 6→8 and 180→240 after the 2026-07-25 stress test: real
    # multi-agent turns landed 149–212s (deep Cedar-Ridge / vendor questions),
    # and a Buildium lease-history fetch needed >6 specialist sub-turns — so
    # the old caps cut turns that were about to finish (the empty "stop early"
    # give-up + "max turns exceeded" defects). #718 also freed ~30–100s of
    # per-turn memory stall, so 240s is comfortable headroom, not a cost blow-up.
    reasoning_loop_specialist_max_turns: int = 8
    reasoning_loop_deadline_s: float = 240.0

    # ⚠ THE PER-TURN COST CEILING. Until this landed, per-turn spend was
    # MEASURED and never BOUNDED: `llm/spend.py` enforces only a per-company
    # DAILY gate, checked once before the turn and documented as failing OPEN
    # ("a ledger read error admits the run"), and `llm/turn_cost.py` says of
    # itself that it is the instrument, not a control. Nothing anywhere raised
    # or refused on what a single turn was spending.
    #
    # Sized from live data, 2026-08-29, n=213 priced runs over three days:
    #   p50 $0.0699   p90 $0.4615   p95 $0.6004   p99 $1.3865   max $4.3439
    # Four runs (1.9%) went past $1.00 and burned $8.51 of the window's $42.64
    # — 20% of all spend in under 2% of turns, which is the shape a ceiling is
    # for. $1.00 sits well above the honest expensive turn (a portfolio review
    # legitimately costs more than a balance lookup; p95 is $0.60) and below
    # the runaway tail.
    #
    # ⚠ Crossing it does NOT kill the turn. It raises the SOFT-deadline error,
    # so the run stops and `deadline_synthesis` writes an answer from what was
    # already gathered — the same rescue the refusal ceiling uses. Killing on
    # cost and returning nothing would re-create the 22%-blank failure gotcha
    # 37 documents, which is the trap this design exists to avoid.
    #
    # <= 0 disables the ceiling entirely.
    turn_cost_ceiling_usd: float = 1.00
    # Haiku fast router (upgrade 2, 2026-07-25) — dark by default, flip with
    # HELIXIS_AGENT__FAST_ROUTER=true AFTER the 8x eval rep pass. A ~300ms
    # cheap-model classification BEFORE the orchestrator: unambiguously
    # single-source questions whose specialist needs no Composio fetch
    # (Buildium reads, Data Analyst) run that specialist DIRECTLY, skipping
    # the Sonnet orchestrator turn entirely. Everything else — email/
    # calendar/drive/web/docs, multi-source, writes, ambiguity, classifier
    # errors or timeout — fails OPEN to the full orchestrator. The router
    # may only ever save a hop, never change behavior when unsure.
    # Adaptive reasoning effort (the 163s median). Only ever DOWNGRADES, and
    # only on question shapes it positively recognises as single-fact lookups;
    # everything unrecognised keeps today's full budget. Safe ON by default for
    # that reason — flip false to pin every turn back to one budget.
    # ⚠ THE DIRECTION OF THIS LEVER IS THE OPPOSITE OF ITS STATED RULE.
    # `effort.py` says "Design rule: only ever DOWNGRADE". Read off the
    # installed LiteLLM transform (`AnthropicConfig._map_reasoning_effort`,
    # 1.94.1) rather than assumed:
    #   * `reasoning_effort=None` sends NO thinking param at all.
    #   * `claude-sonnet-5` (production primary) IS an adaptive-thinking model,
    #     so every level returns `{"type": "adaptive"}` on the first branch —
    #     low/medium/high are byte-identical. The LEVEL is inert; the only
    #     effect is switching adaptive thinking ON for the questions the regex
    #     called trivial.
    #   * `claude-haiku-4-5` (503 failover + PR-gate tier) is NOT adaptive, so
    #     `low` falls through to `{"type": "enabled", "budget_tokens": 1024}`.
    # Either way it cannot downgrade: at best a no-op on level, at worst
    # thinking turned on.
    #
    # ⚠ LEFT ON DELIBERATELY. Direction is settled; MAGNITUDE is not — what
    # adaptive thinking on a trivial question actually costs against no
    # thinking param needs a controlled A/B (a paid battery run per arm plus
    # judge scoring). Flipping a live lever on an unmeasured quality risk is
    # the wrong trade two days from release. The same classifier also gates
    # the specialist cap and `get_lookup_model`, so one regex drives three
    # levers and a flip is not a one-line decision.
    #
    # `tests/unit/test_reasoning_effort_direction.py` re-reads the transform on
    # every run — this is a third-party mapping that can change on any
    # dependency bump with nothing here failing.
    adaptive_effort: bool = True

    # Cheaper MODEL for the same questions ``adaptive_effort`` downgrades.
    # ``effort_settings`` only lowers reasoning effort; it never changed tier,
    # so a one-fact lookup still ran on the $3/$15 model when $1/$5 would do.
    #
    # Ships DARK ("" = off, no swap) and stays that way until calibrated. The
    # effort downgrade is safe on by default because it only removes thinking
    # budget from a question that needs none; a TIER swap changes which model
    # writes the answer, and gotcha 10 is a standing record of Haiku answering
    # differently enough from the production model to fail a case the
    # production model passed minutes earlier. Same reason
    # ``HELIXIS_EVAL_PR_MODEL`` uses the empty-means-off convention: calibrate
    # before trusting it.
    lookup_model: str = ""

    fast_router: bool = False
    fast_router_model: str = "anthropic/claude-haiku-4-5"
    fast_router_timeout_s: float = 3.0


def _load_yaml(path: Path) -> dict[str, Any]:
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


class IdempotencySettings(BaseModel):
    """Agent-action idempotency guard (see helixis.agents.idempotency).

    ``required`` controls the no-store fallback in ``run_guarded``:

    * ``False`` (default — local/test/eval): when the durable store can't be
      obtained, fall back to confirm+execute (the pre-guard behavior), but emit
      a loud warning + observability signal so a real-write passthrough is
      never fully silent. Keeps the 060–064 sweep behaviorally identical.
    * ``True`` (prod): FAIL CLOSED — if the store can't be obtained, do NOT
      execute the side effect; return the needs-review shape. We never run an
      irreversible write without the single-execution guarantee.

    Production gets ``True`` from ``config/production.yaml``, not only from the
    ``HELIXIS_IDEMPOTENCY__REQUIRED`` env var: ``render.yaml`` may not reach
    the live services, and its worker block never set the var. A production
    process that ends up ``False`` anyway says so at boot
    (``weakened_safety_defaults``).
    """

    required: bool = False


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    merged = base.copy()
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# Sentinel marking a config leaf to drop entirely (so it never shadows an
# env var) rather than inject as ``None`` / ``""``.
_DROP = object()


def _prune_empty(value: Any) -> Any:
    """Recursively drop empty leaves (``""`` and ``None``) from a config
    tree, returning ``_DROP`` when nothing survives.

    Why this exists: :meth:`Settings.__init__` injects the merged YAML as
    constructor kwargs, and in pydantic-settings init kwargs OUTRANK env
    vars. A YAML placeholder like ``browserbase_api_key: ""`` (a "set this
    via env" stub) therefore silently shadows
    ``HELIXIS_BROWSER__BROWSERBASE_API_KEY`` and zeroes it out — the same
    trap hides ``tavily_api_key``. Pruning empty leaves before injection
    lets an env var populate any key whose only YAML value was an empty
    placeholder, while leaving every real value — including ``false`` and
    ``0`` — untouched.
    """
    if isinstance(value, dict):
        pruned: dict[str, Any] = {}
        for key, item in value.items():
            kept = _prune_empty(item)
            if kept is not _DROP:
                pruned[key] = kept
        return pruned if pruned else _DROP
    # Only a literal empty string or None counts as empty. ``False`` / ``0``
    # / ``[]`` are meaningful config and must survive.
    if value is None or value == "":
        return _DROP
    return value


def _drop_env_shadowed(value: Any, env_keys: set[str], path: tuple[str, ...] = ()) -> Any:
    """Drop any config leaf that ALSO has a matching ``HELIXIS_*`` env var set,
    so the env var WINS.

    Injected YAML is passed as constructor kwargs, and init kwargs OUTRANK env
    vars in pydantic-settings — per leaf, because nested sources deep-merge
    (the same mechanism ``_prune_empty`` relies on). So a *non-empty*
    ``default.yaml`` leaf shadows its env var: the class of bug behind the CORS
    and langfuse-host regressions, worked around until now by simply NOT setting
    those values in YAML (see the "Do NOT set host here" note in default.yaml).
    Dropping a leaf whose ``HELIXIS_<PATH>`` var is set lets ``env_settings``
    populate it instead. Leaves with no matching env var are untouched — YAML
    still provides them.
    """
    if isinstance(value, dict):
        kept: dict[str, Any] = {}
        for key, item in value.items():
            sub = _drop_env_shadowed(item, env_keys, path + (str(key),))
            if sub is not _DROP:
                kept[key] = sub
        return kept if kept else _DROP
    env_name = "HELIXIS_" + "__".join(path).upper()
    return _DROP if env_name in env_keys else value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HELIXIS_",
        env_nested_delimiter="__",
    )

    env: str = "development"
    # Project-wide Fernet key used to encrypt at-rest secrets like the
    # per-company Buildium credentials (see helixis.security.fernet).
    # Generate with:
    #   python -c 'from cryptography.fernet import Fernet; \
    #              print(Fernet.generate_key().decode())'
    data_key: str = Field(default="", repr=False)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    supabase: SupabaseSettings = Field(default_factory=SupabaseSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    composio: ComposioSettings = Field(default_factory=ComposioSettings)
    buildium: BuildiumSettings = Field(default_factory=BuildiumSettings)
    rentvine: RentvineSettings = Field(default_factory=RentvineSettings)
    rentmanager: RentManagerSettings = Field(default_factory=RentManagerSettings)
    propertyware: PropertywareSettings = Field(default_factory=PropertywareSettings)
    doorloop: DoorLoopSettings = Field(default_factory=DoorLoopSettings)
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    latency: LatencySettings = Field(default_factory=LatencySettings)
    posthog: PostHogSettings = Field(default_factory=PostHogSettings)
    sentry: SentrySettings = Field(default_factory=SentrySettings)
    api: APISettings = Field(default_factory=APISettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    maps: MapsSettings = Field(default_factory=MapsSettings)
    intel: IntelSettings = Field(default_factory=IntelSettings)
    document: DocumentSettings = Field(default_factory=DocumentSettings)
    dev: DevSettings = Field(default_factory=DevSettings)
    crm: CRMSettings = Field(default_factory=CRMSettings)
    company_knowledge: CompanyKnowledgeSettings = Field(default_factory=CompanyKnowledgeSettings)
    recon: ReconSettings = Field(default_factory=ReconSettings)
    analysis: AnalysisSettings = Field(default_factory=AnalysisSettings)
    notes: NotesSettings = Field(default_factory=NotesSettings)
    codeexec: CodeExecSettings = Field(default_factory=CodeExecSettings)
    twilio: TwilioSettings = Field(default_factory=TwilioSettings)
    idempotency: IdempotencySettings = Field(default_factory=IdempotencySettings)
    billing: BillingSettings = Field(default_factory=BillingSettings)

    def __init__(self, **kwargs: Any):
        # Load YAML config first, then let env vars override. The env-specific
        # file is selected from HELIXIS_ENV — the SAME variable pydantic uses to
        # populate the ``env`` field — and not from kwargs alone. In production
        # ``Settings()`` is constructed with no kwargs (env comes from the
        # environment), so ``kwargs.get('env')`` always fell back to
        # "development" → ``config/development.yaml`` (which doesn't exist) →
        # ``config/production.yaml`` was SILENTLY NEVER LOADED. That left the
        # deployed process on default.yaml + whatever env vars weren't shadowed
        # by a non-empty default.yaml leaf (e.g. CORS origins). See the CORS
        # regression where the onboarding origin was rejected despite being set
        # in both production.yaml and render.yaml.
        env_name = kwargs.get("env") or os.environ.get("HELIXIS_ENV", "development")
        yaml_config = _load_yaml(CONFIG_DIR / "default.yaml")
        env_config = _load_yaml(CONFIG_DIR / f"{env_name}.yaml")
        merged = _deep_merge(yaml_config, env_config)

        # Drop empty placeholder leaves (e.g. ``browserbase_api_key: ""``)
        # before injecting: injected kwargs outrank env vars in
        # pydantic-settings, so an empty YAML value would otherwise shadow
        # the matching HELIXIS_* env var and zero it out. See ``_prune_empty``.
        pruned = _prune_empty(merged)
        merged = {} if pruned is _DROP else pruned

        # Let env vars WIN over non-empty YAML leaves: drop any leaf whose
        # HELIXIS_<PATH> env var is set before injecting (injected kwargs
        # outrank env in pydantic-settings). Without this, a non-empty
        # default.yaml value silently shadows its env var — the CORS /
        # langfuse-host bug class. Same drop-before-inject mechanism as
        # _prune_empty; env_settings then populates the dropped leaves.
        env_keys = {k.upper() for k in os.environ}
        deshadowed = _drop_env_shadowed(merged, env_keys)
        merged = {} if deshadowed is _DROP else deshadowed

        # Merge YAML values as defaults, kwargs (env vars) take precedence
        for key, value in merged.items():
            if key not in kwargs:
                kwargs[key] = value

        super().__init__(**kwargs)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def assert_no_openai_default(settings: Settings) -> None:
    """Hard-fail at startup if the resolved reasoning model is OpenAI.

    Helixis policy: Gemini-only in prod (no OpenAI dependency on the
    LLM hot path). The Pydantic class-level default was changed to
    ``gemini/gemini-2.5-flash`` in PR #69, and ``config/default.yaml``
    already points at the same model under ``llm.models.reasoning``.
    This guard is the loud final check — if either default has been
    edited back to an OpenAI model OR if an env override resolves to
    ``openai/...``, we'd rather refuse to boot than silently call
    OpenAI with no key configured.

    Called from ``main.lifespan`` (API) and ``webhooks.worker.main``
    (worker) so both processes share the policy.
    """
    models = settings.llm.models or {}
    reasoning_block = models.get("reasoning") or {}
    reasoning = reasoning_block.get("model") or settings.llm.default_model
    if reasoning.lower().startswith("openai/"):
        raise RuntimeError(
            f"Reasoning model resolved to {reasoning!r}. "
            "Helixis policy forbids OpenAI in prod. "
            "Set HELIXIS_LLM__MODELS__REASONING__MODEL to a gemini/* "
            "model or restore the Gemini default in src/helixis/settings.py."
        )


def weakened_safety_defaults(settings: Settings) -> list[str]:
    """Safety defaults this config turns off, worded for the boot log.

    Each is allowed (an operator may need it for an incident or a probe), so
    none refuses to boot. Each is also invisible once running, so both
    processes log every entry as a WARNING at startup (``main.lifespan`` and
    ``webhooks.worker.run_worker``). Empty when every default holds.
    """
    found: list[str] = []
    if settings.env == "production" and not settings.idempotency.required:
        found.append(
            "guarded writes are not fail-closed (idempotency.required is false): "
            "if the dedup store is unreachable, a Buildium/Rentvine/Gmail write "
            "runs with no dedup record"
        )
    if settings.codeexec.enabled and not settings.codeexec.e2b_deny_egress:
        found.append(
            "code execution is live with sandbox network egress open "
            "(codeexec.e2b_deny_egress is false): agent-written code can send "
            "the data it was given to any host"
        )
    return found
