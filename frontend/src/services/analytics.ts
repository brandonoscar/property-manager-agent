/**
 * PostHog analytics — thin wrapper for Occupella-specific events.
 *
 * PostHog is initialized in main.tsx via PostHogProvider. This module
 * provides typed capture helpers so components don't import posthog-js
 * directly.
 *
 * All helpers early-return when VITE_PUBLIC_POSTHOG_KEY isn't set, so
 * analytics can never break the app.
 *
 * # Sensitive-input convention
 *
 * Add `class="ph-no-capture"` (PostHog's built-in opt-out class) to any
 * element holding sensitive input — API keys, passwords, signing
 * secrets, OAuth tokens. PostHog's autocapture skips elements with that
 * class anywhere in the DOM ancestor chain. Session-recording masking
 * is also configured below to mask text inside `.ph-no-capture` and
 * `[data-ph-no-capture]` regions.
 *
 * Examples:
 *   <input type="password" className="… ph-no-capture" />
 *   <div data-ph-no-capture>{secret}</div>
 */

import posthog from 'posthog-js';

const posthogKey = import.meta.env.VITE_PUBLIC_POSTHOG_KEY as string;

/**
 * PostHog's US ingestion host, used unless VITE_PUBLIC_POSTHOG_HOST names
 * another. posthog-js loads its lazy scripts (web vitals, remote config,
 * surveys, the toolbar) from the matching assets host, and frontend/vercel.json's
 * `script-src` allows exactly that one: `csp.test.ts` asks posthog-js itself
 * which host it is. ⚠ Pointing VITE_PUBLIC_POSTHOG_HOST elsewhere (a reverse
 * proxy, the EU region) needs the CSP changed in the same deploy, or those
 * scripts are refused (the app itself still runs).
 */
export const POSTHOG_DEFAULT_HOST = 'https://us.i.posthog.com';

export const posthogHost =
  (import.meta.env.VITE_PUBLIC_POSTHOG_HOST as string) || POSTHOG_DEFAULT_HOST;

export const posthogConfigured = Boolean(posthogKey);

// Session-recording privacy posture. Recordings capture rendered DOM, which for
// a property manager includes tenant/owner names, addresses, and email bodies —
// mask all text + element attributes so only layout/interactions are sent to
// PostHog, never content. Exported + asserted in analytics.test.ts so it can't
// silently regress to `false`.
export const RECORDING_PRIVACY = {
  mask_all_text: true,
  mask_all_element_attributes: true,
} as const;

/**
 * Where an opt-out is remembered.
 *
 * ⚠ Deliberately OUR key rather than PostHog's own `opt_out_capturing()`
 * persistence. PostHog stores that decision in the same localStorage entry it
 * uses for everything else, so a person who opts out and then clears site data
 * is silently opted back IN — and the whole point of the control is that the
 * answer sticks. Reading our own key BEFORE `init` also means capture never
 * starts, rather than starting and being told to stop a tick later.
 */
const OPT_OUT_KEY = 'occupella_analytics_opt_out';

/** True when this browser has asked not to be measured. */
export function analyticsOptedOut(): boolean {
  try {
    return localStorage.getItem(OPT_OUT_KEY) === '1';
  } catch {
    // A browser blocking storage cannot record a preference either way. Read
    // that as "no preference" rather than as an opt-out: the honest default
    // for an unreadable setting is the one the product documents, and the
    // person still has the control in Settings.
    //
    // ⚠ THAT LAST CLAUSE WAS FALSE FROM 2026-09-08 TO 2026-09-15. The Settings
    // row was cut and `setAnalyticsOptedOut` was left with no caller outside
    // its own tests, so a person whose localStorage throws had no control
    // anywhere — while this comment, and the privacy policy, both said they
    // did. Restored as `ProductAnalyticsRow`; the guard that keeps the two
    // in step is in `settingsCopy.test.tsx`.
    return false;
  }
}

/**
 * Record the choice and act on it in the SAME call.
 *
 * ⚠ Writing the key alone would leave the current tab still capturing until
 * the next reload — the person watches a toggle move and is measured anyway,
 * which is worse than not offering the toggle.
 */
export function setAnalyticsOptedOut(optOut: boolean): void {
  try {
    if (optOut) localStorage.setItem(OPT_OUT_KEY, '1');
    else localStorage.removeItem(OPT_OUT_KEY);
  } catch {
    /* nothing to persist to; still apply it to this session below */
  }
  if (!posthogConfigured) return;
  if (optOut) posthog.opt_out_capturing();
  else posthog.opt_in_capturing();
}

