/**
 * Settings — sectioned preferences with a left-rail section nav.
 *
 * Phase 7 redesign: replaces the single-header tab layout with a
 * two-column layout: 200px section rail on the left, content pane on
 * the right. Rows are hairline-separated, toggles are pill-style
 * animated, all on the new token palette.
 */

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import ReactMarkdown from "react-markdown";
import {
  User,
  Users,
  Plug,
  Trash2,
  ShieldCheck,
  Brain,
  CreditCard,
  Send,
  History,
  UserPlus,
  type LucideIcon,
} from "lucide-react";
import {
  CATEGORY_LABELS,
  connectorRowState,
  listConnectors,
  rowsByCapability,
  type Connector,
  type ConnectorCapability,
  type ConnectorCategory,
} from "../services/connectors";
import { ActivityPage } from "./ActivityPage";
import {
  demoMode,
  performSignOut,
  changePassword,
  changeEmail,
  CurrentPasswordWrong,
} from "../services/auth";
import { suggestEmail } from "../lib/emailTypo";
import { buildDiagnostics } from "../lib/supportDiagnostics";
import {
  firstUnmet,
  meetsRequirements,
  requirements,
  strength,
} from "../lib/passwordStrength";
import {
  confirmEnrolment,
  disableTwoFactor,
  fetchBackupCodeStatus,
  generateBackupCodes,
  listVerifiedFactors,
  startEnrolment,
  WrongCode,
  type BackupCodeStatus,
  type EnrolledFactor,
  type EnrolmentStart,
} from "../services/mfa";
import {
  fetchSessions,
  revokeSession,
  type ActiveSession,
} from "../services/sessions";
import {
  listTeam,
  withdrawInvite,
  changeMemberRole,
  removeMember,
  getMyRole,
  getCompanyKnowledge,
  TEAM_ROLES,
  ROLE_LABEL,
  type TeamRole,
  type Member,
  type PendingInvite,
  type CompanyKnowledge,
  exportCompanyData,
  leaveCompany,
} from "../services/company";
import { InviteUsersDialog } from "./InviteUsersDialog";
import { fetchBillingStatus, type BillingStatus } from "../services/billing";
import { roleAtLeast } from "../lib/roles";
import { usePermissionRefresh } from "../hooks/usePermissionRefresh";
import { useStore } from "../stores/store";
import { ConnectorLogo } from "./ConnectorLogo";
import { FeedbackDialog } from "./FeedbackDialog";
import { BuildiumApiDetail } from "./connectors/BuildiumApiDetail";
import { BuildiumWebhookDetail } from "./connectors/BuildiumWebhookDetail";
import { ComingSoonDetail } from "./connectors/ComingSoonDetail";
import { ComposioOAuthDetail } from "./connectors/ComposioOAuthDetail";
import { PropertywareApiDetail } from "./connectors/PropertywareApiDetail";
import { DoorLoopApiDetail } from "./connectors/DoorLoopApiDetail";
import { RentvineApiDetail } from "./connectors/RentvineApiDetail";
import { RentManagerApiDetail } from "./connectors/RentManagerApiDetail";
import {
  isComposioManaged,
  isPmsConnector,
  visibleConnectorRows,
} from "../services/connectors";
import {
  analyticsOptedOut,
  posthogConfigured,
  setAnalyticsOptedOut,
} from "../services/analytics";
import { SUPPORT_EMAIL } from '../lib/support';
import { BillingPanel } from "./BillingPanel";
import { FormError } from './FormError';

type Section =
  | "account"
  | "security"
  | "team"
  | "ai"
  | "activity"
  | "billing"
  | "connectors";

const SECTIONS: {
  id: Section;
  label: string;
  icon: LucideIcon;
  /** Minimum role to SEE this section (ADR 0005 visibility gating). Omitted
   *  = visible to everyone. */
  minRole?: TeamRole;
}[] = [
  { id: "account", label: "Account", icon: User },
  // ⚠ Its own section, not a group inside Account (founder call, 2026-09-08).
  // Account had grown to sixteen rows spanning identity, password, two-factor,
  // devices, export and deletion — and grouping them under headings shortened
  // nothing. Password and two-factor are what somebody opens Settings to
  // change; they should not be reached by scrolling past a build number.
  { id: "security", label: "Security", icon: ShieldCheck },
  { id: "team", label: "Team & Permissions", icon: Users, minRole: "manager" },
  { id: "ai", label: "AI & Memory", icon: Brain },
  // ⚠ ADMIN, raised 2026-09-04 (founder direction) — was ungated, so a member
  // could see the Buildium connection panel. The API is the enforcement (every
  // route in `routes/buildium.py` now calls `_require_admin`, reads included);
  // this only stops showing a member a section that will 403 on them.
  // ⚠ NO minRole — REOPENED 2026-09-04 (gotcha 85). Closing the whole
  // section was an over-fix: the founder asked for the BUILDIUM connection
  // to be admin-only, and Buildium is one row on this page. Gmail, Calendar
  // and Drive are per-USER OAuth grants — a member connecting their own
  // mailbox is the entire point of a member account, and there is nothing
  // for them to leak to. The Buildium rows are filtered per-row below.
  { id: "connectors", label: "Connectors", icon: Plug },
  // Visible to everyone, not admin-gated: a member who cannot change the
  // plan still needs to see the meter. Checkout inside the panel is the
  // part that carries the admin floor.
  { id: "billing", label: "Billing", icon: CreditCard },
  // Moved off the sidebar (2026-09-02, founder direction). It is a record you
  // consult, not a place you work from — the sidebar is for the four surfaces
  // a day is actually spent in. ⌘K and the `g a` chord still reach it, now by
  // deep-linking this section rather than a top-level view.
  { id: "activity", label: "Team Activity", icon: History },
];

