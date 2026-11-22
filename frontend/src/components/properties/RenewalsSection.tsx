/**
 * Renewals — the property cockpit's expiring-leases card, upgraded into a
 * workflow: each expiring lease shows its offer state and opens an editor
 * to draft (LLM, server-side), edit, and send the renewal offer via the
 * PM's Gmail. Send fires only from the explicit button — never automatic.
 * DESIGN.md: token palette, 1px var(--line) borders, no gradients.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { CalendarClock, X } from 'lucide-react';
import { useFocusTrap } from '../../hooks/useFocusTrap';
import { MoneyInput } from '../MoneyInput';
import { formatMoney, parseMoney } from '../../lib/money';
import { formatCalendarDate } from '../../lib/calendarDate';
import {
  fetchRenewals,
  draftRenewal,
  saveRenewalDraft,
  sendRenewal,
  dismissRenewal,
  type RenewalCandidate,
  type RenewalOffer,
} from '../../services/renewals';

function StatusChip({ offer }: { offer: RenewalOffer | null }) {
  if (!offer) return null;
  const label = offer.renewed
    ? 'Renewed'
    : offer.status === 'sent'
      ? 'Offer sent'
      : offer.status === 'dismissed'
        ? 'Dismissed'
        : 'Draft saved';
  const color = offer.renewed
    ? 'var(--ok, var(--ink-secondary))'
    : offer.status === 'sent'
      ? 'var(--ink-secondary)'
      : 'var(--ink-muted)';
  return (
    <span
      className="rounded-full border px-2 py-0.5 text-[12px]"
      style={{ borderColor: 'var(--line)', color }}
    >
      {label}
    </span>
  );
}

interface EditorState {
  candidate: RenewalCandidate;
  offer: RenewalOffer | null;
  proposedRent: string;
  termMonths: string;
  subject: string;
  body: string;
  busy: boolean;
  error: string | null;
}

export function RenewalsSection({ propertyId }: { propertyId: number }) {
  const [data, setData] = useState<{
    candidates: RenewalCandidate[];
    renewed: RenewalOffer[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editor, setEditor] = useState<EditorState | null>(null);

  const reload = useCallback(() => {
    fetchRenewals(propertyId)
      .then(setData)
      .catch((e: unknown) =>
        setError(e instanceof Error ? e.message : 'Failed to load renewals'),
      );
  }, [propertyId]);

  useEffect(() => {
    setData(null);
    setError(null);
    reload();
  }, [reload]);

  // This dialog shipped with neither of the two things a modal owes a keyboard
  // user: Escape closed nothing, so the only way out was a mouse, and Tab
  // walked straight out into the page behind it.
  const panel = useRef<HTMLDivElement>(null);
  useFocusTrap(editor !== null, panel);

  useEffect(() => {
    if (!editor) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setEditor(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [editor]);

  const openEditor = (candidate: RenewalCandidate) => {
    const offer = candidate.offer;
    setEditor({
      candidate,
      offer,
      proposedRent: String(offer?.proposed_rent ?? candidate.current_rent ?? ''),
      termMonths: String(offer?.term_months ?? 12),
      subject: offer?.subject ?? '',
      body: offer?.body ?? '',
      busy: false,
      error: null,
    });
  };

  const withBusy = async (fn: () => Promise<void>) => {
    if (!editor) return;
    setEditor((e) => (e ? { ...e, busy: true, error: null } : e));
    try {
      await fn();
    } catch (e) {
      setEditor((prev) =>
        prev
          ? { ...prev, busy: false, error: e instanceof Error ? e.message : 'Something went wrong' }
          : prev,
      );
      return;
    }
  };

  const generate = () =>
    withBusy(async () => {
      if (!editor) return;
      const offer = await draftRenewal(propertyId, editor.candidate.lease_buildium_id, {
        proposed_rent: parseMoney(editor.proposedRent),
        term_months: editor.termMonths ? Number(editor.termMonths) : null,
      });
      setEditor((prev) =>
        prev
          ? {
              ...prev,
              offer,
              subject: offer.subject ?? '',
              body: offer.body ?? '',
              busy: false,
            }
          : prev,
      );
      reload();
    });

  const send = () =>
    withBusy(async () => {
      if (!editor) return;
      const leaseId = editor.candidate.lease_buildium_id;
      // Persist the PM's latest edits, then send exactly what they see.
      await saveRenewalDraft(propertyId, leaseId, {
        subject: editor.subject,
        body: editor.body,
        proposed_rent: parseMoney(editor.proposedRent),
        term_months: editor.termMonths ? Number(editor.termMonths) : null,
      });
      await sendRenewal(propertyId, leaseId);
      setEditor(null);
      reload();
    });

  const dismiss = () =>
    withBusy(async () => {
      if (!editor) return;
      await dismissRenewal(propertyId, editor.candidate.lease_buildium_id);
      setEditor(null);
      reload();
    });

  const candidates = data?.candidates ?? [];
  const renewedWins = data?.renewed ?? [];

  return (
    <div
      className="rounded-[var(--r-md)] border"
      style={{ borderColor: 'var(--line)', background: 'var(--canvas-raised)' }}
      data-testid="renewals-section"
    >
      <div
        className="flex items-center gap-2 px-4 py-2.5 border-b text-[13px] font-medium"
        style={{ borderColor: 'var(--line)', color: 'var(--ink)' }}
      >
        <CalendarClock size={13} strokeWidth={1.7} style={{ color: 'var(--ink-muted)' }} />
        Renewals — leases expiring in 90 days
      </div>

      {error ? (
        <div className="px-4 py-3 text-[13px]" style={{ color: 'var(--ink-secondary)' }}>
          {error}
        </div>
      ) : data === null ? (
        <div className="px-4 py-3 text-[13px]" style={{ color: 'var(--ink-muted)' }}>
          Loading…
        </div>
      ) : candidates.length === 0 && renewedWins.length === 0 ? (
        <div className="px-4 py-3 text-[13px]" style={{ color: 'var(--ink-muted)' }}>
          No leases expiring in the next 90 days.
        </div>
      ) : (
        <div className="px-4 py-2">
          {candidates.map((c) => (
            <div
              key={c.lease_buildium_id}
              className="flex items-center justify-between gap-3 py-1.5 text-[13px]"
            >
              <span className="min-w-0 truncate" style={{ color: 'var(--ink)' }}>
                {c.tenant_names || (c.unit_number ? `Unit ${c.unit_number}` : `Lease #${c.lease_buildium_id}`)}
                {c.unit_number && c.tenant_names ? ` · Unit ${c.unit_number}` : ''}
                {c.current_rent != null ? ` · ${formatMoney(c.current_rent)}/mo` : ''}
              </span>
              <span className="flex items-center gap-2 shrink-0">
                <span style={{ color: 'var(--ink-secondary)' }}>
                  {formatCalendarDate(c.lease_to_date)}
                </span>
                <StatusChip offer={c.offer} />
                <button
                  type="button"
                  onClick={() => openEditor(c)}
                  className="rounded-[var(--r-sm)] border px-2 py-0.5 text-[12px] hover:opacity-80"
                  style={{ borderColor: 'var(--line)', color: 'var(--ink)' }}
                >
                  {c.offer ? 'Open' : 'Draft renewal'}
                </button>
              </span>
            </div>
          ))}
          {renewedWins.map((o) => (
            <div
              key={o.id}
              className="flex items-center justify-between gap-3 py-1.5 text-[13px]"
            >
              <span style={{ color: 'var(--ink)' }}>Lease #{o.lease_buildium_id}</span>
              <StatusChip offer={o} />
            </div>
          ))}
        </div>
      )}

      {editor ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ background: 'color-mix(in srgb, var(--canvas) 60%, transparent)' }}
        >
          <div
            className="w-full max-w-[560px] rounded-[var(--r-md)] border flex flex-col max-h-[90vh]"
            style={{ borderColor: 'var(--line)', background: 'var(--canvas-raised)' }}
            ref={panel}
            role="dialog"
            aria-modal="true"
            aria-label="Renewal offer"
          >
            <div
              className="flex items-center justify-between px-4 py-2.5 border-b"
              style={{ borderColor: 'var(--line)' }}
            >
              <span className="text-[13px] font-medium" style={{ color: 'var(--ink)' }}>
                Renewal offer —{' '}
                {editor.candidate.tenant_names ||
                  (editor.candidate.unit_number
                    ? `Unit ${editor.candidate.unit_number}`
                    : `Lease #${editor.candidate.lease_buildium_id}`)}
              </span>
              <button
                type="button"
                aria-label="Close"
                onClick={() => setEditor(null)}
                style={{ color: 'var(--ink-muted)' }}
              >
                <X size={15} strokeWidth={1.7} />
              </button>
            </div>

            <div className="px-4 py-3 flex flex-col gap-3 overflow-y-auto">
              <div className="text-[12px]" style={{ color: 'var(--ink-secondary)' }}>
                Current: {formatMoney(editor.candidate.current_rent)}/mo, ends{' '}
                {editor.candidate.lease_to_date
                  ? formatCalendarDate(editor.candidate.lease_to_date)
                  : '—'}
                {editor.candidate.tenant_email
                  ? ` · will send to ${editor.candidate.tenant_email}`
                  : ' · ⚠ no tenant email on file'}
              </div>

              <div className="grid grid-cols-2 gap-3">
                <MoneyInput
                  label="Proposed rent (per month)"
                  value={editor.proposedRent}
                  onChange={(next) => setEditor((p) => (p ? { ...p, proposedRent: next } : p))}
                  min={0}
                />
                <label className="text-[12px]" style={{ color: 'var(--ink-secondary)' }}>
                  Term (months)
                  <input
                    type="number"
                    min="1"
                    max="60"
                    value={editor.termMonths}
                    // The wheel steps a focused number input, and this panel
                    // scrolls — so scrolling past a focused field changes the
                    // lease term that goes into the offer. Dropping focus is
                    // what stops it (lib/money.ts records the same trap).
                    onWheel={(e) => e.currentTarget.blur()}
                    onChange={(e) =>
                      setEditor((p) => (p ? { ...p, termMonths: e.target.value } : p))
                    }
                    className="mt-1 w-full rounded-[var(--r-sm)] border px-2 py-1.5 text-[13px]"
                    style={{
                      borderColor: 'var(--line)',
                      background: 'var(--canvas)',
                      color: 'var(--ink)',
                    }}
                  />
                </label>
              </div>

              <button
                type="button"
                onClick={generate}
                disabled={editor.busy}
                className="self-start rounded-[var(--r-sm)] border px-2.5 py-1 text-[12px] hover:opacity-80 disabled:opacity-50"
                style={{ borderColor: 'var(--line)', color: 'var(--ink)' }}
              >
                {editor.busy ? 'Working…' : editor.offer ? 'Regenerate draft' : 'Generate draft'}
              </button>

              {editor.offer || editor.subject || editor.body ? (
                <>
                  <label className="text-[12px]" style={{ color: 'var(--ink-secondary)' }}>
                    Subject
                    <input
                      value={editor.subject}
                      onChange={(e) =>
                        setEditor((p) => (p ? { ...p, subject: e.target.value } : p))
                      }
                      className="mt-1 w-full rounded-[var(--r-sm)] border px-2 py-1.5 text-[13px]"
                      style={{
                        borderColor: 'var(--line)',
                        background: 'var(--canvas)',
                        color: 'var(--ink)',
                      }}
                    />
                  </label>
                  <label className="text-[12px]" style={{ color: 'var(--ink-secondary)' }}>
                    Body
                    <textarea
                      value={editor.body}
                      onChange={(e) => setEditor((p) => (p ? { ...p, body: e.target.value } : p))}
                      rows={9}
                      className="mt-1 w-full rounded-[var(--r-sm)] border px-2 py-1.5 text-[13px] resize-y"
                      style={{
                        borderColor: 'var(--line)',
                        background: 'var(--canvas)',
                        color: 'var(--ink)',
                      }}
                    />
                  </label>
                </>
              ) : null}

              {editor.error ? (
                <div className="text-[12px]" style={{ color: 'var(--danger)' }}>
                  {editor.error}
                </div>
              ) : null}
            </div>

            <div
              className="flex items-center justify-between px-4 py-2.5 border-t"
              style={{ borderColor: 'var(--line)' }}
            >
              <button
                type="button"
                onClick={dismiss}
                disabled={editor.busy || !editor.offer}
                className="text-[12px] hover:underline disabled:opacity-40"
                style={{ color: 'var(--ink-muted)' }}
              >
                Not renewing
              </button>
              <button
                type="button"
                onClick={send}
                disabled={
                  editor.busy ||
                  !editor.subject.trim() ||
                  !editor.body.trim() ||
                  !editor.candidate.tenant_email
                }
                className="rounded-[var(--r-sm)] border px-3 py-1.5 text-[13px] font-medium hover:opacity-80 disabled:opacity-50"
                style={{
                  borderColor: 'var(--line)',
                  background: 'var(--canvas)',
                  color: 'var(--ink)',
                }}
              >
                {editor.busy ? 'Sending…' : 'Send via Gmail'}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
