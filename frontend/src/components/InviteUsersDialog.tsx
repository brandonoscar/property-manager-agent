/**
 * Invite users — several at once, with what it costs stated before you send.
 *
 * REPLACES the inline "Invite a teammate" field, which could only ever do one
 * person and said nothing about money. Two things were wrong with that:
 *
 *  * Adding a team was N separate sends with no sense of the whole. Worse, each
 *    one pushed a fresh seat quantity to Stripe, so "add four people" landed
 *    four proration lines for one action — the reason `inviteMembers` posts a
 *    batch rather than looping (see `services/company.ts`).
 *  * `billing/seats.py` says, in its own header: *"A SEAT IS COUNTED AT INVITE,
 *    NOT AT ACCEPTANCE… State it out loud on the invite screen either way:
 *    charging for a seat nobody has used yet is defensible only if it is
 *    disclosed."* The old screen disclosed nothing. This one does, and that
 *    sentence is why the caveat below is not optional polish.
 *
 * ⚠ EVERY FIGURE COMES FROM THE SERVER (`status.currentPlan`). There is no
 * price table here and there must never be one — `PlanOut`'s docstring records
 * the billing dispute that shape produces. When the plan is unknown (an older
 * backend), the dialog says nothing about cost rather than guessing.
 *
 * ⚠ The refusal rules from the single-invite form are carried over WHOLE, and
 * they are not stylistic: a disabled button swallows the click before any code
 * runs, so there is nowhere left to put the explanation. Every refusal here is
 * something a person can read. See `InviteUsersDialog.test.tsx`.
 */

import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Loader2, Plus, X } from 'lucide-react';
import { toast } from 'sonner';

import {
  MAX_BULK_INVITES,
  TEAM_ROLES,
  inviteMembers,
  type InviteResult,
  type TeamRole,
} from '../services/company';
import { fetchSeatQuote, type CurrentPlan, type SeatQuote } from '../services/billing';
import { demoMode } from '../services/auth';
import { useFocusTrap } from '../hooks/useFocusTrap';
import { EmailTypoHint } from './EmailTypoHint';
import { FormError } from './FormError';

/** Same shape as the backend's `_EMAIL_SHAPE`. Deliberately loose — real
 *  validation is GoTrue's job; this only catches obvious typos. */
const EMAIL_SHAPE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export interface InviteRow {
  id: number;
  email: string;
  role: TeamRole;
}

// ── the two pure parts, tested directly ──────────────────────────────

/**
 * Split pasted text into addresses.
 *
 * ⚠ THIS IS THE FEATURE, not a convenience. Nobody types six colleagues one
 * field at a time; they paste them out of an email client or a spreadsheet,
 * which means commas, semicolons, newlines, tabs, and `Name <a@b.com>`. A
 * dialog that accepts a list only one row at a time is the old form with extra
 * steps.
 *
 * Returns the addresses in order, de-duplicated case-insensitively. Does NOT
 * validate: an unparseable entry has to reach a row so the person can see and
 * fix it — silently dropping it is a refusal nobody can perceive.
 */
export function parseAddresses(text: string): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const raw of text.split(/[\s,;]+/)) {
    // `Display Name <someone@example.com>` — take what is inside the angles.
    // The split above has already broken the display name off into its own
    // token, so this only ever sees the address fragment.
    const cleaned = raw.replace(/^</, '').replace(/>$/, '').trim();
    if (!cleaned) continue;
    const key = cleaned.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(cleaned);
  }
  return out;
}

/**
 * How many seats a company is BILLED for at `memberCount` people.
 *
 * ⚠ A MIRROR of `billing/seats.py::billable_seats`, and it cannot import it —
 * TypeScript does not read Python. So this is a second copy of a rule about
 * money, which is the shape this repo has been bitten by before (`PaidPlanKey`
 * in `billing.ts`). Two things keep it honest: the numbers it consumes
 * (`priceUsd`, `maxSeats`, `perSeat`) all come from the server rather than
 * from here, so only the ARITHMETIC is duplicated; and both sides are pinned
 * against the same written table — `test_billing_seats.py` and
 * `InviteUsersDialog.test.tsx`, each of which names the other.
 *
 * ⚠ A flat plan is always 1, whatever the headcount. Starter and Scale are
 * priced per ACCOUNT and Scale's own highlight says "the allowance is pooled,
 * not charged per person" — multiplying by headcount would make that sentence
 * false on the screen where somebody is deciding whether to add people.
 */