export function SettingsPage() {
  // ⚠ The store owns this, not a local useState — and that was a live bug,
  // not a refactor. `settingsSection` was WRITTEN by `openSettings` and by the
  // `g a` nav chord, and read by nothing: measured in a real browser, `g a`
  // opened Settings and landed on Account while the rail showed Team Activity
  // highlighted nowhere. This file's own comment on the Activity row claimed
  // the opposite ("⌘K and the `g a` chord still reach it, now by deep-linking
  // this section"), which is gotcha 48 — a comment cannot fail, so it went on
  // being wrong. Reading the store fixes the chord AND makes the section
  // addressable as /settings/<section>.
  const section = useStore((s) => s.settingsSection) as Section;
  const setSection = useStore((s) => s.openSettings);
  const companyRole = useStore((s) => s.companyRole);
  const setCompanyRole = useStore((s) => s.setCompanyRole);

  // Probe the caller's role once so the rail can hide screens they can't use
  // (member can't see Team & Permissions). Member-callable endpoint, so it
  // never 403s for a legitimate member; on any failure we leave the role null
  // (fail closed → gated sections stay hidden).
  const loadRole = useCallback(() => {
    getMyRole()
      .then((r) => setCompanyRole(r.role))
      .catch(() => {
        /* leave role null — gated sections stay hidden */
      });
  }, [setCompanyRole]);

  useEffect(() => {
    loadRole();
  }, [loadRole]);

  // A 403 anywhere is the one positive signal that this cached role is stale
  // (the rail would otherwise keep offering screens the person lost).
  usePermissionRefresh(loadRole);

  const visibleSections = SECTIONS.filter(
    (s) => !s.minRole || roleAtLeast(companyRole, s.minRole),
  );

  // If the active section became hidden (role resolved to something lower),
  // fall back to Account so the pane never renders a screen the rail hides.
  useEffect(() => {
    if (!visibleSections.some((s) => s.id === section)) {
      setSection("account");
    }
  }, [visibleSections, section]);

  return (
    <div className="flex-1 flex flex-col overflow-hidden min-h-0">
      {/* Header — full-width border-b, centered content (matches PageShell). */}
      <div
        className="h-12 shrink-0 border-b"
        style={{ borderColor: "var(--line)" }}
      >
        <div
          className="h-full px-12 max-[899px]:px-4 mx-auto w-full flex items-center"
          style={{ maxWidth: "1200px" }}
        >
          <h1
            className="text-[20px] font-semibold tracking-tight"
            style={{
              color: "var(--ink)",
              fontFamily: "var(--font-display)",
              letterSpacing: "-0.015em",
            }}
          >
            Settings
          </h1>
        </div>
      </div>

      {/* Body — rail (220px) + content; centered together inside 1200px
          so the rail doesn't pin against the sidebar boundary while the
          content gets enough room to breathe. */}
      <div
        className="flex flex-1 min-h-0 mx-auto w-full max-[899px]:flex-col"
        style={{ maxWidth: "1200px" }}
      >
        {/* Section rail. Below 900px it becomes a row of tabs across the
            top that scrolls sideways, so the content gets the full width
            (it was a 220px rail beside a 150px column at 375px). */}
        <nav
          aria-label="Settings sections"
          className="w-[220px] shrink-0 px-3 py-4 flex flex-col gap-[1px] border-r max-[899px]:w-full max-[899px]:flex-row max-[899px]:overflow-x-auto max-[899px]:px-4 max-[899px]:py-2 max-[899px]:gap-1 max-[899px]:border-r-0 max-[899px]:border-b"
          style={{
            borderColor: "var(--line)",
            background: "var(--canvas-raised)",
          }}
        >
          {visibleSections.map(({ id, label, icon: Icon }) => {
            const active = section === id;
            return (
              <button
                key={id}
                type="button"
                onClick={() => setSection(id)}
                aria-current={active ? "page" : undefined}
                // Below 900px the rail is a sideways-scrolling tab row, and
                // /settings/billing opened with Billing off the right edge.
                ref={(el) => {
                  if (active) el?.scrollIntoView?.({ block: "nearest", inline: "nearest" });
                }}
                className="group relative flex items-center gap-2.5 h-8 pl-4 pr-3 rounded-[var(--r-sm)] text-left transition-colors duration-150 max-[899px]:shrink-0 max-[899px]:pl-3 whitespace-nowrap"
                style={{
                  background: active ? "var(--iris-soft)" : "transparent",
                  color: active ? "var(--ink)" : "var(--ink-muted)",
                  fontSize: 13,
                }}
                onMouseEnter={(e) => {
                  if (active) return;
                  (e.currentTarget as HTMLElement).style.background =
                    "var(--canvas-hover)";
                  (e.currentTarget as HTMLElement).style.color = "var(--ink)";
                }}
                onMouseLeave={(e) => {
                  if (active) return;
                  (e.currentTarget as HTMLElement).style.background =
                    "transparent";
                  (e.currentTarget as HTMLElement).style.color =
                    "var(--ink-muted)";
                }}
              >
                <span
                  aria-hidden
                  className="absolute left-[5px] top-1.5 bottom-1.5 w-[2px] rounded-full max-[899px]:hidden"
                  style={{
                    background: active ? "var(--iris)" : "transparent",
                  }}
                />
                <Icon
                  size={15}
                  strokeWidth={1.5}
                  style={{
                    color: active ? "var(--iris-hover)" : "var(--ink-subtle)",
                  }}
                />
                <span className="flex-1">{label}</span>
              </button>
            );
          })}
        </nav>

        {/* Content pane */}
        <div className="flex-1 min-w-0 overflow-y-auto px-12 py-8 max-[899px]:px-4 max-[899px]:py-6">
          <div className="max-w-[720px]">
            {section === "account" && <AccountPanel />}
            {section === "security" && <SecurityPanel />}
            {section === "team" && <TeamPanel />}
            {section === "ai" && <AiMemoryPanel />}
            {section === "connectors" && <ConnectorsPanel />}
            {section === "billing" && (
              <>
                <SectionHeader
                  title="Billing"
                />
                <BillingPanel />
              </>
            )}
            {section === "activity" && <ActivityPage />}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Section shell ──────────────────────────────────────────────────

function SectionHeader({
  title,
  description,
}: {
  title: string;
  description?: string;
}) {
  return (
    <div className="mb-5">
      <h2
        className="text-[16px] font-semibold tracking-tight"
        style={{
          color: "var(--ink)",
          letterSpacing: "-0.01em",
        }}
      >
        {title}
      </h2>
      {description && (
        <p className="text-[13px] mt-1" style={{ color: "var(--ink-muted)" }}>
          {description}
        </p>
      )}
    </div>
  );
}

/**
 * A sub-heading inside one panel.
 *
 * ⚠ **Asymmetric on purpose: 28px above, 6px below.** A heading centred in its
 * own whitespace belongs to neither side, which is what the first cut did
 * (32/4, plus the row's own 16px top padding — so it sat closer to the group
 * ABOVE it than the one it names). Proximity is the only thing that makes a
 * heading a heading here; there is no rule, no fill and no indent doing that
 * job.
 */
function GroupHeader({ title }: { title: string }) {
  return (
    <h3
      className="text-[12px] font-semibold uppercase mt-7 mb-1.5"
      style={{
        color: "var(--ink-muted)",
        fontFamily: "var(--font-sans)",
        letterSpacing: "0.05em",
      }}
    >
      {title}
    </h3>
  );
}

/**
 * One label-and-control line.
 *
 * ⚠ **`items-center` when there is no description, `items-start` when there
 * is.** Every row on the Account pane is now one line of text beside a 32px
 * button, and top-aligning those left the label floating a few pixels above
 * the control it belongs to — sixteen times down one column, which reads as
 * sloppiness rather than as any single misplaced element. A row that DOES
 * carry a description is two lines tall and has to stay top-aligned, or the
 * button drifts down to the middle of the paragraph.
 */
function SettingRow({
  label,
  description,
  control,
}: {
  label: string;
  description?: string;
  control: React.ReactNode;
}) {
  return (
    <div
      className={`flex ${description ? "items-start py-3.5" : "items-center py-2.5"} justify-between gap-6 border-b`}
      style={{ borderColor: "var(--line)" }}
    >
      <div className="min-w-0 flex-1">
        <div
          className="text-[14px] font-medium"
          style={{ color: "var(--ink)" }}
        >
          {label}
        </div>
        {description && (
          <div
            className="text-[13px] mt-1 leading-snug"
            style={{ color: "var(--ink-subtle)" }}
          >
            {description}
          </div>
        )}
      </div>
      <div className="shrink-0">{control}</div>
    </div>
  );
}

function StaticValue({ children }: { children: React.ReactNode }) {
  return (
    <span
      className="text-[13px] font-mono"
      style={{ color: "var(--ink-muted)" }}
    >
      {children}
    </span>
  );
}

// ── Account ─────────────────────────────────────────────────────────

function AccountPanel() {
  const user = useStore((s) => s.user);
  const companyId = useStore((s) => s.companyId);
  const version = typeof __APP_VERSION__ === "string" ? __APP_VERSION__ : "";
  return (
    <>
      {/* ⚠ No description under this heading, and none under most rows below.
          The pane read as a product explaining itself to somebody who had
          already found their way here — "Ends your session on every device,
          including this one" under a button that says Sign out everywhere.
          Founder call, 2026-09-08: a description survives only where the
          CHOICE is genuinely non-obvious, which is the plan picker, the
          connectors and team roles. Everywhere else the label is the whole
          sentence, and a paragraph under it reads as filler. */}
      <SectionHeader title="Account" />
      <SettingRow
        label="Email"
        control={<StaticValue>{user?.email ?? "Not signed in"}</StaticValue>}
      />
      {/* ⚠ Only worth a row when it is NOT the normal state. On a real account
          "Production" told nobody anything, and the line under it named our
          database vendor — which means nothing to a property manager and is
          exactly the plumbing this pass is removing. */}
      {demoMode && (
        <SettingRow label="Demo data" control={<StaticValue>On</StaticValue>} />
      )}
      {/* ⚠ The two things support actually asks for, side by side. "Which
          build are you on" was here; "which account are you" existed only
          inside the diagnostics blob, so answering the first question still
          cost a round trip for the second. */}
      <SettingRow
        label="Account ID"
        control={
          <StaticValue>
            <span data-testid="account-id">{companyId || "unknown"}</span>
          </StaticValue>
        }
      />
      <SettingRow
        label="Version"
        control={
          <span
            className="text-[13px] tabular-nums"
            style={{ color: "var(--ink-muted)" }}
            data-testid="app-version"
          >
            {/* An empty string means the build-time define did not run — a
                local `vite dev` without it, or a broken build. Saying so beats
                a blank space that reads as a value nobody can describe. */}
            {version || "unknown build"}
          </span>
        }
      />
      <SupportDiagnosticsRow />
      <ProductAnalyticsRow />

      <GroupHeader title="Workspace" />
      <ExportDataRow />
      <LeaveWorkspaceRow />
      <DeleteAccountRow />
      <SettingRow
        label="Sign out"
        control={
          <button
            type="button"
            onClick={() => {
              void performSignOut();
            }}
            className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium transition-colors duration-150"
            style={{
              borderColor: "var(--line-strong)",
              background: "var(--canvas)",
              color: "var(--danger)",
            }}
            onMouseEnter={(e) => {
              const el = e.currentTarget as HTMLElement;
              el.style.background = "var(--canvas-hover)";
            }}
            onMouseLeave={(e) => {
              const el = e.currentTarget as HTMLElement;
              el.style.background = "var(--canvas-elevated)";
            }}
          >
            Sign out
          </button>
        }
      />
      <LegalFooter />
    </>
  );
}

// ── Security ─────────────────────────────────────────────────────────

/**
 * Password, second factor, email address and live sessions.
 *
 * ⚠ **Lifted OUT of Account, not merely grouped inside it** (founder call,
 * 2026-09-08). These five rows are the reason people open Settings; under
 * Account they sat below a build number and an export button, five scrolls
 * from the top of a sixteen-row column. A heading does not shorten a list.
 *
 * ⚠ **`ChangeEmailRow` is here rather than beside `Email` on Account.** The
 * Account row REPORTS the address; this one changes it, and changing the
 * address you sign in with is a credential change — it is the same lane as
 * the password, and Supabase's "Secure email change" makes it a two-mailbox
 * confirmation. Splitting the report from the change is deliberate: the row
 * you read and the row that mails two people are not the same row.
 */
function SecurityPanel() {
  return (
    <>
      <SectionHeader title="Security" />
      <ChangePasswordRow />
      <TwoFactorRow />
      <ChangeEmailRow />
      <ActiveSessionsRow />
      <SignOutEverywhereRow />
    </>
  );
}

// ── Legal ────────────────────────────────────────────────────────────

/** The published policy pages.
 *
 * ⚠ They existed and NOTHING in the product linked to them. A terms page
 * nobody can reach from inside the app is a file on a server, not a published
 * policy.
 *
 * ⚠ Same-origin `/legal/*.html`, never an absolute link to this app's own
 * host — that path is answered by the SPA and renders the login screen (see
 * legalLinks.test.tsx). The dangerous host was `occupella.com` until the
 * 2026-09-15 domain swap and is `app.occupella.com` after it; a path cannot go
 * stale that way, which is why it is one.
 */
const LEGAL_PAGES: ReadonlyArray<{ href: string; label: string }> = [
  { href: "/legal/terms.html", label: "Terms" },
  { href: "/legal/privacy.html", label: "Privacy" },
  { href: "/legal/billing.html", label: "Billing & refunds" },
  { href: "/legal/sms.html", label: "SMS consent" },
  { href: "/legal/accessibility.html", label: "Accessibility" },
  { href: "/legal/subprocessors.html", label: "Subprocessors" },
];

/**
 * A footer, not a setting.
 *
 * ⚠ It used to be a `SettingRow` with a paragraph beside it explaining what
 * terms and a privacy policy are. Six standalone links given the same weight
 * as two-factor authentication is what made the pane read as legal-heavy — and
 * nobody arrives at Settings to read the subprocessor list. They have to be
 * reachable; they do not have to be prominent.
 */
function LegalFooter() {
  return (
    <div
      className="flex flex-wrap items-center gap-x-4 gap-y-1 pt-5"
      data-testid="legal-links"
    >
      {LEGAL_PAGES.map((page) => (
        <a
          key={page.href}
          href={page.href}
          target="_blank"
          rel="noreferrer"
          // ⚠ 19px tall as measured in a real browser. WCAG 2.2 exempts a link
          // INSIDE a sentence and these are not — they are six standalone
          // targets in a wrapping row, which on a phone is exactly where a
          // mis-tap opens the wrong policy. The text stays 12px; the box
          // around it reaches the 24px minimum.
          className="inline-flex items-center min-h-6 text-[12px]"
          style={{ color: "var(--ink-subtle)" }}
        >
          {page.label}
        </a>
      ))}
    </div>
  );
}

// ── Delete account ───────────────────────────────────────────────────

/**
 * Ask for the account and its data to be deleted.
 *
 * The request goes to `SUPPORT_EMAIL` (`lib/support`). The runbook requires
 * it come from the address on file, so a mailto from the person's own client
 * is the evidence the operator needs — not a convenience.
 *
 * ⚠ This does NOT delete anything. Deletion is an operator-run script
 * (`scripts/purge_company_data.py`, `docs/runbooks/account-deletion.md`) with
 * five manual legs the script does not cover, and it has never been exercised
 * end to end. A self-serve button wired to a cascade nobody has run once is a
 * worse failure than a slow deletion — the half-done state is unrecoverable
 * and the proof record would certify something untrue.
 *
 * What it fixes is DISCOVERABILITY. The documented path was "email us", with
 * the address written nowhere in the product, so from inside the app the exit
 * did not exist — the same defect the billing portal link had. Telling people
 * plainly how to leave, and what leaving does and does not touch, is the
 * standard; hiding it is what reads as evasive.
 *
 * ⚠ The Buildium sentence is load-bearing and must stay accurate. Occupella
 * only ever holds a CACHE of their Buildium records (ToS §18), and people
 * assume the worst — someone who believes deleting here wipes their actual
 * books will not ask.
 */
export function DeleteAccountRow() {
  const companyRole = useStore((s) => s.companyRole);
  // Deleting removes the whole company's data, not one person's login, so it
  // is an admin request. A member who wants out is asking to be removed from
  // the workspace, which is a different thing and their admin's to do.
  const isAdmin = companyRole === "admin";

  // ⚠ HIDDEN from non-admins entirely, 2026-09-04 (founder direction). It used
  // to render for everyone with softer copy AND a live "Request deletion"
  // mailto — so a member could email support asking for the whole workspace to
  // be wiped, which is a request a member cannot legitimately make.
  // `companyRole` is null while the probe is in flight, so this hides during
  // load and reveals on resolve — the safe direction.
  //
  // ⚠ CORRECTED 2026-09-08: this comment used to say "there is no endpoint".
  // There is — `DELETE /api/v1/company`, admin-only, with a typed-name
  // confirmation. It stays UNWIRED deliberately, for two reasons that are
  // both about billing and neither about squeamishness:
  //
  //   1. That route's own docstring says it does not cancel Stripe and that
  //      "the UI enforces that ordering". No such UI exists, so wiring the
  //      button today would delete the `helixis_billing` row that holds the
  //      ONLY link to the Stripe customer — after which they are charged
  //      every month for an account that is gone and cannot be found to
  //      refund. `docs/runbooks/account-deletion.md` calls that the single
  //      worst outcome available here, and it is right.
  //   2. That runbook states the cascade is manual on purpose until it has
  //      been exercised against a real account more than once. It has not.
  //
  // So the honest surface is still "ask us", and the mailto is the mechanism
  // the runbook depends on — it requires the request to arrive from the
  // address on file, which is the step that stops a disgruntled ex-employee
  // erasing a company's books. Wiring the button is a deliberate follow-up:
  // add a Stripe-active guard, exercise the cascade twice, then flip it.
  if (!isAdmin) return null;

  const subject = encodeURIComponent("Delete my Occupella account");
  const body = encodeURIComponent(
    "I'd like my Occupella account and all associated data deleted.\n\n" +
      "Please confirm what will be removed before you start.",
  );

  return (
    <SettingRow
      label="Delete account"
      control={
        <a
          href={`mailto:${SUPPORT_EMAIL}?subject=${subject}&body=${body}`}
          className="inline-flex h-8 items-center rounded-[var(--r-sm)] border px-3 text-[13px] font-medium transition-colors duration-150"
          style={{
            borderColor: "var(--line-strong)",
            background: "var(--canvas)",
            color: "var(--danger)",
          }}
        >
          Request deletion
        </a>
      }
    />
  );
}

/**
 * Download everything, as a zip of CSVs.
 *
 * ⚠ The endpoint has existed and nothing called it. "Export to CSV" is a
 * day-one ask for people who live in spreadsheets, and a product that holds a
 * portfolio and cannot hand it back reads as a trap — which is exactly how
 * somebody evaluating Occupella with their whole book would read it.
 */
function ExportDataRow() {
  const companyRole = useStore((s) => s.companyRole);
  const [busy, setBusy] = useState(false);
  if (companyRole !== "admin") return null;

  const run = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const { blob, filename } = await exportCompanyData();
      // ⚠ Revoke the object URL. It holds the whole portfolio in memory, and
      // leaking one per click means a long settings session keeps every
      // export the customer ever built.
      const url = URL.createObjectURL(blob);
      try {
        const link = document.createElement("a");
        link.href = url;
        link.download = filename;
        link.click();
      } finally {
        URL.revokeObjectURL(url);
      }
      toast.success("Export downloaded.");
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("[occupella] export failed", err);
      toast.error(err instanceof Error ? err.message : "Couldn't build your export.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingRow
      label="Export your data"
      control={
        <button
          type="button"
          onClick={() => void run()}
          disabled={busy}
          className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium transition-colors duration-150 disabled:opacity-60"
          style={{
            borderColor: "var(--line-strong)",
            background: "var(--canvas)",
            color: "var(--ink)",
          }}
        >
          {busy ? "Preparing…" : "Download export"}
        </button>
      }
    />
  );
}

/**
 * Remove yourself from a workspace you no longer work at.
 *
 * Also already built and unreachable. Until this was wired, leaving meant
 * asking an admin — so somebody who had changed jobs stayed on the members
 * list, kept a billed seat, and kept access to a portfolio they had no
 * business seeing. "Ask someone else to revoke your access" is not an access
 * control.
 *
 * ⚠ The last-admin refusal comes from the SERVER and is rendered verbatim,
 * because its message names the fix ("promote someone else first"). A generic
 * failure would leave the person stuck with no idea what to do next.
 */
function LeaveWorkspaceRow() {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await leaveCompany();
      toast.success("You've left the workspace.");
      await performSignOut();
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("[occupella] leave workspace failed", err);
      toast.error(err instanceof Error ? err.message : "Couldn't leave the workspace.", {
        duration: 10000,
      });
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <SettingRow
      label="Leave workspace"
      control={
        !confirming ? (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium transition-colors duration-150"
            style={{
              borderColor: "var(--line-strong)",
              background: "var(--canvas)",
              color: "var(--ink)",
            }}
          >
            Leave
          </button>
        ) : (
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => void run()}
              disabled={busy}
              className="h-8 px-3 rounded-[var(--r-sm)] text-[13px] font-medium transition-colors disabled:opacity-60"
              style={{ background: "var(--danger)", color: "var(--canvas)" }}
            >
              {busy ? "Leaving…" : "Yes, leave"}
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              disabled={busy}
              className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] transition-colors"
              style={{
                borderColor: "var(--line-strong)",
                background: "var(--canvas)",
                color: "var(--ink-muted)",
              }}
            >
              Cancel
            </button>
          </div>
        )
      }
    />
  );
}

