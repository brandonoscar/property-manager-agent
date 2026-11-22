"""Connector management endpoints.

GET  /connectors                  — List supported connectors with per-company status.
POST /connectors/{id}/connect     — Mark a connector as connected (stub until OAuth).
POST /connectors/{id}/disconnect  — Mark a connector as disconnected.

The list returned here is the canonical curated set the frontend renders
on Settings → Connectors. We deliberately do NOT gate it on Composio
being configured — the page should always render the full menu so users
see what's possible. ``isConnected`` is False for every entry on the
backend until per-company OAuth state is wired through; the demo path
in ``frontend/src/services/connectors.ts`` keeps showing pre-connected
accounts because that lives behind the demo flag.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from helixis.api.auth import AuthenticatedUser, get_current_user
from helixis.api.rate_limit import connectors_limiter
from helixis.pms.registry import BUILDIUM, RENTVINE
from helixis.settings import get_settings

router = APIRouter(prefix="/connectors", tags=["connectors"])

logger = logging.getLogger(__name__)


# Curated list of supported integrations. Mirrors the demo seed shape in
# frontend/src/services/connectors.ts so the production page looks like
# the demo (sans pre-connected account info, until OAuth lands).
SUPPORTED_CONNECTORS: list[dict[str, Any]] = [
    {
        "id": "buildium",
        "name": "Buildium",
        "category": "property_management",
        "description": "Leases, work orders, GL, tenants, owners.",
        "authMethod": "api_key",
    },
    # The second PMS (ADR 0010). Listed only while the connector is on or the
    # company already holds Rentvine keys, and never beside a Buildium
    # connection: one PMS per workspace (migration 0100). `list_connectors`.
    {
        "id": "rentvine",
        "name": "Rentvine",
        "category": "property_management",
        "description": "Properties, units, leases, tenants, owners, vendors, work orders.",
        "authMethod": "api_key",
    },
    # The third PMS, back with a working connect path (api/routes/rentmanager.py,
    # RentManagerApiDetail.tsx). Listed by the shared card rule below: while
    # its switch is on and the workspace is on no PMS, or while it holds Rent
    # Manager keys. The description is what its sync fills (rentmanager/sync.py).
    {
        "id": "rentmanager",
        "name": "Rent Manager",
        "category": "property_management",
        "description": (
            "Properties, units, leases, tenants, owners, vendors, work orders, bills, balances."
        ),
        "authMethod": "api_key",
    },
    # Listed by the shared card rule, like Rentvine's: while the switch is on
    # and the workspace is on no PMS, or while it holds Propertyware keys.
    {
        "id": "propertyware",
        "name": "Propertyware",
        "category": "property_management",
        "description": (
            "Buildings, units, leases, tenants, owners, vendors, work orders, bills, "
            "rent balances, GL accounts."
        ),
        "authMethod": "api_key",
    },
    # Listed by the same shared card rule (design/doorloop-connector.md). Its
    # connector has no sync yet, so the description says what connecting does
    # today and nothing more (rule 14): slice A1 replaces it with what the
    # sync fills.
    {
        "id": "doorloop",
        "name": "DoorLoop",
        "category": "property_management",
        "description": "Connects with an API key from DoorLoop's Premium plan.",
        "authMethod": "api_key",
    },
    # AppFolio / Yardi / Zillow / Apartments.com / Realtor.com were removed
    # from this catalog 2026-07-14 (founder call): they were coming-soon
    # placeholders with no working connect path, and showing a user
    # integrations that don't work erodes trust. Rent Manager, removed with
    # them, is back with its connector (its row is above). Re-add a row here
    # (plus its CAPABILITY_OVERRIDES entry in frontend services/connectors.ts)
    # when an integration actually ships.
    {
        "id": "gmail",
        "name": "Gmail",
        "category": "communication",
        "description": "Send and reply to tenant emails from your inbox.",
        "authMethod": "oauth",
    },
    {
        "id": "google_calendar",
        "name": "Google Calendar",
        "category": "calendar",
        "description": "Schedule inspections, walkthroughs, vendor visits.",
        "authMethod": "oauth",
    },
    {
        "id": "google_drive",
        "name": "Google Drive",
        "category": "calendar",
        "description": "Pull SOPs and property docs into the agent's context.",
        "authMethod": "oauth",
    },
    # Docs + Sheets are SEPARATE Composio toolkits (separate OAuth grants), but
    # they feed the SAME specialist as Drive — the files live in Drive and
    # "find my SOP" / "read that rent roll" is one user intent. Three agents
    # competing for it would re-create the specialist fan-out that gotchas
    # 25/32/34 took four PRs to bound. Connecting Drive alone still works;
    # each toolkit contributes its tools independently.
    {
        "id": "google_docs",
        "name": "Google Docs",
        "category": "calendar",
        "description": "Read and draft lease addenda, notices, and SOPs.",
        "authMethod": "oauth",
    },
    {
        "id": "google_sheets",
        "name": "Google Sheets",
        "category": "calendar",
        "description": "Read rent rolls, owner statements, and tracking sheets.",
        "authMethod": "oauth",
    },
]


# Composio's canonical toolkit slugs have no underscores
# (``googlecalendar`` not ``google_calendar``). Passing the underscored
# form to Composio's API returns "Failed to fetch app from downstream
# service", which is what was happening before this map was corrected.
# The frontend continues to use the underscored ids — translation
# happens at this boundary only.
_COMPOSIO_TOOLKITS: dict[str, str] = {
    "gmail": "gmail",
    "google_calendar": "googlecalendar",
    "google_drive": "googledrive",
    "google_docs": "googledocs",
    "google_sheets": "googlesheets",
    "slack": "slack",
}


#: How long a proof that a PMS's keys WORKED stays good for.
#:
#: Deliberately generous. The mirror syncs far more often than this, so a
#: healthy connection renews its proof many times over inside the window —
#: seven missed days is a real signal, not a blip. Erring short here would
#: tell a working account to reconnect, which is gotcha 55's failure pointed
#: at the connectors page: converting an over-claim into a false alarm is not
#: an improvement, it just moves the lie.
_PMS_PROOF_WINDOW = timedelta(days=7)


def pms_connection_state(
    *,
    configured: bool,
    last_tested_at: datetime | None,
    last_sync_at: datetime | None,
    now: datetime | None = None,
) -> tuple[bool, bool]:
    """``(is_connected, needs_reauth)`` for a PMS card, Buildium or Rentvine.

    Both keep a per-company credentials row with a ``last_tested_at`` that
    only a successful test sets and a key change clears, and both stamp
    ``helixis_mirror_sync_state`` only after a fetch that returned data, so
    one rule serves both cards.

    ⚠ **"Connected" used to mean "a credentials row exists."** The variable
    was even named ``buildium_configured`` and was assigned straight to
    ``is_connected`` — so the card read Connected when the keys were wrong,
    revoked, expired, or undecryptable after a data-key rotation. Storing a
    secret is not the same as it working, exactly as a 2xx is not the same as
    a write landing (gotcha 70), and ``configured`` is not ``enabled``
    (gotcha 67). Same mistake, third surface.

    Connected now means **proven to have worked recently**, which is what a
    person reads the word to mean. There are two independent proofs and either
    will do:

    * ``last_tested_at`` — set to ``NOW()`` only on a SUCCESSFUL credential
      test, and NULLed whenever the keys are replaced;
    * the newest mirror sync — ``record_sync`` runs only after a fetch that
      actually returned data.

    Both are evidence by EFFECT rather than by assertion, and neither costs an
    API call on page load.

    Returns ``needs_reauth`` for the has-keys-but-unproven case, which the
    settings card already renders as **Reconnect** — the right affordance both
    for keys that stopped working and for keys never confirmed to work.
    """
    if not configured:
        # No keys is not a reauth prompt; it is the plain unconnected state.
        return False, False

    now = now or datetime.now(UTC)
    proofs = [t for t in (last_tested_at, last_sync_at) if t is not None]
    # ⚠ A naive timestamp compared against an aware one raises, and this runs
    # on a page load that must never 500. Anything without a timezone is read
    # as UTC rather than discarded — dropping it would flag a working account.
    fresh = False
    for stamp in proofs:
        aware = stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)
        if now - aware <= _PMS_PROOF_WINDOW:
            fresh = True
            break
    return fresh, not fresh


def _connector_payload(
    spec: dict[str, Any],
    *,
    connected: bool,
    needs_reauth: bool = False,
    state_unknown: bool = False,
) -> dict[str, Any]:
    """Materialise a connector spec into the frontend Connector shape.

    ``needs_reauth`` marks a connector that HAS a Composio connection whose
    OAuth grant has lapsed (expired / dropped / inactive) — distinct from
    never-connected. The frontend renders a "Reconnect" affordance for it
    instead of a first-time "Connect", so a user whose Google grant died
    (the #183 orphan class, or the 7-day testing-mode revoke) sees why the
    integration went quiet instead of a card that looks like they never set
    it up.

    ``state_unknown`` is the THIRD state and it is about US, not the customer:
    we could not read their connection status at all — Composio is unconfigured
    on this deployment, or ``list_connections`` raised. Both previously rendered
    as ``isConnected=False``, i.e. byte-identical to "you never set this up",
    which is the same false story ``needs_reauth`` exists to stop one step
    earlier. The card then offers a Connect button whose route answers 503.

    ⚠ Composio-backed rows only. Buildium's state is read from a per-company
    credentials row, so a Composio gap tells us nothing about it and marking it
    unknown would hide a fact we actually hold (gotchas 55/57 — decline to
    assert, but only about the thing you genuinely cannot see).
    """
    payload: dict[str, Any] = {
        "id": spec["id"],
        "name": spec["name"],
        "category": spec["category"],
        "description": spec["description"],
        "authMethod": spec["authMethod"],
        "isConnected": connected,
        "needsReauth": needs_reauth,
        "stateUnknown": state_unknown,
    }
    if connected:
        # Per-company connection metadata isn't persisted yet, so fill in
        # a generic placeholder. Real account info will come from a
        # company_connectors table once OAuth is wired.
        payload["accountInfo"] = "Connected"
        payload["lastSyncAt"] = int(time.time() * 1000)
    return payload


def _find_connector(connector_id: str) -> dict[str, Any]:
    for spec in SUPPORTED_CONNECTORS:
        if spec["id"] == connector_id:
            return spec
    raise HTTPException(status_code=404, detail=f"Unknown connector: {connector_id}")


class ConnectInitRequest(BaseModel):
    """Optional body for the OAuth init endpoint.

    ``callbackUrl`` lets the frontend tell Composio where to redirect
    the consent popup after a successful grant — we point it at
    ``/oauth/callback``, which auto-closes the popup. Optional: when
    omitted, Composio shows its own hosted success page.
    """

    model_config = ConfigDict(populate_by_name=True)

    callback_url: str | None = Field(default=None, alias="callbackUrl")


def _require_company(user: AuthenticatedUser) -> str:
    """Pick the user's primary company. Non-Composio connectors
    (Buildium, webhook-only services) stay company-scoped — those
    are workspace-shared by design."""
    if not user.company_ids:
        raise HTTPException(
            status_code=400,
            detail="User is not a member of any company",
        )
    return user.company_ids[0]


def _composio_user_id(user: AuthenticatedUser) -> str:
    """Identity to send to Composio as ``user_id``.

    Composio-managed connectors (Gmail, Google Calendar, Google
    Drive) are PERSONAL — they connect a specific human's Google
    account. The original design routed Composio's ``user_id`` to
    our ``company_id`` so the whole workspace shared one Gmail
    inbox; in practice that broke as soon as more than one person
    in a workspace tried to read "my" mail or "my" calendar, and it
    made the connect/get_tools paths look broken when the OAuth
    state straddled identity scopes.

    Use the auth user's id instead so each user's Google connection
    stays theirs. Workspace-shared connectors (Buildium) keep the
    company-scoped helper above.
    """
    return user.user_id


async def _other_pms(company_id: str) -> str | None:
    """The system this workspace is on when it is neither Buildium nor
    Rentvine (Rent Manager, Propertyware), else ``None``.

    One ``company_pms`` read. ⚠ A failed read answers ``None``, which shows the
    cards every workspace saw before a third system existed, and never fails
    the page. A connect the workspace should not make is still refused by the
    trigger, with a readable 409 (``company_pms.one_pms_refusal``).
    """
    from helixis.db.company_pms import company_pms
    from helixis.db.engine import get_pool

    try:
        on = await company_pms(get_pool(), company_id)
    except Exception:  # noqa: BLE001 — never fail the page load
        logger.debug("pms lookup failed; showing the Buildium card", exc_info=True)
        return None
    return None if on in (BUILDIUM, RENTVINE) else on


async def _last_tested(key: str, company_id: str) -> datetime | None:
    """When this workspace's keys for ``key`` last passed a test, from that
    system's credentials table (the registry names it; never a caller's
    string). Best-effort: a failed read is ``None``, which leaves only the
    sync stamps as proof, and never fails the page."""
    from helixis.db.engine import get_pool
    from helixis.pms import registry

    found = registry.spec(key)
    if found is None:
        return None
    try:
        value = await get_pool().fetchval(
            f"SELECT last_tested_at FROM public.{found.credentials_table} "  # noqa: S608
            "WHERE company_id = $1::uuid",
            company_id,
        )
    except Exception:  # noqa: BLE001 — never fail the page load
        logger.debug("%s credentials lookup failed; no test proof", key, exc_info=True)
        return None
    return value if isinstance(value, datetime) else None


@router.get("/")
async def list_connectors(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[dict[str, Any]]:
    """List all supported connectors as a flat array.

    The frontend's ``listConnectors()`` types the response as
    ``Connector[]`` and renders it directly — returning an array here
    avoids an unwrap step on the client.

    PR #131 — populates ``isConnected`` from Composio for the
    Composio-managed toolkits (gmail / google_calendar /
    google_drive / slack). Buildium and the other api_key/webhook
    connectors stay disconnected at this layer; their per-capability
    state lives behind ``/buildium/credentials`` etc. The Composio
    list call is best-effort: if it fails (no API key, network
    blip, SDK mismatch) we log + show everything as disconnected
    rather than 500ing the page load.
    """
    await connectors_limiter.check_by_user(request, user)

    # Best-effort Composio status lookup. ``connected_toolkits`` is a
    # set so the per-row check is O(1).
    composio = getattr(request.app.state, "composio_client", None)
    connected_toolkits: set[str] = set()
    # Toolkits that HAVE a connection but in a non-active status (expired /
    # dropped / inactive / failed) — the grant lapsed and the user must
    # re-OAuth. Kept separate from ``connected_toolkits`` so the row can say
    # "Reconnect", not "Connect".
    lapsed_toolkits: set[str] = set()
    # True when we could not determine the customer's connection state AT ALL —
    # either Composio is unconfigured here or the lookup failed. Distinct from
    # "not connected", which is a fact about them (see _connector_payload).
    connection_state_unknown = composio is None or not composio.available
    if composio is not None and composio.available:
        try:
            composio_user_id = _composio_user_id(user)
            for conn in composio.list_connections(composio_user_id):
                toolkit = (conn.get("toolkit") or "").lower()
                status = str(conn.get("status") or "").lower()
                if not toolkit:
                    continue
                if status in {"active", "connected", "ok"}:
                    connected_toolkits.add(toolkit)
                else:
                    lapsed_toolkits.add(toolkit)
        except Exception:  # noqa: BLE001 — never fail the page load
            import logging

            logging.getLogger(__name__).exception(
                "composio list_connections failed; connection state is UNKNOWN",
            )
            # ⚠ NOT "disconnected". The call failing tells us nothing about
            # what the customer has connected, and saying "not connected" here
            # is a claim we cannot support.
            connection_state_unknown = True

    # Buildium is a manual api_key connector (no Composio toolkit), so its
    # connected state lives in the per-company credentials row, not in
    # ``connected_toolkits``. Reflect it here so the connector card matches the
    # Buildium detail panel (which reads the same row) instead of always
    # showing "Connect" (#7 F4b). Best-effort: a DB blip leaves it disconnected
    # rather than 500ing the page.
    buildium_configured = False
    buildium_last_tested: datetime | None = None
    company_id = user.company_ids[0] if user.company_ids else None
    if company_id:
        try:
            from helixis.buildium.credentials import fetch_buildium_row

            _row = await fetch_buildium_row(company_id)
            buildium_configured = _row is not None
            if _row is not None:
                buildium_last_tested = _row.get("last_tested_at")
        except Exception:  # noqa: BLE001 — never fail the page load
            import logging

            logging.getLogger(__name__).debug(
                "buildium credentials lookup failed; showing disconnected", exc_info=True
            )

    # Rentvine: the same kind of row, read the same way (ADR 0010). Its routes
    # already existed; until this row did, a Rentvine workspace could connect
    # only in the onboarding wizard and had no screen to replace a key on,
    # while chat sent people to this page to do it (rentvine_tools.py).
    rentvine_configured = False
    rentvine_last_tested: datetime | None = None
    if company_id:
        try:
            from helixis.rentvine.credentials import fetch_rentvine_row

            _rv = await fetch_rentvine_row(company_id)
            rentvine_configured = _rv is not None
            if _rv is not None:
                rentvine_last_tested = _rv.get("last_tested_at")
        except Exception:  # noqa: BLE001 — never fail the page load
            logger.debug("rentvine credentials lookup failed; showing disconnected", exc_info=True)

    # A workspace on a third system (Rent Manager, Propertyware). Asked only
    # when neither Buildium's nor Rentvine's keys are held: the trigger makes
    # the systems exclusive, so a Buildium or Rentvine workspace costs no extra
    # query.
    other_pms: str | None = None
    if company_id and not (buildium_configured or rentvine_configured):
        other_pms = await _other_pms(company_id)
    on_another_pms = other_pms is not None

    # One PMS per workspace (the trigger from migration 0100, four-way since
    # 0107), so the card for a system a workspace is NOT on could only offer a
    # connect that trigger refuses. The Rentvine card also needs the connector
    # on, or keys already stored: those must stay visible to be disconnected
    # (gotcha 120).
    show_buildium = buildium_configured or not (rentvine_configured or on_another_pms)
    show_rentvine = rentvine_configured or (
        bool(get_settings().rentvine.enabled) and not buildium_configured and not on_another_pms
    )

    # Real PMS freshness (migration 0057): the newest per-table sync
    # timestamp replaces the "now()" placeholder so the card's "last sync"
    # is a fact, not a page-load time. Best-effort — placeholder on any miss.
    # One table serves either PMS: a workspace has one, and its disconnect
    # purges these stamps with the rest of the mirror (buildium/purge.py).
    pms_last_sync_ms: int | None = None
    # Kept as a datetime too: the card shows the millis, and
    # `pms_connection_state` needs the same value as PROOF that the
    # stored keys actually work.
    pms_newest_sync: datetime | None = None
    if (buildium_configured or rentvine_configured or on_another_pms) and company_id:
        try:
            from helixis.db.engine import get_pool
            from helixis.db.repositories.mirror_sync_state import get_freshness

            freshness = await get_freshness(get_pool(), company_id)
            if freshness:
                pms_newest_sync = max(freshness.values())
                pms_last_sync_ms = int(pms_newest_sync.timestamp() * 1000)
        except Exception:  # noqa: BLE001 — never fail the page load
            pass

    pms_rows = {
        "buildium": (show_buildium, buildium_configured, buildium_last_tested),
        "rentvine": (show_rentvine, rentvine_configured, rentvine_last_tested),
    }
    # The connectors built on helixis.pms (Rent Manager, Propertyware), by the
    # same rules as Rentvine's card: listed while the system's switch is on
    # and the workspace is on no PMS yet, or while it holds that system's
    # keys (which must stay visible to be disconnected, gotcha 120). Each
    # connector adds its own catalog row with its panel
    # (test_every_pms_row_the_server_lists_opens_its_own_panel pins the two
    # together); until then nothing here is listed.
    from helixis.pms import registry, sync_cycle

    no_pms = not (buildium_configured or rentvine_configured or on_another_pms)
    for key in sync_cycle.systems():
        configured = other_pms == key
        last_tested = await _last_tested(key, company_id) if configured and company_id else None
        pms_rows[key] = (configured or (registry.enabled(key) and no_pms), configured, last_tested)
    out: list[dict[str, Any]] = []
    for spec in SUPPORTED_CONNECTORS:
        pms = pms_rows.get(spec["id"])
        if pms is not None and not pms[0]:
            continue
        toolkit = _COMPOSIO_TOOLKITS.get(spec["id"])
        is_connected = bool(toolkit and toolkit.lower() in connected_toolkits)
        # A grant is "lapsed" only when it exists-but-inactive AND isn't
        # currently active (an active connection always wins).
        needs_reauth = bool(toolkit and toolkit.lower() in lapsed_toolkits and not is_connected)
        if pms is not None:
            is_connected, needs_reauth = pms_connection_state(
                configured=pms[1],
                last_tested_at=pms[2],
                last_sync_at=pms_newest_sync,
            )
        payload = _connector_payload(
            spec,
            connected=is_connected,
            needs_reauth=needs_reauth,
            # Composio-backed rows only: a PMS's state comes from its own
            # credentials row and stays knowable through a Composio outage.
            state_unknown=bool(toolkit) and connection_state_unknown,
        )
        if pms is not None:
            # ⚠ REPLACE, including with absence. `_connector_payload` stamps
            # every connected card with `now()` as a placeholder, so a Buildium
            # account that has NEVER been synced reported "last sync: just now"
            # — a fabricated timestamp for an event that did not happen, on the
            # one connector where the real value is available. Worse, it is now
            # load-bearing: the getting-started checklist and the properties
            # empty state read this field to tell "you have no properties" apart
            # from "we have not looked yet", and a placeholder answers "we
            # looked" for everybody. The other connectors keep the placeholder
            # (no per-company OAuth sync record exists to replace it with yet).
            payload.pop("lastSyncAt", None)
            if pms_last_sync_ms is not None:
                payload["lastSyncAt"] = pms_last_sync_ms
        out.append(payload)

    # Instant-on-connect Drive index (#67 PR-2). This GET is the post-OAuth
    # signal — the frontend polls it after the consent popup closes. If Google
    # Drive is now active for this user and we have no index yet, kick a
    # background build so the company profile learns their Drive within seconds
    # instead of waiting for the daily cycle. Fire-and-forget + fully guarded:
    # it must never block or fail the page load.
    if "googledrive" in connected_toolkits and company_id:
        settings = getattr(request.app.state, "settings", None)
        if (
            settings is not None
            and getattr(settings.company_knowledge, "drive_source", False)
            and composio is not None
            and composio.available
        ):
            asyncio.create_task(
                _kick_drive_index_on_connect(
                    settings=settings,
                    composio=composio,
                    company_id=company_id,
                    composio_user_id=_composio_user_id(user),
                )
            )
    return out


@router.post("/{connector_id}/connect")
async def connect_connector(
    connector_id: str,
    request: Request,
    body: ConnectInitRequest = Body(default_factory=ConnectInitRequest),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Initiate the OAuth flow for a Composio-managed connector.

    PR #131 — was a stub that just returned ``isConnected: true``.
    Now, for Composio-managed connectors (gmail / google_calendar /
    google_drive / slack), returns the OAuth ``redirectUrl`` the
    frontend opens in a new tab. The user grants consent on the
    provider's page (Composio hosts the redirect), then returns to
    Helixis; the next ``GET /connectors/`` reflects the new state.

    For non-Composio connectors (Buildium with its api_key flow,
    webhook-only connectors like Zillow) this returns 400 — those
    use their own dedicated endpoints.
    """
    await connectors_limiter.check_by_user(request, user)
    _find_connector(connector_id)  # raises 404 if unknown

    toolkit = _COMPOSIO_TOOLKITS.get(connector_id)
    if toolkit is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Connector {connector_id!r} is not Composio-managed. "
                "Use its dedicated connect endpoint instead."
            ),
        )

    composio = getattr(request.app.state, "composio_client", None)
    if composio is None or not composio.available:
        raise HTTPException(
            status_code=503,
            detail=(
                "Composio is not configured on this deployment. "
                "Set HELIXIS_COMPOSIO__API_KEY to enable Google connectors."
            ),
        )

    composio_user_id = _composio_user_id(user)
    result = composio.initiate_connection(
        user_id=composio_user_id,
        toolkit=toolkit,
        callback_url=body.callback_url,
    )
    if result is None or not result.get("redirect_url"):
        raise HTTPException(
            status_code=502,
            detail="Composio did not return a redirect URL for this toolkit.",
        )

    return {
        "connectorId": connector_id,
        "redirectUrl": result["redirect_url"],
        "connectionId": result.get("connection_id"),
        "status": result.get("status", "pending"),
    }


