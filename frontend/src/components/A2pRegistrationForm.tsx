/**
 * Register the business for text messaging — the white-label application.
 *
 * The customer never opens a carrier account, never sees a vendor name and
 * never leaves Occupella. They fill this in; we submit it on their behalf
 * against our own messaging account.
 *
 * ⚠ **No vendor name appears in any string here.** That is the whole point of
 * white-labeling, and it is a rule about copy, not a styling preference — the
 * moment a sentence says "go to <vendor> and…" the flow has stopped being
 * white-label. `test_white_label_carrier.py` scans this file for exactly that.
 *
 * ⚠ **Submitting QUEUES it. No string here may say the carriers have it.**
 * The backend moves the row draft -> submitted; a separate runner does the
 * carrier calls. Copy that outran the row would be the same defect class as a
 * 2xx read as a completed write — telling somebody an outcome happened when
 * it has not.
 *
 * ⚠ **The 10-15 day wait is stated plainly and never softened.** Campaign
 * vetting genuinely takes that long and nothing we build shortens it. A
 * customer who is told "shortly" and waits a fortnight files a support ticket;
 * one who is told a fortnight waits a fortnight. The honest number is also the
 * cheaper one.
 *
 * ⚠ **The EIN question is asked plainly and its blank is MEANINGFUL.** A
 * business with no EIN registers as a sole proprietor and one with an EIN
 * registers as standard — the carriers reject the wrong kind. So the field is
 * optional in the form and the empty value is sent as absent, never as "".
 */

import { useCallback, useEffect, useMemo, useState } from 'react';

import { useUnsavedWork } from '../hooks/useUnsavedWork';
import { toast } from 'sonner';

import {
  BUSINESS_TYPES,
  JOB_POSITIONS,
  fetchRegistration,
  saveRegistration,
  submitRegistration,
  type A2pRegistration,
} from '../services/a2p';
import { Button } from './Button';
import { Skeleton } from './Skeleton';

const inputCls = 'h-9 px-3 rounded-[var(--r-sm)] border text-[13px] outline-none w-full';
const inputStyle = {
  background: 'var(--canvas)',
  color: 'var(--ink)',
  borderColor: 'var(--line)',
} as const;

/** Every text field the form owns, so state is one object rather than 18. */
type FieldKey =
  | 'legal_business_name'
  | 'ein'
  | 'business_type'
  | 'business_vertical'
  | 'website'
  | 'street'
  | 'city'
  | 'region'
  | 'postal_code'
  | 'rep_first_name'
  | 'rep_last_name'
  | 'rep_email'
  | 'rep_phone'
  | 'rep_job_position';

/**
 * ⚠ `consent_attested` is a BOOLEAN and deliberately outside `FieldKey`.
 *
 * Every other field here is a fact the customer types about their business.
 * This one is an attestation about their own paperwork — the single thing in
 * the whole registration that we can neither generate nor verify — and giving
 * it a different type keeps it from being handled as just another text input.
 */
type FormState = Record<FieldKey, string> & { consent_attested: boolean };

const BLANK: FormState = {
  legal_business_name: '',
  ein: '',
  business_type: '',
  business_vertical: 'REAL_ESTATE',
  website: '',
  street: '',
  city: '',
  region: '',
  postal_code: '',
  rep_first_name: '',
  rep_last_name: '',
  rep_email: '',
  rep_phone: '',
  rep_job_position: '',
  consent_attested: false,
};

/**
 * The one consent fact only this customer can supply.
 *
 * ⚠ Must stay in step with `comms/a2p_defaults.CONSENT_ATTESTATION`, which is
 * what actually reaches the carrier. A customer reads THIS and a reviewer
 * reads that; if they describe different facts, somebody has been misled — and
 * it is the customer who is attesting.
 */
const CONSENT_ATTESTATION =
  'Our lease, rental application, or vendor agreement collects mobile ' +
  'numbers and tells people they may receive text messages about their ' +
  'property.';