/**
 * The copyable answer to "it looks wrong on my screen".
 *
 * ⚠ This is deliberately the thing that is NOT operator impersonation. The
 * obvious fix for "let me see what they see" is a switch that puts an
 * operator inside a customer's account — a real cross-tenant read path in a
 * multi-tenant product, and a backdoor if it is ever wrong or compromised.
 * This solves the same support problem from the other end: the CUSTOMER hands
 * over the context, on purpose, and there is no privilege to escalate.
 *
 * ⚠ Nothing secret is in the block, and `supportDiagnostics` has a test that
 * asserts that on the OUTPUT rather than on intent — the whole value is that
 * this is safe to paste into an email or a screenshot.
 */
function SupportDiagnosticsRow() {
  const user = useStore((s) => s.user);
  const companyId = useStore((s) => s.companyId);
  const companyIds = useStore((s) => s.companyIds);
  const companyRole = useStore((s) => s.companyRole);
  const view = useStore((s) => s.view);
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    const text = buildDiagnostics({
      userId: user?.id ?? null,
      email: user?.email ?? null,
      companyId,
      companyRole,
      companyCount: companyIds.length,
      appVersion: typeof __APP_VERSION__ === "string" ? __APP_VERSION__ : "",
      userAgent: navigator.userAgent,
      now: new Date().toISOString(),
      view,
    });
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      // Reset so a second problem later in the session gets a real
      // confirmation rather than a tick that never went away.
      setTimeout(() => setCopied(false), 2500);
    } catch {
      // Clipboard is permission-gated and blocked outright in some contexts.
      // Falling back to a toast carrying the text means the feature degrades
      // to "select and copy" instead of silently doing nothing.
      toast.message("Copy this and send it to support:", {
        description: text,
        duration: 30000,
      });
    }
  };

  return (
    <SettingRow
      label="Support details"
      control={
        <button
          type="button"
          onClick={() => void copy()}
          className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium transition-colors duration-150"
          style={{
            borderColor: "var(--line-strong)",
            background: "var(--canvas)",
            color: copied ? "var(--positive)" : "var(--ink)",
          }}
        >
          {copied ? "Copied" : "Copy details"}
        </button>
      }
    />
  );
}

/**
 * The product-analytics opt-out.
 *
 * ⚠ RESTORED 2026-09-15, AND THE REASON IS NOT TASTE. The 2026-09-08 pass cut
 * two switches off this pane — `ReduceMotionRow` and `AnalyticsRow` — on the
 * stated ground that neither had a purpose here. That was right about reduce
 * motion, which duplicates a browser setting the OS already owns. It was wrong
 * about this one, and the cost was invisible: `frontend/public/legal/
 * privacy.html` tells every customer, in as many words, "You can turn analytics
 * off at any time in Settings → Account → Product analytics". They came here,
 * found nothing by that name, and had no way to opt out at all — a published
 * privacy policy naming a mechanism that did not exist.
 *
 * ⚠ `setAnalyticsOptedOut` was thoroughly tested the whole time and called by
 * NOTHING outside its own tests (`analytics.optout.test.ts`) — the shape
 * gotcha 62 records, arriving where the thing wired to nothing is a promise to
 * a customer rather than a card in an inbox. A behavioural test of a helper
 * cannot see that no screen reaches it, so the guard for this is in
 * `settingsCopy.test.tsx` and reads the policy and the pane together.
 *
 * ⚠ NO DESCRIPTION, AND THAT IS DELIBERATE RATHER THAN AN OVERSIGHT. The first
 * cut carried one — "which pages you open and which controls you use, never
 * the text on your screen" — and `settingsCopy.test.tsx` failed it, correctly.
 * The founder call of 2026-09-08 is that a description survives only where the
 * CHOICE is genuinely non-obvious, and *Delete account* sits four rows below
 * this one with no paragraph explaining that it deletes the account. A privacy
 * control is not more deserving of prose than that. What the row collects is
 * answered in the privacy policy, one click away, in the sentence that sends
 * people to this row in the first place — so the pane stays the shape that was
 * asked for and the question still has an answer. Do not add a sentence back.
 *
 * ⚠ Hidden when analytics is not configured. An opt-out for measurement that
 * is not running is a promise about nothing, and it is exactly the kind of
 * control that reads as boilerplate and gets ignored on the day it is real.
 */
function ProductAnalyticsRow() {
  // Read in an effect rather than during render: `analyticsOptedOut` touches
  // localStorage, which throws outright in some privacy modes.
  const [optedOut, setOptedOut] = useState(false);
  useEffect(() => setOptedOut(analyticsOptedOut()), []);

  if (!posthogConfigured) return null;

  return (
    <SettingRow
      label="Product analytics"
      control={
        <button
          type="button"
          onClick={() => {
            const next = !optedOut;
            setAnalyticsOptedOut(next);
            setOptedOut(next);
            toast.success(
              next
                ? "Analytics is off for this browser"
                : "Analytics is on for this browser",
            );
          }}
          className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium transition-colors duration-150"
          style={{
            borderColor: "var(--line-strong)",
            background: "var(--canvas)",
            color: "var(--ink)",
          }}
        >
          {/* The button says what pressing it DOES, not what the current state
              is — "On" beside a control is the ambiguity that makes somebody
              press it to find out. */}
          {optedOut ? "Turn on" : "Turn off"}
        </button>
      }
    />
  );
}

/**
 * Two-factor authentication.
 *
 * A password alone protects somebody else's entire portfolio. Occupella asks
 * property managers to put all of it in one place, so the second factor is
 * what makes that ask reasonable rather than a nicety.
 *
 * ⚠ THE SETUP KEY GETS EQUAL BILLING WITH THE QR CODE, deliberately.
 * Supabase issues no recovery codes, so that key is the ONLY way back from a
 * lost phone — and the standard design, where the key hides behind a
 * "can't scan?" link, is the one that produces a locked-out manager on a
 * Monday morning. It is shown, it is copyable, and the screen says in as many
 * words what happens if they skip it.
 *
 * ⚠ The confirm step is not optional and cannot be skipped. Enrolling without
 * proving the app works would arm a lock whose key was never tested.
 */
