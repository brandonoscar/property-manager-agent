/**
 * Integrations / connectors data layer.
 *
 * Demo mode returns a seeded list where some connectors are pre-connected
 * with plausible account info. Production mode fetches from the backend
 * (/api/v1/connectors/) and the page hydrates Buildium's per-capability
 * connection state from /api/v1/buildium/credentials.
 *
 * Each connector lists its ``capabilities`` — APIs (the agent reaches
 * out to the service) and/or webhooks (the service reaches in). The
 * Settings → Connectors page renders these as two top-level sections.
 */

import { apiFetch } from './api';
import { demoMode } from './auth';

export type ConnectorCategory =
  | 'property_management'
  | 'listings'
  | 'communication'
  | 'calendar';

export type ConnectorCapability = 'api' | 'webhook';

export interface Connector {
  id: string;
  name: string;
  category: ConnectorCategory;
  description: string;
  capabilities: ConnectorCapability[];
  /** Copy shown for the row when rendered under the APIs section. */
  apiDescription?: string;
  /** Copy shown for the row when rendered under the Webhooks section. */
  webhookDescription?: string;
  /** Per-capability connection state. Either or both can be true. */
  apiConnected?: boolean;
  webhookConnected?: boolean;
  /** True when a connection EXISTS but its OAuth grant lapsed (expired /
   *  dropped) — the row shows "Reconnect", not first-time "Connect". */
  needsReauth?: boolean;
  /** True when we could not read the connection state AT ALL — Composio is
   *  unconfigured on this deployment, or the lookup failed. ⚠ This is a fact
   *  about US, not about the customer: their connector may be perfectly
   *  healthy. It used to render as `isConnected: false`, byte-identical to
   *  "you never set this up", so the row offered a Connect button whose route
   *  answers 503. Composio-backed rows only — Buildium's state comes from its
   *  own credentials row and stays knowable. */
  stateUnknown?: boolean;
  accountInfo?: string;
  /** When this connector last synced. ⚠ For the PMS rows ({@link PMS_CONNECTOR_IDS})
   *  ONLY this is a real per-table stamp (`helixis_mirror_sync_state`)
   *  and its ABSENCE means no sync has ever completed — see
   *  {@link pmsLastSyncAt}. Every other connector still gets a page-load
   *  placeholder, so absence means nothing there. */
  lastSyncAt?: number;
  authMethod: 'oauth' | 'api_key' | 'webhook';
}

/** The property-management connectors. A workspace has at most one
 *  (migration 0100), and the backend lists only the one it is on once it has
 *  keys for it. */
export const PMS_CONNECTOR_IDS: readonly string[] = [
  'buildium',
  'rentvine',
  'rentmanager',
  'propertyware',
  'doorloop',
];

export function isPmsConnector(id: string): boolean {
  return PMS_CONNECTOR_IDS.includes(id);
}

/**
 * When this workspace's PMS last finished a sync, or `undefined` if it never
 * has.
 *
 * ONE answer to "has this company's portfolio ever synced", because two
 * screens ask it and they must not disagree: the getting-started checklist
 * ticks "Sync your portfolio" off it, and the properties grid uses it to tell
 * *"your account has no rental properties"* apart from *"we have not looked
 * yet"*. Those two states look identical — an empty table — and need opposite
 * instructions, which is the same distinction the agent's empty-mirror message
 * draws server-side.
 *
 * ⚠ Read the PMS rows specifically. The backend stamps every *other*
 * connected card with a `now()` placeholder, so a generic "does any connector
 * have a lastSyncAt" test answers "synced" for an account that never ran one.
 */
export function pmsLastSyncAt(rows: Connector[]): number | undefined {
  return rows.find((r) => isPmsConnector(r.id) && r.lastSyncAt !== undefined)?.lastSyncAt;
}

/**
 * The PMS row this workspace is on: the one that is connected or has synced,
 * else `undefined` (a fresh workspace, or a list with no PMS row at all).
 */
export function connectedPmsRow(rows: Connector[]): Connector | undefined {
  return rows.find(
    (r) =>
      isPmsConnector(r.id) && (r.apiConnected || r.webhookConnected || r.lastSyncAt !== undefined),
  );
}

export const CATEGORY_LABELS: Record<ConnectorCategory, string> = {
  property_management: 'Property Management',
  listings: 'Listing Platforms',
  communication: 'Communication',
  calendar: 'Calendar & Scheduling',
};

/**
 * Capability mapping per connector id. The backend route currently
 * returns a single ``isConnected`` flag per row; capabilities live
 * client-side because they're a layout concern, not state. Add a new
 * connector here to opt it into the right section(s).
 */
const CAPABILITY_OVERRIDES: Record<
  string,
  {
    capabilities: ConnectorCapability[];
    apiDescription?: string;
    webhookDescription?: string;
  }