/**
 * The status line a customer reads, in their terms rather than ours.
 *
 * ⚠ The draft case is deliberately a LABEL and not an explanation. The page
 * that renders this form (`LeasingComplianceGate`) already says why carriers
 * require registration, directly above; saying it twice in two paragraphs is
 * what made the screen read as filler.
 */
function statusLine(reg: A2pRegistration): { title: string; body: string } {
  switch (reg.status) {
    case 'submitted':
      return {
        title: 'Queued for registration',
        body:
          'We send this to the carriers for you. Their review usually takes 10 to 15 ' +
          'days, and texting switches on the day it clears.',
      };
    case 'brand_pending':
      return {
        title: 'Business under review',
        body: 'Your business details are being verified. This part takes minutes to a day.',
      };
    case 'brand_approved':
    case 'campaign_pending':
      return {
        title: 'Business verified, messaging under review',
        body:
          'Your business cleared. The messaging review is the long one: usually 10 to 15 ' +
          'days. Texting switches on the day it clears.',
      };
    case 'approved':
      return { title: 'Approved', body: 'Texting is live on your number.' };
    case 'failed':
      return {
        title: 'Registration was rejected',
        body: 'Fix what is flagged below and submit again. Resubmitting is free.',
      };
    default:
      return { title: 'Your registration', body: '' };
  }
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <div className="text-[12px] font-medium mb-1" style={{ color: 'var(--ink)' }}>
        {label}
      </div>
      {children}
      {hint ? (
        <div className="text-[12px] mt-1 leading-relaxed" style={{ color: 'var(--ink-subtle)' }}>
          {hint}
        </div>
      ) : null}
    </label>
  );
}

/**
 * One bordered group.
 *
 * ⚠ A 1px edge on a raised surface, never a shadow — DESIGN.md reserves those
 * for elements that genuinely float. The sections previously sat directly on
 * the page with an uppercase label and nothing else holding them, so a long
 * form read as one undifferentiated column of inputs.
 */
function Section({
  title,
  note,
  children,
}: {
  title: string;
  note?: string;
  children: React.ReactNode;
}) {
  return (
    <section
      className="rounded-[var(--r-md)] border p-4"
      style={{ background: 'var(--canvas-raised)', borderColor: 'var(--card-edge)' }}
    >
      <div
        className="text-[12px] font-medium uppercase tracking-wider"
        style={{ color: 'var(--ink-muted)' }}
      >
        {title}
      </div>
      {note ? (
        <p className="text-[12px] mt-1.5 leading-relaxed" style={{ color: 'var(--ink-subtle)' }}>
          {note}
        </p>
      ) : null}
      <div className="flex flex-col gap-3 mt-3">{children}</div>
    </section>
  );
}

