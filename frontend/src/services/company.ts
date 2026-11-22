/**
 * Company / workspace API client — team roster, roles, and invites
 * (Settings → Team & Permissions).
 *
 * Roles are the reconciled ADR 0005 set: **Admin / Manager / Member** (no
 * "Owner" — that word is reserved for rental Owners in Buildium). The backend
 * `require_role` is the primary gate; this client just surfaces the API. In
 * demo mode the reads return a static roster and writes are no-ops, so the
 * screen is browsable without a backend.
 */

import { apiFetch } from './api';
import { demoMode } from './auth';

/** The reconciled team roles (ADR 0005), highest privilege first. */
export type TeamRole = 'admin' | 'manager' | 'member';

export const TEAM_ROLES: { value: TeamRole; label: string; blurb: string }[] = [
  { value: 'admin', label: 'Admin', blurb: 'Full access — manage the team, connectors, and every setting.' },
  { value: 'manager', label: 'Manager', blurb: 'Runs day-to-day work and can see the team, but not change roles.' },
  { value: 'member', label: 'Member', blurb: 'Uses Occupella day-to-day; company settings stay hidden.' },
];

export const ROLE_LABEL: Record<TeamRole, string> = {
  admin: 'Admin',
  manager: 'Manager',
  member: 'Member',
};

export interface Member {
  user_id: string;
  email: string | null;
  display_name: string | null;
  role: TeamRole;
  is_primary_admin: boolean;
  is_self: boolean;
  /**
   * Sent an invitation and has never signed in.
   *
   * ⚠ The membership row is created UP FRONT at invite time, so before this
   * existed an invitee who never opened the email was an ordinary-looking row
   * — indistinguishable from a colleague who set up weeks ago. An admin could
   * not answer "did Sam ever get in?" from the product, and the seat is billed
   * from the invite either way.
   *
   * Optional so an older payload without it reads as not-pending rather than
   * marking the whole team as waiting.
   */
  invite_pending?: boolean;
}

/**
 * An invite to someone who already has an account, waiting for them to accept it
 * (migration 0112). NOT a member: no seat, no access. Listed apart from `Member`
 * so nothing reading the roster can mistake one for the other.
 */
export interface PendingInvite {
  id: string;
  email: string | null;
  role: TeamRole;
  invited_at: string;
  expires_at: string;
}

/** An invite waiting for the signed-in user: who asked them to join what. */
export interface MyInvite {
  id: string;
  company_id: string;
  company_name: string;
  role: TeamRole;
  invited_by_email: string | null;
  expires_at: string;
}

export interface MyRole {
  company_id: string;
  role: TeamRole;
  is_primary_admin: boolean;
}

/** The standing company-knowledge profile (ADR 0006 + #67).
 *  `content_md` is the auto-learned daily profile (background job; empty until
 *  it has produced something). Occupella learns the company — there is no
 *  hand-typed layer. Read-only. */
export interface CompanyKnowledge {
  company_id: string;
  content_md: string;
  generated_on: string | null;
  updated_at: string | null;
}

// ── demo fixtures ────────────────────────────────────────────────────

const DEMO_MEMBERS: Member[] = [
  {
    user_id: 'demo-you',
    email: 'you@acme-property.com',
    display_name: 'You',
    role: 'admin',
    is_primary_admin: true,
    is_self: true,
  },
  {
    user_id: 'demo-tara',
    email: 'tara.lee@acme-property.com',
    display_name: 'Tara Lee',
    role: 'manager',
    is_primary_admin: false,
    is_self: false,
  },
  {
    user_id: 'demo-marcus',
    email: 'marcus@acme-property.com',
    display_name: 'Marcus Byrd',
    role: 'member',
    is_primary_admin: false,
    is_self: false,
  },
  {
    // The state the demo could not previously show, and the one an admin most
    // needs to see: invited, never signed in, already taking a seat.
    user_id: 'demo-priya',
    email: 'priya.n@acme-property.com',
    display_name: null,
    role: 'member',
    is_primary_admin: false,
    is_self: false,
    invite_pending: true,
  },
];

/**
 * The caller's own role in the company. Member-callable (unlike the roster,
 * which is manager+), so the UI can decide what to show before fetching more.
 */