> = {
  buildium: {
    capabilities: ['api', 'webhook'],
    apiDescription: 'Leases, work orders, GL, tenants, owners.',
    webhookDescription: 'Tenant payments, work order updates, lease events.',
  },
  // API only. Rentvine's webhook receiver refuses every delivery until its
  // signature scheme is captured, so a webhook row would offer nothing that
  // works; the scheduled sync is how changes arrive.
  rentvine: {
    capabilities: ['api'],
    apiDescription: 'Properties, units, leases, tenants, owners, vendors, work orders.',
  },
  // API only: Occupella has no Propertyware webhook receiver, so the scheduled
  // sync is how changes arrive.
  propertyware: {
    capabilities: ['api'],
    apiDescription:
      'Buildings, units, leases, tenants, owners, vendors, work orders, bills, rent balances, GL accounts.',
  },
  // API only: DoorLoop sends no webhooks anyone has found, and its sync is not
  // built yet, so the row says what connecting does today
  // (design/doorloop-connector.md). The server's description says the same.
  doorloop: {
    capabilities: ['api'],
    apiDescription: "Connects with an API key from DoorLoop's Premium plan.",
  },
  appfolio: {
    capabilities: ['api', 'webhook'],
    apiDescription: 'Applications, screening, lease management.',
    webhookDescription: 'Application + lease event push.',
  },
  yardi: {
    capabilities: ['api'],
  },
  // API only: Rent Manager has no webhooks Occupella reads; the scheduled
  // sync is how changes arrive. What it syncs: `rentmanager/sync.py`.
  rentmanager: {
    capabilities: ['api'],
    apiDescription:
      'Properties, units, leases, tenants, owners, vendors, work orders, bills, balances.',
  },
  zillow: {
    capabilities: ['webhook'],
    webhookDescription: 'Listing publication + inquiry webhooks.',
  },
  apartments: {
    capabilities: ['webhook'],
    webhookDescription: 'Syndicated listing inquiries.',
  },
  realtor: {
    capabilities: ['webhook'],
    webhookDescription: 'Inquiry webhooks for syndicated listings.',
  },
  gmail: {
    capabilities: ['api'],
  },
  google_calendar: {
    capabilities: ['api'],
  },
  google_drive: {
    capabilities: ['api'],
    apiDescription:
      'Read your SOPs, lease docs, and property notes so Occupella can quote them.',
  },
  // Separate OAuth grants, but all three feed the ONE Drive specialist —
  // connecting any subset works, and each contributes only its own tools.
  google_docs: {
    capabilities: ['api'],
    apiDescription: 'Read and draft lease addenda, notices, and SOPs.',
  },
  google_sheets: {
    capabilities: ['api'],
    apiDescription: 'Read rent rolls, owner statements, and tracking sheets.',
  },
};

function withCapabilities(c: {
  id: string;
  authMethod: Connector['authMethod'];
}): Pick<Connector, 'capabilities' | 'apiDescription' | 'webhookDescription'> {
  const override = CAPABILITY_OVERRIDES[c.id];
  if (override) return override;
  // Reasonable fallback for unmapped connectors: oauth / api_key →
  // APIs section; webhook → Webhooks section.
  return {
    capabilities: c.authMethod === 'webhook' ? ['webhook'] : ['api'],
  };
}

