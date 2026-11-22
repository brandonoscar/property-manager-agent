/**
 * Root shell — two-zone OS layout.
 *
 *   ┌──────────────────────────────────────────────────────┐
 *   │   TitleBar (32px)                                    │
 *   ├──────────┬───────────────────────────────────────────┤
 *   │ Sidebar  │                                           │
 *   │  260px   │   Main surface (routed view)              │
 *   │          │                                           │
 *   └──────────┴───────────────────────────────────────────┘
 *
 * Tool cards + approval gates render inline in the conversation
 * (and on each page's own detail pane) — no separate rail. Mobile
 * drawer behavior for the sidebar is preserved.
 */

import { useEffect, useState } from 'react';
import { Toaster, toast } from 'sonner';
import { Sidebar } from './components/Sidebar';
import { ChatThread } from './components/ChatThread';
import { CommandPalette } from './components/CommandPalette';
import { InputBar } from './components/InputBar';
import { LoginForm } from './components/LoginForm';
import { BootScreen, BOOT_MIN_MS, useMinimumVisible } from './components/BootScreen';
import { TrialBanner } from './components/TrialBanner';
import { InvitesBanner } from './components/InvitesBanner';
import { ServiceBanner } from './components/ServiceBanner';
import { SessionBanner } from './components/SessionBanner';
import {
  CHECKOUT_MESSAGE,
  checkoutOutcome,
  clearCheckoutParam,
} from './services/checkoutReturn';
import { useDocumentTitle } from './hooks/useDocumentTitle';
import { consumeSignOutIntent, rememberReturnView, takeReturnView } from './services/sessionEnd';
import { SetPasswordForm } from './components/SetPasswordForm';
import { TwoFactorGate, useTwoFactorGate } from './components/TwoFactorGate';
import { InboxPage } from './components/InboxPage';
import { DevRunsPage } from './components/DevRunsPage';
import { OperationsPage } from './components/OperationsPage';
import { CrmPage } from './components/CrmPage';
import { LeasingComplianceGate } from './components/LeasingComplianceGate';
import { SearchPage } from './components/SearchPage';
import { SettingsPage } from './components/SettingsPage';
import { TitleBar } from './components/TitleBar';
import { useNavChord } from './hooks/useNavChord';
import { useUrlSync } from './hooks/useUrlSync';
import { useInboxCount } from './hooks/useInboxCount';
import {
  supabase,
  supabaseConfigured,
  ensureCompanyProvisioned,
  readStoredSession,
  demoMode,
  DEMO_USER,
  DEMO_SESSION,
  DEMO_COMPANY_ID,
  type Session,
  type User,
} from './services/auth';
import {
  captureViewChanged,
  identifyUser,
  resetIdentity,
} from './services/analytics';
import { clearSentryUser, setSentryUser } from './services/sentry';
import { pendingPasswordFlow } from './services/authFlow';
import { useStore } from './stores/store';

/**
 * Chat view — renders ChatThread. When there's an active thread the
 * bottom-pinned InputBar stays; when it's the empty hero the input
 * bar is rendered inline inside the hero column (by ChatThread) so
 * the whole hero reads as a single centered stack.
 */
function ChatView() {
  const activeThreadId = useStore((s) => s.activeThreadId);
  return (
    <div className="flex flex-1 min-h-0 overflow-hidden">
      {/* The quick-actions left rail was removed (founder call, 2026-07-14):
          it crowded the chat page. The under-composer QUICK_ACTIONS chips in
          InputBar remain the cold-start affordance. */}
      <div className="flex flex-1 min-w-0 flex-col overflow-hidden">
        {/* First-run checklist (BR #6) renders INSIDE ChatThread's empty
            hero column now (Stage 3) — one centered column, no top band. */}
        <ChatThread />
        {activeThreadId && <InputBar />}
      </div>
    </div>
  );
}