@router.post("/{connector_id}/disconnect")
async def disconnect_connector(
    connector_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Tear down a Composio-managed connector's OAuth state.

    PR #131 — was a stub. Now actually invokes Composio's
    ``connected_accounts.delete`` so the user's grant is revoked
    server-side. Idempotent: a disconnect on a not-currently-
    connected toolkit returns success.
    """
    await connectors_limiter.check_by_user(request, user)
    spec = _find_connector(connector_id)

    toolkit = _COMPOSIO_TOOLKITS.get(connector_id)
    if toolkit is None:
        # Non-Composio connector — return the placeholder shape so
        # the frontend's existing path (which calls this URL for
        # every connector type) still works. The actual disconnect
        # for raw connectors happens through their own endpoints.
        return _connector_payload(spec, connected=False)

    composio = getattr(request.app.state, "composio_client", None)
    if composio is None or not composio.available:
        # Treat as success — nothing to disconnect if Composio isn't
        # even configured.
        return _connector_payload(spec, connected=False)

    composio_user_id = _composio_user_id(user)
    ok = composio.disconnect(user_id=composio_user_id, toolkit=toolkit)
    if not ok:
        raise HTTPException(
            status_code=502,
            detail="Composio disconnect failed; check Render logs for the SDK error.",
        )
    return _connector_payload(spec, connected=False)


@router.get("/debug/composio")
async def debug_composio_state(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Dump the raw Composio state for the authenticated user.

    Diagnostic endpoint — exists to break the "UI says Connected but
    tool execution says No connected account found" deadlock. Returns
    the literal ``connected_accounts.list`` payload so we can see
    every field Composio holds (status, auth_config_id, toolkit slug,
    created_at) for each connection, instead of reading the digested
    summary the connectors list returns.

    ⚠ ADMIN ONLY. This docstring used to say it "lives behind auth so it
    never leaks one user's connection state to another" — which was false,
    and falsely reassuring. The probe below deliberately queries BOTH the
    caller's user identity AND `company_ids[0]`, and the signup trigger makes
    every auto-provisioned company id equal to a user id, so authentication
    alone let any member dump the connection metadata (status, auth_config_id,
    toolkit, timestamps) held under the company identity. The route is mounted
    unconditionally in production with no env gate, so "diagnostic" was not a
    limit on who could reach it.

    It is intentionally permissive about FORMAT — this is for an operator to
    diagnose, not for the frontend to consume.
    """
    import logging as _logging

    from helixis.api.roles import ADMIN
    from helixis.api.routes.company import require_role
    from helixis.db.engine import get_pool

    log = _logging.getLogger(__name__)

    # Fail closed on an unreadable role, like every other floor in this
    # product: a diagnostic that dumps connection state is not the place to
    # decide that an unknown caller is probably fine.
    if user.company_ids:
        await require_role(
            get_pool(),
            user.user_id,
            user.company_ids[0],
            ADMIN,
            detail="Only admins can read connector diagnostics.",
        )
    composio = getattr(request.app.state, "composio_client", None)
    if composio is None or not composio.available:
        return {
            "available": False,
            "reason": "composio_client not initialized or HELIXIS_COMPOSIO__API_KEY unset",
        }

    composio_user_id = _composio_user_id(user)
    company_id = user.company_ids[0] if user.company_ids else None

    # Probe both identities so we can see if connections accidentally
    # got stored under company_id instead of user_id.
    out: dict[str, Any] = {
        "available": True,
        "auth_user_id": composio_user_id,
        "company_id": company_id,
        "connections_by_user_id": [],
        "connections_by_company_id": [],
        "raw_user_id_response": None,
        "raw_company_id_response": None,
    }

    try:
        client = composio._client  # noqa: SLF001 — diagnostic only
        ca = getattr(client, "connected_accounts", None)
        if ca is not None and hasattr(ca, "list"):
            try:
                resp = ca.list(user_ids=[composio_user_id])
                items = list(getattr(resp, "items", None) or [])
                out["raw_user_id_response"] = [
                    {
                        attr: _safe_attr(c, attr)
                        for attr in (
                            "id",
                            "user_id",
                            "status",
                            "toolkit",
                            "auth_config",
                            "auth_config_id",
                            "created_at",
                            "updated_at",
                        )
                    }
                    for c in items
                ]
            except Exception as e:  # noqa: BLE001
                out["raw_user_id_response"] = f"ERROR: {e}"

            if company_id:
                try:
                    resp = ca.list(user_ids=[company_id])
                    items = list(getattr(resp, "items", None) or [])
                    out["raw_company_id_response"] = [
                        {
                            attr: _safe_attr(c, attr)
                            for attr in (
                                "id",
                                "user_id",
                                "status",
                                "toolkit",
                                "auth_config",
                                "auth_config_id",
                                "created_at",
                                "updated_at",
                            )
                        }
                        for c in items
                    ]
                except Exception as e:  # noqa: BLE001
                    out["raw_company_id_response"] = f"ERROR: {e}"

        out["connections_by_user_id"] = composio.list_connections(composio_user_id)
        if company_id:
            out["connections_by_company_id"] = composio.list_connections(company_id)
    except Exception as e:  # noqa: BLE001
        log.exception("debug_composio_state failed")
        out["error"] = str(e)

    # Log hygiene: this ran at WARNING and dumped the FULL connection payload
    # (ids, auth_config_ids, statuses, toolkits) into the log stream on every
    # call. Downgrade to debug and log only counts — the caller still gets the
    # full ``out`` in the response (auth'd + self-scoped); the log doesn't need
    # the metadata.
    log.debug(
        "composio debug probed for user=%s (by_user=%d, by_company=%d)",
        composio_user_id,
        len(out.get("connections_by_user_id") or []),
        len(out.get("connections_by_company_id") or []),
    )
    return out


def _safe_attr(obj: Any, name: str) -> Any:
    """Pull an attribute as a serialisable value (str/int/None/list/dict)."""
    val = getattr(obj, name, None)
    if val is None or isinstance(val, (str, int, float, bool, list, dict)):
        return val
    # Pydantic-style nested objects (e.g. ``toolkit`` is a sub-model
    # carrying ``slug``). Try ``.slug`` then fall back to repr().
    slug = getattr(val, "slug", None)
    if slug is not None:
        return slug
    return repr(val)


async def _kick_drive_index_on_connect(
    *,
    settings: Any,
    composio: Any,
    company_id: str,
    composio_user_id: str,
) -> None:
    """Build this user's Drive index the moment we first see Drive connected.

    Scheduled fire-and-forget from ``list_connectors`` (#67 PR-2). Only runs on
    the FIRST connect — once the index has rows, the daily company-knowledge
    cycle keeps it fresh, so repeated connectors-page loads don't re-enumerate.
    Fully guarded: a background task must never raise unretrieved, so every
    failure degrades to a logged no-op.
    """
    try:
        from helixis.company_knowledge.drive import refresh_drive_index_for_user
        from helixis.db.engine import get_pool
        from helixis.db.repositories import drive_index as drive_repo

        pool = get_pool()
        # First-connect guard — skip if we already have an index for this user.
        existing = await drive_repo.count_for_user(
            pool, company_id=company_id, composio_user_id=composio_user_id
        )
        if existing:
            return
        indexed = await refresh_drive_index_for_user(
            pool,
            settings,
            company_id=company_id,
            composio_user_id=composio_user_id,
            composio=composio,
        )
        if indexed:
            logger.info(
                "drive index: on-connect build indexed %d file(s) for user=%s",
                indexed,
                composio_user_id,
            )
    except (Exception, SystemExit):  # noqa: BLE001 — background best-effort
        logger.warning("drive index: on-connect kick failed", exc_info=True)