export async function getMyRole(): Promise<MyRole> {
  if (demoMode) return { company_id: 'demo-company', role: 'admin', is_primary_admin: true };
  const res = await apiFetch('/api/v1/company/me');
  return (await res.json()) as MyRole;
}

/** The full team roster and the invites still waiting to be accepted. Manager+
 *  on the backend; a member gets a 403 (which the caller should treat as "no
 *  access to the team screen"). `pendingInvites` reads empty from an older
 *  backend that does not send it. */
export async function listTeam(): Promise<{ members: Member[]; pendingInvites: PendingInvite[] }> {
  if (demoMode) return { members: DEMO_MEMBERS.map((m) => ({ ...m })), pendingInvites: [] };
  const res = await apiFetch('/api/v1/company/members');
  const data = (await res.json()) as { members: Member[]; pending_invites?: PendingInvite[] };
  return { members: data.members, pendingInvites: data.pending_invites ?? [] };
}

/** The roster alone. */
export async function listMembers(): Promise<Member[]> {
  return (await listTeam()).members;
}

/** Take back an invite nobody has answered. Admin only. */
export async function withdrawInvite(inviteId: string): Promise<void> {
  if (demoMode) return;
  await apiFetch(`/api/v1/company/invites/${encodeURIComponent(inviteId)}`, {
    method: 'DELETE',
  });
}

/** The invites waiting for the signed-in user. Any signed-in user. */
export async function getMyInvites(): Promise<MyInvite[]> {
  if (demoMode) return [];
  const res = await apiFetch('/api/v1/company/invites');
  const data = (await res.json()) as { invites?: MyInvite[] };
  return data.invites ?? [];
}

/**
 * Join the company that invited you. Throws with the server's words on a refusal
 * (409: you already belong to another workspace; 404: the invite is gone).
 */
export async function acceptInvite(
  inviteId: string,
): Promise<{ company_id: string; company_name: string; role: TeamRole }> {
  const res = await apiFetch(`/api/v1/company/invites/${encodeURIComponent(inviteId)}/accept`, {
    method: 'POST',
  });
  return (await res.json()) as { company_id: string; company_name: string; role: TeamRole };
}

/** Turn an invite down. */
export async function declineInvite(inviteId: string): Promise<void> {
  await apiFetch(`/api/v1/company/invites/${encodeURIComponent(inviteId)}/decline`, {
    method: 'POST',
  });
}

/** Change a member's role. Admin only; the backend blocks demoting the last
 *  admin (409). */
export async function changeMemberRole(userId: string, role: TeamRole): Promise<void> {
  if (demoMode) return;
  await apiFetch(`/api/v1/company/members/${encodeURIComponent(userId)}/role`, {
    method: 'POST',
    body: JSON.stringify({ role }),
  });
}

/** Remove a member from the company. Admin only; the backend blocks removing
 *  the last admin (409). */
export async function removeMember(userId: string): Promise<void> {
  if (demoMode) return;
  await apiFetch(`/api/v1/company/members/${encodeURIComponent(userId)}`, {
    method: 'DELETE',
  });
}

const DEMO_KNOWLEDGE_MD = `## Portfolio

Acme Property Co manages a **mixed residential portfolio** — roughly 40 leases
on file, with steady maintenance turnover (plumbing and HVAC are the most common
work-order categories over the last month).

## Documents on file

- Standard Residential Lease
- Pet Addendum

## Recent activity

Most webhook activity in the last 30 days has been **maintenance** and **leasing**
events, consistent with an actively-leased portfolio.`;

/**
 * The company's standing knowledge profile (Settings → AI & Memory). Read-only;
 * a daily background job maintains it. In demo mode this returns a static sample.
 */
export async function getCompanyKnowledge(): Promise<CompanyKnowledge> {
  if (demoMode) {
    return {
      company_id: 'demo-company',
      content_md: DEMO_KNOWLEDGE_MD,
      generated_on: '2026-07-04',
      updated_at: '2026-07-04T09:00:00Z',
    };
  }
  const res = await apiFetch('/api/v1/company/knowledge');
  return (await res.json()) as CompanyKnowledge;
}

/**
 * Invite a teammate into the caller's company with a role.
 *
 * Returns whether an email actually went out, and whether the invite waits for
 * them. An address that already has an account gets no mail, and since
 * migration 0112 is NOT added: they see the invite when they sign in and must
 * accept it. The admin has to know both, or they wait for a delivery that is
 * not coming (2026-08-31) or think someone is on the team who isn't.
 *
 * Throws ApiError / ApiAuthError on failure.
 */
