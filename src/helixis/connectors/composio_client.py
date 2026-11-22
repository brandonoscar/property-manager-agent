"""Composio integration — managed OAuth connectors for external APIs.

Composio provides 1000+ pre-built API integrations with managed OAuth.
We use it for Gmail, Google Calendar, Google Drive, Buildium API,
Slack, etc.

PR #131 — connection-lifecycle methods added (``initiate_connection``,
``is_connected``, ``list_connections``, ``disconnect``). The Settings
→ Connectors page invokes these so users can click Connect → grant
consent on Google's page (Composio hosts the OAuth redirect) →
return to Helixis to see "Connected".

SDK note: Composio's Python SDK underwent a v1 → v3 rename in 2024-
2025. Method names differ between versions. The wrappers below try
the v3 ``client.connected_accounts.*`` surface first and fall back
to the legacy entity-based v0.5 surface (``client.get_entity(...)``)
so the code works against whichever version is pinned in
``pyproject.toml``. Errors are logged + return safe defaults so the
backend never 500s on a Composio SDK mismatch — the user sees the
connector listed as disconnected and the issue surfaces in Render
logs rather than as a request failure.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from helixis.agents.call_budget import budget_tools
from helixis.connectors.tool_allowlist import filter_tools
from helixis.connectors.tool_gate import fence_tool_errors, gate_tools
from helixis.settings import Settings

logger = logging.getLogger(__name__)

# Per-turn, ``create_orchestrator`` re-fetched ~200 Composio tool schemas
# (Gmail + Calendar + Drive) plus the connection list — synchronous, blocking
# network calls that, on a single worker, froze the event loop and stalled the
# SSE stream (a turn taking ~120s and emitting almost nothing). The schemas for
# a connected toolkit don't change between turns, so we cache them per user and
# bust on connect/disconnect.
#
# Non-empty lists get a long TTL (they're stable); EMPTY lists get a short TTL
# so a toolkit the user just finished connecting is picked up within seconds
# rather than waiting out the full window.
_TOOLS_CACHE_TTL = 300.0  # 5 min — a connected toolkit's tools are stable
_EMPTY_TOOLS_CACHE_TTL = 30.0  # short — re-check soon in case a connect lands
_CONN_CACHE_TTL = 30.0  # connection status flips on connect/disconnect

# Connection statuses that mean a toolkit is usable. Anything else a connection
# reports (expired / dropped / inactive / failed / …) means the OAuth grant
# lapsed and the user must RECONNECT — the orphan incident (#183) left users
# with dropped + expired connections and zero active ones.
_ACTIVE_STATUSES = frozenset({"active", "connected", "ok"})


def _toolkit_slug(account: Any) -> str | None:
    """Extract the toolkit slug from a v3 ConnectedAccount object.

    v3 nests the toolkit under ``account.toolkit.slug`` (or ``.name``).
    Older builds exposed it as a direct string. Returns ``None`` when
    nothing recognisable is present.
    """
    toolkit_attr = getattr(account, "toolkit", None)
    if isinstance(toolkit_attr, str):
        return toolkit_attr
    if toolkit_attr is not None:
        return getattr(toolkit_attr, "slug", None) or getattr(toolkit_attr, "name", None)
    return None


class ComposioClient:
    """Wrapper around the Composio SDK for managed API connectors."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None
        self._initialized = False
        # Caches keyed by user. ``create_orchestrator`` now runs in a worker
        # thread (see run_agent_streaming), so accesses are guarded by a lock.
        # Values store an ABSOLUTE expiry (monotonic seconds), not a timestamp.
        self._tools_cache: dict[tuple[str, tuple[str, ...]], tuple[float, list[Any]]] = {}
        self._conn_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._cache_lock = threading.Lock()

    def invalidate_user(self, user_id: str) -> None:
        """Drop every cached tool list / connection list for ``user_id``.

        Called when the user connects or disconnects a toolkit so the next
        chat turn rebuilds their real surface instead of serving a stale
        snapshot.
        """
        with self._cache_lock:
            self._conn_cache.pop(user_id, None)
            stale = [key for key in self._tools_cache if key[0] == user_id]
            for key in stale:
                self._tools_cache.pop(key, None)

    async def initialize(self) -> None:
        """Initialize the Composio client with API key."""
        api_key = self.settings.composio.api_key
        if not api_key:
            logger.warning("Composio API key not configured — API connectors disabled")
            return

        try:
            from composio import Composio

            # OpenAIAgentsProvider turns Composio tools into the FunctionTool
            # shape the openai-agents SDK consumes. Without it, the agent
            # sees Composio's neutral tool objects and can't actually call
            # them — the orchestrator runs as if Gmail/Calendar/Drive weren't
            # connected at all. Optional so the import failure is graceful
            # in environments that don't have the provider plugin installed.
            provider = None
            try:
                from composio_openai_agents import OpenAIAgentsProvider

                provider = OpenAIAgentsProvider()
            except ImportError:
                logger.warning(
                    "composio-openai-agents not installed — tools won't be "
                    "callable by the openai-agents orchestrator",
                )

            if provider is not None:
                self._client = Composio(api_key=api_key, provider=provider)
            else:
                self._client = Composio(api_key=api_key)
            self._initialized = True
            # Log which SDK surface is available so a future drift between
            # the legacy and v3 packages is visible in Render logs without
            # having to reproduce a 502.
            has_toolkits = hasattr(self._client, "toolkits") and hasattr(
                getattr(self._client, "toolkits", None), "authorize"
            )
            logger.info(
                "Composio client initialized (v3 toolkits.authorize=%s, openai_agents_provider=%s)",
                has_toolkits,
                provider is not None,
            )
        except ImportError:
            logger.warning("composio package not installed — API connectors disabled")
        except Exception as e:
            logger.error("Failed to initialize Composio: %s", e)

    @property
    def available(self) -> bool:
        return self._initialized and self._client is not None

    def get_tools(
        self,
        toolkits: list[str] | None = None,
        user_id: str = "",
    ) -> list[Any]:
        """Get Composio tools for the given toolkits.

        Args:
            toolkits: List of Composio toolkit slugs (e.g., ["GMAIL"]). When
                omitted, derived from this user's connected accounts so the
                agent only sees tools they've actually authorized.
            user_id: Company/user ID for scoped OAuth sessions

        Returns:
            List of FunctionTool objects compatible with the OpenAI Agents
            SDK (when ``composio-openai-agents`` is installed; otherwise
            Composio's neutral shape, which the agent can't call).
        """
        if not self.available:
            logger.info(
                "composio.get_tools(user_id=%s): client unavailable — returning []",
                user_id,
            )
            return []

        # Derive the toolkit list from connected accounts when the caller
        # didn't pin one. Composio v3 expects an explicit list of slugs;
        # passing an empty list returns zero tools, which was the bug —
        # the agent silently had no Gmail/Calendar/Drive surface even
        # after the user connected those accounts.
        slugs = list(toolkits or [])
        active_connections: list[dict[str, Any]] = []
        if not slugs:
            for conn in self.list_connections(user_id):
                toolkit_slug = (conn.get("toolkit") or "").upper()
                status = str(conn.get("status") or "").lower()
                active_connections.append({"toolkit": toolkit_slug, "status": status})
                if toolkit_slug and status in _ACTIVE_STATUSES:
                    slugs.append(toolkit_slug)
            logger.info(
                "composio.get_tools(user_id=%s): derived %d slug(s) from %d "
                "connection(s) — connections=%s",
                user_id,
                len(slugs),
                len(active_connections),
                active_connections,
            )

        if not slugs:
            logger.info(
                "composio.get_tools(user_id=%s): no toolkits resolved — returning []",
                user_id,
            )
            return []

        # v3 expects uppercase slugs (GMAIL, GOOGLECALENDAR, GOOGLEDRIVE).
        # Normalise in case a caller passed lowercase from a different
        # boundary (e.g. our list_connections output is lowercase).
        slugs = [s.upper() for s in slugs]

        # Cache hit? Schemas are stable between turns; this is the change that
        # stops every chat turn re-fetching ~200 tools and freezing the loop.
        cache_key = (user_id, tuple(sorted(slugs)))
        with self._cache_lock:
            cached = self._tools_cache.get(cache_key)
            if cached is not None and time.monotonic() < cached[0]:
                logger.debug(
                    "composio.get_tools(user_id=%s, toolkits=%s): cache hit (%d tool(s))",
                    user_id,
                    slugs,
                    len(cached[1]),
                )
                return cached[1]

        try:
            tools_accessor = getattr(self._client, "tools", None)
            if tools_accessor is None or not hasattr(tools_accessor, "get"):
                logger.warning(
                    "Composio client has no .tools.get — SDK shape unexpected; returning no tools",
                )
                return []
            # ``limit`` defaults to the API's page size (~20) when omitted.
            # Google Calendar / Gmail / Drive each ship 30+ tools per toolkit
            # and the FIND / LIST variants sit past the first page, which
            # produced the "I can create / update / delete events but I
            # can't list them" reply the user hit on calendar. 200 is
            # comfortably above every toolkit's actual count and well
            # below the API's documented max of 1000.
            try:
                tools = tools_accessor.get(user_id=user_id, toolkits=slugs, limit=200)
            except TypeError:
                # Older SDK builds didn't accept ``limit`` on .get().
                # Fall through to the no-limit call rather than 500ing.
                logger.warning(
                    "composio.tools.get does not accept limit — falling back "
                    "to default page size (LIST/FIND variants may be missing)",
                )
                tools = tools_accessor.get(user_id=user_id, toolkits=slugs)
            # The OpenAIAgentsProvider returns a plain list[FunctionTool].
            # Older shapes wrapped it in a collection with .items; handle both.
            if hasattr(tools, "items") and not isinstance(tools, list):
                tools = list(tools.items)
            tools = list(tools or [])
            # ── Allowlist + stable order (the cost chokepoint) ─────────────
            # Unfiltered, drive_agent received GOOGLEDRIVE 90 + GOOGLEDOCS 43 +
            # GOOGLESHEETS 53 = 186 schemas. The cache breakpoint sits on
            # ``tools[-1]``, so that array IS the cached prefix — measured from
            # the production ledger at 166k-215k tokens, 5-7x the ENTIRE
            # 74-tool Buildium set, and near the context ceiling before the
            # specialist reads a single document (gotcha 42's mechanism).
            #
            # ``filter_tools`` also SORTS. Composio returns whatever order the
            # API gives, behind a 300s fetch cache, and a reordered array is a
            # different prefix — so an unstable order would miss the cache on a
            # byte-identical payload and #913's 1h TTL could never be collected.
            # Sorting settles that without needing to know Composio's order.
            #
            # Fails OPEN by design (see tool_allowlist): a cost control must
            # never be able to leave a specialist unable to do its job.
            tools = filter_tools(tools)
            # WARNING level so the "which calendar tools loaded?"
            # diagnostic isn't gated on root log level. Drop back to
            # INFO once the connector layer is stable in production.
            logger.warning(
                "composio.get_tools(user_id=%s, toolkits=%s): %d tool(s) loaded — names=%s",
                user_id,
                slugs,
                len(tools),
                [getattr(t, "name", "?") for t in tools][:50],
            )
            # Write-confirmation gate (chokepoint): wrap every write-classified
            # tool so it requires user approval before executing. Reads pass
            # through unchanged. Every consumer of get_tools inherits this.
            # Per-run memo on the READS (2026-08-27). No Composio tool was
            # budgeted before this: ``_MIRROR_READS``/``_READ_PREFIXES`` are
            # entirely Buildium, so every Gmail/Drive/Calendar read re-executed
            # even when an earlier consult in the SAME run had already fetched
            # it. Because ``.as_tool()`` sub-runs start cold, a turn that
            # consulted gmail_agent four times paid for four inbox fetches —
            # 28 model calls and $0.83 for one question about one email.
            #
            # OUTERMOST on purpose: a memo hit must skip the gate and the
            # fetch, not run them and discard the answer. ``budget_tool``
            # ``copy.copy``s the tool and replaces only ``on_invoke_tool``, so
            # every marker ``gate_tools``/``fence_tool_errors`` set survives —
            # gotcha 34's dropped-attribute trap does not apply to a shallow
            # copy.
            #
            # ⚠ WRITES ARE NEVER MEMOIZED. ``_is_composio_read`` defers to
            # ``is_write_tool``, which fails closed, so a send/update wrapped
            # here is a pass-through. A memo on a write would replay a stored
            # success without executing.
            gated = budget_tools(fence_tool_errors(gate_tools(tools)))
            # Cache it. Non-empty lists are stable (long TTL); an empty list
            # gets a short TTL so a just-connected toolkit is picked up soon.
            ttl = _TOOLS_CACHE_TTL if gated else _EMPTY_TOOLS_CACHE_TTL
            with self._cache_lock:
                self._tools_cache[cache_key] = (time.monotonic() + ttl, gated)
            return gated
        except Exception as e:
            logger.exception(
                "composio.get_tools(user_id=%s, toolkits=%s) failed: %s",
                user_id,
                slugs,
                e,
            )
            return []

    # ── Direct tool execution (email-ingest poll + reply) ─────────────

    def execute_tool(
        self,
        *,
        user_id: str,
        slug: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Execute one Composio tool by slug, OUTSIDE the agent loop.

        The agent path (``get_tools`` → openai-agents FunctionTool) is for
        the LLM to call tools mid-conversation. The email-ingest worker
        needs to call Gmail tools directly — fetch new messages on a poll
        cycle, send a reply once the user confirms — without spinning up
        an agent. This is that path.

        ``slug`` is a Composio tool slug, e.g. ``"GMAIL_FETCH_EMAILS"`` /
        ``"GMAIL_REPLY_TO_THREAD"``. ``arguments`` is the tool's input
        payload. ``user_id`` is the same per-user OAuth scope key the
        agent path uses (the auth user id).

        Returns the tool result as a dict, or ``None`` when Composio is
        unavailable / the SDK call fails — every caller must treat ``None``
        as "couldn't run" and degrade (skip this poll cycle, surface a
        retry), NEVER crash. Mirrors the defensive contract of every other
        method here (the orphan/SDK-drift incidents, #183).
        """
        if not self.available:
            logger.info("composio.execute_tool(%s): client unavailable", slug)
            return None

        # ``available`` guarantees the client was constructed.
        assert self._client is not None
        client = self._client

        tools_accessor = getattr(client, "tools", None)
        if tools_accessor is None or not hasattr(tools_accessor, "execute"):
            logger.warning(
                "composio.execute_tool(%s): client has no .tools.execute — SDK shape unexpected",
                slug,
            )
            return None

        try:
            # v3: ``client.tools.execute(slug, user_id=..., arguments=...)``.
            # ⚠ Composio SDK 0.13.1 REQUIRES a toolkit version for manual
            # execution and rejects the default "latest" with
            # ToolVersionRequiredError — so an unversioned call silently
            # returns None here and the worker fetches 0 emails forever
            # (first observed validating email-ingest, 2026-06-27).
            # ``dangerously_skip_version_check=True`` opts into "use latest"
            # for manual execution, matching this wrapper's best-effort style;
            # it unblocks BOTH the Gmail fetch (poll) and the reply send.
            result = tools_accessor.execute(
                slug,
                user_id=user_id,
                arguments=arguments,
                dangerously_skip_version_check=True,
            )
        except TypeError:
            # Older SDK without the version-check kwarg (predates 0.13.1's
            # requirement). Fall back to the plain unversioned call.
            try:
                result = tools_accessor.execute(slug, user_id=user_id, arguments=arguments)
            except Exception:  # noqa: BLE001
                logger.exception("composio.execute_tool(%s): execute signature mismatch", slug)
                return None
        except Exception:  # noqa: BLE001
            logger.exception("composio.execute_tool(%s) failed", slug)
            return None

        # Normalise to a dict. v3 returns a dict-like; older returns an
        # object with ``.data`` / ``.model_dump``.
        if isinstance(result, dict):
            return result
        for attr in ("data", "model_dump"):
            candidate = getattr(result, attr, None)
            if callable(candidate):
                try:
                    dumped = candidate()
                    if isinstance(dumped, dict):
                        return dumped
                except Exception:  # noqa: BLE001
                    pass
            elif isinstance(candidate, dict):
                return candidate
        logger.warning(
            "composio.execute_tool(%s): result shape unrecognised (%s)",
            slug,
            type(result).__name__,
        )
        return None

    # ── Connection lifecycle (PR #131) ────────────────────────────────

    def initiate_connection(
        self,
        user_id: str,
        toolkit: str,
        callback_url: str | None = None,
    ) -> dict[str, Any] | None:
        """Start an OAuth connection for ``user_id`` against ``toolkit``.

        When ``callback_url`` is supplied, Composio redirects the consent
        popup to that URL after a successful grant — the frontend points
        this at ``/oauth/callback`` and auto-closes the popup so the user
        lands back in Helixis. When omitted, Composio shows its own
        hosted "Connection successful" page (which is what was happening
        before this knob was wired through).

        Returns a dict with at least ``redirect_url`` (the URL the
        frontend opens in a popup / new tab so the user can grant
        consent on the provider's page). ``connection_id`` and
        ``status`` are best-effort — Composio's SDK shape varies by
        version, so the caller should treat absent fields as unknown
        rather than asserting them.

        Returns ``None`` when Composio isn't configured or the SDK
        call fails. The caller (the connect_init endpoint) returns
        503 in that case so the frontend can surface a clean error.
        """
        if not self.available:
            return None

        # The user is (re)connecting — drop any cached snapshot so the toolkit's
        # tools/connection are re-read once the grant lands (within the empty
        # TTL) instead of serving a stale "not connected" result.
        self.invalidate_user(user_id)

        # ``available`` is True only when ``self._client`` was constructed
        # (see the property), so the client is non-None past this point.
        assert self._client is not None
        client = self._client

        # Composio v3 surface. ``toolkits.authorize(user_id, toolkit)`` is the
        # one-liner — but it doesn't accept ``callback_url``, so when one is
        # provided we drop down to ``connected_accounts.initiate(...)`` and
        # resolve the auth_config_id via the same private helper that
        # ``toolkits.authorize`` uses internally. No callback → keep the
        # simple authorize() path.
        toolkits = getattr(client, "toolkits", None)
        if toolkits is not None and hasattr(toolkits, "authorize"):
            try:
                # Composio's SDK result shape varies by version (read via
                # getattr below); declared Any and assigned on every branch.
                result: Any = None
                if callback_url:
                    auth_config_id = toolkits._get_auth_config_id(toolkit=toolkit)
                    logger.warning(
                        "composio.initiate (callback path) user_id=%s toolkit=%s "
                        "auth_config_id=%s callback_url=%s",
                        user_id,
                        toolkit,
                        auth_config_id,
                        callback_url,
                    )
                    # allow_multiple=True so a re-click on Connect always
                    # produces a fresh OAuth flow that honours callback_url.
                    # Without it, Composio short-circuits to a static success
                    # page when an existing connection is present, leaving
                    # the popup stuck on platform.composio.dev.
                    result = client.connected_accounts.initiate(
                        user_id=user_id,
                        auth_config_id=auth_config_id,
                        callback_url=callback_url,
                        allow_multiple=True,
                    )
                else:
                    logger.warning(
                        "composio.initiate (authorize path) user_id=%s toolkit=%s",
                        user_id,
                        toolkit,
                    )
                    result = toolkits.authorize(user_id=user_id, toolkit=toolkit)
                payload = {
                    "redirect_url": getattr(result, "redirect_url", None),
                    "connection_id": getattr(result, "id", None)
                    or getattr(result, "connection_id", None),
                    "status": getattr(result, "status", "pending"),
                }
                logger.warning(
                    "composio.initiate ack user_id=%s toolkit=%s -> "
                    "connection_id=%s status=%s redirect_url_set=%s",
                    user_id,
                    toolkit,
                    payload["connection_id"],
                    payload["status"],
                    bool(payload["redirect_url"]),
                )
                return payload
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Composio toolkits.authorize failed for toolkit=%s — trying session.authorize",
                    toolkit,
                )
        else:
            logger.warning(
                "Composio v3 toolkits.authorize unavailable — SDK is likely the "
                "deprecated composio-core. Pin ``composio>=0.13.0`` instead.",
            )

        # Session-scoped v3 surface — ``client.create(user_id).authorize(toolkit)``.
        # Same wrapper, accessed via the session object on older v3 builds.
        if hasattr(client, "create"):
            try:
                session = client.create(user_id=user_id)
                if hasattr(session, "authorize"):
                    result = session.authorize(toolkit)
                    return {
                        "redirect_url": getattr(result, "redirect_url", None),
                        "connection_id": getattr(result, "id", None)
                        or getattr(result, "connection_id", None),
                        "status": getattr(result, "status", "pending"),
                    }
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Composio session.authorize failed for toolkit=%s — trying legacy",
                    toolkit,
                )

        # Legacy composio-core surface — ``client.get_entity(id).initiate_connection(...)``.
        # Composio retired this endpoint for managed-OAuth flows on 2026-05-08
        # so it only works on (a) old orgs and (b) custom auth_configs. Kept
        # purely as a last-resort path.
        try:
            entity = client.get_entity(id=user_id)
            request = entity.initiate_connection(app_name=toolkit)
            return {
                "redirect_url": getattr(request, "redirect_url", None),
                "connection_id": getattr(request, "connection_id", None),
                "status": "pending",
            }
        except Exception:  # noqa: BLE001
            logger.exception(
                "Composio legacy initiate path also failed for toolkit=%s — no SDK surface worked",
                toolkit,
            )
            return None

    def list_connections(self, user_id: str) -> list[dict[str, Any]]:
        """Return all active connections for ``user_id``.

        Each item is a dict with ``toolkit`` (e.g. "googlecalendar"),
        ``status``, ``connection_id``, ``connected_at``. Shape is
        best-effort; callers that need a specific field should
        ``.get()`` it.
        """
        if not self.available:
            return []

        # ``available`` guarantees the client was constructed (see property).
        assert self._client is not None

        # Cache hit? Connection status drives ``degraded_toolkits`` on every
        # turn; short TTL keeps it fresh while sparing a per-turn round-trip.
        with self._cache_lock:
            cached = self._conn_cache.get(user_id)
            if cached is not None and time.monotonic() < cached[0]:
                return cached[1]

        # Composio v3 surface — ``connected_accounts.list(user_ids=[...])``
        # returns a paginated response object whose items live under ``.items``.
        # The old call ``ca.list(user_id=...)`` silently no-op'd: the kwarg
        # was wrong (plural ``user_ids``) and even if it had matched, the
        # response isn't directly iterable. That's why the Connectors page
        # stayed on Connect after a successful OAuth — the poll loop never
        # saw the new connection.
        try:
            client = self._client
            ca = getattr(client, "connected_accounts", None)
            if ca is not None and hasattr(ca, "list"):
                response = ca.list(user_ids=[user_id])
                items = getattr(response, "items", None)
                if items is None:
                    # Older v3 builds returned a list directly.
                    items = response if isinstance(response, list) else []
                result = [
                    {
                        "toolkit": (
                            _toolkit_slug(c)
                            or getattr(c, "integration_id", None)
                            or getattr(c, "app_name", None)
                        ),
                        "status": getattr(c, "status", None),
                        "connection_id": getattr(c, "id", None)
                        or getattr(c, "connection_id", None),
                        "connected_at": getattr(c, "created_at", None),
                    }
                    for c in items
                ]
                logger.info(
                    "composio.list_connections(user_id=%s): %d connection(s) — %s",
                    user_id,
                    len(result),
                    [{"toolkit": r["toolkit"], "status": r["status"]} for r in result],
                )
                with self._cache_lock:
                    self._conn_cache[user_id] = (time.monotonic() + _CONN_CACHE_TTL, result)
                return result
        except Exception:  # noqa: BLE001
            logger.exception("Composio v3 list_connections failed — trying legacy")

        # Legacy surface.
        try:
            entity = self._client.get_entity(id=user_id)
            items = entity.get_connections() or []
            legacy = [
                {
                    "toolkit": getattr(c, "app_name", None),
                    "status": getattr(c, "status", "active"),
                    "connection_id": getattr(c, "connection_id", None),
                    "connected_at": getattr(c, "created_at", None),
                }
                for c in items
            ]
            with self._cache_lock:
                self._conn_cache[user_id] = (time.monotonic() + _CONN_CACHE_TTL, legacy)
            return legacy
        except Exception:  # noqa: BLE001
            logger.exception("Composio legacy list_connections also failed")
            return []

    def is_connected(self, user_id: str, toolkit: str) -> bool:
        """True when ``user_id`` has an ACTIVE connection for
        ``toolkit``. Convenience wrapper over ``list_connections``."""
        for conn in self.list_connections(user_id):
            if (conn.get("toolkit") or "").lower() != toolkit.lower():
                continue
            status = str(conn.get("status") or "").lower()
            if status in _ACTIVE_STATUSES:
                return True
        return False

    def connection_health(self, user_id: str) -> dict[str, str]:
        """Map toolkit slug (UPPER) → its current connection status (lower).

        Lets callers distinguish 'never connected' (toolkit ABSENT from the
        map) from a lapsed 'expired/dropped' grant (PRESENT but non-active), so
        a specialist can tell the user to RECONNECT rather than connect fresh
        (#183). When a toolkit has several rows (the orphan incident left
        multiple dropped connections per toolkit), an active status wins over a
        non-active one so we never cry "expired" on a healthy connection."""
        health: dict[str, str] = {}
        for conn in self.list_connections(user_id):
            toolkit = (conn.get("toolkit") or "").upper()
            if not toolkit:
                continue
            status = str(conn.get("status") or "").lower()
            if toolkit not in health or status in _ACTIVE_STATUSES:
                health[toolkit] = status
        return health

    def degraded_toolkits(self, user_id: str) -> set[str]:
        """Toolkits (UPPER slugs) the user HAS a connection for that is not
        active — i.e. needs reconnecting. Excludes never-connected toolkits, so
        callers can show a 'reconnect' message only when there's a lapsed grant
        rather than a first-time connect (#183)."""
        return {
            toolkit
            for toolkit, status in self.connection_health(user_id).items()
            if status not in _ACTIVE_STATUSES
        }

    def disconnect(self, user_id: str, toolkit: str) -> bool:
        """Tear down ``user_id``'s connection for ``toolkit``.

        Returns True on success (or when nothing was connected to
        begin with — idempotent), False when the disconnect call
        itself errored.
        """
        if not self.available:
            return False

        # ``available`` guarantees the client was constructed (see property).
        assert self._client is not None

        # Clear any cached snapshot first so the lookup below reads live state,
        # then the toolkit's tools won't be served stale after we revoke it.
        self.invalidate_user(user_id)

        # Find the connection_id for this toolkit.
        target_id: str | None = None
        for conn in self.list_connections(user_id):
            if (conn.get("toolkit") or "").lower() == toolkit.lower():
                target_id = conn.get("connection_id")
                break
        if target_id is None:
            # Nothing connected — treat as success (idempotent).
            return True

        # Composio v3.
        try:
            client = self._client
            ca = getattr(client, "connected_accounts", None)
            if ca is not None and hasattr(ca, "delete"):
                ca.delete(target_id)
                self.invalidate_user(user_id)  # drop the now-revoked toolkit
                return True
        except Exception:  # noqa: BLE001
            logger.exception(
                "Composio v3 disconnect failed for connection_id=%s — trying legacy",
                target_id,
            )

        # Legacy v0.5.
        try:
            entity = self._client.get_entity(id=user_id)
            if hasattr(entity, "disable_connection"):
                entity.disable_connection(target_id)
            elif hasattr(entity, "delete_connection"):
                entity.delete_connection(target_id)
            self.invalidate_user(user_id)  # drop the now-revoked toolkit
            return True
        except Exception:  # noqa: BLE001
            logger.exception(
                "Composio legacy disconnect also failed for connection_id=%s",
                target_id,
            )
            return False

    def get_available_connectors(self) -> list[dict[str, str]]:
        """List connectors available for property management."""
        return [
            {
                "name": "Gmail",
                "toolkit": "gmail",
                "description": "Send, draft, and read emails",
            },
            {
                "name": "Google Calendar",
                "toolkit": "google_calendar",
                "description": "Manage viewings, inspections, follow-ups",
            },
            {
                "name": "Google Drive",
                "toolkit": "google_drive",
                "description": "Pull SOPs + property docs into the agent's context",
            },
            {
                "name": "Slack",
                "toolkit": "slack",
                "description": "Team notifications",
            },
            {
                "name": "Google Sheets",
                "toolkit": "google_sheets",
                "description": "Reporting and data export",
            },
            {
                "name": "Buildium",
                "toolkit": "buildium",
                "description": "Property management platform API",
            },
        ]