function demoSeed(): Connector[] {
  const now = Date.now();
  const m = (mins: number) => now - mins * 60_000;
  const base: Array<Omit<Connector, 'capabilities'> & { capabilities?: undefined }> = [
    {
      id: 'buildium',
      name: 'Buildium',
      category: 'property_management',
      description: 'Leases, work orders, GL, tenants, owners.',
      apiConnected: true,
      webhookConnected: true,
      accountInfo: 'occupella-demo · 14 properties · 49 units',
      lastSyncAt: m(4),
      authMethod: 'api_key',
    },
    {
      id: 'appfolio',
      name: 'AppFolio',
      category: 'property_management',
      description: 'Applications, screening, lease management.',
      apiConnected: true,
      webhookConnected: false,
      accountInfo: 'Westside Residential · 37 units',
      lastSyncAt: m(12),
      authMethod: 'oauth',
    },
    {
      id: 'yardi',
      name: 'Yardi Breeze',
      category: 'property_management',
      description: 'Lease signing and commercial unit tracking.',
      apiConnected: true,
      accountInfo: 'Midtown Mixed-Use · 12 units',
      lastSyncAt: m(38),
      authMethod: 'api_key',
    },
    {
      id: 'rentmanager',
      name: 'Rent Manager',
      category: 'property_management',
      // What its sync fills, as the server's catalog row says
      // (api/routes/connectors.py); it has no tenant portal sync.
      description: 'Properties, units, leases, tenants, owners, vendors, work orders, bills, balances.',
      apiConnected: false,
      authMethod: 'api_key',
    },
    {
      id: 'zillow',
      name: 'Zillow Rental Manager',
      category: 'listings',
      description: 'Listing publication and inquiry webhooks.',
      webhookConnected: true,
      accountInfo: '4 active listings · 12 inquiries / 7d',
      lastSyncAt: m(18),
      authMethod: 'oauth',
    },
    {
      id: 'apartments',
      name: 'Apartments.com',
      category: 'listings',
      description: 'Syndicated listings and lead capture.',
      webhookConnected: false,
      authMethod: 'oauth',
    },
    {
      id: 'realtor',
      name: 'Realtor.com',
      category: 'listings',
      description: 'Listing syndication.',
      webhookConnected: false,
      authMethod: 'oauth',
    },
    {
      id: 'gmail',
      name: 'Gmail',
      category: 'communication',
      description: 'Send and reply to tenant emails from your inbox.',
      apiConnected: true,
      accountInfo: 'brandon@occupella.demo',
      lastSyncAt: m(2),
      authMethod: 'oauth',
    },
    {
      id: 'google_calendar',
      name: 'Google Calendar',
      category: 'calendar',
      description: 'Schedule inspections, walkthroughs, vendor visits.',
      apiConnected: true,
      accountInfo: 'Primary calendar · brandon@occupella.demo',
      lastSyncAt: m(9),
      authMethod: 'oauth',
    },
    {
      id: 'google_drive',
      name: 'Google Drive',
      category: 'calendar',
      description: 'Read your SOPs and property docs so Occupella can quote them.',
      apiConnected: false,
      authMethod: 'oauth',
    },
  ];
  return base.map((c) => ({ ...c, ...withCapabilities(c) }) as Connector);
}

/**
 * Filter a connector list to those that participate in a given capability.
 * Used by the Settings page to render the two top-level sections.
 */
export function rowsByCapability(
  connectors: Connector[],
  capability: ConnectorCapability,
): Connector[] {
  return connectors.filter((c) => c.capabilities.includes(capability));
}

/**
 * The three mutually-exclusive things a connector row can be saying.
 *
 * ONE HOME, two surfaces (the `buildium/money.py::bill_amount` precedent). The
 * Settings list row and the OAuth detail panel each derived these from the
 * same three wire fields, independently, so a fix to one silently missed the
 * other — which is exactly how `stateUnknown` came to exist: `needsReauth` had
 * already been added twice by hand.
 *
 * ⚠ ORDER IS THE CLAIM. `stateUnknown` outranks `needsReauth`, because with
 * the integration unreachable the degraded-toolkit probe never ran and the
 * lapse flag cannot be trusted either way. "Reconnect" is then the one
 * instruction guaranteed not to help, and an instruction that cannot change
 * anything is worse than none (gotchas 102/106).
 *
 * ⚠ Both derived flags are gated on NOT being connected: an active connection
 * always wins, so a working row never wears a warning badge.
 */
export type ConnectorRowState = {
  isConnected: boolean;
  needsReauth: boolean;
  stateUnknown: boolean;
};

export function connectorRowState(
  connector: Connector,
  capability: ConnectorCapability = 'api',
): ConnectorRowState {
  const isConnected =
    capability === 'api'
      ? !!connector.apiConnected
      : !!connector.webhookConnected;
  // Only the API row carries an OAuth grant, so the webhook row can be neither
  // lapsed nor unreadable — it has no Composio state to read.
  const apiRow = capability === 'api' && !isConnected;
  const stateUnknown = apiRow && !!connector.stateUnknown;
  return {
    isConnected,
    needsReauth: apiRow && !!connector.needsReauth && !stateUnknown,
    stateUnknown,
  };
}

/**
 * The connector rows a caller in `companyRole` may see.
 *
 * ⚠ PER-ROW, NOT PER-SECTION — gotcha 85. The whole Connectors section was
 * briefly closed to non-admins, which was an over-fix: the founder asked for
 * the BUILDIUM connection to be admin-only, and Buildium is one row on this
 * page. Rentvine is the same kind of row and its routes carry the same admin
 * floor (`api/routes/rentvine.py::_require_admin`). Everything else is a per-USER OAuth grant — a member connecting their
 * own Gmail, Calendar or Drive is what a member account is FOR, and there is
 * nothing of the company's to leak in doing it.
 *
 * Buildium is different in kind: it is the company's books, its API
 * credentials and its webhook secret. This filter is the EXPLANATION; the
 * enforcement is the admin floor on the credential routes themselves, because
 * a UI gate cannot bind an API (gotcha 67).
 *
 * ⚠ `companyRole` is `null` while the role probe is in flight, so the check is
 * strict equality against "admin" — anything else hides the row. A Buildium
 * row that flashes in and then disappears is the same leak with worse timing.
 */
