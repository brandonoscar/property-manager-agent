/**
 * Top application chrome — 36px navy bar sitting above the sidebar +
 * main surface. Part of the dark "chrome island" with the Sidebar, so
 * it reads chrome tokens (light ink, translucent-white lines), not the
 * light content-area tokens. Three zones:
 *   • Wordmark (left)
 *   • Command palette trigger (center)
 *   • Activity-rail toggle + user avatar popover (right)
 *
 * No platform drag region — Occupella ships as a web app inside a
 * custom Chromium build, so the "draggable titlebar" from the spec
 * is a visual affordance only.
 *
 * Note: the user popover (UserMenu) is a floating dropdown that opens
 * over the white CONTENT area, so it keeps the light content tokens —
 * only the bar itself and the controls that sit ON the navy do.
 */

import { useEffect, useRef, useState } from 'react';
import {
  Settings as SettingsIcon,
  LogOut,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
} from 'lucide-react';
import { useStore } from '../stores/store';
import { Wordmark } from './Wordmark';
import { performSignOut } from '../services/auth';

interface TitleBarProps {
  /**
   * Mobile-only (below md): opens the sidebar as an overlay drawer.
   * The sidebar is off-canvas at mobile widths (Sidebar's max-md
   * classes) and this is the ONLY opener — without it every page is
   * a navigation dead-end on a phone (2026-08-15 audit, debt #1).
   */
  onOpenNav?: () => void;
}

export function TitleBar({ onOpenNav }: TitleBarProps = {}) {
  const user = useStore((s) => s.user);
  const setView = useStore((s) => s.setView);
  const sidebarCollapsed = useStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useStore((s) => s.toggleSidebar);

  const userName = displayName(user?.email);
  const initial = userName.charAt(0).toUpperCase();

  return (
    <div
      className="flex items-center h-8 shrink-0"
      style={{
        background: 'var(--titlebar)',
        // The rail is the same white as the canvas below it, so the
        // hairline is the ONLY thing separating them. A shadow here
        // would make a 32px strip read as a floating slab.
        borderBottom: '1px solid var(--titlebar-line)',
      }}
    >
      {/* Mobile (below md) — hamburger + wordmark. The fixed-width
          desktop wordmark column below is hidden at these widths; the
          hamburger opens the sidebar as an overlay drawer (scrim +
          slide-in live in App.tsx / Sidebar.tsx). */}
      {onOpenNav && (
        <button
          type="button"
          onClick={onOpenNav}
          aria-label="Open navigation"
          data-testid="titlebar-open-nav"
          className="md:hidden flex items-center justify-center w-8 h-8 ml-1 shrink-0 rounded-[var(--r-sm)] transition-colors duration-150"
          style={{ color: 'var(--titlebar-ink-muted)' }}
          onMouseEnter={(e) => {
            const el = e.currentTarget as HTMLElement;
            el.style.background = 'var(--titlebar-raised)';
            el.style.color = 'var(--titlebar-ink)';
          }}
          onMouseLeave={(e) => {
            const el = e.currentTarget as HTMLElement;
            el.style.background = 'transparent';
            el.style.color = 'var(--titlebar-ink-muted)';
          }}
        >
          <Menu size={16} strokeWidth={1.5} />
        </button>
      )}
      <span className="md:hidden ml-1 inline-flex">
        <Wordmark size={18} />
      </span>

      {/* No wordmark column here on desktop: the bar starts to the RIGHT
          of the sidebar now, so there is no sidebar width to align with
          and the wordmark lives in the sidebar's own masthead. The
          collapse toggle below is the bar's first control. */}

      {/* Sidebar collapse toggle — desktop-only; collapse is meaningless
          when the sidebar is a mobile overlay drawer. */}
      <button
        type="button"
        onClick={toggleSidebar}
        aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        title={`${sidebarCollapsed ? 'Expand' : 'Collapse'} sidebar (⌘B)`}
        className="max-md:hidden flex items-center justify-center w-6 h-6 ml-1 rounded-[var(--r-sm)] transition-colors duration-150"
        style={{ color: 'var(--titlebar-ink-muted)' }}
        onMouseEnter={(e) => {
          const el = e.currentTarget as HTMLElement;
          el.style.background = 'var(--titlebar-raised)';
          el.style.color = 'var(--titlebar-ink)';
        }}
        onMouseLeave={(e) => {
          const el = e.currentTarget as HTMLElement;
          el.style.background = 'transparent';
          el.style.color = 'var(--titlebar-ink-muted)';
        }}
      >
        {sidebarCollapsed ? (
          <PanelLeftOpen size={14} strokeWidth={1.5} />
        ) : (
          <PanelLeftClose size={14} strokeWidth={1.5} />
        )}
      </button>

      {/* Center spacer */}
      <div className="flex-1" />

      {/* Right — user popover */}
      <div className="flex items-center gap-1 pr-3">
        <UserMenu
          initial={initial}
          userName={userName}
          email={user?.email ?? ''}
          onSettings={() => setView('settings')}
          onSignOut={() => {
            void performSignOut();
          }}
        />
      </div>
    </div>
  );
}