export function A2pRegistrationForm({ onStatusChange }: { onStatusChange?: () => void }) {
  const [reg, setReg] = useState<A2pRegistration | null>(null);
  const [form, setForm] = useState<FormState>(BLANK);
  /**
   * What the server last confirmed it holds. `form` is compared against this
   * to answer "is there work here that would be lost", and it is a SNAPSHOT
   * rather than a dirty flag on purpose: typing a character and deleting it
   * again leaves nothing to save, and a flag would go on warning about it.
   */
  const [saved, setSaved] = useState<FormState>(BLANK);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState(false);
  /**
   * ⚠ Has the customer actually TRIED to submit?
   *
   * `reg.problems` is computed server-side and arrives with the row, so an
   * untouched draft used to open with fourteen red-flagged fields before the
   * customer had typed a character — a wall of failures for doing nothing
   * wrong. A validation message is only useful once there is something to
   * validate, so the list waits for a submit attempt. A registration the
   * carriers already REJECTED is the exception: those problems are the whole
   * reason that screen exists.
   */
  const [attempted, setAttempted] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetchRegistration();
      setReg(r);
      setLoadError(false);
      const next = { ...BLANK };
      (Object.keys(BLANK) as (keyof FormState)[]).forEach((k) => {
        if (k === 'consent_attested') return;
        const v = r.details[k];
        if (v) next[k] = v;
      });
      // A saved opt-in description can only exist because the box was ticked
      // (the server renders it empty otherwise), so it IS the stored state of
      // the checkbox — there is no separate column and none is needed.
      next.consent_attested = Boolean(r.details.opt_in_description);
      setForm(next);
      setSaved(next);
    } catch {
      // ⚠ Do NOT render an empty form on a failed load. It reads as "nothing
      // is saved", and the admin would retype details that already exist and
      // possibly overwrite good ones with worse ones.
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const set = useCallback((key: keyof FormState, value: string | boolean) => {
    setForm((prev) => ({ ...prev, [key]: value }));
  }, []);

  const body = useMemo(
    () => ({
      ...form,
      // '' means "not supplied" off an HTML form, and for the EIN that
      // distinction decides which brand kind is registered.
      ein: form.ein.trim() || null,
      website: form.website.trim() || null,
      // ⚠ No campaign_description / opt_in_description / sample_messages /
      // business_vertical here. The server fills all four from
      // comms/a2p_defaults and ignores anything a client sends for them, so
      // posting our own copy would be a second source of truth that silently
      // does nothing.
    }),
    [form],
  );

  // This form is long — legal name, EIN, address, an authorised
  // representative — and none of it is autosaved. Closing the tab half way
  // through is the exact "gone silently" case, and a customer who has to
  // re-source their own EIN twice does not come back to it the same day.
  const dirty = (Object.keys(BLANK) as (keyof FormState)[]).some(
    (k) => form[k] !== saved[k],
  );
  useUnsavedWork(dirty && !busy);

  const save = useCallback(async () => {
    setBusy(true);
    try {
      setReg(await saveRegistration(body));
      // ⚠ `form`, not `body`. `body` nulls empty optional fields for the wire,
      // so comparing against it would read a blank EIN as an unsaved edit
      // forever — a warning that never clears is one people learn to ignore.
      setSaved(form);
      toast.success('Saved');
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Could not save');
    } finally {
      setBusy(false);
    }
  }, [body]);

  const submit = useCallback(async () => {
    setBusy(true);
    setAttempted(true);
    try {
      // Save first, always. Submitting whatever was last persisted while the
      // user looks at edited fields would register details they can see are
      // different from the ones on screen.
      //
      // ⚠ Keep this response. It carries the freshly computed `problems`, and
      // the list the customer is about to read has to describe what they just
      // typed rather than whatever the row held on page load.
      setReg(await saveRegistration(body));
      const r = await submitRegistration();
      setReg(r);
      setSaved(form);
      toast.success('Registration sent — we take it from here');
      onStatusChange?.();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'Could not submit');
    } finally {
      setBusy(false);
    }
  }, [body, onStatusChange]);

  if (loading) return <Skeleton className="h-64 w-full" />;

  if (loadError || !reg) {
    return (
      <div className="text-[13px]" style={{ color: 'var(--ink-muted)' }}>
        {/* Says what happened and what it means for them. The old copy
            explained our reasoning for hiding the form, which is our problem
            and not theirs to read. */}
        <p className="mb-3">
          We couldn&rsquo;t load your saved registration. The form stays hidden until we
          can, so you don&rsquo;t retype details that are already there.
        </p>
        <button
          onClick={() => void load()}
          className="h-9 px-4 rounded-[var(--r-sm)] border text-[13px] font-medium"
          style={inputStyle}
        >
          Try again
        </button>
      </div>
    );
  }

  const { title, body: blurb } = statusLine(reg);
  const editable = reg.editable;

  return (
    <div data-testid="a2p-registration-form">
      <h3 className="text-[16px] font-semibold" style={{ color: 'var(--ink)' }}>
        {title}
      </h3>
      {blurb ? (
        <p className="text-[13px] leading-relaxed mt-1" style={{ color: 'var(--ink-muted)' }}>
          {blurb}
        </p>
      ) : null}
      <div className="mb-5" />

      {reg.failure_reason ? (
        // The carrier's own words, verbatim. "Address could not be validated"
        // tells somebody exactly what to fix; a paraphrase tells them nothing.
        <div
          className="text-[13px] rounded-[var(--r-sm)] border p-3 mb-5"
          style={{ borderColor: 'var(--line)', color: 'var(--ink)' }}
          data-testid="a2p-failure-reason"
        >
          <span style={{ color: 'var(--ink-muted)' }}>Reason given: </span>
          {reg.failure_reason}
        </div>
      ) : null}

      {!editable ? (
        <p className="text-[13px]" style={{ color: 'var(--ink-subtle)' }}>
          Nothing further is needed from you. We&rsquo;ll email you when it clears.
        </p>
      ) : (
        <div className="flex flex-col gap-4">
          <Section title="Your business">
            <Field label="Legal business name" hint="Exactly as it appears on your tax filings.">
              <input
                className={inputCls}
                style={inputStyle}
                value={form.legal_business_name}
                onChange={(e) => set('legal_business_name', e.target.value)}
              />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field
                label="EIN (federal tax ID)"
                hint="Leave blank if you don't have one — that registers you as a sole proprietor."
              >
                <input
                  className={`${inputCls} font-mono tabular-nums`}
                  style={inputStyle}
                  value={form.ein}
                  onChange={(e) => set('ein', e.target.value)}
                  placeholder="12-3456789"
                />
              </Field>
              <Field label="Business structure">
                <select
                  className={inputCls}
                  style={inputStyle}
                  value={form.business_type}
                  onChange={(e) => set('business_type', e.target.value)}
                >
                  <option value="">Select…</option>
                  {BUSINESS_TYPES.map((t) => (
                    <option key={t.value} value={t.value}>
                      {t.label}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            {/* Industry is fixed to REAL_ESTATE server-side — every business
                that buys this product is a property manager, so the dropdown
                could only ever be used to get it wrong. */}
            <div className="grid grid-cols-1 gap-3">
              <Field
                label="Website"
                hint="Optional, but a registration without one is scored lower and allowed fewer messages a day."
              >
                <input
                  className={inputCls}
                  style={inputStyle}
                  value={form.website}
                  onChange={(e) => set('website', e.target.value)}
                  placeholder="https://"
                />
              </Field>
            </div>
            <Field label="Street address">
              <input
                className={inputCls}
                style={inputStyle}
                value={form.street}
                onChange={(e) => set('street', e.target.value)}
              />
            </Field>
            <div className="grid grid-cols-3 gap-3">
              <Field label="City">
                <input
                  className={inputCls}
                  style={inputStyle}
                  value={form.city}
                  onChange={(e) => set('city', e.target.value)}
                />
              </Field>
              <Field label="State">
                <input
                  className={inputCls}
                  style={inputStyle}
                  value={form.region}
                  onChange={(e) => set('region', e.target.value)}
                  placeholder="CO"
                />
              </Field>
              <Field label="ZIP">
                <input
                  className={`${inputCls} tabular-nums`}
                  style={inputStyle}
                  value={form.postal_code}
                  onChange={(e) => set('postal_code', e.target.value)}
                />
              </Field>
            </div>
          </Section>

          <Section
            title="Who to contact"
            note="This person gets a verification email during the review. If it goes unanswered the registration stalls, so use an address someone checks."
          >
            <div className="grid grid-cols-2 gap-3">
              <Field label="First name">
                <input
                  className={inputCls}
                  style={inputStyle}
                  value={form.rep_first_name}
                  onChange={(e) => set('rep_first_name', e.target.value)}
                />
              </Field>
              <Field label="Last name">
                <input
                  className={inputCls}
                  style={inputStyle}
                  value={form.rep_last_name}
                  onChange={(e) => set('rep_last_name', e.target.value)}
                />
              </Field>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Email">
                <input
                  className={inputCls}
                  style={inputStyle}
                  value={form.rep_email}
                  onChange={(e) => set('rep_email', e.target.value)}
                />
              </Field>
              <Field label="Mobile number">
                <input
                  className={`${inputCls} tabular-nums`}
                  style={inputStyle}
                  value={form.rep_phone}
                  onChange={(e) => set('rep_phone', e.target.value)}
                  placeholder="+15551234567"
                />
              </Field>
            </div>
            {/* ⚠ A SELECT, not an input. This was free text with
                placeholder="Owner" — and the carrier rejects "Owner" with
                "Job Level is invalid.", so the field's own suggestion was the
                one value guaranteed to fail. The enum is unpublished; these
                were measured against the policy evaluation (services/a2p.ts).
                "Owner / other" is first because it is what a small property
                manager actually is, and it sends the accepted "Other". */}
            <Field label="Job title">
              <select
                className={inputCls}
                style={inputStyle}
                value={form.rep_job_position}
                onChange={(e) => set('rep_job_position', e.target.value)}
              >
                <option value="">Select…</option>
                {JOB_POSITIONS.map((j) => (
                  <option key={j.value} value={j.value}>
                    {j.label}
                  </option>
                ))}
              </select>
            </Field>
          </Section>

          {/* ⚠ The three free-text fields that USED to live here — what your
              texts are for, how people agree, example messages — are gone on
              purpose. They were the ones carriers reject over (this form's own
              hint said so), and a property manager has no reason to know what
              a reviewer looks for. The server fills them from
              comms/a2p_defaults, and the examples are rendered from the live
              message templates so registered and sent cannot drift.

              What remains is the ONE fact only this customer knows: whether
              their own paperwork discloses texting. We can neither enforce nor
              verify that, so it is theirs to attest — as a checkbox, which is
              impossible to word badly and is still a real attestation. */}
          <Section title="How people agree to receive them">
            <label className="flex items-start gap-2.5 cursor-pointer">
              <input
                type="checkbox"
                className="mt-0.5 shrink-0"
                checked={form.consent_attested}
                onChange={(e) => set('consent_attested', e.target.checked)}
                data-testid="a2p-consent-attested"
              />
              <span className="text-[13px] leading-relaxed" style={{ color: 'var(--ink)' }}>
                {CONSENT_ATTESTATION}
              </span>
            </label>
            <p className="text-[12px] leading-relaxed" style={{ color: 'var(--ink-muted)' }}>
              We write the rest of the compliance wording, including the example messages,
              which come from the texts Occupella actually sends. If your lease or
              application doesn&rsquo;t mention text messages yet, add it before you tick
              this.
            </p>
          </Section>

          {/* ⚠ Only after they have tried, or after the carriers rejected it.
              These arrive with the row, so rendering them unconditionally
              greeted an untouched draft with every field flagged at once. */}
          {reg.problems.length && (attempted || reg.status === 'failed') ? (
            <div
              className="rounded-[var(--r-md)] border p-3.5"
              style={{ borderColor: 'var(--caution)', background: 'var(--canvas-raised)' }}
              data-testid="a2p-problems"
            >
              <div className="text-[12px] font-medium mb-1.5" style={{ color: 'var(--ink)' }}>
                Still needed
              </div>
              <ul
                className="text-[12px] leading-relaxed list-disc pl-4"
                style={{ color: 'var(--ink-muted)' }}
              >
                {reg.problems.map((p) => (
                  <li key={p}>{p}</li>
                ))}
              </ul>
            </div>
          ) : null}

          <div className="flex items-center gap-2">
            <Button
              variant="primary"
              onClick={() => void submit()}
              disabled={busy}
              data-testid="a2p-submit"
            >
              {busy ? 'Working…' : 'Submit registration'}
            </Button>
            <Button
              variant="secondary"
              onClick={() => void save()}
              disabled={busy}
              data-testid="a2p-save"
            >
              Save for later
            </Button>
          </div>
          <p className="text-[12px] -mt-3" style={{ color: 'var(--ink-subtle)' }}>
            Nothing is registered until you submit. You can save and come back.
          </p>
        </div>
      )}
    </div>
  );
}