export function visibleConnectorRows(
  connectors: Connector[],
  companyRole: string | null,
): Connector[] {
  const isAdmin = companyRole === "admin";
  return connectors.filter((c) => isAdmin || !isPmsConnector(c.id));
}

interface RawBackendConnector {
  id: string;
  name: string;
  category: ConnectorCategory;
  description: string;
  isConnected?: boolean;
  needsReauth?: boolean;
  stateUnknown?: boolean;
  accountInfo?: string;
  lastSyncAt?: number;
  authMethod: 'oauth' | 'api_key' | 'webhook';
}

function enrichBackendConnector(c: RawBackendConnector): Connector {
  const caps = withCapabilities(c);
  // The backend's single ``isConnected`` flag is a v1 stub. For v2 we
  // map it to the API capability when present (most non-webhook
  // connectors live there). Buildium's per-capability state is
  // overlaid separately in the page-level fetch.
  const apiConnected = caps.capabilities.includes('api')
    ? Boolean(c.isConnected)
    : false;
  const webhookConnected = caps.capabilities.includes('webhook')
    ? Boolean(c.isConnected)
    : false;
  return {
    id: c.id,
    name: c.name,
    category: c.category,
    description: c.description,
    accountInfo: c.accountInfo,
    lastSyncAt: c.lastSyncAt,
    authMethod: c.authMethod,
    needsReauth: Boolean(c.needsReauth),
    stateUnknown: Boolean(c.stateUnknown),
    apiConnected,
    webhookConnected,
    ...caps,
  };
}

export async function listConnectors(): Promise<Connector[]> {
  if (demoMode) return demoSeed();
  const res = await apiFetch('/api/v1/connectors/');
  const raw = (await res.json()) as RawBackendConnector[];
  return raw.map(enrichBackendConnector);
}

export async function setConnectorConnection(
  connectorId: string,
  connect: boolean,
): Promise<Connector> {
  if (demoMode) {
    await new Promise((r) => setTimeout(r, 600));
    const seed = demoSeed().find((c) => c.id === connectorId);
    if (!seed) throw new Error(`Unknown connector: ${connectorId}`);
    return {
      ...seed,
      apiConnected: connect && seed.capabilities.includes('api'),
      webhookConnected: connect && seed.capabilities.includes('webhook'),
      accountInfo: connect ? seed.accountInfo ?? 'Connected (demo)' : undefined,
      lastSyncAt: connect ? Date.now() : undefined,
    };
  }

  const res = await apiFetch(
    `/api/v1/connectors/${connectorId}/${connect ? 'connect' : 'disconnect'}`,
    { method: 'POST' },
  );
  const raw = (await res.json()) as RawBackendConnector;
  return enrichBackendConnector(raw);
}


// PR #131 — Composio-managed OAuth flow.
//
// Composio-managed connectors (google_calendar, google_drive, gmail,
// slack) hit a different ``connect`` shape than the legacy stub: the
// backend returns a ``redirectUrl`` we open in a popup / new tab so
// the user can grant consent on the provider's page (Composio hosts
// the OAuth redirect). After they consent, the next ``listConnectors``
// poll reflects the new state via Composio's ``connected_accounts.list``.
const COMPOSIO_MANAGED_IDS = new Set<string>([
  'gmail',
  'google_calendar',
  'google_drive',
  'slack',
]);

export function isComposioManaged(connectorId: string): boolean {
  return COMPOSIO_MANAGED_IDS.has(connectorId);
}

export interface ConnectInitResponse {
  connectorId: string;
  redirectUrl: string;
  connectionId?: string | null;
  status?: string;
}

/**
 * Initiate an OAuth connection for a Composio-managed connector.
 * Returns the redirect URL the caller should open in a popup; the
 * actual connection state lands once the user comes back and the
 * next ``listConnectors`` poll runs.
 */
export async function initiateOAuthConnection(
  connectorId: string,
): Promise<ConnectInitResponse> {
  if (demoMode) {
    // Demo mode — pretend the popup worked instantly so the user
    // sees the connector flip to Connected without leaving the page.
    await new Promise((r) => setTimeout(r, 400));
    return {
      connectorId,
      redirectUrl: '',
      status: 'demo_connected',
    };
  }
  // ``callbackUrl`` tells Composio where to redirect the consent popup
  // after the grant succeeds. ``/oauth/callback`` is handled in main.tsx
  // — it auto-closes the popup so the user returns to Occupella instead of
  // stopping at Composio's own success page.
  const callbackUrl = `${window.location.origin}/oauth/callback`;
  const res = await apiFetch(`/api/v1/connectors/${connectorId}/connect`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ callbackUrl }),
  });
  return (await res.json()) as ConnectInitResponse;
}