function TwoFactorRow() {
  const [factors, setFactors] = useState<EnrolledFactor[] | null>(null);
  const [setup, setSetup] = useState<EnrolmentStart | null>(null);
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [disarming, setDisarming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedKey, setSavedKey] = useState(false);
  //: The plaintext codes, held ONLY while the panel showing them is open.
  //: There is no second fetch that returns them, so dismissing this is the
  //: point of no return — which is why dismissing is gated on a checkbox.
  const [codes, setCodes] = useState<string[] | null>(null);
  const [savedCodes, setSavedCodes] = useState(false);
  //: How many are left. `null` = not checked or the check failed, and it
  //: renders as silence rather than "0 left" — telling somebody they have no
  //: codes when we simply could not look is gotcha 55's false claim, and it
  //: would send them to regenerate and kill a sheet that was working.
  const [remaining, setRemaining] = useState<BackupCodeStatus | null>(null);

  useEffect(() => {
    let live = true;
    listVerifiedFactors()
      .then((f) => live && setFactors(f))
      // A failed probe must not render as "you have no 2FA" — that is a
      // confident false claim about a security control (gotcha 55). Null
      // stays null and the row says it could not check.
      .catch(() => live && setFactors(null));
    return () => {
      live = false;
    };
  }, []);

  const enabled = (factors?.length ?? 0) > 0;

  // Only asked for once 2FA is actually on: the count means nothing to
  // somebody who has no second factor, and it is a request per Settings open.
  useEffect(() => {
    if (!enabled) {
      setRemaining(null);
      return;
    }
    let live = true;
    fetchBackupCodeStatus()
      .then((s) => live && setRemaining(s))
      .catch(() => live && setRemaining(null));
    return () => {
      live = false;
    };
  }, [enabled]);

  /**
   * Issue codes and show them.
   *
   * ⚠ NEVER fails the thing that called it. At enrolment, 2FA is already ON
   * by the time this runs — rolling that back because the code service
   * blipped would trade a working second factor for a failed convenience.
   * The caller gets `false` and says so.
   */
  const issueCodes = async (): Promise<boolean> => {
    try {
      setCodes(await generateBackupCodes());
      setSavedCodes(false);
      setRemaining(await fetchBackupCodeStatus().catch(() => null));
      return true;
    } catch {
      return false;
    }
  };

  const begin = async () => {
    setBusy(true);
    setError(null);
    try {
      setSetup(await startEnrolment());
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("[occupella] 2FA enrolment start failed", err);
      setError("Couldn't start setup. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  };

  const confirm = async () => {
    if (!setup || busy) return;
    setBusy(true);
    setError(null);
    try {
      await confirmEnrolment(setup.factorId, code);
      toast.success("Two-factor authentication is on.");
      setSetup(null);
      setCode("");
      setSavedKey(false);
      setFactors(await listVerifiedFactors());
      // ⚠ Issued HERE, unprompted, rather than offered as an optional extra
      // afterwards. The moment somebody has just armed a lock is the only
      // moment they are thinking about losing the key; a "you can also
      // generate backup codes" note in Settings is read by nobody until the
      // phone is already gone.
      if (!(await issueCodes())) {
        setError(
          "Two-factor is on, but we couldn't create backup codes. Use " +
            "“Regenerate backup codes” below — without them, a lost " +
            "phone means emailing support.",
        );
      }
    } catch (err) {
      setError(
        err instanceof WrongCode
          ? err.message
          : "Couldn't finish setup. Try again in a moment.",
      );
    } finally {
      setBusy(false);
    }
  };

  const disable = async () => {
    const factor = factors?.[0];
    if (!factor || busy) return;
    setBusy(true);
    setError(null);
    try {
      await disableTwoFactor(factor.id, password);
      toast.success("Two-factor authentication is off.");
      setPassword("");
      setDisarming(false);
      setFactors(await listVerifiedFactors());
    } catch (err) {
      setError(
        err instanceof CurrentPasswordWrong
          ? "That password is incorrect."
          : "Couldn't turn it off. Try again in a moment.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingRow
      label="Two-factor authentication"
      control={
        <div className="flex flex-col gap-2 w-full max-w-[300px] items-end">
          {factors === null && (
            <span className="text-[12px]" style={{ color: "var(--ink-subtle)" }}>
              Couldn't check your setup.
            </span>
          )}

          {/* ── the codes, shown once ──
              Deliberately the FIRST branch: while this panel is open it is
              the only thing that matters, and it must not be possible to
              navigate past it by clicking something else in the row. */}
          {codes ? (
            <div className="flex flex-col gap-2 w-full">
              <span className="text-[13px] font-medium" style={{ color: "var(--ink)" }}>
                Save these backup codes
              </span>
              <span className="text-[12px]" style={{ color: "var(--ink-muted)" }}>
                Each one works once and turns two-factor off, so you can sign in
                with your password if your phone is gone. This is the only time
                they are shown.
              </span>
              <div
                className="grid grid-cols-2 gap-x-3 gap-y-1 px-2.5 py-2 rounded-[var(--r-sm)] border"
                style={{ borderColor: "var(--line)", background: "var(--canvas)" }}
              >
                {codes.map((c) => (
                  <code
                    key={c}
                    className="text-[12px] font-mono tabular-nums"
                    style={{ color: "var(--ink)" }}
                  >
                    {c}
                  </code>
                ))}
              </div>
              <button
                type="button"
                onClick={() => void navigator.clipboard?.writeText(codes.join("\n"))}
                className="self-start text-[12px] underline underline-offset-2"
                style={{ color: "var(--iris)" }}
              >
                Copy all
              </button>
              <label
                className="flex items-start gap-2 text-[12px] cursor-pointer"
                style={{ color: "var(--ink-muted)" }}
              >
                <input
                  type="checkbox"
                  checked={savedCodes}
                  onChange={(e) => setSavedCodes(e.target.checked)}
                  style={{ accentColor: "var(--iris)", marginTop: 2 }}
                />
                <span>I've saved these somewhere I can reach without my phone.</span>
              </label>
              {/* ⚠ Gated on the checkbox for the same reason the setup key is:
                  dismissing this is irreversible and there is no second fetch.
                  A plain "Done" is one stray click away from somebody who
                  believes they are covered and is not. */}
              <button
                type="button"
                onClick={() => {
                  setCodes(null);
                  setSavedCodes(false);
                }}
                disabled={!savedCodes}
                className="h-8 px-3 self-start rounded-[var(--r-sm)] text-[13px] font-medium transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                style={{ background: "var(--iris)", color: "var(--canvas)" }}
              >
                Done
              </button>
            </div>
          ) : /* ── setting it up ── */
          setup ? (
            <div className="flex flex-col gap-2 w-full">
              <span className="text-[13px]" style={{ color: "var(--ink)" }}>
                1. Scan this in your authenticator app
              </span>
              <img
                src={setup.qrCode}
                alt="QR code for setting up two-factor authentication"
                width={148}
                height={148}
                style={{ background: "#fff", borderRadius: "var(--r-sm)", padding: 6 }}
              />
              <span className="text-[13px] mt-1" style={{ color: "var(--ink)" }}>
                2. Save this setup key somewhere safe
              </span>
              {/* ⚠ NOT hidden behind a "can't scan?" link. Supabase issues no
                  recovery codes, so this key is the only way back from a lost
                  phone — burying it is what produces a locked-out manager. */}
              <div
                className="flex items-center gap-2 px-2 py-1.5 rounded-[var(--r-sm)] border"
                style={{ borderColor: "var(--line)", background: "var(--canvas)" }}
              >
                <code
                  className="text-[12px] font-mono break-all"
                  style={{ color: "var(--ink)" }}
                >
                  {setup.secret}
                </code>
                <button
                  type="button"
                  onClick={() => void navigator.clipboard?.writeText(setup.secret)}
                  className="ml-auto shrink-0 text-[12px] underline underline-offset-2"
                  style={{ color: "var(--iris)" }}
                >
                  Copy
                </button>
              </div>
              <label
                className="flex items-start gap-2 text-[12px] cursor-pointer"
                style={{ color: "var(--ink-muted)" }}
              >
                <input
                  type="checkbox"
                  checked={savedKey}
                  onChange={(e) => setSavedKey(e.target.checked)}
                  style={{ accentColor: "var(--iris)", marginTop: 2 }}
                />
                <span>
                  I've saved this key. Without it and my phone, I can't get back in.
                </span>
              </label>

              <span className="text-[13px] mt-1" style={{ color: "var(--ink)" }}>
                3. Enter the current code
              </span>
              <PlainField
                placeholder="6-digit code"
                value={code}
                onChange={setCode}
                disabled={busy}
              />
              {error && <FormError>{error}</FormError>}
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => void confirm()}
                  disabled={busy || code.trim().length < 6 || !savedKey}
                  className="h-8 px-3 rounded-[var(--r-sm)] text-[13px] font-medium transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                  style={{ background: "var(--iris)", color: "var(--canvas)" }}
                >
                  {busy ? "Checking…" : "Turn on"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setSetup(null);
                    setCode("");
                    setSavedKey(false);
                    setError(null);
                  }}
                  disabled={busy}
                  className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] transition-colors"
                  style={{
                    borderColor: "var(--line-strong)",
                    background: "var(--canvas)",
                    color: "var(--ink-muted)",
                  }}
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : disarming ? (
            /* ── turning it off ── */
            <div className="flex flex-col gap-2 w-full">
              <PasswordField
                placeholder="Your password"
                value={password}
                onChange={setPassword}
                disabled={busy}
                autoComplete="current-password"
                onEnter={() => void disable()}
              />
              {error && <FormError>{error}</FormError>}
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => void disable()}
                  disabled={busy || !password}
                  className="h-8 px-3 rounded-[var(--r-sm)] text-[13px] font-medium transition-colors disabled:opacity-60"
                  style={{ background: "var(--danger)", color: "var(--canvas)" }}
                >
                  {busy ? "Turning off…" : "Turn off"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setDisarming(false);
                    setPassword("");
                    setError(null);
                  }}
                  disabled={busy}
                  className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] transition-colors"
                  style={{
                    borderColor: "var(--line-strong)",
                    background: "var(--canvas)",
                    color: "var(--ink-muted)",
                  }}
                >
                  Cancel
                </button>
              </div>
            </div>
          ) : enabled ? (
            <div className="flex flex-col gap-1.5 items-end">
              {/* ⚠ The count renders only when we actually read it. `null`
                  says nothing rather than "0 left", which would be a false
                  claim on a failed request AND would push somebody to
                  regenerate — killing a sheet that was working fine. */}
              {remaining && (
                <span
                  className="text-[12px] tabular-nums"
                  style={{
                    color: remaining.remaining <= 2 ? "var(--caution)" : "var(--ink-subtle)",
                  }}
                >
                  {remaining.remaining === 0
                    ? "No backup codes left"
                    : `${remaining.remaining} of ${remaining.issuedPerBatch} backup codes left`}
                </span>
              )}
              {error && <FormError className="text-right">{error}</FormError>}
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setError(null);
                    void issueCodes().then((ok) => {
                      if (!ok) setError("Couldn't create backup codes. Try again in a moment.");
                    });
                  }}
                  disabled={busy}
                  className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium transition-colors duration-150 disabled:opacity-60"
                  style={{
                    borderColor: "var(--line-strong)",
                    background: "var(--canvas)",
                    color: "var(--ink)",
                  }}
                  // Said on the control itself, not in a toast afterwards —
                  // by then the old sheet is already dead.
                  title="Creates a new set and stops the old codes working"
                >
                  {remaining?.remaining === 0 ? "Get backup codes" : "Regenerate backup codes"}
                </button>
                <button
                  type="button"
                  onClick={() => setDisarming(true)}
                  className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium transition-colors duration-150"
                  style={{
                    borderColor: "var(--line-strong)",
                    background: "var(--canvas)",
                    color: "var(--danger)",
                  }}
                >
                  Turn off
                </button>
              </div>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => void begin()}
              disabled={busy}
              className="h-8 px-3 rounded-[var(--r-sm)] text-[13px] font-medium transition-colors disabled:opacity-60"
              style={{ background: "var(--iris)", color: "var(--canvas)" }}
            >
              {busy ? "Starting…" : "Set up"}
            </button>
          )}
        </div>
      }
    />
  );
}

