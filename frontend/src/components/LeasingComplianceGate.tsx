/**
 * Leasing setup blocker — the pipeline stays closed until texting is legal.
 *
 * The Leasing section's whole job is working leads over SMS. Two things have
 * to be true before that is allowed, and neither is a preference:
 *
 *   1. **Manager or admin.** Placing calls already requires it server-side;
 *      the reply path is the same class of outbound action on the company's
 *      registered number.
 *   2. **The A2P 10DLC campaign is approved and the channel is configured.**
 *      Registration covers every application-to-person message from that
 *      number. Sending before carrier approval is a violation, not a
 *      degraded experience.
 *
 * ⚠ **White-labeled: the customer never sees or touches the carrier.** The
 * messaging account is ours; they get a dedicated number on it. So the two
 * states split by WHOSE problem they are — an unapproved campaign is a wait
 * they should understand, an unconfigured channel is our outage and is said
 * that way. Neither one asks them for a credential, and no copy here names a
 * vendor. If you add a state, decide which side of that line it falls on
 * before you write its sentence.
 *
 * ⚠ This panel is the EXPLANATION, never the enforcement. The backend refuses
 * the send (``outbound_channel_block`` in ``comms/sms.py``, consulted by
 * ``POST /crm/leads/{id}/reply``); a UI gate cannot bind an API. Rendering
 * this without that backend check would be theatre — which is exactly what
 * the Leasing lane was before, since it checked only that credentials
 * existed.
 *
 * ⚠ Consent is deliberately NOT a blocker here. It is per-RECIPIENT and the
 * send path enforces it fail-closed; blocking the whole section on a
 * company-wide consent count would permanently lock out a new account that
 * has not texted anyone yet. It shows as an informational line instead.
 */

import { SUPPORT_EMAIL } from '../lib/support';
import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { toast } from 'sonner';

import {
  fetchSmsStatus,
  listTwilioNumbers,
  provisionTwilioNumber,
  saveTwilioNumber,
  type SmsStatus,
  type TwilioNumber,
} from '../services/sms';
import { demoMode } from '../services/auth';
import { fetchBillingStatus, hasFeature, type BillingStatus } from '../services/billing';
import { getMyRole, type TeamRole } from '../services/company';
import { A2pRegistrationForm } from './A2pRegistrationForm';
import { roleAtLeast } from '../lib/roles';
import { useStore } from '../stores/store';
import { usePermissionRefresh } from '../hooks/usePermissionRefresh';
import { Skeleton } from './Skeleton';

/** The minimum role that may work the lead pipeline. */
export const LEASING_MIN_ROLE: TeamRole = 'manager';

export type LeasingBlock =
  | 'loading'
  | 'role'
  | 'plan'
  | 'disabled'
  | 'not_configured'
  | 'campaign_pending'
  | 'campaign_failed'
  | 'no_number'
  | null;

/** Registration states that mean "submitted, now waiting on the carriers". */
const IN_REVIEW = ['submitted', 'brand_pending', 'brand_approved', 'campaign_pending'];

/**
 * The pure decision, exported so it can be tested without a DOM.
 *
 * ⚠ **`status.block` is the backend predicate's own answer, not an inference.**
 * The panel used to work out WHICH blocker fired from `enabled` + `configured`
 * — two booleans that cannot distinguish "the operator has not switched the
 * channel on" from "your campaign is still with the carriers", which need
 * opposite instructions. Re-deriving it here is how the explanation comes to
 * disagree with the refusal it is explaining.
 *
 * The one thing this DOES decide is pending-vs-failed, because both are the
 * same `block` value and only `campaign_status` separates them.
 */
