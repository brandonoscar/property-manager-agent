/**
 * Connector detail modal — shared chrome for the API and Webhook
 * panels. The modal's content is the panel-specific body; this file
 * owns the overlay, header, close affordance, and error banner so the
 * panels themselves stay focused on their flow.
 */

import { useEffect, useRef, type ReactNode } from 'react';
import { X } from 'lucide-react';

import { useFocusTrap } from '../../hooks/useFocusTrap';

interface Props {
  title: ReactNode;
  subtitle: string;
  onClose: () => void;
  errorBanner?: string | null;
  /** Bumps the modal up to ~620px so the redesigned forms breathe.
   *  Default keeps the older narrower layout for any callers we
   *  haven't polished yet. */
  wide?: boolean;
  children: ReactNode;
}

export function DetailShell({
  title,
  subtitle,
  onClose,
  errorBanner,
  wide = false,
  children,
}: Props) {
  // ⚠ Escape closed NOTHING here, and this is the shell behind every connector
  // panel — Buildium API, webhooks, Composio, SMS. Clicking the backdrop was
  // the only dismissal that did not require finding the X, which is a mouse
  // affordance with no keyboard equivalent. One handler covers all of them.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const panel = useRef<HTMLDivElement>(null);
  useFocusTrap(true, panel);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: 'rgba(0, 0, 0, 0.55)' }}
      onClick={onClose}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        className="rounded-[var(--r-lg)] overflow-hidden flex flex-col"
        onClick={(e) => e.stopPropagation()}
        style={{
          width: wide ? 'min(620px, 94vw)' : 'min(560px, 92vw)',
          maxHeight: '90vh',
          background: 'var(--canvas)',
          border: '1px solid var(--line)',
        }}
      >
        <header
          className="flex items-start gap-3 px-6 py-5 border-b"
          style={{ borderColor: 'var(--line)' }}
        >
          <div className="flex-1">
            <div
              className="text-[16px] font-semibold leading-tight"
              style={{ color: 'var(--ink)', letterSpacing: '-0.01em' }}
            >
              {title}
            </div>
            <div
              className="text-[13px] mt-1 leading-relaxed"
              style={{ color: 'var(--ink-muted)' }}
            >
              {subtitle}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="p-1.5 rounded-[var(--r-sm)] hover:bg-[var(--canvas-elevated)] mt-0.5"
            aria-label="Close"
          >
            <X size={16} style={{ color: 'var(--ink-muted)' }} />
          </button>
        </header>

        <div className="flex-1 overflow-auto p-6">
          {errorBanner && (
            <div
              className="mb-4 px-3 py-2 rounded-[var(--r-sm)] text-[13px]"
              style={{ background: 'var(--negative-bg)', color: 'var(--negative)' }}
            >
              {errorBanner}
            </div>
          )}
          {children}
        </div>
      </div>
    </div>
  );
}