export function billableSeats(plan: CurrentPlan, memberCount: number): number {
  if (!plan.perSeat) return 1;
  return Math.max(1, Math.min(memberCount, plan.maxSeats));
}

export interface SeatImpact {
  /** One sentence about what this invite does to the bill. Rendered verbatim. */
  line: string;
  /** A second sentence when the ceiling changes what the first one means. */
  caveat: string | null;
  /** True when this invite adds money. Drives the disclosure below it. */
  billed: boolean;
}

/**
 * What inviting `adding` more people does to this company's bill.
 *
 * Returns `null` when the plan is unknown — an older backend omits
 * `current_plan`, and the honest response to "I cannot price this" is silence,
 * not a guess. Every branch below is a fact the server supplied.
 */
export function seatImpact(
  plan: CurrentPlan | null,
  memberCount: number | null,
  adding: number,
): SeatImpact | null {
  if (!plan) return null;
  // ⚠ An UNLOADED roster is not an empty one, and the difference is a wrong
  // number rather than a missing one. With `memberCount` treated as 0, a Pro
  // company adding one person computes billedNow=1 and billedAfter=1 — the
  // clamp's floor — so `delta` is 0 and the dialog cheerfully reports "this
  // won't change your bill" for an invite that costs $199. Declining to
  // price it is the same posture as an unknown plan.
  if (memberCount === null) return null;

  if (!plan.perSeat) {
    return {
      line: `Everyone shares the ${plan.label} plan — adding people doesn't change your bill.`,
      caveat: null,
      billed: false,
    };
  }

  const after = memberCount + adding;
  const cappedOut = Number.isFinite(plan.maxSeats) && after > plan.maxSeats;

  // The trial: per-seat, but free. The cap is the whole point of saying
  // anything at all — past it a teammate is real but brings no allowance, and
  // nothing else in the product would ever tell them that.
  if (plan.priceUsd === 0) {
    return {
      line: Number.isFinite(plan.maxSeats)
        ? `Your ${plan.label.toLowerCase()} covers ${plan.maxSeats} people at no cost.`
        : `Your ${plan.label.toLowerCase()} costs nothing to add people to.`,
      caveat: cappedOut
        ? `You'd be at ${after}. The ${after - plan.maxSeats} past ${plan.maxSeats} still get access, ` +
          `but they don't add to your monthly allowance.`
        : null,
      billed: false,
    };
  }

  const billedNow = billableSeats(plan, memberCount);
  const billedAfter = billableSeats(plan, after);
  const delta = billedAfter - billedNow;

  if (delta <= 0) {
    // ⚠ Only ever reachable at the ceiling — below it every added person is a
    // seat. Asserted rather than assumed: if some future clamp made delta 0
    // for another reason, this branch would tell somebody their bill is not
    // changing when it is, which is the one direction of error that reaches an
    // invoice. Saying nothing is survivable; a false reassurance about money
    // is not.
    if (!cappedOut) return null;
    return {
      line: `You're at the ${plan.maxSeats}-seat limit on ${plan.label}, so this won't change your bill.`,
      caveat: `The ${after - plan.maxSeats} past ${plan.maxSeats} still get access, but they don't add to your monthly allowance.`,
      billed: false,
    };
  }

  const total = billedAfter * plan.priceUsd;
  return {
    line:
      `${billedAfter} seats × $${plan.priceUsd} = $${total.toLocaleString('en-US')}/month ` +
      `— $${(delta * plan.priceUsd).toLocaleString('en-US')} more than today.`,
    // ⚠ The disclosure `billing/seats.py` asks for by name. A seat is created
    // when the membership row is, which is at invite; waiting for acceptance
    // would need an `accepted_at` column that does not exist. Charging for a
    // seat nobody has used yet is defensible only if it is said here.
    caveat: 'Seats are counted when you invite someone, not when they accept.',
    billed: true,
  };
}