export function leasingBlock(
  role: TeamRole | null,
  status: SmsStatus | null,
  numbers: TwilioNumber[] | null,
  crmIncluded: boolean | null = true,
): LeasingBlock {
  // Fail closed while ANY probe is outstanding. An unknown state that renders
  // the pipeline is open for the whole first paint.
  if (role === null || status === null || numbers === null || crmIncluded === null) {
    return 'loading';
  }
  if (!roleAtLeast(role, LEASING_MIN_ROLE)) return 'role';

  // ⚠ AFTER the role check and BEFORE every setup step, and both halves of
  // that placement matter. Telling a member to upgrade the company's plan is
  // an instruction they cannot carry out — the role is the nearer wall. And
  // walking a Starter customer through carrier registration for a section
  // their plan does not carry would have them finish a compliance flow and
  // still be refused, which is the worst of the orderings.
  //
  // ⚠ `crmIncluded` defaults TRUE, and a failed billing read passes TRUE too
  // (see the probe). Every other input here fails CLOSED because it guards a
  // carrier obligation; this one guards revenue, so it takes `gate.py`'s rule
  // 1 — a database blip must not black out a section somebody pays for.
  if (!crmIncluded) return 'plan';

  // ⚠ `block` is optional so an older backend (or a cached bundle) still
  // fails CLOSED rather than opening the pipeline: no reason plus `enabled:
  // false` lands on 'disabled', the most conservative panel.
  const reason = status.block ?? (status.enabled ? '' : 'disabled');
  if (reason === 'campaign_pending') {
    // ⚠ The backend returns ONE value for every not-yet-approved state,
    // including 'draft' and an unreadable row. Only a registration actually
    // in flight should say "waiting on the carriers" — telling someone who
    // has not filled in the form to wait is an instruction to do nothing
    // forever, so anything else falls through to the form.
    const s = status.campaign_status ?? '';
    if (s === 'failed') return 'campaign_failed';
    if (IN_REVIEW.includes(s)) return 'campaign_pending';
    return 'disabled';
  }
  if (reason === 'not_configured') return 'not_configured';
  if (reason === 'disabled') return 'disabled';
  if (!status.enabled) return 'disabled';

  // This company's OWN number. Everything above is about permission to send;
  // this is about having an identity to send from, and it is last because the
  // others cannot be fixed from here — a number can.
  if (numbers.length === 0) return 'no_number';
  return null;
}

function Step({
  done,
  title,
  children,
  testId,
}: {
  done: boolean;
  title: string;
  children: ReactNode;
  testId: string;
}) {
  return (
    <li className="flex gap-3 items-start" data-testid={testId}>
      {/* ⚠ A pending step carries a dot, never an empty ring. An empty ring
          beside a label is the shape of an unselected radio button, so the
          two waiting steps read as a choice nobody had made rather than as
          progress — which is what they are. */}
      <span
        aria-hidden
        className="shrink-0 inline-flex items-center justify-center rounded-full"
        style={{
          width: 20,
          height: 20,
          marginTop: 1,
          fontSize: 12,
          color: done ? 'var(--color-positive)' : 'var(--color-ink-subtle)',
          background: done ? 'rgba(92,189,138,0.12)' : 'var(--surface-sunken, rgba(14,22,32,0.05))',
          border: `1px solid ${done ? 'rgba(92,189,138,0.35)' : 'var(--line)'}`,
        }}
      >
        {done ? (
          '✓'
        ) : (
          <span
            style={{
              width: 5,
              height: 5,
              borderRadius: '50%',
              background: 'var(--color-ink-subtle)',
            }}
          />
        )}
      </span>
      <div className="min-w-0">
        <div
          className="font-medium"
          style={{ fontSize: 14, color: 'var(--color-ink)' }}
        >
          {title}
        </div>
        <div style={{ fontSize: 13, color: 'var(--color-ink-muted)', lineHeight: 1.5 }}>
          {children}
        </div>
      </div>
    </li>
  );
}

/**
 * "Your number rings nowhere." Shown once Leasing is open, never as a blocker.
 *
 * ⚠ The gap this closes: `forward_to_number` has been a column since 0083 and
 * was writable from nowhere, so the A2P sweep — which buys a number unattended
 * the moment carriers approve — left every white-label customer with a number
 * that sent every call to voicemail. There was no screen anywhere to change
 * it.
 *
 * ⚠ Advisory, not a gate. Voicemail is a legitimate choice, and the column
 * cannot tell "never set" from "deliberately voicemail" — so this asks, takes
 * "not now" for an answer, and never stands between anyone and their leads.
 */