export async function inviteMember(
  email: string,
  role: TeamRole = 'member',
): Promise<{ emailSent: boolean; awaitingAcceptance: boolean }> {
  if (demoMode) return { emailSent: true, awaitingAcceptance: false };
  const res = await apiFetch('/api/v1/company/invite', {
    method: 'POST',
    body: JSON.stringify({ email, role }),
  });
  const body = await res.json().catch(() => null);
  // ⚠ Defaults to TRUE on an older backend that omits the field. "We emailed
  // them" is the pre-existing message and the pre-existing behaviour; the
  // alternative default would tell every admin on a stale deploy to chase a
  // teammate who did get an email.
  return {
    emailSent: body?.email_sent !== false,
    awaitingAcceptance: body?.awaiting_acceptance === true,
  };
}

/** What happened to ONE address in a bulk invite. Mirrors `InviteOutcome`. */
export interface InviteResult {
  email: string;
  role: TeamRole;
  /** `invited` mail sent, on the team now · `pending` existing account, no
   *  mail, waits for them to accept · `already_member` no-op · `failed` see
   *  `error`. `added` is what a backend before migration 0112 said for an
   *  existing account (added without asking). */
  status: 'invited' | 'pending' | 'added' | 'already_member' | 'failed';
  emailSent: boolean;
  /** The server's own words for this address. Render verbatim. */
  error: string | null;
}

/** The batch ceiling the server enforces (`MAX_BULK_INVITES`).
 *
 * ⚠ A SECOND COPY of a server constant, and it drifts silently — the same
 * class as `PaidPlanKey` in `billing.ts`. It is here so the dialog can refuse
 * a 26th row with a sentence instead of letting the whole batch 422 after the
 * admin has typed all of them. `test_company_routes` reads both sides. If they
 * disagree, the client is the one that is wrong.
 */
export const MAX_BULK_INVITES = 25;

/**
 * Invite several people in one request.
 *
 * ⚠ NOT a client-side loop over `inviteMember`, and the reasons are the
 * server's: the invite rate limiter is per-request, so five loops spend five
 * of a deliberately tight budget; and each single invite pushes a new seat
 * quantity to Stripe, so a loop prorates every intermediate number for one
 * action the admin took once.
 *
 * ⚠ Resolves even when some addresses failed — read `status` on each result.
 * It rejects only when the whole REQUEST failed (not an admin, rate-limited,
 * invites unconfigured), which are facts about the caller rather than about
 * anybody's email.
 */
export async function inviteMembers(
  invites: { email: string; role: TeamRole }[],
): Promise<InviteResult[]> {
  if (demoMode) {
    return invites.map((i) => ({
      email: i.email,
      role: i.role,
      status: 'invited' as const,
      emailSent: true,
      error: null,
    }));
  }
  const res = await apiFetch('/api/v1/company/invite/bulk', {
    method: 'POST',
    body: JSON.stringify({ invites }),
  });
  const body = (await res.json()) as {
    results: {
      email: string;
      role: TeamRole;
      status: InviteResult['status'];
      email_sent?: boolean;
      error?: string | null;
    }[];
  };
  return (body.results ?? []).map((r) => ({
    email: r.email,
    role: r.role,
    status: r.status,
    emailSent: r.email_sent === true,
    error: r.error ?? null,
  }));
}

// ── Leaving, exporting, deleting ─────────────────────────────────────
//
// ⚠ All three endpoints ALREADY EXISTED on the backend and nothing in the
// frontend called any of them. `GET /company/export` (a zip of CSVs),
// `POST /company/leave` (with a last-admin guard) and `DELETE /company`
// (with a typed name confirmation) shipped live and unreachable — while
// `DeleteAccountRow` rendered a mailto whose own comment said "there is no
// endpoint; support confirms scope by hand". That was true when written and
// stale by the time the route landed (gotcha 48: a comment cannot fail, so a
// change that invalidates one has to go and find it).
//
// The cost is not cosmetic. "Email support to leave" is what a product looks
// like when leaving is discouraged, and somebody deciding whether to trust
// Occupella with their whole portfolio reads it exactly that way.