export function initPostHog(): typeof posthog | null {
  if (!posthogKey) {
    // Intentional — don't noise the console on unconfigured local dev.
    return null;
  }

  posthog.init(posthogKey, {
    api_host: posthogHost,
    persistence: 'localStorage',
    // ⚠ Read BEFORE the first capture, not after. Session recording and
    // autocapture both start inside init(), so opting out afterwards would
    // still have sent the load of the page the person opted out on.
    opt_out_capturing_by_default: analyticsOptedOut(),
    // Autocapture is on, but PostHog automatically excludes any element
    // (and its descendants) carrying class="ph-no-capture" — that's the
    // documented opt-out and the rrweb session-recording default
    // (blockClass = 'ph-no-capture'). The
    // element_attribute_ignorelist below drops sensitive HTML attributes
    // from captured events even if the element itself is captured —
    // defence in depth for things like aria-label leaking a tenant's
    // name through a destructive action button.
    autocapture: {
      element_attribute_ignorelist: ['data-secret', 'data-pii', 'data-token'],
    },
    /**
     * ⚠ OFF IN CODE, NOT ONLY IN THE DASHBOARD — and the two-key rule is the
     * whole point.
     *
     * `false` (the posthog-js default, and what this said until 2026-09-15)
     * does NOT mean "record". It means "record if the project says so", and
     * the project toggle lives in a web UI one click away from anyone who
     * opens it to look at something else. So what the product collects from
     * a signed-in property manager — every screen holding tenant names,
     * addresses and email bodies — hung on a dashboard setting with no code
     * change, no review, and no matching edit to privacy.html.
     *
     * `true` is a hard client-side off: the SDK never starts, whatever the
     * project says. Turning recording ON now takes BOTH the project toggle
     * and this line, and this line is in a diff somebody reads.
     *
     * To enable it, in ONE change: flip this to `false`, enable session
     * replay in the PostHog project, and add the disclosure to
     * `frontend/public/legal/privacy.html`. The third step is not optional
     * and not a follow-up — `tests/unit/test_session_recording_matches_the_
     * policy.py` fails until the policy and this line agree.
     */
    disable_session_recording: true,
    capture_performance: true,
    /**
     * Never send rendered content — see RECORDING_PRIVACY above.
     *
     * ⚠ INERT WHILE THE LINE ABOVE IS `true`, AND KEPT ANYWAY. It is the
     * masking that makes enabling recording a one-line change rather than a
     * design question, and deleting an unused config is exactly how the flip
     * later arrives unmasked. Its own test still pins both flags.
     */
    ...RECORDING_PRIVACY,
  });

  return posthog;
}

// ── Identity ────────────────────────────────────────────────────────

/**
 * Call after login. Links all future events to this user + company.
 */
export function identifyUser(
  userId: string,
  email: string | undefined,
  companyId: string,
): void {
  if (!posthogConfigured) return;
  posthog.identify(userId, { email });
  if (companyId) {
    posthog.group('company', companyId);
  }
}

/**
 * Call on logout. Clears identity so the next user on this device
 * starts with a fresh anonymous profile.
 */
export function resetIdentity(): void {
  if (!posthogConfigured) return;
  posthog.reset();
}

// ── Custom events ───────────────────────────────────────────────────

export function captureTaskSubmitted(taskLength: number): void {
  if (!posthogConfigured) return;
  posthog.capture('task submitted', { task_length: taskLength });
}

export function captureTaskCompleted(durationMs: number): void {
  if (!posthogConfigured) return;
  posthog.capture('task completed', { duration_ms: durationMs });
}

export function captureTaskFailed(error: string): void {
  if (!posthogConfigured) return;
  posthog.capture('task failed', { error: error.slice(0, 200) });
}

export function captureConfirmation(approved: boolean): void {
  if (!posthogConfigured) return;
  posthog.capture('confirmation resolved', { approved });
}

export function captureViewChanged(view: string): void {
  if (!posthogConfigured) return;
  posthog.capture('view changed', { view });
}

export function captureThreadCreated(): void {
  if (!posthogConfigured) return;
  posthog.capture('thread created');
}

/** Fired when a web-app user clicks a desktop-app download button (the Search
 *  gate). `available=false` means they clicked before a release was published. */
export function captureDesktopDownloadClicked(
  platform: string,
  available: boolean,
): void {
  if (!posthogConfigured) return;
  posthog.capture('desktop download clicked', { platform, available });
}

// ── Onboarding funnel ───────────────────────────────────────────────

export type SignupMethod = 'email' | 'oauth_google' | 'oauth_github' | 'oauth_microsoft';

export function captureSignupStarted(method: SignupMethod = 'email'): void {
  if (!posthogConfigured) return;
  posthog.capture('signup_started', { method });
}

export function captureSignupCompleted(
  method: SignupMethod,
  companyId?: string,
): void {
  if (!posthogConfigured) return;
  // The real company id isn't known client-side at signup (it's created
  // server-side by a DB trigger, migration 0006). Only attach company_id when a
  // real one is passed — never a `user-<id>` placeholder, which would pollute
  // the PostHog `company` group with phantom rows and break the funnel's
  // group-by-company. Server-side `agent run started` carries the true group.
  const props = companyId ? { method, company_id: companyId } : { method };
  posthog.capture('signup_completed', props);
}

export function captureApiKeyAdded(connector: string): void {
  if (!posthogConfigured) return;
  posthog.capture('api_key_added', { connector });
}

export function captureWebhookConfigured(provider: string): void {
  if (!posthogConfigured) return;
  posthog.capture('webhook_configured', { provider });
}

/** A Composio-managed OAuth connector (Gmail / Calendar / Drive) finished
 *  connecting. ``connector`` is the connector id (e.g. "gmail"). Powers the
 *  "connect Google" step of the activation funnel. */
export function captureConnectorConnected(connector: string): void {
  if (!posthogConfigured) return;
  posthog.capture('connector connected', { connector });
}

// ── Command palette + nav chords ────────────────────────────────────

export function capturePaletteOpened(trigger: 'keyboard' | 'click'): void {
  if (!posthogConfigured) return;
  posthog.capture('palette_opened', { trigger });
}

export function capturePaletteCommandExecuted(
  commandId: string,
  group: string,
): void {
  if (!posthogConfigured) return;
  posthog.capture('palette_command_executed', {
    command_id: commandId,
    group,
  });
}

export function captureNavChordUsed(chord: string, from: string): void {
  if (!posthogConfigured) return;
  posthog.capture('nav_chord_used', { chord, from });
}

// ── Feature flags ───────────────────────────────────────────────────

export function isFeatureEnabled(flagKey: string): boolean {
  if (!posthogConfigured) return false;
  return posthog.isFeatureEnabled(flagKey) ?? false;
}