function CallForwardingPrompt({
  number,
  onSaved,
}: {
  number: TwilioNumber;
  onSaved: (n: TwilioNumber) => void;
}) {
  const [value, setValue] = useState('');
  const [saving, setSaving] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  if (dismissed || (number.forward_to_number ?? '').trim()) return null;

  const save = async () => {
    setSaving(true);
    try {
      const saved = await saveTwilioNumber({
        phone_number: number.phone_number,
        forward_to_number: value,
      });
      onSaved(saved);
      toast.success(`Calls to ${number.phone_number} will ring ${saved.forward_to_number}.`);
    } catch (err) {
      // Stays on screen: a failed save must not read as a completed one.
      toast.error(
        err instanceof Error ? err.message : 'Could not save that forwarding number.',
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      data-testid="leasing-forwarding-prompt"
      className="mx-6 mt-4 rounded-[var(--r-md)] p-3.5"
      style={{ background: 'var(--info-bg)', border: '1px solid var(--line)' }}
    >
      <div style={{ fontSize: 13, color: 'var(--color-ink)', lineHeight: 1.55 }}>
        Calls to {number.phone_number} go straight to voicemail. Where should they ring?
      </div>
      <div className="flex items-center gap-2 mt-2.5 flex-wrap">
        <input
          data-testid="leasing-forwarding-input"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="(512) 555-0143"
          inputMode="tel"
          className="rounded-[var(--r-sm)] px-3 py-2"
          style={{
            fontSize: 13,
            width: 190,
            color: 'var(--color-ink)',
            background: 'var(--surface)',
            border: '1px solid var(--line)',
          }}
        />
        <button
          type="button"
          data-testid="leasing-forwarding-save"
          disabled={saving || !value.trim()}
          onClick={() => void save()}
          className="rounded-[var(--r-sm)] px-3.5 py-2 font-medium"
          style={{
            fontSize: 13,
            opacity: saving || !value.trim() ? 0.55 : 1,
            color: 'var(--color-iris-hover)',
            background: 'var(--iris-tint)',
            border: '1px solid var(--iris-ring)',
          }}
        >
          {saving ? 'Saving…' : 'Ring this number'}
        </button>
        <button
          type="button"
          data-testid="leasing-forwarding-dismiss"
          onClick={() => setDismissed(true)}
          className="rounded-[var(--r-sm)] px-3 py-2"
          style={{ fontSize: 13, color: 'var(--color-ink-muted)' }}
        >
          Voicemail is fine
        </button>
      </div>
    </div>
  );
}

export function LeasingComplianceGate({ children }: { children: ReactNode }) {
  // ⚠ Demo mode renders Leasing directly. Every other page already returns
  // fixtures in demo mode (`crm.ts` wires all ten seams, reply and call
  // included), so this gate was the one thing standing between a demo build
  // and the section it exists to describe — it probes a backend that a demo
  // build does not have, fails closed, and shows a setup checklist forever.
  //
  // This is NOT a weakening of the control. The gate's own docstring says a UI
  // gate cannot bind an API: enforcement is the backend's refusal in
  // `POST /crm/leads/{id}/reply`, which is untouched and still fails closed on
  // A2P approval. And `demoMode` is build-gated behind `VITE_DEMO_MODE`
  // (`resolveDemoMode`), which production never sets, so a prod visitor cannot
  // reach this branch with `?demo=1`.
  if (demoMode) return <>{children}</>;
  return <LiveLeasingComplianceGate>{children}</LiveLeasingComplianceGate>;
}

function LiveLeasingComplianceGate({ children }: { children: ReactNode }) {
  const companyRole = useStore((s) => s.companyRole);
  const setCompanyRole = useStore((s) => s.setCompanyRole);
  const openSettings = useStore((s) => s.openSettings);
  const [status, setStatus] = useState<SmsStatus | null>(null);
  const [numbers, setNumbers] = useState<TwilioNumber[] | null>(null);
  // ⚠ The whole status, not a boolean. The panel needs the CURRENT plan's
  // label and the cheapest plan that carries Leasing, and both come from the
  // server — a client that wrote "upgrade to Pro" itself would still say Pro
  // after the ladder shifted a rung, which is what happened on 2026-09-04.
  const [billing, setBilling] = useState<BillingStatus | null>(null);
  // Separate from `probeFailed` on purpose — that one keeps the gate SHUT,
  // this one lets it through. Two failure modes with opposite answers cannot
  // share a flag.
  const [billingFailed, setBillingFailed] = useState(false);
  const [areaCode, setAreaCode] = useState('');
  // Asked at purchase because this is the one moment a human is present. The
  // A2P sweep buys unattended on carrier approval and has nobody to ask, which
  // is why CallForwardingPrompt exists as well.
  const [forwardTo, setForwardTo] = useState('');
  const [buying, setBuying] = useState(false);
  // The purchase is confirmed in two steps on purpose: this spends money
  // (a recurring monthly charge) and a single button would make that a
  // one-click accident.
  const [confirming, setConfirming] = useState(false);
  // Distinguish "probe still running" from "probe failed". A failed probe
  // must stay blocked — it cannot prove the channel is approved — but it
  // should say so rather than spinning forever.
  const [probeFailed, setProbeFailed] = useState(false);
  // Bumped to re-run the probes. Registration is the one action inside this
  // panel that can change what the panel decides, so it needs a way to ask
  // — otherwise a submitted registration leaves a stale gate on screen
  // describing a state that has moved on.
  const [reloadKey, setReloadKey] = useState(0);
  const refresh = useCallback(() => setReloadKey((n) => n + 1), []);

  // ⚠ Clearing the cached role is the load-bearing half. The probe below
  // only runs `if (companyRole === null)`, so after a demotion the store
  // still holds the OLD role and bumping `reloadKey` alone would skip it —
  // a refresh that refreshes everything except the thing that changed.
  usePermissionRefresh(
    useCallback(() => {
      setCompanyRole(null);
      refresh();
    }, [setCompanyRole, refresh]),
  );

  useEffect(() => {
    let alive = true;
    // ``companyRole`` is only populated when SettingsPage mounts, so a user
    // who lands straight on Leasing has it null. Probe it here too; the
    // endpoint is member-callable and the store caches the result.
    if (companyRole === null) {
      getMyRole()
        .then((r) => {
          if (alive) setCompanyRole(r.role);
        })
        .catch(() => {
          if (alive) setProbeFailed(true);
        });
    }
    fetchSmsStatus()
      .then((s) => {
        if (alive) setStatus(s);
      })
      .catch(() => {
        if (alive) setProbeFailed(true);
      });
    fetchBillingStatus()
      .then((b) => {
        if (alive) setBilling(b);
      })
      .catch(() => {
        // ⚠ NOT `setProbeFailed(true)`, unlike every other probe here. Those
        // guard a carrier obligation and must stay closed when unreadable;
        // this one guards revenue, and `gate.py`'s rule 1 says a database
        // blip must never lock a paying customer out. `crmIncluded` is
        // computed with `hasFeature`, which reads a null status as "yes", so
        // leaving `billing` null on failure is the fail-OPEN answer — the
        // panel below is skipped entirely and the Twilio steps decide.
        if (alive) setBillingFailed(true);
      });
    listTwilioNumbers()
      .then((rows) => {
        if (alive) setNumbers(rows);
      })
      .catch(() => {
        // ⚠ NOT `setNumbers([])`. An unreadable list is "we don't know",
        // and rendering "you have no number — buy one" on that basis invites
        // a duplicate purchase for a company that already has one.
        if (alive) setProbeFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [companyRole, setCompanyRole, reloadKey]);

  const buyNumber = useCallback(async () => {
    setBuying(true);
    try {
      const result = await provisionTwilioNumber(areaCode, forwardTo);
      setNumbers([result.number]);
      setConfirming(false);
      toast.success(
        result.purchased
          ? `Your number is ${result.number.phone_number} — Leasing is ready.`
          : `You already had ${result.number.phone_number} — Leasing is ready.`,
      );
    } catch (err) {
      // Left on the setup screen deliberately: a failed purchase must not
      // look like a completed one, and the message names what to do next.
      toast.error(
        err instanceof Error ? err.message : 'Could not set up a phone number.',
      );
    } finally {
      setBuying(false);
    }
  }, [areaCode, forwardTo]);

  // `null` only while the read is genuinely outstanding; a FAILED read is
  // `true`, not `null`, or a billing outage would hold Leasing on a skeleton
  // forever for customers who are paying for it.
  const crmIncluded = billingFailed ? true : billing === null ? null : hasFeature(billing, 'crm');
  const block = leasingBlock(companyRole, status, numbers, crmIncluded);
  if (block === null) {
    // Setup is complete — but a number that rings nowhere is a setup that
    // looks complete and answers nothing. Ask above the pipeline, never
    // instead of it.
    const own = numbers?.[0];
    return (
      <div className="flex-1 flex flex-col min-h-0">
        {own ? (
          <CallForwardingPrompt
            number={own}
            onSaved={(saved) => setNumbers((rows) => (rows ? [saved, ...rows.slice(1)] : [saved]))}
          />
        ) : null}
        {children}
      </div>
    );
  }

  if (block === 'loading' && !probeFailed) {
    return (
      <div className="px-6 py-6 w-full" style={{ maxWidth: 640, margin: '0 auto' }}>
        <Skeleton />
      </div>
    );
  }

  if (block === 'plan') {
    // ⚠ Its own panel, not another row in the setup checklist. Everything
    // else this component shows is a step the customer can complete; this is
    // a packaging decision, and dressing it as a to-do would suggest that
    // finishing the other steps unlocks it. It does not — `crm.py` refuses
    // the routes on the plan alone.
    //
    // Both names come from the server: `planLabel` is what they are on, and
    // the upgrade target is the FIRST plan in ladder order that carries the
    // capability, so it is the cheapest one that works and it re-points
    // itself if Leasing ever moves rung.
    const upgradeTo = billing?.plans.find((p) => p.features.includes('crm'));
    return (
      <div
        className="flex-1 overflow-y-auto"
        data-testid="leasing-compliance-gate"
        data-block="plan"
      >
        <div className="px-6 py-10 w-full" style={{ maxWidth: 640, margin: '0 auto' }}>
          <h2
            className="m-0 font-semibold"
            style={{ fontSize: 16, color: 'var(--color-ink)', letterSpacing: '-0.01em' }}
          >
            Leasing needs a bigger plan
          </h2>
          <p
            className="mt-2 mb-6"
            style={{ fontSize: 14, color: 'var(--color-ink-muted)', lineHeight: 1.6 }}
          >
            Working leads here means texting and calling them from your own registered
            number, with the applicant pipeline behind it
            {billing?.planLabel ? ` — that isn't part of ${billing.planLabel}` : ''}.
            {upgradeTo
              ? ` ${upgradeTo.label} includes it, and everything already on your plan carries over.`
              : ' Have a look at the plans in Settings → Billing.'}
          </p>
          <button
            type="button"
            onClick={() => openSettings('billing')}
            className="h-9 px-4 rounded-[var(--r-sm)] text-[13px] font-medium text-white transition-colors duration-150"
            style={{ background: 'var(--iris)' }}
          >
            {upgradeTo ? `See ${upgradeTo.label}` : 'See plans'}
          </button>
        </div>
      </div>
    );
  }

  const roleBlocked = block === 'role';

  return (
    <div
      className="flex-1 overflow-y-auto"
      data-testid="leasing-compliance-gate"
      data-block={probeFailed && block === 'loading' ? 'unknown' : block}
    >
      <div className="px-6 py-10 w-full" style={{ maxWidth: 640, margin: '0 auto' }}>
        <h2
          className="m-0 font-semibold"
          style={{ fontSize: 16, color: 'var(--color-ink)', letterSpacing: '-0.01em' }}
        >
          {roleBlocked
            ? 'Leasing is limited to managers'
            : block === 'no_number'
              ? 'Set up your phone number'
              : block === 'campaign_pending'
                ? 'Your registration is with the carriers'
                : block === 'campaign_failed'
                  ? 'The carriers sent your registration back'
                  : 'Finish texting setup to use Leasing'}
        </h2>
        <p
          className="mt-2 mb-6"
          style={{ fontSize: 14, color: 'var(--color-ink-muted)', lineHeight: 1.6 }}
        >
          {roleBlocked ? (
            <>
              Working the lead pipeline sends messages from your company&rsquo;s registered
              phone number. Ask an admin to make you a manager if you need access.
            </>
          ) : probeFailed && block === 'loading' ? (
            <>
              We couldn&rsquo;t check whether texting is approved on this account, so Leasing
              stays closed. Reload the page; if it keeps happening, email {SUPPORT_EMAIL}.
            </>
          ) : block === 'campaign_pending' ? (
            /* ⚠ Their wait, and it is a real one — campaign vetting runs
               10-15 days and nothing we build shortens it. Saying the number
               out loud is the whole point: "pending" with no timeframe reads
               as broken, and a customer who thinks it is broken opens a
               ticket we cannot resolve. */
            <>
              Your registration is with the carriers. Their review usually takes 10 to 15
              days and there is nothing left for you to do. You can still call leads in the
              meantime, and texting switches on the day it clears.
            </>
          ) : block === 'campaign_failed' ? (
            /* ⚠ Actionable, and the carrier's own wording is what makes it
               so — the registration form below shows the verbatim reason.
               "Registration failed" alone is a support ticket. */
            <>
              The carriers sent your registration back. It is usually a detail on the form,
              most often a business address that didn&rsquo;t validate. Their reason is
              below. Fix it, resubmit, and the review starts again.
            </>
          ) : block === 'not_configured' ? (
            /* ⚠ Under white-label the messaging account is OURS, so an
               unconfigured channel is our outage and not the customer's task.
               Sending them to a settings page they cannot act on would be
               worse than saying nothing. */
            <>
              Texting isn&rsquo;t switched on for your account yet. That one is ours to fix,
              not yours. Leads keep arriving in the meantime.
            </>
          ) : block === 'no_number' ? (
            <>
              Leasing works leads over text and calls, so your company needs its own number.
              It is what leads reply to, and it is how we know a reply is yours and not
              another company&rsquo;s. We set it up; there is nothing to configure
              afterwards.
            </>
          ) : (
            /* ⚠ Plain words on purpose. "A2P 10DLC campaign" is carrier
               jargon that means nothing to a property manager, and this
               state is the one place they have something to DO — the
               registration form renders directly below. Naming the process
               in their terms is what makes the form read as the next step
               rather than as more paperwork about somebody else's system. */
            <>
              Carriers won&rsquo;t deliver business texts from an unregistered company, and
              Leasing runs on text. We file the registration for you: fill in the form below
              and we take it from there. It clears in 10 to 15 days, and leads keep arriving
              in the meantime.
            </>
          )}
        </p>

        {/* ⚠ Where you are, ABOVE the thing you act on — and only once
            something is actually in flight.

            It used to render below the whole registration form, which put a
            two-line status readout at the bottom of a fourteen-field page
            where nobody scrolls to find it. Worse, it rendered in the
            'disabled' state too: a checklist of everything not yet done,
            shown to somebody who has not filled the form in yet, under a
            heading already telling them to fill the form in. Before the
            submission there is one thing to do and the form is it.

            ⚠ The "Connect Twilio" step is GONE, not hidden. It asked the
            customer for an account SID and auth token — credentials that,
            white-labeled, are ours and they could never supply. A checklist
            item nobody can complete is not a checklist, it is a dead end.

            ⚠ `configured` belongs in this condition and a first cut left it
            out. A company whose channel is set up but not yet approved is
            REGISTERED AND WAITING — exactly the reader progress is for — and
            it arrives here as `disabled`, not as `campaign_pending`. Gating
            on the block value alone hid the readout from the one person who
            needed it. The existing test caught that. */}
        {!roleBlocked &&
          (block === 'campaign_pending' || block === 'campaign_failed' || status?.configured) && (
          <ul className="flex flex-col gap-4 m-0 p-0 mb-6" style={{ listStyle: 'none' }}>
            <Step
              testId="leasing-step-approved"
              done={Boolean(status?.enabled)}
              title="Carrier approval for your number"
            >
              {/* ⚠ "a few days" was wrong and contradicted every other
                  sentence on this page, which says 10 to 15. Campaign vetting
                  is the long step and understating it is how somebody who is
                  waiting normally decides they are stuck. */}
              {status?.configured && !status?.enabled
                ? 'Registered and waiting. The carriers grant this, not us, so we cannot rush it.'
                : 'We register your business with the carriers. Their review takes 10 to 15 days.'}
            </Step>
            <Step
              testId="leasing-step-consent"
              done={(status?.consent_count ?? 0) > 0}
              title="Record consent as you go"
            >
              {/* Informational on purpose — see the module note. A company
                  with no consent rows yet is a new company, not a
                  misconfigured one. */}
              This one never blocks you. We check consent per recipient when a text is sent,
              and refuse a number that has none.
              {status ? ` ${status.consent_count} on file.` : ''}
            </Step>
            </ul>
          )}

        {/* ⚠ Rendered for the in-review and rejected states too, not just
            'disabled'. The form IS the status surface — it shows where the
            registration got to and, on a rejection, the carrier's verbatim
            reason. Hiding it while someone waits leaves them with a sentence
            and no way to see their own submission. */}
        {(block === 'disabled' ||
          block === 'campaign_pending' ||
          block === 'campaign_failed') && (
          <div
            className="mt-5 pt-5 border-t"
            style={{ borderColor: 'var(--line)' }}
            data-testid="leasing-a2p-registration"
          >
            {/* Re-probe on submit: the gate's own `status.enabled` is what
                closes this section, so a registration that changes state
                must refresh it rather than leave a stale panel up. */}
            <A2pRegistrationForm onStatusChange={() => void refresh()} />
          </div>
        )}

        {block === 'no_number' && (
          <div data-testid="leasing-number-setup">
            {!confirming ? (
              <div className="flex items-center gap-2.5 flex-wrap">
                <input
                  data-testid="leasing-area-code"
                  value={areaCode}
                  onChange={(e) => setAreaCode(e.target.value.replace(/\D/g, '').slice(0, 3))}
                  placeholder="Area code (optional)"
                  inputMode="numeric"
                  className="rounded-[var(--r-sm)] px-3 py-2"
                  style={{
                    fontSize: 13,
                    width: 190,
                    color: 'var(--color-ink)',
                    background: 'var(--surface)',
                    border: '1px solid var(--line)',
                  }}
                />
                {/* ⚠ Optional, and it says so. A required field here would
                    block the setup it exists to complete, and voicemail is a
                    legitimate choice. */}
                <input
                  data-testid="leasing-forward-to"
                  value={forwardTo}
                  onChange={(e) => setForwardTo(e.target.value)}
                  placeholder="Ring my phone (optional)"
                  inputMode="tel"
                  className="rounded-[var(--r-sm)] px-3 py-2"
                  style={{
                    fontSize: 13,
                    width: 210,
                    color: 'var(--color-ink)',
                    background: 'var(--surface)',
                    border: '1px solid var(--line)',
                  }}
                />
                <button
                  type="button"
                  data-testid="leasing-get-number"
                  onClick={() => setConfirming(true)}
                  className="rounded-[var(--r-sm)] px-3.5 py-2 font-medium"
                  style={{
                    fontSize: 13,
                    color: 'var(--color-iris-hover)',
                    background: 'var(--iris-tint)',
                    border: '1px solid var(--iris-ring)',
                  }}
                >
                  Get a number
                </button>
              </div>
            ) : (
              /* ⚠ Two steps, deliberately. This BUYS a phone number and the
                 charge recurs monthly — a single button would make that a
                 one-click accident. The cost is stated before the commit,
                 not after. */
              <div
                className="rounded-[var(--r-md)] p-4"
                style={{ background: 'var(--info-bg)', border: '1px solid var(--color-line-strong)' }}
              >
                {/* ⚠ The old copy said "Twilio bills it monthly (about
                    $1.15)". Under white-label the number sits on OUR carrier
                    account, so that sentence named a vendor the customer has
                    no relationship with and a charge they will never see.
                    Removing it is a correction, not a concealment. */}
                <div style={{ fontSize: 14, color: 'var(--color-ink)', lineHeight: 1.6 }}>
                  We&rsquo;ll reserve a dedicated number for your company
                  {areaCode ? ` in area code ${areaCode}` : ''} and point its texts and
                  calls at Occupella. It stays yours for as long as you use Leasing.
                </div>
                <div className="flex items-center gap-2.5 mt-3.5">
                  <button
                    type="button"
                    data-testid="leasing-confirm-number"
                    disabled={buying}
                    onClick={buyNumber}
                    className="rounded-[var(--r-sm)] px-3.5 py-2 font-medium"
                    style={{
                      fontSize: 13,
                      opacity: buying ? 0.6 : 1,
                      color: 'var(--color-iris-hover)',
                      background: 'var(--iris-tint)',
                      border: '1px solid var(--iris-ring)',
                    }}
                  >
                    {buying ? 'Setting up…' : 'Yes, get the number'}
                  </button>
                  <button
                    type="button"
                    data-testid="leasing-cancel-number"
                    disabled={buying}
                    onClick={() => setConfirming(false)}
                    className="rounded-[var(--r-sm)] px-3 py-2"
                    style={{ fontSize: 13, color: 'var(--color-ink-muted)' }}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

      </div>
    </div>
  );
}