/**
 * Download the workspace's data as a zip of CSVs. Admin only — the API
 * enforces it and a non-admin gets a 403 with a reason.
 *
 * ⚠ Returns the Blob rather than starting the download itself, so the caller
 * owns the object-URL lifecycle. A helper that quietly created one and never
 * revoked it would hold the customer's entire portfolio in memory for as long
 * as the tab stayed open.
 */
export async function exportCompanyData(): Promise<{ blob: Blob; filename: string }> {
  const res = await apiFetch('/api/v1/company/export');
  if (!res.ok) {
    if (res.status === 403) throw new Error('Only an admin can export company data.');
    throw new Error('Could not build your export. Try again in a moment.');
  }
  // The SERVER names the file (workspace name + date). Parsed rather than
  // reconstructed here, so the two cannot drift into disagreeing.
  const match = /filename="([^"]+)"/.exec(res.headers.get('Content-Disposition') ?? '');
  return { blob: await res.blob(), filename: match?.[1] ?? 'occupella-export.zip' };
}

/** The server's own words for a failure, when it sent any. */
async function detailOr(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json();
    return typeof body?.detail === 'string' ? body.detail : fallback;
  } catch {
    return fallback;
  }
}

/** Remove YOURSELF from the workspace. */
export async function leaveCompany(): Promise<void> {
  if (demoMode) return;
  const res = await apiFetch('/api/v1/company/leave', { method: 'POST' });
  if (res.ok) return;
  // 409 is the last-admin guard and its message names the fix ("promote
  // someone else first"). Surfacing the server's own words beats a generic
  // failure, because the caller can act on them.
  throw new Error(await detailOr(res, 'Could not leave this workspace.'));
}

/** What a deletion actually removed, including the leg that can come back unfinished. */
export interface DeleteCompanyResult {
  /** Buildium mirror rows purged, per table. */
  deletedRows: Record<string, number>;
  /** Uploaded documents, browser screenshots and exports removed from Storage. */
  storageObjectsDeleted: number;
  /**
   * True only when every bucket was enumerated AND emptied.
   *
   * ⚠ False does NOT mean the deletion failed — the workspace is gone either
   * way, by design: blocking a GDPR right on a Storage hiccup is unrecoverable
   * for the customer while leftover objects stay findable under the same
   * prefix. It means one leg is unfinished, and `storageNote` says which
   * buckets. It also covers the case nobody thinks about — a bucket that could
   * not be ENUMERATED — because "there were no files" and "I could not look"
   * produce the same count and opposite obligations.
   */
  storageComplete: boolean;
  /** Empty when complete; otherwise names the buckets still holding data. */
  storageNote: string;
}

/**
 * Delete the workspace and everything in it.
 *
 * `confirmName` must equal the workspace's name. The API compares it and
 * refuses on a mismatch — this is the confirmation, not a client-side nicety,
 * and typing a name is deliberately harder to do by reflex than dismissing a
 * dialog.
 *
 * ⚠ **Still called by nothing, and that is `DeleteAccountRow`'s deliberate
 * decision, not an oversight** — it names two preconditions (a Stripe-active
 * guard, and exercising the cascade against a real account) before a button
 * may be wired. This returns the storage verdict anyway so that whoever does
 * wire it cannot silently drop the one field that says the erasure is
 * unfinished. Reading `deleted_rows` alone was already throwing that away.
 */
export async function deleteCompany(confirmName: string): Promise<DeleteCompanyResult> {
  const res = await apiFetch('/api/v1/company', {
    method: 'DELETE',
    body: JSON.stringify({ confirm_name: confirmName }),
  });
  if (!res.ok) throw new Error(await detailOr(res, 'Could not delete this workspace.'));
  const body = await res.json().catch(() => null);
  return {
    deletedRows: body?.deleted_rows ?? {},
    storageObjectsDeleted: body?.storage_objects_deleted ?? 0,
    // ⚠ Defaults to FALSE, not true. An older backend, a proxy error page or a
    // body that failed to parse must read as "we do not know", because the one
    // thing this flag exists to prevent is telling somebody their files are
    // gone when nobody looked (gotchas 55/57).
    storageComplete: body?.storage_complete === true,
    storageNote: body?.storage_note ?? '',
  };
}