// ── User popover ────────────────────────────────────────────────────

function UserMenu({
  initial,
  userName,
  email,
  onSettings,
  onSignOut,
}: {
  initial: string;
  userName: string;
  email: string;
  onSettings: () => void;
  onSignOut: () => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div className="relative" ref={ref}>
      {/* Avatar sits on the WHITE rail. A 5%-ink disc would vanish
          there, so it carries the one spot of brand color on the bar —
          solid --iris, white initial (--iris is 4.6:1 on white; it was
          unusable on the old navy ground).

          ⚠ The DISC is 22px and the TARGET is 28px, and they are two
          different things. Measured in a real browser at 375x667, this
          button rendered 22x22 on every page and every viewport — under
          the 24x24 WCAG 2.2 AA minimum, on the most-reached control in
          the chrome. Growing the disc would have been the wrong fix (it
          is a deliberate size on a 32px bar); the button is the target,
          so the button grew and the disc is a span inside it. */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Account menu"
        data-testid="titlebar-account"
        className="flex items-center justify-center w-7 h-7 rounded-full transition-all duration-150"
      >
        <span
          aria-hidden="true"
          className="flex items-center justify-center w-[22px] h-[22px] rounded-full text-[12px] font-semibold"
          style={{
            background: 'var(--iris)',
            color: '#FFFFFF',
            border: `1px solid ${open ? 'var(--iris-hover)' : 'var(--iris)'}`,
          }}
        >
          {initial}
        </span>
      </button>

      {/* Dropdown floats over the white content area below the bar, so
          it keeps the light content tokens (not chrome). */}
      {open && (
        <div
          role="menu"
          className="absolute right-0 top-[30px] w-[220px] rounded-[var(--r-md)] border fade-in"
          style={{
            background: 'var(--canvas-overlay)',
            borderColor: 'var(--line)',
            boxShadow: 'var(--shadow-md)',
            zIndex: 50,
          }}
        >
          <div className="px-3 py-2.5 border-b" style={{ borderColor: 'var(--line)' }}>
            <div className="text-[13px] font-medium text-[var(--ink)] truncate">
              {userName}
            </div>
            {email && (
              <div className="text-[12px] text-[var(--ink-subtle)] truncate mt-0.5">
                {email}
              </div>
            )}
          </div>
          <div className="py-1">
            <MenuItem
              icon={<SettingsIcon size={14} strokeWidth={1.5} />}
              label="Settings"
              onClick={() => {
                setOpen(false);
                onSettings();
              }}
            />
            <MenuItem
              icon={<LogOut size={14} strokeWidth={1.5} />}
              label="Sign out"
              onClick={() => {
                setOpen(false);
                onSignOut();
              }}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function MenuItem({
  icon,
  label,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className="flex items-center gap-2.5 w-full px-3 py-2 text-left text-[13px] transition-colors duration-150"
      style={{ color: 'var(--ink-muted)' }}
      onMouseEnter={(e) => {
        const el = e.currentTarget as HTMLElement;
        el.style.background = 'var(--canvas-hover)';
        el.style.color = 'var(--ink)';
      }}
      onMouseLeave={(e) => {
        const el = e.currentTarget as HTMLElement;
        el.style.background = 'transparent';
        el.style.color = 'var(--ink-muted)';
      }}
    >
      <span style={{ color: 'var(--ink-subtle)' }}>{icon}</span>
      {label}
    </button>
  );
}

function displayName(email: string | null | undefined): string {
  if (!email) return 'Operator';
  const local = email.split('@')[0];
  return local.charAt(0).toUpperCase() + local.slice(1);
}