// ── the dialog ───────────────────────────────────────────────────────

let nextRowId = 1;
const blankRow = (role: TeamRole = 'member'): InviteRow => ({
  id: nextRowId++,
  email: '',
  role,
});

export function InviteUsersDialog({
  onClose,
  onInvited,
  memberCount,
  plan,
}: {
  onClose: () => void;
  /** Refresh the roster. Called after any invite lands, even a partial batch. */
  onInvited?: () => void;
  /** Live membership count, or null while the roster is still loading. */
  memberCount: number | null;
  /** `status.currentPlan`, or null when the backend did not send one. */
  plan: CurrentPlan | null;
}) {
  const [rows, setRows] = useState<InviteRow[]>([blankRow()]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<InviteResult[] | null>(null);
  const inputs = useRef(new Map<number, HTMLInputElement>());

  const filled = rows.filter((r) => r.email.trim() !== '');
  const impact = seatImpact(plan, memberCount, filled.length);

  // ⚠ **Consent, not disclosure.** The monthly line above has been correct
  // since this dialog shipped — "5 seats × $199 = $995/month" — and a founder
  // still reported inviting four people and discovering a $995 bill he had
  // never agreed to (2026-09-09). A number rendered NEXT TO a button is a
  // notice; the button still says "Send invite" and the person reads it as
  // free. So a charge now needs a second, explicit act.
  const [quote, setQuote] = useState<SeatQuote | null>(null);
  const [agreed, setAgreed] = useState(false);

  const addingCount = filled.length;
  useEffect(() => {
    if (addingCount === 0) {
      setQuote(null);
      return;
    }
    let alive = true;
    fetchSeatQuote(addingCount).then((q) => {
      if (alive) setQuote(q);
    });
    return () => {
      alive = false;
    };
  }, [addingCount]);

  // ⚠ Any change to WHO or HOW MANY withdraws the agreement. Ticking the box
  // for one person and then adding two more would otherwise carry consent
  // across to a charge three times the size — the exact shape of the
  // complaint, re-created inside the fix.
  useEffect(() => {
    setAgreed(false);
  }, [addingCount]);

  /**
   * Whether this invite needs an explicit yes.
   *
   * ⚠ **The SERVER's `billable` decides, and an absent quote still asks.**
   * Gotcha 73 is this repo shipping a status field the client re-derived into
   * a different verdict than the send path used. When the quote could not be
   * fetched we fall back to the local estimate and, if that also cannot say,
   * to asking — over-asking costs one click, under-asking is the bug.
   */
  const needsConsent =
    addingCount > 0 && (quote ? quote.billable : (impact?.billed ?? false));

  const setRow = (id: number, patch: Partial<InviteRow>) => {
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, ...patch } : r)));
    // Clear on the next keystroke, not on blur: the complaint is about what is
    // in the field, so it has to stop being true the moment they start fixing
    // it. Carried over from the single-invite form.
    if (error) setError(null);
  };

  const addRow = () => setRows((prev) => [...prev, blankRow()]);

  const removeRow = (id: number) =>
    // Never below one row: an empty dialog has nothing to type into and no
    // affordance that obviously brings a field back.
    setRows((prev) => (prev.length <= 1 ? prev : prev.filter((r) => r.id !== id)));

  /**
   * Paste splits into rows. The pasted text lands in THIS row and any extras
   * become new rows after it, so a paste into the middle of a half-filled form
   * does not throw away what is already there.
   */
  const handlePaste = (row: InviteRow, e: React.ClipboardEvent<HTMLInputElement>) => {
    const text = e.clipboardData.getData('text');
    const found = parseAddresses(text);
    if (found.length < 2) return; // one address pastes normally
    e.preventDefault();
    setRows((prev) => {
      const at = prev.findIndex((r) => r.id === row.id);
      if (at < 0) return prev;
      const room = MAX_BULK_INVITES - prev.length + 1;
      const take = found.slice(0, Math.max(0, room));
      if (take.length < found.length) {
        toast.info(
          `Pasted the first ${take.length} of ${found.length} — ` +
            `${MAX_BULK_INVITES} is the most in one go.`,
        );
      }
      const made = take.map((email) => ({ ...blankRow(row.role), email }));
      return [...prev.slice(0, at), ...made, ...prev.slice(at + 1)];
    });
    if (error) setError(null);
  };

  const send = async () => {
    if (busy) return; // double-submit only; the button says "Sending…"

    if (filled.length === 0) {
      setError('Type at least one email address.');
      inputs.current.get(rows[0].id)?.focus();
      return;
    }
    // ⚠ Names the FIRST address it could not parse and puts the cursor there.
    // "One of these is wrong" in a six-row form is a refusal the person cannot
    // act on.
    const bad = filled.find((r) => !EMAIL_SHAPE.test(r.email.trim()));
    if (bad) {
      setError(`That doesn't look like an email address — check ${bad.email.trim()}.`);
      inputs.current.get(bad.id)?.focus();
      return;
    }
    if (filled.length > MAX_BULK_INVITES) {
      setError(`That's ${filled.length} people — ${MAX_BULK_INVITES} is the most in one go.`);
      return;
    }
    // ⚠ Enforced HERE, not only by disabling the button. A disabled control
    // is the explanation; a guard on the action is what actually stops a
    // charge nobody approved — the same split `LeasingComplianceGate` gets
    // wrong-way-round if the panel is mistaken for the enforcement.
    if (needsConsent && !agreed) {
      setError('Tick the box to confirm the change to your bill.');
      return;
    }
    if (demoMode) {
      toast.info('Demo mode — invitations are disabled here.');
      return;
    }

    setError(null);
    setBusy(true);
    try {
      const out = await inviteMembers(
        filled.map((r) => ({ email: r.email.trim().toLowerCase(), role: r.role })),
      );
      setResults(out);

      const landed = out.filter((r) => r.status !== 'failed');
      const failed = out.filter((r) => r.status === 'failed');
      // ⚠ Refresh whenever ANYTHING landed, including a partial batch. The
      // roster is the only place the admin sees who is actually on the team.
      if (landed.length > 0) onInvited?.();

      if (failed.length === 0) {
        // ⚠ Someone who already has an account is NOT on the team until they
        // accept (migration 0112), and gets no email. Saying only "Invited"
        // would leave the admin expecting them on the roster.
        const waiting = landed.filter((r) => r.status === 'pending').length;
        toast.success(
          landed.length === 1
            ? landed[0].status === 'pending'
              ? `${landed[0].email} already has an account. They'll see your invite next time they sign in.`
              : `Invited ${landed[0].email}.`
            : waiting > 0
              ? `Invited ${landed.length} people. ${waiting} of them already had an account and must accept in the app to join.`
              : `Invited ${landed.length} people.`,
        );
        // ⚠ Only closes on a CLEAN batch. With any failure the dialog stays so
        // the per-address outcome below is readable — closing would replace the
        // one explanation of what went wrong with a toast that says a number.
        onClose();
      } else {
        // Keep only the failures, so "Send" retries exactly those and cannot
        // re-invite the ones that worked.
        setRows(
          failed.map((f) => ({ ...blankRow(f.role), email: f.email })),
        );
      }
    } catch (err) {
      // The server's own words, in the form AND in the toast. Leaving them only
      // in the toast means the one sentence that explains the failure is the
      // one thing that disappears.
      const message = err instanceof Error ? err.message : 'Could not send the invites.';
      setError(message);
      toast.error(message, { duration: 8000 });
    } finally {
      setBusy(false);
    }
  };

  // Escape always closes — the dialog must never require a page refresh.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Tab stays inside the card, and focus goes back to whatever opened it.
  // The trap deliberately does not steal focus from the first email row —
  // this dialog already focuses it, and the trap only acts when nothing
  // inside has focus yet.
  const panel = useRef<HTMLDivElement>(null);
  useFocusTrap(true, panel);

  return createPortal(
    <div
      className="dialog-backdrop fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: 'color-mix(in srgb, var(--canvas) 60%, transparent)' }}
      data-testid="invite-users-dialog"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="dialog-panel w-full max-w-[560px] rounded-[var(--r-lg)] border flex flex-col max-h-[88vh]"
        style={{
          borderColor: 'var(--card-edge)',
          background: 'var(--canvas)',
          boxShadow: 'var(--shadow-md)',
        }}
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label="Invite users"
      >
        <div
          className="flex items-center justify-between px-4 py-2.5 border-b shrink-0"
          style={{ borderColor: 'var(--line)' }}
        >
          <span className="text-[13px] font-medium" style={{ color: 'var(--ink)' }}>
            Invite users
          </span>
          <button type="button" aria-label="Close" onClick={onClose} style={{ color: 'var(--ink-muted)' }}>
            <X size={15} strokeWidth={1.7} />
          </button>
        </div>

        <div className="px-4 py-3 flex flex-col gap-3 overflow-y-auto">
          <p className="text-[13px]" style={{ color: 'var(--ink-muted)' }}>
            They&rsquo;ll get an email to join this workspace and set a password. Paste
            a list of addresses to add several at once.
          </p>

          {rows.map((row) => (
            <div key={row.id} className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              <input
                ref={(el) => {
                  if (el) inputs.current.set(row.id, el);
                  else inputs.current.delete(row.id);
                }}
                type="email"
                placeholder="teammate@company.com"
                value={row.email}
                onChange={(e) => setRow(row.id, { email: e.target.value })}
                onPaste={(e) => handlePaste(row, e)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    void send();
                  }
                }}
                disabled={busy}
                autoComplete="off"
                aria-label="Email address"
                className="h-9 px-3 rounded-[var(--r-sm)] border text-[13px] outline-none flex-1 min-w-0"
                style={{
                  background: 'var(--canvas)',
                  color: 'var(--ink)',
                  borderColor: 'var(--line)',
                }}
              />
              <select
                value={row.role}
                onChange={(e) => setRow(row.id, { role: e.target.value as TeamRole })}
                disabled={busy}
                aria-label="Role"
                className="h-9 px-2.5 rounded-[var(--r-sm)] border text-[13px] outline-none cursor-pointer shrink-0"
                style={{
                  background: 'var(--canvas)',
                  color: 'var(--ink)',
                  borderColor: 'var(--line)',
                }}
              >
                {TEAM_ROLES.map((r) => (
                  <option key={r.value} value={r.value}>
                    {r.label}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={() => removeRow(row.id)}
                disabled={busy || rows.length <= 1}
                aria-label={`Remove ${row.email || 'this row'}`}
                className="h-9 w-8 inline-flex items-center justify-center rounded-[var(--r-sm)] shrink-0 disabled:opacity-30"
                style={{ color: 'var(--ink-muted)' }}
              >
                <X size={14} strokeWidth={1.6} />
              </button>
            </div>
            {/* ⚠ Per ROW, and this is the form where a typo costs most. On
                sign-in a mistyped domain fails immediately; here the address
                is well-formed, the API accepts it, the mail goes to a mailbox
                that does not exist, and the team screen carries an "Invite
                pending" row for somebody who was never asked. Nobody finds
                out until they wonder why Sam never joined. */}
            <EmailTypoHint
              email={row.email}
              onAccept={(v) => setRow(row.id, { email: v })}
              className="pl-1"
            />
            </div>
          ))}

          <div>
            <button
              type="button"
              onClick={addRow}
              disabled={busy || rows.length >= MAX_BULK_INVITES}
              className="inline-flex items-center gap-1.5 text-[13px] disabled:opacity-40"
              style={{ color: 'var(--iris)' }}
            >
              <Plus size={13} strokeWidth={1.8} />
              Add another
            </button>
          </div>

          {/* ⚠ aria-live: the outcome of pressing the button is text, and text
              that is only drawn is text a screen reader never announces. */}
          <div aria-live="polite" className="flex flex-col gap-2">
            {error && <FormError>{error}</FormError>}

            {results && results.some((r) => r.status === 'failed') && (
              <div className="flex flex-col gap-1">
                {results.map((r) => (
                  <p
                    key={r.email}
                    className="text-[12px]"
                    style={{
                      color: r.status === 'failed' ? 'var(--danger)' : 'var(--ink-muted)',
                    }}
                  >
                    {r.status === 'failed'
                      ? `${r.email} — ${r.error ?? 'could not be invited.'}`
                      : r.status === 'already_member'
                        ? `${r.email} was already on the team.`
                        : r.status === 'pending'
                          ? `${r.email} already has an account. No email sent: they'll see your invite when they sign in, and join once they accept.`
                          : r.emailSent
                            ? `Invited ${r.email}.`
                            : `${r.email} already has an account — added, no email sent.`}
                  </p>
                ))}
              </div>
            )}
          </div>
        </div>

        <div
          className="flex items-center justify-between gap-3 px-4 py-2.5 border-t shrink-0"
          style={{ borderColor: 'var(--line)' }}
        >
          {/* The bill, before the button. ⚠ Renders nothing when the plan is
              unknown rather than guessing a price — see `seatImpact`. */}
          <div className="text-[12px] leading-snug" style={{ color: 'var(--ink-faint)' }}>
            {impact && (
              <>
                <span style={{ color: impact.billed ? 'var(--ink-muted)' : 'var(--ink-faint)' }}>
                  {impact.line}
                </span>
                {/* ⚠ The charge that lands TODAY, which the monthly line never
                    named. Stripe prorates a mid-period seat change, so the
                    first thing that actually happens is a part-month charge —
                    and being surprised by a smaller number than the monthly
                    one is still being surprised. Omitted entirely when the
                    server could not price it: a missing figure shown as $0.00
                    would say this invite is free. */}
                {quote?.billable && quote.proratedNowUsd !== null && quote.daysLeft !== null && (
                  <>
                    <br />
                    <span style={{ color: 'var(--ink-muted)' }}>
                      About ${quote.proratedNowUsd.toLocaleString('en-US', {
                        minimumFractionDigits: 2,
                        maximumFractionDigits: 2,
                      })}{' '}
                      today for the {quote.daysLeft} days left in this billing period.
                    </span>
                  </>
                )}
                {impact.caveat && (
                  <>
                    <br />
                    {impact.caveat}
                  </>
                )}
              </>
            )}
            {/* The agreement itself. Deliberately a checkbox rather than a
                second confirm dialog: the numbers it refers to are on screen
                directly above it, and a modal over a modal would move them
                out of view at the moment they matter most. */}
            {needsConsent && (
              <label
                className="mt-2 flex items-start gap-2 cursor-pointer"
                style={{ color: 'var(--ink)' }}
              >
                <input
                  type="checkbox"
                  checked={agreed}
                  onChange={(e) => {
                    setAgreed(e.target.checked);
                    if (error) setError(null);
                  }}
                  data-testid="invite-billing-consent"
                  className="mt-0.5"
                />
                <span className="text-[12px] leading-snug">
                  {quote && quote.monthlyAfterUsd > quote.monthlyNowUsd
                    ? `I understand this raises my bill to $${quote.monthlyAfterUsd.toLocaleString('en-US')}/month.`
                    : 'I understand this changes what I am charged.'}
                </span>
              </label>
            )}
          </div>
          <button
            type="button"
            onClick={() => void send()}
            // ⚠ `busy` ONLY. Disabling on an unparseable address is what made a
            // click do nothing with nothing to read; the click always lands and
            // `send` says why when it refuses.
            disabled={busy}
            className="rounded-[var(--r-sm)] px-3.5 py-1.5 text-[13px] font-medium inline-flex items-center gap-1.5 shrink-0 disabled:opacity-50"
            style={{ background: 'var(--iris)', color: 'var(--canvas)' }}
          >
            {busy && <Loader2 aria-hidden className="h-3.5 w-3.5 animate-spin" />}
            {busy
              ? 'Sending…'
              : filled.length > 1
                ? `Send ${filled.length} invites`
                : 'Send invite'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