// ── Change password ──────────────────────────────────────────────────

// ⚠ THE LOCAL `MIN_PASSWORD_LENGTH` IS GONE, 2026-09-08. It read 6 here while
// `SetPasswordForm` (invite + reset) used 8 — so the same account could be
// given a password on one screen that the other would have refused, and
// neither number was wrong on its own terms. Both forms now gate on
// `meetsRequirements` from `lib/passwordStrength`, which owns the floor. A
// second constant naming the same rule is exactly how the two drifted apart.

/**
 * Change your own password, from Account.
 *
 * Until this shipped there was no way to do it while signed in — you had to
 * sign out and use "Forgot password?", which is a workaround people notice.
 *
 * ⚠ The CURRENT password is required, and `changePassword` is what enforces
 * it: Supabase's `updateUser({ password })` needs only a live session, so a
 * form that called it directly would let anyone at an unlocked laptop lock the
 * real owner out of their company's books. Do not "simplify" this by dropping
 * the current-password field.
 *
 * Collapsed by default. An always-open password form in a settings page is
 * something browsers offer to autofill and people fill in by accident.
 */
// Exported for its own test file: the current-password requirement is a
// security property, and a test that has to render the whole SettingsPage
// to reach it would fail for a dozen unrelated reasons.
/**
 * Change the address the account is identified by — and, crucially, the one
 * every future password-reset link is sent to.
 *
 * ⚠ It asks for the current password for the same reason ChangePasswordRow
 * does, and the stakes are HIGHER: an unguarded email change from an unlocked
 * laptop is a complete takeover (move the address, sign out, "forgot
 * password"). See `changeEmail` in services/auth.
 *
 * ⚠ The success copy must NOT say "done". Supabase's secure email change
 * sends a confirmation to BOTH addresses and only moves the account when both
 * are clicked — so reporting success on a change still waiting on two clicks
 * teaches people the product is broken when their address does not move.
 */
/**
 * The devices you are signed in on, and a way to end any one of them.
 *
 * `SignOutEverywhereRow` is the blunt instrument — it ends every session
 * including this one. This is the scalpel: end the laptop you left at the
 * office and keep working here.
 *
 * ⚠ Loads on demand, not on mount. The Settings page is opened constantly for
 * connectors and billing, and this is the one panel almost nobody needs on a
 * given visit — fetching it every time spends a round trip per open to render
 * something not being looked at.
 *
 * ⚠ `isCurrent === null` must render as "unknown", never as "another device".
 * The backend returns null when the token carried no `session_id` claim, and
 * collapsing that into false labels the session you are USING as somebody
 * else's — which is an invitation to sign yourself out while trying to be
 * careful. See `services/sessions`.
 */
function ActiveSessionsRow() {
  const [open, setOpen] = useState(false);
  const [sessions, setSessions] = useState<ActiveSession[] | null>(null);
  const [identified, setIdentified] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await fetchSessions();
      setSessions(result.sessions);
      setIdentified(result.currentIdentified);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("[occupella] session list failed", err);
      setError("Couldn't load your devices. Try again.");
    } finally {
      setLoading(false);
    }
  };

  const end = async (id: string) => {
    setRevoking(id);
    try {
      await revokeSession(id);
      // Drop it locally rather than refetching: the row is gone by
      // definition, and a refetch would make the list flicker for a change
      // the user just watched happen.
      setSessions((prev) => (prev ?? []).filter((s) => s.id !== id));
      toast.success("That device has been signed out.");
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("[occupella] session revoke failed", err);
      toast.error("Couldn't end that session. Try again.");
    } finally {
      setRevoking(null);
    }
  };

  return (
    <SettingRow
      label="Devices"
      control={
        !open ? (
          <button
            type="button"
            onClick={() => {
              setOpen(true);
              void load();
            }}
            className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium transition-colors duration-150"
            style={{
              borderColor: "var(--line-strong)",
              background: "var(--canvas)",
              color: "var(--ink)",
            }}
          >
            Show devices
          </button>
        ) : (
          <div className="flex flex-col gap-2 w-full max-w-[340px]">
            {loading && (
              <span className="text-[13px]" style={{ color: "var(--ink-muted)" }}>
                Loading…
              </span>
            )}
            {error && <FormError>{error}</FormError>}
            {!loading && !error && sessions?.length === 0 && (
              <span className="text-[13px]" style={{ color: "var(--ink-muted)" }}>
                No other sessions on record.
              </span>
            )}
            {!identified && sessions && sessions.length > 0 && (
              <span className="text-[12px]" style={{ color: "var(--ink-subtle)" }}>
                We can't tell which of these is the device you're on right now.
              </span>
            )}
            {sessions?.map((s) => (
              <div
                key={s.id}
                className="flex items-start justify-between gap-3 py-2 border-b last:border-b-0"
                style={{ borderColor: "var(--line)" }}
              >
                <div className="min-w-0">
                  <div className="text-[13px]" style={{ color: "var(--ink)" }}>
                    {s.device}
                    {s.isCurrent === true && (
                      <span
                        className="ml-2 text-[12px] px-1.5 py-0.5 rounded-[var(--r-xs)]"
                        style={{ background: "var(--iris-soft)", color: "var(--iris-hover)" }}
                      >
                        This device
                      </span>
                    )}
                  </div>
                  <div
                    className="text-[12px] mt-0.5 tabular-nums"
                    style={{ color: "var(--ink-subtle)" }}
                  >
                    Last active {new Date(s.lastSeen).toLocaleString()}
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => void end(s.id)}
                  disabled={revoking === s.id}
                  className="h-7 px-2.5 shrink-0 rounded-[var(--r-sm)] border text-[12px] transition-colors disabled:opacity-60"
                  style={{
                    borderColor: "var(--line-strong)",
                    background: "var(--canvas)",
                    color: "var(--danger)",
                  }}
                >
                  {revoking === s.id
                    ? "Ending…"
                    : s.isCurrent === true
                      ? "Sign out here"
                      : "End"}
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="self-start h-8 px-3 rounded-[var(--r-sm)] border text-[13px] transition-colors"
              style={{
                borderColor: "var(--line-strong)",
                background: "var(--canvas)",
                color: "var(--ink-muted)",
              }}
            >
              Hide
            </button>
          </div>
        )
      }
    />
  );
}

export function ChangeEmailRow() {
  const current = useStore((s) => s.user?.email ?? null);
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const [next, setNext] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const suggestion = suggestEmail(next);

  const reset = () => {
    setPassword("");
    setNext("");
    setError(null);
  };

  const submit = async () => {
    if (busy) return;
    setError(null);
    if (!next.includes("@")) {
      setError("That doesn't look like an email address.");
      return;
    }
    setBusy(true);
    try {
      await changeEmail(password, next);
      toast.success(
        `Confirmation sent to ${current ?? "your old address"} and ${next.trim()}. The change lands once both are confirmed.`,
        { duration: 14000 },
      );
      reset();
      setOpen(false);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("[occupella] email change failed", err);
      setError(
        err instanceof CurrentPasswordWrong
          ? "That password is incorrect."
          : err instanceof Error && err.message
            ? err.message
            : "Couldn't change your email. Try again, or contact team@occupella.com.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingRow
      label="Change email"
      control={
        !open ? (
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium transition-colors duration-150"
            style={{
              borderColor: "var(--line-strong)",
              background: "var(--canvas)",
              color: "var(--ink)",
            }}
          >
            Change email
          </button>
        ) : (
          <div className="flex flex-col gap-2 w-full max-w-[280px]">
            <PlainField
              placeholder="New email address"
              value={next}
              onChange={setNext}
              disabled={busy}
              type="email"
              autoComplete="email"
            />
            {suggestion && (
              <button
                type="button"
                onClick={() => setNext(suggestion)}
                className="text-[12px] text-left underline underline-offset-2"
                style={{ color: "var(--iris)" }}
              >
                Did you mean {suggestion}?
              </button>
            )}
            <PasswordField
              placeholder="Your password"
              value={password}
              onChange={setPassword}
              disabled={busy}
              autoComplete="current-password"
              onEnter={() => void submit()}
            />
            {error && <FormError>{error}</FormError>}
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => void submit()}
                disabled={busy || !password || !next}
                className="h-8 px-3 rounded-[var(--r-sm)] text-[13px] font-medium transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                style={{ background: "var(--iris)", color: "var(--canvas)" }}
              >
                {busy ? "Sending…" : "Send confirmations"}
              </button>
              <button
                type="button"
                onClick={() => {
                  reset();
                  setOpen(false);
                }}
                disabled={busy}
                className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] transition-colors"
                style={{
                  borderColor: "var(--line-strong)",
                  background: "var(--canvas)",
                  color: "var(--ink-muted)",
                }}
              >
                Cancel
              </button>
            </div>
          </div>
        )
      }
    />
  );
}

/**
 * The control that answers "I signed in on a laptop I no longer have."
 *
 * ⚠ A password change does NOT do this. Refresh tokens already issued to
 * other devices keep working, which is exactly the assumption that makes a
 * lost device dangerous — so this is a separate control, not a side effect of
 * one that already exists.
 *
 * ⚠ Confirm-gated, because it is irreversible in the only sense that matters
 * to a team: every colleague on every device is signed out too, mid-task,
 * with no undo. And it signs THIS device out as well, necessarily — the
 * button says so rather than surprising them.
 */
export function SignOutEverywhereRow() {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    if (busy) return;
    setBusy(true);
    try {
      // performSignOut marks the intent BEFORE the network call, so the
      // SIGNED_OUT this triggers is not reported back to the user as
      // "your session ended" (services/sessionEnd).
      await performSignOut({ everywhere: true });
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("[occupella] global sign-out failed", err);
      toast.error("Couldn't sign out everywhere. Try again.");
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <SettingRow
      label="Sign out everywhere"
      control={
        !confirming ? (
          <button
            type="button"
            onClick={() => setConfirming(true)}
            className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium transition-colors duration-150"
            style={{
              borderColor: "var(--line-strong)",
              background: "var(--canvas)",
              color: "var(--ink)",
            }}
          >
            Sign out everywhere
          </button>
        ) : (
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => void run()}
              disabled={busy}
              className="h-8 px-3 rounded-[var(--r-sm)] text-[13px] font-medium transition-colors disabled:opacity-60"
              style={{ background: "var(--danger)", color: "var(--canvas)" }}
            >
              {busy ? "Signing out…" : "Yes, sign out everywhere"}
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              disabled={busy}
              className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] transition-colors"
              style={{
                borderColor: "var(--line-strong)",
                background: "var(--canvas)",
                color: "var(--ink-muted)",
              }}
            >
              Cancel
            </button>
          </div>
        )
      }
    />
  );
}

/** Same shape as PasswordField, for a field that is not a secret. */
function PlainField({
  placeholder,
  value,
  onChange,
  disabled,
  type = "text",
  autoComplete,
}: {
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  type?: string;
  autoComplete?: string;
}) {
  const [focused, setFocused] = useState(false);
  return (
    <input
      type={type}
      placeholder={placeholder}
      aria-label={placeholder}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      disabled={disabled}
      autoComplete={autoComplete}
      className="h-9 px-3 rounded-[var(--r-sm)] border text-[13px] outline-none transition-colors w-full"
      style={{
        background: "var(--canvas)",
        color: "var(--ink)",
        borderColor: focused ? "var(--line-focus)" : "var(--line)",
        boxShadow: focused ? "0 0 0 3px var(--iris-ring)" : "none",
      }}
    />
  );
}