export function App() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // ⚠ Close the drawer when the window grows past the mobile breakpoint.
  // Found by rotating a phone in a real browser: open the menu at 375, turn
  // the handset (many phones are wider than 768 in landscape, so the drawer
  // becomes the permanent rail — correct), turn it back, and the drawer is
  // still `open`, now covering the app. The user gets a menu they do not
  // remember opening and has to dismiss it.
  //
  // Above the breakpoint `open` means nothing — the rail is always there —
  // so clearing it on the way out is free, and it is the only moment we can
  // tell the two states apart. 767px is Tailwind's `md`, matched to the
  // `max-md:` classes on the drawer itself.
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 767px)');
    const onChange = (e: MediaQueryListEvent) => {
      if (!e.matches) setSidebarOpen(false);
    };
    mq.addEventListener('change', onChange);
    return () => mq.removeEventListener('change', onChange);
  }, []);
  // The URL is app state. Before this the whole product lived at one address:
  // Back left it entirely, "look at the Leasing pipeline" could not be a link,
  // a refresh dropped you on Chat, and three open tabs were three copies of
  // the same URL. See lib/routing.ts for why the paths are what they are.
  useUrlSync();
  // If this page was opened from a Supabase invite / password-recovery
  // email link, show the set-password screen instead of dropping the user
  // straight into the app (otherwise an invited user never sets a password
  // and can't sign back in). Snapshotted at import, before Supabase strips
  // the URL hash. Demo mode never has a real invite link.
  const [passwordFlow, setPasswordFlow] = useState(() =>
    demoMode ? null : pendingPasswordFlow(),
  );
  const user = useStore((s) => s.user);
  const view = useStore((s) => s.view);
  // ⚠ Runs on every render, before any early return — a hook behind a
  // conditional is a rules-of-hooks violation, and the checks below are all
  // early returns.
  const [mfaPassed, setMfaPassed] = useState(false);
  const mfaPhase = useTwoFactorGate(user?.id ?? null);
  // The tab strip is the only place three open Occupella tabs differ.
  useDocumentTitle(view, Boolean(user));
  const isAuthLoading = useStore((s) => s.isAuthLoading);
  const booting = useMinimumVisible(isAuthLoading, BOOT_MIN_MS);
  const setAuth = useStore((s) => s.setAuth);
  const setCompanies = useStore((s) => s.setCompanies);
  const clearAuth = useStore((s) => s.clearAuth);

  // Vim-style g-chord navigation. The hook itself ignores keys when focus
  // is in an editable element; passing disabled while signed-out keeps
  // the chord from firing on the marketing/login surface.
  useNavChord({ disabled: !user });

  // The sidebar's Inbox badge. Gated on a signed-in user for the obvious
  // reason and on `!passwordFlow` for a subtler one: during the invite /
  // recovery screen there is a session but the person has not finished
  // becoming themselves yet, and a count is neither useful nor theirs.
  useInboxCount(Boolean(user) && !passwordFlow);

  // Acknowledge a return from Stripe Checkout.
  //
  // ⚠ Nothing did, and it is the worst moment for silence: the customer has
  // just paid, has no receipt from us, and the billing screen very likely still
  // says "Free trial" because our row is written by the webhook rather than by
  // the redirect. The natural next action — click the plan again — used to
  // start a SECOND subscription.
  //
  // Mounted at the ROOT rather than in BillingPanel because `success_url` is
  // the app's base URL: the customer lands wherever they last were, which is
  // usually not the billing page.
  useEffect(() => {
    const outcome = checkoutOutcome(window.location.search);
    if (!outcome) return;
    // Clear FIRST. Left in place, every refresh re-announces a purchase that
    // happened once, and a shared link tells somebody else they subscribed.
    clearCheckoutParam();
    const { title, description } = CHECKOUT_MESSAGE[outcome];
    if (outcome === 'success') toast.success(title, { description, duration: 10000 });
    else toast.info(title, { description });
  }, []);

  useEffect(() => {
    if (demoMode) {
      setAuth(DEMO_USER, DEMO_SESSION);
      setCompanies([DEMO_COMPANY_ID]);
      identifyUser(
        DEMO_USER.id,
        DEMO_USER.email ?? undefined,
        DEMO_COMPANY_ID,
      );
      setSentryUser(DEMO_USER.id);
      return;
    }

    // Check existing session on mount. Read directly from the
    // localStorage blob Supabase persists — going through
    // ``supabase.auth.getSession()`` here acquires the auth Web Lock
    // and races against the auto-refresh loop, leaving the page blank
    // when the lock gets stolen. The blob is the same source of truth
    // ``getSession()`` consults internally.
    const stored = readStoredSession();
    if (stored?.user && stored.access_token) {
      const session = stored as unknown as Session;
      const sessionUser = stored.user as User;
      setAuth(sessionUser, session);
      // PR #74 — ensureCompanyProvisioned self-heals if the
      // company_users row is missing (pre-0012 signup, trigger
      // race). Falls through to fetchCompanyIds when the row is
      // already there.
      ensureCompanyProvisioned().then((ids) => {
        setCompanies(ids);
        identifyUser(sessionUser.id, sessionUser.email, ids[0] || '');
        setSentryUser(sessionUser.id);
      });
    } else {
      setAuth(null, null);
    }

    // Subscribe to auth state changes
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange(async (event, session) => {
      if (event === 'SIGNED_IN' && session) {
        setAuth(session.user, session);
        // Land them back where the session ended, not on the default view.
        // Consumed on read, so this only ever fires for the sign-in that
        // directly follows an involuntary sign-out.
        const returnView = takeReturnView();
        if (returnView) {
          useStore
            .getState()
            .setView(returnView as Parameters<ReturnType<typeof useStore.getState>['setView']>[0]);
        }
        // PR #74 — same self-heal on fresh sign-in. If the new
        // user's company_users row didn't get created by the
        // trigger, ensureCompanyProvisioned hits the backend
        // bootstrap endpoint and retries.
        const companyIds = await ensureCompanyProvisioned();
        setCompanies(companyIds);
        identifyUser(session.user.id, session.user.email, companyIds[0] || '');
        setSentryUser(session.user.id);
      } else if (event === 'SIGNED_OUT') {
        // A sign-out the user did NOT ask for — a session revoked from
        // another device, a refresh that finally failed. Silently rendering
        // the login screen is indistinguishable from the product crashing
        // and binning their work, so say what happened and come back here.
        if (!consumeSignOutIntent()) {
          rememberReturnView(useStore.getState().view);
          toast.error('Your session ended. Sign in again to pick up where you left off.', {
            duration: 12000,
          });
        }
        resetIdentity();
        clearSentryUser();
        clearAuth();
      } else if (event === 'TOKEN_REFRESHED' && session) {
        setAuth(session.user, session);
      }
    });

    return () => subscription.unsubscribe();
  }, [setAuth, setCompanies, clearAuth]);

  // Track view changes in PostHog so "view changed" funnels work.
  useEffect(() => {
    captureViewChanged(view);
  }, [view]);

  const toaster = (
    <Toaster
      position="top-right"
      theme="dark"
      toastOptions={{
        style: {
          background: 'var(--canvas-elevated)',
          border: '1px solid var(--line)',
          color: 'var(--ink)',
        },
      }}
    />
  );

  // Show config error if Supabase env vars are missing (and not in demo mode)
  if (!supabaseConfigured && !demoMode) {
    return (
      <div
        className="flex items-center justify-center h-screen"
        style={{ background: 'var(--canvas)' }}
      >
        {toaster}
        {/* ⚠ No skip link here on purpose. This branch renders one card and
            has neither a sidebar to skip nor a `#main` to land on — the copy
            that used to sit here was a focusable control pointing at an
            anchor that does not exist. The real one is in the app render
            below, which is where the ~20 nav stops actually are. */}
        <div
          className="w-full max-w-[400px] text-center rounded-[var(--r-lg)] border"
          style={{
            background: 'var(--canvas-elevated)',
            borderColor: 'var(--line)',
            padding: '40px 32px',
            boxShadow: 'var(--shadow-md)',
          }}
        >
          <h1
            className="text-[28px] font-bold tracking-tight mb-2"
            style={{ color: 'var(--ink)' }}
          >
            Occupella
          </h1>
          <p style={{ color: 'var(--danger)' }}>Configuration Error</p>
          <p
            className="text-[13px] mt-2"
            style={{ color: 'var(--ink-muted)' }}
          >
            Missing <code>VITE_SUPABASE_URL</code> or{' '}
            <code>VITE_SUPABASE_ANON_KEY</code>.
          </p>
        </div>
      </div>
    );
  }

  // Invite / password-recovery landing — must win over the normal auth
  // routing, because the invite token signs the user in (so `user` is set
  // and the app would otherwise render) before they've chosen a password.
  if (passwordFlow) {
    return (
      <>
        {toaster}
        <SetPasswordForm flow={passwordFlow} onDone={() => setPasswordFlow(null)} />
      </>
    );
  }

  // Boot screen while checking auth. `booting` outlasts `isAuthLoading` by
  // up to BOOT_MIN_MS so the state is actually seen — see BootScreen.tsx.
  if (booting) {
    return (
      <>
        {toaster}
        <BootScreen />
      </>
    );
  }

  // Show login if not authenticated
  if (!user) {
    return (
      <>
        {toaster}
        <LoginForm />
      </>
    );
  }

  // ⚠ BETWEEN sign-in and the app, and it has to be an EARLY RETURN rather
  // than an overlay. Supabase issues an aal1 session on the password alone
  // even with a factor enrolled, so without this the Settings toggle would
  // say "On" and stop nothing. Rendering the shell behind a modal would be
  // the same leak with a curtain in front of it.
  if (mfaPhase === 'checking' && !mfaPassed) {
    return (
      <div
        className="flex items-center justify-center h-screen"
        style={{ background: 'var(--canvas)' }}
      >
        {toaster}
        <span className="spinner" />
      </div>
    );
  }
  if (mfaPhase === 'needs-code' && !mfaPassed) {
    return (
      <>
        {toaster}
        <TwoFactorGate onPassed={() => setMfaPassed(true)} />
      </>
    );
  }

  return (
    <div
      className="app-layout relative z-[1] flex flex-col h-screen w-screen"
      style={{ background: 'var(--canvas)' }}
    >
      {toaster}

      {/* ⚠ This existed and was in the WRONG BRANCH. The only copy sat inside
          the "Supabase not configured" error screen, so the comment there —
          "it is the first stop for everyone else" — described a guarantee the
          product did not have: the main app render had none, and a keyboard
          user walked all ~20 sidebar controls before reaching the page, on
          every page, every time. Measured by tabbing the running app: stop #1
          was the sidebar's Chat button.
          Visually hidden by `top: -100px` rather than `display: none`, which
          is the whole technique — a hidden element is not focusable, so the
          usual way of hiding this is also the way of breaking it. */}
      <a href="#main" className="skip-link" data-testid="skip-to-content">
        Skip to content
      </a>

      {/* ⚠ ABOVE the trial bar, which is itself above the invite nudge. The
          existing ordering rule is "the one that costs money to miss reads
          first"; this one outranks even that, because while the backend is
          unreachable NOTHING the other two ask for can be done — you cannot
          pay, invite, or save. It also renders nothing almost always. */}
      <ServiceBanner />

      {/* ⚠ BESIDE the reachability strip rather than inside it, and ordered
          after it for the same reason that one outranks the trial bar: while
          the device has no network, nothing can be signed into either, and
          the offline sentence is the one the reader can check for themselves.
          The two are independent facts — a 401 PROVES the backend is up — so
          they can both be true, and neither is allowed to hide the other. */}
      <SessionBanner />

      {/* Subscription state — renders nothing on a healthy paid plan. */}
      <TrialBanner />

      {/* An invite waiting for this account's answer (migration 0112). Nothing
          of theirs changes until they accept; renders nothing almost always. */}
      <InvitesBanner />

      {/* The invite nudge that sat here (InviteTeamBanner) is gone: "Invite a
          teammate" is a step in the Getting started checklist, so the trial
          bar is the only strip above the app (brief, 2026-09-29). */}

      {/* Mobile sidebar overlay */}
      <div
        className={`fixed inset-0 z-[99] bg-black/50 md:hidden ${
          sidebarOpen ? 'block' : 'hidden'
        }`}
        onClick={() => setSidebarOpen(false)}
      />

      <div className="flex flex-1 min-h-0">
        {/* The navy sidebar runs the FULL height of the window — the
            title bar sits beside it, not above it, so no white ever
            crosses over the navy island (founder call, 2026-08-18).
            That also means the bar's wordmark moved INTO the sidebar;
            see Sidebar's masthead row. */}
        <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

        <div className="flex flex-1 min-w-0 flex-col overflow-hidden">
          {/* onOpenNav renders the mobile-only hamburger — the sole way
              to reach navigation below md, where the sidebar is an
              off-canvas drawer (2026-08-15 audit, debt #1). Scrim above
              + Sidebar's own onClose handle dismissal. */}
          <TitleBar onOpenNav={() => setSidebarOpen(true)} />

          <main
            id="main"
            tabIndex={-1}
            className="relative z-[1] flex flex-1 min-w-0 flex-col overflow-hidden"
          >
            {view === 'chat' && <ChatView />}
            {view === 'inbox' && <InboxPage />}
            {view === 'properties' && <OperationsPage />}
            {/* Leasing works leads over SMS, so the section stays closed
                until texting is legally allowed on this account. The panel
                is the explanation; the backend refuses the send
                (comms/sms.py::outbound_channel_block). CrmPage itself is
                untouched — the gate wraps it. */}
            {view === 'crm' && (
              <LeasingComplianceGate>
                <CrmPage />
              </LeasingComplianceGate>
            )}
            {view === 'search' && <SearchPage />}
            {view === 'settings' && <SettingsPage />}
            {view === 'dev-runs' && <DevRunsPage />}
          </main>
        </div>
      </div>

      <CommandPalette />
    </div>
  );
}