/**
 * The checklist + strength bar, for the change-password form.
 *
 * ⚠ Same two-layer split as the invite screen: the CHECKLIST is the enforced
 * floor (length, a capital, a number) and the meter is a glance on top of it.
 *
 * ⚠ **They used to disagree on purpose** — an entropy estimate that called
 * `Password1` Weak despite it ticking every printed box. That was true and it
 * read as the form arguing with you, so on 2026-09-09 the meter was cut to
 * the ordinary three bands and tuned never to contradict the checklist.
 */
function PasswordRequirements({ password }: { password: string }) {
  const reqs = requirements(password);
  const { score, label } = strength(password);

  const colour =
    score >= 3 ? "var(--positive)" : score === 2 ? "var(--caution)" : "var(--danger)";

  return (
    <div className="flex flex-col gap-1.5">
      {password && (
        <div className="flex items-center gap-2">
          <div className="flex gap-1 flex-1" aria-hidden="true">
            {[1, 2, 3].map((band) => (
              <div
                key={band}
                className="h-[3px] flex-1 rounded-full transition-colors"
                style={{ background: score >= band ? colour : "var(--line)" }}
              />
            ))}
          </div>
          <span
            aria-live="polite"
            className="text-[12px] font-medium tabular-nums"
            style={{ color: colour, minWidth: 48, textAlign: "right" }}
          >
            {label}
          </span>
        </div>
      )}
      <ul className="flex flex-col gap-1">
        {reqs.map((r) => (
          <li
            key={r.id}
            className="flex items-center gap-1.5 text-[12px]"
            // ⚠ --ink-subtle, not --ink-faint, for the UNMET line. Measured
            // 2026-09-08: --ink-faint is 2.61:1 on --canvas and 2.34:1 on
            // --canvas-elevated, against WCAG AA's 4.5:1 for text this size.
            // These are the instructions somebody has to READ in order to
            // choose a password that will be accepted, so an unmet rule is
            // the one line here that must not be the hardest to see —
            // exactly backwards from where the palette had it. --ink-subtle
            // measures 4.73:1 and keeps met/unmet visibly different.
            style={{ color: r.met ? "var(--ink-muted)" : "var(--ink-subtle)" }}
          >
            {/* Colour is never the only signal — the glyph changes too
                (WCAG 1.4.1). */}
            <span aria-hidden="true" style={{ width: 10, display: "inline-block" }}>
              {r.met ? "\u2713" : "\u00b7"}
            </span>
            <span>{r.label}</span>
            <span className="sr-only">{r.met ? " \u2014 met" : " \u2014 not yet met"}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ChangePasswordRow() {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setCurrent("");
    setNext("");
    setConfirm("");
    setError(null);
  };

  const submit = async () => {
    if (busy) return;
    setError(null);
    // Checked here so the mismatch is caught before a round trip AND before
    // the current password is verified — a typo in the confirm box should
    // never cost a re-authentication.
    if (next !== confirm) {
      setError("The new passwords don't match.");
      return;
    }
    const missing = firstUnmet(next);
    if (missing) {
      setError(missing);
      return;
    }
    if (next === current) {
      setError("That's the password you already have.");
      return;
    }
    setBusy(true);
    try {
      await changePassword(current, next);
      toast.success("Password updated.");
      reset();
      setOpen(false);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("[occupella] password change failed", err);
      setError(
        err instanceof CurrentPasswordWrong
          ? // Named separately from a failed update: "couldn't change your
            // password" for a typo sends the user to support for something
            // they can fix in three seconds.
            "That current password is incorrect."
          : "Couldn't change your password. Try again, or contact team@occupella.com.",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingRow
      label="Password"
      control={
        !open ? (
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium transition-colors duration-150"
            style={{
              borderColor: "var(--line-strong)",
              background: "var(--canvas)",
              color: "var(--ink)",
            }}
          >
            Change password
          </button>
        ) : (
          <div className="flex flex-col gap-2 w-full max-w-[280px]">
            <PasswordField
              placeholder="Current password"
              value={current}
              onChange={setCurrent}
              disabled={busy}
              autoComplete="current-password"
            />
            <PasswordField
              placeholder="New password"
              value={next}
              onChange={setNext}
              disabled={busy}
              autoComplete="new-password"
            />
            <PasswordRequirements password={next} />
            <PasswordField
              placeholder="Confirm new password"
              value={confirm}
              onChange={setConfirm}
              disabled={busy}
              autoComplete="new-password"
              onEnter={() => void submit()}
            />
            {error && <FormError>{error}</FormError>}
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => void submit()}
                disabled={busy || !current || !confirm || !meetsRequirements(next)}
                className="h-8 px-3 rounded-[var(--r-sm)] text-[13px] font-medium transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
                style={{ background: "var(--iris)", color: "var(--canvas)" }}
              >
                {busy ? "Saving…" : "Save password"}
              </button>
              <button
                type="button"
                onClick={() => {
                  reset();
                  setOpen(false);
                }}
                disabled={busy}
                className="h-8 px-3 rounded-[var(--r-sm)] border text-[13px] transition-colors"
                style={{
                  borderColor: "var(--line-strong)",
                  background: "var(--canvas)",
                  color: "var(--ink-muted)",
                }}
              >
                Cancel
              </button>
            </div>
          </div>
        )
      }
    />
  );
}

function PasswordField({
  placeholder,
  value,
  onChange,
  disabled,
  autoComplete,
  onEnter,
}: {
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  autoComplete: string;
  onEnter?: () => void;
}) {
  const [focused, setFocused] = useState(false);
  return (
    <input
      type="password"
      placeholder={placeholder}
      aria-label={placeholder}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter" && onEnter) onEnter();
      }}
      onFocus={() => setFocused(true)}
      onBlur={() => setFocused(false)}
      disabled={disabled}
      autoComplete={autoComplete}
      className="h-9 px-3 rounded-[var(--r-sm)] border text-[13px] outline-none transition-colors w-full"
      style={{
        background: "var(--canvas)",
        color: "var(--ink)",
        borderColor: focused ? "var(--line-focus)" : "var(--line)",
        boxShadow: focused ? "0 0 0 3px var(--iris-ring)" : "none",
      }}
    />
  );
}

// ── Company ─────────────────────────────────────────────────────────

// ── Team & Permissions (ADR 0005) ────────────────────────────────────

function RoleBadge({ role }: { role: TeamRole }) {
  const tone: Record<TeamRole, { bg: string; fg: string }> = {
    admin: { bg: "var(--iris-soft)", fg: "var(--iris-hover)" },
    manager: { bg: "var(--canvas-hover)", fg: "var(--ink)" },
    member: { bg: "var(--canvas-hover)", fg: "var(--ink-muted)" },
  };
  const t = tone[role];
  return (
    <span
      className="inline-flex items-center h-6 px-2 rounded-full text-[12px] font-medium"
      style={{ background: t.bg, color: t.fg }}
    >
      {ROLE_LABEL[role]}
    </span>
  );
}

function TeamPanel() {
  const companyRole = useStore((s) => s.companyRole);
  const isAdmin = companyRole === "admin";
  const [members, setMembers] = useState<Member[] | null>(null);
  // Invites to people who already have an account, waiting for them to accept
  // (migration 0112). Not members: no seat, no access.
  const [pendingInvites, setPendingInvites] = useState<PendingInvite[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [inviteOpen, setInviteOpen] = useState(false);
  // ⚠ Read for the invite dialog's cost line and nothing else. A failure here
  // must NOT block inviting: `seatImpact` returns null on a missing plan and
  // the dialog simply says nothing about money. Refusing to open an invite
  // screen because a billing read failed would be an outage in the wrong place.
  const [billing, setBilling] = useState<BillingStatus | null>(null);

  const refresh = () => {
    listTeam()
      .then((team) => {
        setMembers(team.members);
        setPendingInvites(team.pendingInvites);
        setLoadError(null);
      })
      .catch((err) => {
        setLoadError(
          err instanceof Error ? err.message : "Could not load the team.",
        );
      });
  };

  useEffect(refresh, []);

  useEffect(() => {
    if (!isAdmin) return; // only the invite dialog reads this, and it is admin-only
    fetchBillingStatus()
      .then(setBilling)
      .catch(() => setBilling(null)); // silent by design — see the state above
  }, [isAdmin]);

  const adminCount = (members ?? []).filter((m) => m.role === "admin").length;

  const handleRoleChange = async (member: Member, role: TeamRole) => {
    if (role === member.role || busyId) return;
    if (demoMode) {
      toast.info("Demo mode — role changes are disabled here.");
      return;
    }
    setBusyId(member.user_id);
    try {
      await changeMemberRole(member.user_id, role);
      toast.success(`${member.email ?? "Member"} is now ${ROLE_LABEL[role]}.`);
      refresh();
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Could not change the role.",
        {
          duration: 8000,
        },
      );
    } finally {
      setBusyId(null);
    }
  };

  const handleWithdraw = async (invite: PendingInvite) => {
    if (busyId) return;
    if (demoMode) {
      toast.info("Demo mode — invites are disabled here.");
      return;
    }
    if (!window.confirm(`Withdraw the invite to ${invite.email ?? "this person"}?`)) return;
    setBusyId(invite.id);
    try {
      await withdrawInvite(invite.id);
      toast.success(`Withdrew the invite to ${invite.email ?? "them"}.`);
      refresh();
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Could not withdraw the invite.",
        { duration: 8000 },
      );
    } finally {
      setBusyId(null);
    }
  };

  const handleRemove = async (member: Member) => {
    if (busyId) return;
    if (demoMode) {
      toast.info("Demo mode — removing members is disabled here.");
      return;
    }
    if (
      !window.confirm(
        `Remove ${member.email ?? "this member"} from the company?`,
      )
    )
      return;
    setBusyId(member.user_id);
    try {
      await removeMember(member.user_id);
      toast.success(`Removed ${member.email ?? "member"}.`);
      refresh();
    } catch (err) {
      toast.error(
        err instanceof Error ? err.message : "Could not remove the member.",
        {
          duration: 8000,
        },
      );
    } finally {
      setBusyId(null);
    }
  };

  return (
    <>
      <div className="flex items-start justify-between gap-4">
        <SectionHeader
          title="Team & Permissions"
          // ⚠ Says what each role CANNOT do, which is the only part somebody
        // picking one needs. The old line ran the three roles as a
        // semicolon-parallel list — "Admins manage everything; Managers run
        // day-to-day; Members use Occupella without seeing company settings" —
        // which reads as filler and does not answer the actual question, which
        // is what you are handing over when you make someone an admin.
        description="Admins can change anything, including billing and the Buildium connection. Managers do everything else. Members never see this screen."
        />
        {isAdmin && (
          <button
            type="button"
            onClick={() => setInviteOpen(true)}
            className="shrink-0 h-9 px-3.5 rounded-[var(--r-sm)] text-[13px] font-medium inline-flex items-center gap-1.5"
            style={{ background: "var(--iris)", color: "var(--canvas)" }}
          >
            <UserPlus size={14} strokeWidth={1.8} />
            Invite users
          </button>
        )}
      </div>

      {loadError && (
        <div
          className="text-[13px] py-3 px-3 rounded-[var(--r-sm)] mb-3"
          style={{
            background: "var(--canvas-hover)",
            color: "var(--ink-muted)",
          }}
        >
          {loadError}
        </div>
      )}

      {members === null && !loadError && (
        <div
          className="text-[13px] py-4"
          style={{ color: "var(--ink-subtle)" }}
        >
          Loading team…
        </div>
      )}

      {members && (
        <div className="flex flex-col">
          {members.map((m) => {
            const isBusy = busyId === m.user_id;
            // The last admin can't be demoted or removed (backend guards with a
            // 409); disable the controls so the UI matches and explains why.
            const lastAdmin = m.role === "admin" && adminCount <= 1;
            return (
              <div
                key={m.user_id}
                className="flex items-center justify-between gap-4 py-3 border-b"
                style={{ borderColor: "var(--line)" }}
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span
                      className="text-[14px] font-medium truncate"
                      style={{ color: "var(--ink)" }}
                    >
                      {m.display_name || m.email || "Unknown"}
                    </span>
                    {m.is_self && (
                      <span
                        className="text-[12px]"
                        style={{ color: "var(--ink-subtle)" }}
                      >
                        (you)
                      </span>
                    )}
                    {m.is_primary_admin && (
                      <ShieldCheck
                        size={13}
                        strokeWidth={1.75}
                        style={{ color: "var(--iris-hover)" }}
                        aria-label="Primary admin"
                      />
                    )}
                    {/* ⚠ The membership row is created UP FRONT at invite
                        time, so without this an invitee who never opened the
                        email looks exactly like a colleague who set up weeks
                        ago — and the seat is billed from the invite either
                        way. The chip is the only thing that answers "did Sam
                        ever get in?". */}
                    {m.invite_pending && (
                      <span
                        data-testid="member-invite-pending"
                        className="text-[12px] font-medium uppercase tracking-wide px-1.5 py-0.5 rounded-[var(--r-sm)] border shrink-0"
                        style={{
                          color: "var(--ink-secondary)",
                          borderColor: "var(--line-strong)",
                          background: "var(--canvas-elevated)",
                        }}
                      >
                        Invite pending
                      </span>
                    )}
                  </div>
                  {m.email && m.display_name && (
                    <div
                      className="text-[12px] mt-0.5 truncate"
                      style={{ color: "var(--ink-subtle)" }}
                    >
                      {m.email}
                    </div>
                  )}
                  {m.invite_pending && (
                    <div
                      className="text-[12px] mt-0.5"
                      style={{ color: "var(--ink-subtle)" }}
                    >
                      Hasn't signed in yet — this seat is already counted.
                    </div>
                  )}
                </div>

                {isAdmin ? (
                  <div className="flex items-center gap-2 shrink-0">
                    <select
                      value={m.role}
                      onChange={(e) =>
                        void handleRoleChange(m, e.target.value as TeamRole)
                      }
                      disabled={isBusy || lastAdmin}
                      aria-label={`Role for ${m.email ?? m.user_id}`}
                      title={
                        lastAdmin
                          ? "The company's last admin can't be demoted."
                          : undefined
                      }
                      className="h-8 px-2 rounded-[var(--r-sm)] border text-[13px] outline-none cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                      style={{
                        background: "var(--canvas)",
                        color: "var(--ink)",
                        borderColor: "var(--line)",
                      }}
                    >
                      {TEAM_ROLES.map((r) => (
                        <option key={r.value} value={r.value}>
                          {r.label}
                        </option>
                      ))}
                    </select>
                    {!m.is_self && (
                      <button
                        type="button"
                        onClick={() => void handleRemove(m)}
                        disabled={isBusy || lastAdmin}
                        aria-label={`Remove ${m.email ?? m.user_id}`}
                        title={
                          lastAdmin
                            ? "The company's last admin can't be removed."
                            : "Remove"
                        }
                        className="h-8 w-8 grid place-items-center rounded-[var(--r-sm)] border transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                        style={{
                          borderColor: "var(--line)",
                          color: "var(--ink-muted)",
                        }}
                        onMouseEnter={(e) => {
                          if (isBusy || lastAdmin) return;
                          const el = e.currentTarget as HTMLElement;
                          el.style.color = "var(--danger)";
                          el.style.borderColor = "var(--line-strong)";
                        }}
                        onMouseLeave={(e) => {
                          const el = e.currentTarget as HTMLElement;
                          el.style.color = "var(--ink-muted)";
                          el.style.borderColor = "var(--line)";
                        }}
                      >
                        <Trash2 size={14} strokeWidth={1.5} />
                      </button>
                    )}
                  </div>
                ) : (
                  <RoleBadge role={m.role} />
                )}
              </div>
            );
          })}
          {/* ⚠ After the members and never mixed into them: these people are
              NOT on the team. They already had an Occupella account, so no
              email went out; they join only when they accept the invite in the
              app (migration 0112). */}
          {pendingInvites.map((inv) => (
            <div
              key={inv.id}
              data-testid="pending-invite"
              className="flex items-center justify-between gap-4 py-3 border-b"
              style={{ borderColor: "var(--line)" }}
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span
                    className="text-[14px] font-medium truncate"
                    style={{ color: "var(--ink)" }}
                  >
                    {inv.email ?? "Unknown"}
                  </span>
                  <span
                    className="text-[12px] font-medium uppercase tracking-wide px-1.5 py-0.5 rounded-[var(--r-sm)] border shrink-0"
                    style={{
                      color: "var(--ink-secondary)",
                      borderColor: "var(--line-strong)",
                      background: "var(--canvas-elevated)",
                    }}
                  >
                    Pending
                  </span>
                </div>
                <div
                  className="text-[12px] mt-0.5"
                  style={{ color: "var(--ink-subtle)" }}
                >
                  Invited as {ROLE_LABEL[inv.role]}. They join when they accept
                  it in Occupella. No seat until then.
                </div>
              </div>
              {isAdmin && (
                <button
                  type="button"
                  onClick={() => void handleWithdraw(inv)}
                  disabled={busyId === inv.id}
                  className="h-8 px-2.5 shrink-0 rounded-[var(--r-sm)] border text-[13px] disabled:opacity-50"
                  style={{ borderColor: "var(--line)", color: "var(--ink-muted)" }}
                >
                  Withdraw
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      {inviteOpen && (
        <InviteUsersDialog
          onClose={() => setInviteOpen(false)}
          onInvited={refresh}
          // ⚠ The LIVE roster length, not `billing.seats`. That field is the
          // quantity Stripe is currently billing, synced back by the webhook —
          // it lags a membership change and would price the next invite off a
          // stale number.
          //
          // ⚠ NULL while it is still loading, never 0. `seatImpact` declines to
          // price an unknown roster; defaulting to 0 made a $199 invite render
          // as "this won't change your bill".
          memberCount={members === null ? null : members.length}
          plan={billing?.currentPlan ?? null}
        />
      )}
      {!isAdmin && members && (
        <p
          className="text-[13px] mt-4"
          style={{ color: "var(--ink-subtle)" }}
        >
          Only admins can change roles or invite teammates.
        </p>
      )}
    </>
  );
}

// ── AI & Memory (ADR 0006) ──────────────────────────────────────────

function formatUpdated(iso: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function AiMemoryPanel() {
  const [knowledge, setKnowledge] = useState<CompanyKnowledge | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    getCompanyKnowledge()
      .then((k) => {
        if (alive) {
          setKnowledge(k);
          setLoadError(null);
        }
      })
      .catch((err) => {
        if (alive)
          setLoadError(err instanceof Error ? err.message : "Could not load.");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  const content = knowledge?.content_md?.trim() ?? "";
  const updated = formatUpdated(
    knowledge?.generated_on ?? knowledge?.updated_at ?? null,
  );

  return (
    <>
      <SectionHeader
        title="AI & Memory"
        // ⚠ Names the SOURCES, because "learned automatically" is the part
        // that worries people and the part that says nothing. This page is
        // read-only, so the copy must not imply you can edit or delete an
        // entry here — there is no control that does either.
        description="What Occupella has picked up about your company from your Buildium records and past conversations."
      />

      {loadError && (
        <div
          className="text-[13px] py-3 px-3 rounded-[var(--r-sm)] mb-3"
          style={{
            background: "var(--canvas-hover)",
            color: "var(--ink-muted)",
          }}
        >
          {loadError}
        </div>
      )}

      {loading && (
        <div
          className="text-[13px] py-4"
          style={{ color: "var(--ink-subtle)" }}
        >
          Loading…
        </div>
      )}

      {!loading && !loadError && (
        <div
          className="rounded-[var(--r-md)] border p-5"
          style={{
            borderColor: "var(--card-edge)",
            background: "var(--canvas)",
          }}
        >
          <div className="flex items-center gap-2 mb-3">
            <Brain
              size={15}
              strokeWidth={1.75}
              style={{ color: "var(--iris-hover)" }}
            />
            <span
              className="text-[13px] font-medium"
              style={{ color: "var(--ink)" }}
            >
              What Occupella knows about your company
            </span>
            {updated && (
              <span
                className="text-[12px] ml-auto"
                style={{ color: "var(--ink-subtle)" }}
              >
                Updated {updated}
              </span>
            )}
          </div>

          {content ? (
            <div
              className="text-[13px] leading-relaxed flex flex-col gap-2"
              style={{ color: "var(--ink-muted)" }}
            >
              <ReactMarkdown
                components={{
                  h1: ({ children }) => (
                    <div
                      className="text-[12px] font-semibold uppercase tracking-wide mt-2"
                      style={{ color: "var(--ink-subtle)" }}
                    >
                      {children}
                    </div>
                  ),
                  h2: ({ children }) => (
                    <div
                      className="text-[12px] font-semibold uppercase tracking-wide mt-2"
                      style={{ color: "var(--ink-subtle)" }}
                    >
                      {children}
                    </div>
                  ),
                  p: ({ children }) => <p className="mb-1">{children}</p>,
                  ul: ({ children }) => (
                    <ul className="list-disc pl-5 flex flex-col gap-0.5">
                      {children}
                    </ul>
                  ),
                  li: ({ children }) => <li>{children}</li>,
                  strong: ({ children }) => (
                    <strong style={{ color: "var(--ink)", fontWeight: 600 }}>
                      {children}
                    </strong>
                  ),
                }}
              >
                {content}
              </ReactMarkdown>
            </div>
          ) : (
            <p
              className="text-[13px] leading-relaxed"
              style={{ color: "var(--ink-subtle)" }}
            >
              Occupella is still learning about your company. Once your Buildium
              account is connected and a few documents are uploaded, a short
              profile will appear here — and Occupella will use it to follow how
              your business works.
            </p>
          )}
        </div>
      )}
    </>
  );
}

// ── Connectors ──────────────────────────────────────────────────────

interface OpenDetail {
  connectorId: string;
  capability: ConnectorCapability;
}

function ConnectorsPanel() {
  const companyRole = useStore((s) => s.companyRole);
  const [connectors, setConnectors] = useState<Connector[] | null>(null);
  const [requesting, setRequesting] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [openDetail, setOpenDetail] = useState<OpenDetail | null>(null);

  const reloadConnectors = () => {
    setLoadError(null);
    console.info("[connectors] GET /connectors start");
    return listConnectors()
      .then((rows) => {
        console.info("[connectors] GET /connectors 2xx (%d rows)", rows.length);
        setConnectors(rows);
      })
      .catch((err: unknown) => {
        const message =
          err instanceof Error ? err.message : "Failed to load connectors";
        console.error("[connectors] GET /connectors failed", err);
        setLoadError(message);
        toast.error(`Failed to load connectors: ${message}`);
      });
  };

  useEffect(() => {
    void reloadConnectors();
  }, []);

  if (connectors === null && loadError === null) {
    return (
      <div className="flex justify-center py-10">
        <span className="spinner" />
      </div>
    );
  }

  if (connectors === null && loadError !== null) {
    return (
      <div className="flex flex-col items-start gap-3 py-10">
        <div
          className="text-[14px] font-medium"
          style={{ color: "var(--ink)" }}
        >
          Couldn&rsquo;t load connectors.
        </div>
        <div
          className="text-[13px] leading-snug"
          style={{ color: "var(--ink-muted)" }}
        >
          {loadError}
        </div>
        <button
          type="button"
          onClick={() => void reloadConnectors()}
          className="h-9 px-3 rounded-[var(--r-sm)] text-[13px] font-medium"
          style={{ background: "var(--iris)", color: "var(--canvas)" }}
        >
          Try again
        </button>
      </div>
    );
  }

  // After the two early returns above, ``connectors`` is non-null.
  //
  // ⚠ PER-ROW, not per-section (gotcha 85). The Buildium connection is the
  // company's books plus its API credentials and webhook secret, so it is
  // admin-only — and the reads are gated server-side too (a UI gate cannot
  // bind an API). Everything else here is a per-USER OAuth grant: a member
  // connects their own Gmail, their own Calendar, their own Drive, and those
  // are the connectors a member account exists to use.
  //
  // companyRole is null while the role probe is in flight, so a strict
  // equality check hides Buildium until it resolves — a row that flashes in
  // and then disappears is the same leak with worse timing.
  const rows: Connector[] = visibleConnectorRows(
    connectors ?? [],
    companyRole,
  );
  const apiRows = rowsByCapability(rows, "api");
  const webhookRows = rowsByCapability(rows, "webhook");

  const open = openDetail
    ? rows.find((c) => c.id === openDetail.connectorId)
    : undefined;

  const closeDetail = () => {
    setOpenDetail(null);
    void reloadConnectors();
  };

  return (
    <>
      {/* ⚠ NO SectionHeader here (founder call, 2026-09-09). The rail entry
          already says Connectors, and the header carried "The accounts
          Occupella reads from and acts in on your behalf" directly above
          "APIs — Accounts Occupella reads and writes through": the same
          sentence twice, before a single row. The capability headings below
          are the structure this pane actually needs. */}
      <CapabilitySection
        heading="APIs"
        sub="Accounts Occupella reads and writes through."
        rows={apiRows}
        capability="api"
        onOpen={(c) => setOpenDetail({ connectorId: c.id, capability: "api" })}
      />

      <CapabilitySection
        heading="Webhooks"
        sub="Inbound events from your systems into Occupella."
        rows={webhookRows}
        capability="webhook"
        onOpen={(c) =>
          setOpenDetail({ connectorId: c.id, capability: "webhook" })
        }
      />

      {/* Most rows on this page are "coming soon", which tells the user what
          we have not built and gives them nowhere to say which one they
          actually need. This is that channel — the roadmap question asked at
          the moment they are looking at the gap. */}
      <div
        className="mt-6 flex items-center gap-3 rounded-[var(--r-md)] px-4 py-3"
        style={{
          background: "var(--canvas-raised)",
          border: "1px solid var(--line)",
        }}
        data-testid="connector-request-row"
      >
        <div className="min-w-0">
          <div
            className="text-[13px] font-medium"
            style={{ color: "var(--ink)" }}
          >
            Missing something you use?
          </div>
          <div className="text-[12px]" style={{ color: "var(--ink-muted)" }}>
            Tell us which integration to build next.
          </div>
        </div>
        <button
          type="button"
          onClick={() => setRequesting(true)}
          data-testid="connector-request-open"
          className="ml-auto shrink-0 inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium"
          style={{
            borderColor: "var(--line-strong)",
            background: "var(--canvas)",
            color: "var(--ink)",
          }}
        >
          <Send size={13} strokeWidth={1.75} />
          Request an integration
        </button>
      </div>

      {requesting && (
        <FeedbackDialog
          onClose={() => setRequesting(false)}
          preset={{
            category: "idea",
            // The triage tag. See FeedbackDialog's note on why this is not a
            // fourth `category` value.
            view: "connectors",
            title: "Request an integration",
            placeholder:
              "Which system should Occupella connect to, and what would you want it to do there?",
            hint: "Goes to the Occupella team with the page you sent it from. No reply by default \u2014 say so if you want one.",
            lockCategory: true,
          }}
        />
      )}

      {open && openDetail?.capability === "api" && open.id === "buildium" && (
        <BuildiumApiDetail onClose={closeDetail} onChange={reloadConnectors} />
      )}
      {open && openDetail?.capability === "api" && open.id === "rentvine" && (
        <RentvineApiDetail onClose={closeDetail} onChange={reloadConnectors} />
      )}
      {open && openDetail?.capability === "api" && open.id === "rentmanager" && (
        <RentManagerApiDetail onClose={closeDetail} onChange={reloadConnectors} />
      )}
      {open && openDetail?.capability === "api" && open.id === "propertyware" && (
        <PropertywareApiDetail onClose={closeDetail} onChange={reloadConnectors} />
      )}
      {open && openDetail?.capability === "api" && open.id === "doorloop" && (
        <DoorLoopApiDetail onClose={closeDetail} onChange={reloadConnectors} />
      )}
      {open &&
        openDetail?.capability === "webhook" &&
        open.id === "buildium" && (
          <BuildiumWebhookDetail
            onClose={closeDetail}
            onChange={reloadConnectors}
            onOpenApi={() =>
              setOpenDetail({ connectorId: "buildium", capability: "api" })
            }
          />
        )}
      {/* PR #131 — Composio-managed connectors (Google Calendar /
          Drive, Gmail, Slack) get the OAuth-init panel. The PMS rows
          have their own panels above; anything else keeps the "coming
          soon" placeholder. */}
      {open &&
        openDetail &&
        !isPmsConnector(open.id) &&
        isComposioManaged(open.id) &&
        openDetail.capability === "api" && (
          <ComposioOAuthDetail
            connector={open}
            onClose={closeDetail}
            onChange={reloadConnectors}
          />
        )}
      {open &&
        openDetail &&
        !isPmsConnector(open.id) &&
        !(isComposioManaged(open.id) && openDetail.capability === "api") && (
          <ComingSoonDetail
            connectorName={open.name}
            capability={openDetail.capability}
            onClose={closeDetail}
          />
        )}
    </>
  );
}

function CapabilitySection({
  heading,
  sub,
  rows,
  capability,
  onOpen,
}: {
  heading: string;
  sub: string;
  rows: Connector[];
  capability: ConnectorCapability;
  onOpen: (c: Connector) => void;
}) {
  if (rows.length === 0) return null;

  const byCategory = rows.reduce<Record<ConnectorCategory, Connector[]>>(
    (acc, c) => {
      (acc[c.category] = acc[c.category] ?? []).push(c);
      return acc;
    },
    { property_management: [], listings: [], communication: [], calendar: [] },
  );

  return (
    <section className="mb-8">
      <div
        className="mb-3 pb-2 border-b"
        style={{ borderColor: "var(--line)" }}
      >
        <h2
          className="text-[16px] font-semibold"
          style={{ color: "var(--ink)", letterSpacing: "-0.01em" }}
        >
          {heading}
        </h2>
        <div
          className="text-[13px] mt-0.5"
          style={{ color: "var(--ink-muted)" }}
        >
          {sub}
        </div>
      </div>

      {(Object.keys(byCategory) as ConnectorCategory[]).map((cat) => {
        const items = byCategory[cat];
        if (items.length === 0) return null;
        return (
          <div key={`${capability}-${cat}`} className="mb-5">
            <div
              className=" uppercase mb-2.5"
              style={{
                fontSize: 12,
                letterSpacing: "0.14em",
                color: "var(--ink-subtle)",
              }}
            >
              {CATEGORY_LABELS[cat]}
            </div>
            <div className="flex flex-col gap-2">
              {items.map((c) => (
                <ConnectorRow
                  key={`${capability}-${c.id}`}
                  connector={c}
                  capability={capability}
                  onClick={() => onOpen(c)}
                />
              ))}
            </div>
          </div>
        );
      })}
    </section>
  );
}

function ConnectorRow({
  connector,
  capability,
  onClick,
}: {
  connector: Connector;
  capability: ConnectorCapability;
  onClick: () => void;
}) {
  // One home, shared with the OAuth detail panel (connectorRowState). A lapsed
  // grant surfaces on the list so a user notices without opening the panel;
  // `stateUnknown` — we could not READ the state, a fact about this deployment
  // and not about the customer — already outranks it inside the helper, so
  // nothing here restates the precedence.
  const { isConnected, needsReauth, stateUnknown } = connectorRowState(
    connector,
    capability,
  );

  const description =
    (capability === "api"
      ? connector.apiDescription
      : connector.webhookDescription) ?? connector.description;

  return (
    <div
      className="flex items-center gap-3 px-3.5 py-3 rounded-[var(--r-md)] border"
      style={{
        background: "var(--canvas)",
        // Muted --positive tint, not the raw #4ade80 neon DESIGN.md bans; the
        // "Connected" badge already carries the status, so this is a whisper.
        borderColor: isConnected
          ? "color-mix(in srgb, var(--positive) 30%, var(--line))"
          : needsReauth
            ? "color-mix(in srgb, var(--caution) 40%, var(--line))"
            : "var(--line)",
      }}
    >
      <ConnectorLogo id={connector.id} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span
            className="text-[14px] font-medium"
            style={{ color: "var(--ink)" }}
          >
            {connector.name}
          </span>
          {isConnected && (
            <span
              className=" uppercase px-1.5 py-0.5 rounded-[var(--r-xs)]"
              style={{
                fontSize: 12,
                letterSpacing: "0.14em",
                background: "var(--positive-bg)",
                color: "var(--positive)",
              }}
            >
              Connected
            </span>
          )}
          {needsReauth && (
            <span
              className=" uppercase px-1.5 py-0.5 rounded-[var(--r-xs)]"
              style={{
                fontSize: 12,
                letterSpacing: "0.14em",
                background: "var(--caution-bg)",
                color: "var(--caution)",
              }}
            >
              Reconnect
            </span>
          )}
          {stateUnknown && (
            <span
              className=" uppercase px-1.5 py-0.5 rounded-[var(--r-xs)]"
              style={{
                fontSize: 12,
                letterSpacing: "0.14em",
                background: "var(--canvas-raised)",
                color: "var(--ink-muted)",
              }}
            >
              Status unavailable
            </span>
          )}
        </div>
        <div
          className="text-[12px] mt-0.5 leading-snug truncate"
          style={{ color: "var(--ink-muted)" }}
        >
          {stateUnknown
            ? "We can't check this connection right now — that's a setup problem on our side, not something wrong with your account."
            : description}
        </div>
      </div>
      <button
        type="button"
        onClick={onClick}
        disabled={stateUnknown}
        className="h-8 px-3 rounded-[var(--r-sm)] text-[13px] font-medium transition-colors"
        style={{
          background:
            isConnected || stateUnknown ? "var(--canvas-raised)" : "var(--iris)",
          color: stateUnknown
            ? "var(--ink-muted)"
            : isConnected
              ? "var(--ink)"
              : "white",
          border: `1px solid ${isConnected || stateUnknown ? "var(--line)" : "var(--iris)"}`,
          cursor: stateUnknown ? "not-allowed" : undefined,
        }}
      >
        {stateUnknown
          ? "Unavailable"
          : isConnected
            ? "Manage"
            : needsReauth
              ? "Reconnect"
              : "Connect"}
      </button>
    </div>
  );
}

// ── Notifications ──────────────────────────────────────────────────

// ── Preferences ────────────────────────────────────────────────────

// ── Toggle ──────────────────────────────────────────────────────────
