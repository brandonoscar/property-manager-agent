/**
 * Left sidebar — section nav + threads list, with a collapsible
 * icon-rail mode.
 *
 * Expanded (260px): workspace chip, section labels, NavItems with
 *   labels, Threads panel, Settings + version footer.
 * Collapsed (60px): workspace avatar only, icon-only NavItems with
 *   hover tooltips, no section labels, no threads, no version.
 *
 * Toggle: ⌘B / Ctrl+B (handled here) or the button in TitleBar.
 * State persists to the `helixis_sidebar_collapsed` cookie.
 *
 * The rail itself is the navy "chrome island" (with TitleBar), so its
 * surfaces read chrome tokens — light ink, translucent-white lines,
 * hover-goes-lighter (--chrome-raised), active accent --chrome-accent
 * (the on-navy blue; --iris is too dark on navy). NOT the light
 * content-area tokens.
 *
 * Exceptions (deliberate): the three FLOATING overlays that escape the
 * rail and hover over the white content area keep the light content
 * tokens, matching the command palette + TitleBar user menu —
 *   • CollapsedTooltip (floats right of the collapsed rail)
 *   • ThreadRow 3-dot dropdown menu
 *   • ThreadMenuItem
 * They are their own elevated LIGHT popovers, not part of the navy bar.
 */

import { useEffect, useRef, useState } from 'react';
import {
  MessageSquare,
  MessageSquarePlus,
  Inbox as InboxIcon,
  Settings as SettingsIcon,
  Building2 as PropertiesIcon,
  Contact as CrmIcon,
  Plus,
  MoreHorizontal,
  Pencil,
  Trash2,
  LogOut,
  X,
} from 'lucide-react';
import { captureThreadCreated } from '../services/analytics';
import { performSignOut, demoMode } from '../services/auth';
import { listChatSessions, loadChatMessages } from '../services/chat';
import { useWatchThread } from '../hooks/useWatchThread';
import { pathForRoute } from '../lib/routing';
import { useStore } from '../stores/store';
import { FeedbackDialog } from './FeedbackDialog';
import { Wordmark } from './Wordmark';

const WIDTH_EXPANDED = 260;
const WIDTH_COLLAPSED = 60;

interface SidebarProps {
  open?: boolean;
  onClose?: () => void;
}

export function Sidebar({ open, onClose }: SidebarProps) {
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const view = useStore((s) => s.view);
  const inboxCount = useStore((s) => s.inboxCount);
  const setView = useStore((s) => s.setView);
  const threads = useStore((s) => s.threads);
  const activeThreadId = useStore((s) => s.activeThreadId);
  const createThread = useStore((s) => s.createThread);
  const setActiveThread = useStore((s) => s.setActiveThread);
  const hydrateThreads = useStore((s) => s.hydrateThreads);
  const hydrateThreadMessages = useStore((s) => s.hydrateThreadMessages);
  const collapsedPref = useStore((s) => s.sidebarCollapsed);
  // Collapse is a DESKTOP affordance. Below md the sidebar is a 260px
  // overlay drawer opened by the title-bar hamburger, and the CSS
  // already forces that width — but the collapsed flag still drove
  // icon-only rendering, so a user who had collapsed the rail on
  // desktop got an unlabelled strip of icons in the mobile drawer.
  const isMobile = useIsMobile();
  const collapsed = collapsedPref && !isMobile;
  const toggleSidebar = useStore((s) => s.toggleSidebar);
  const watchThread = useWatchThread();

  // ⌘B / Ctrl+B toggles collapse.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'b') {
        const target = e.target as HTMLElement | null;
        if (
          target &&
          (target.tagName === 'INPUT' ||
            target.tagName === 'TEXTAREA' ||
            target.isContentEditable)
        ) {
          return;
        }
        e.preventDefault();
        toggleSidebar();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [toggleSidebar]);

  const handleThreadClick = (id: string) => {
    setView('chat');
    setActiveThread(id);
    // Best-effort replay: load this session's persisted messages so
    // the user sees their prior conversation when they click a stored
    // thread. The orchestrator will pick up new messages live as the
    // SSE stream resumes for this session.
    const thread = threads.find((t) => t.id === id);
    if (thread && thread.messages.length === 0 && !demoMode) {
      void loadChatMessages(id)
        .then((page) => hydrateThreadMessages(id, page.rows))
        .catch(() => {
          // Read-only replay failure — leave the thread empty and
          // log to the console. The live SSE still works.
          // eslint-disable-next-line no-console
          console.warn('[chat] replay failed for', id);
        });
    }
    // Live watch (multiplayer PR): a thread with a run in flight that WE
    // aren't driving → attach read-only. Covers both a teammate's run and
    // our own run after a page refresh (the driver's re-attach path).
    if (
      thread &&
      thread.live &&
      thread.status !== 'running' &&
      thread.status !== 'watching' &&
      !demoMode
    ) {
      void watchThread(id);
    }
    onClose?.();
  };

  // PR #71 — hydrate the threads list from the backend on mount so
  // the sidebar shows real prior conversations rather than the empty
  // local store. Demo mode keeps the empty in-memory list.
  useEffect(() => {
    if (demoMode) return;
    let cancelled = false;
    void listChatSessions()
      .then((page) => {
        if (cancelled) return;
        hydrateThreads(page.rows);
      })
      .catch(() => {
        // First-paint failure is acceptable; the SSE-driven path will
        // still let the user start a new thread.
        // eslint-disable-next-line no-console
        console.warn('[chat] failed to hydrate sessions');
      });
    return () => {
      cancelled = true;
    };
  }, [hydrateThreads]);

  const goTo = (
    target: 'chat' | 'inbox' | 'properties' | 'crm' | 'search' | 'settings',
  ) => {
    setView(target);
    onClose?.();
  };

  // Escape closes the mobile drawer — it was one of the audit's "dead
  // Esc" surfaces (the overlay only closed via the scrim tap).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose?.();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  return (
    <aside
      role="dialog"
      aria-modal={open ? true : undefined}
      // ⚠ A CLOSED drawer is still in the tab order, and on a phone that was
      // the first NINE stops. Measured: focus walked the whole nav at
      // left:-260px, so Tab appeared to do nothing nine times and Enter could
      // fire a control nobody could see. `inert` removes the whole subtree
      // from focus and from the accessibility tree.
      //
      // ⚠ Only when it is ACTUALLY a drawer. Above the breakpoint this same
      // <aside> is the permanent rail, and marking that inert would make the
      // entire navigation unreachable by keyboard — the opposite bug, and a
      // far worse one.
      inert={isMobile && !open}
      className={`
        flex flex-col h-full relative z-[1] shrink-0
        transition-[width] duration-200 ease-out
        max-md:fixed max-md:top-0 max-md:bottom-0 max-md:z-[100]
        max-md:!w-[260px] max-md:min-w-[260px]
        max-md:transition-[left] max-md:duration-200 max-md:ease-out
        ${open ? 'max-md:left-0' : 'max-md:-left-[260px]'}
      `}
      style={{
        width: collapsed ? WIDTH_COLLAPSED : WIDTH_EXPANDED,
        minWidth: collapsed ? WIDTH_COLLAPSED : WIDTH_EXPANDED,
        background: 'var(--chrome)',
        // The rail is light now, so value alone no longer separates it
        // from the white canvas — it needs a real edge.
        borderRight: '1px solid var(--chrome-line)',
      }}
    >
      {/* Masthead — the wordmark moved here from the title bar when the
          sidebar became full-height (2026-08-18). h-8 matches the title
          bar's height, so the wordmark and the bar's controls sit on the
          same baseline across the seam. Hidden when collapsed, exactly
          as the old title-bar column was. */}
      <div
        className={`h-8 shrink-0 flex items-center ${collapsed ? 'justify-center px-2' : 'justify-between px-6'}`}
      >
        {!collapsed && (
          <Wordmark size={20} />
        )}
        {/* Mobile drawer close — the overlay previously only dismissed
            via scrim tap, with no visible affordance (Stage 3 audit). */}
        {!collapsed && (
          <button
            type="button"
            aria-label="Close menu"
            data-testid="sidebar-drawer-close"
            onClick={() => onClose?.()}
            className="hidden max-md:inline-flex items-center justify-center w-7 h-7 rounded-[var(--r-sm)]"
            style={{ color: 'var(--chrome-ink-muted)' }}
          >
            <X size={16} strokeWidth={1.75} />
          </button>
        )}
      </div>


      {/* Primary nav */}
      <nav className={`flex flex-col gap-[1px] pt-3 ${collapsed ? 'px-2' : 'px-6'}`}>
        <NavItem
          collapsed={collapsed}
          active={view === 'chat'}
          onClick={() => goTo('chat')}
          href={pathForRoute({ view: 'chat' })}
          icon={<MessageSquare size={16} strokeWidth={1.5} />}
          label="Chat"
        />
        {/* Inbox: the property-manager's day-to-day surface. Every
            Buildium event Occupella enriches + plans for shows up here
            as a conversational thread with the AI's proposed actions
            (PR #70). */}
        {/* ⚠ The one badge in the product, and the reason it is here rather
            than on a bell: the Inbox IS the notification surface, and it only
            spoke to people standing on it. The page says "4 new items need
            your attention" while the rail said nothing, so anybody working in
            Chat or Operations had no idea. `NavItem` has carried this prop
            through both render branches and its collapsed tooltip since it
            was written and nothing ever passed one.
            `undefined` (unknown) draws nothing — see the store field. */}
        <NavItem
          collapsed={collapsed}
          active={view === 'inbox'}
          onClick={() => goTo('inbox')}
          href={pathForRoute({ view: 'inbox' })}
          icon={<InboxIcon size={16} strokeWidth={1.5} />}
          label="Inbox"
          badge={inboxCount ?? undefined}
        />
        {/* Operations (2026-08-19): the key-entity hub — properties,
            tenants, vendors, team, each a sortable analytics table. The
            internal view id stays 'properties' (persisted in localStorage;
            same label-vs-key precedent as CRM → "Leasing"). The old
            ×Roles routing grid lives inside it as "Assignment rules". */}
        <NavItem
          collapsed={collapsed}
          active={view === 'properties'}
          onClick={() => goTo('properties')}
          href={pathForRoute({ view: 'properties' })}
          icon={<PropertiesIcon size={16} strokeWidth={1.5} />}
          label="Operations"
        />
        {/* Leasing (self-updating lead pipeline). Ships behind a "Coming
            soon" gate — the section renders a design preview with sample
            leads until the ingestion path is wired. The internal view id
            stays 'crm' (store + /crm API contract unchanged); only the
            user-facing label reads "Leasing". */}
        <NavItem
          collapsed={collapsed}
          active={view === 'crm'}
          onClick={() => goTo('crm')}
          href={pathForRoute({ view: 'crm' })}
          icon={<CrmIcon size={16} strokeWidth={1.5} />}
          label="Leasing"
        />
      </nav>

      {/* Threads — only expanded + chat view */}
      {view === 'chat' && !collapsed ? (
        <div className="flex flex-col flex-1 min-h-0 mt-4">
          <div
            className="h-px mx-6"
            style={{ background: 'var(--chrome-line)' }}
          />
          <div className="flex items-center justify-between pl-7 pr-6 pt-3 pb-1.5">
            <span
              className=" uppercase"
              style={{
                fontSize: 12,
                letterSpacing: '0.14em',
                color: 'var(--chrome-ink-muted)',
              }}
            >
              Threads
            </span>
            <button
              type="button"
              onClick={() => {
                setView('chat');
                createThread();
                captureThreadCreated();
              }}
              aria-label="New thread"
              className="h-7 flex items-center gap-1.5 px-2 rounded-[var(--r-sm)] border transition-colors duration-150"
              style={{
                background: 'var(--chrome-raised)',
                borderColor: 'var(--chrome-line)',
                color: 'var(--chrome-ink-muted)',
                fontSize: 12,
              }}
              onMouseEnter={(e) => {
                const el = e.currentTarget as HTMLElement;
                el.style.borderColor = 'var(--chrome-line-strong)';
                el.style.color = 'var(--chrome-ink)';
              }}
              onMouseLeave={(e) => {
                const el = e.currentTarget as HTMLElement;
                el.style.borderColor = 'var(--chrome-line)';
                el.style.color = 'var(--chrome-ink-muted)';
              }}
            >
              <Plus size={12} strokeWidth={1.75} />
              New
            </button>
          </div>

          <div className="chrome-scroll flex-1 overflow-y-auto px-6 pb-2">
            {threads.length === 0 && (
              <div
                className="px-3 py-3 text-[12px]"
                style={{ color: 'var(--chrome-ink-muted)' }}
              >
                No threads yet.
              </div>
            )}
            {groupThreadsByRecency(threads).map((group) => (
              <div key={group.label} className="mb-2">
                <div
                  className=" uppercase px-3 pt-1.5 pb-1"
                  style={{
                    fontSize: 12,
                    letterSpacing: '0.12em',
                    color: 'var(--chrome-ink-muted)',
                  }}
                >
                  {group.label}
                </div>
                {group.threads.map((thread) => (
                  <ThreadRow
                    key={thread.id}
                    title={thread.title}
                    createdAt={thread.createdAt}
                    active={thread.id === activeThreadId}
                    live={
                      thread.live ||
                      thread.status === 'running' ||
                      thread.status === 'watching'
                    }
                    onSelect={() => handleThreadClick(thread.id)}
                  />
                ))}
              </div>
            ))}
          </div>
        </div>
      ) : (
        <div className="flex-1" />
      )}

      {/* Footer */}
      <div
        className={`pt-2 pb-3 border-t ${collapsed ? 'px-2' : 'px-6'}`}
        style={{ borderColor: 'var(--chrome-line)' }}
      >
        <NavItem
          collapsed={collapsed}
          active={view === 'settings'}
          onClick={() => goTo('settings')}
          href={pathForRoute({ view: 'settings' })}
          icon={<SettingsIcon size={16} strokeWidth={1.5} />}
          label="Settings"
        />
        <NavItem
          collapsed={collapsed}
          active={false}
          onClick={() => setFeedbackOpen(true)}
          icon={<MessageSquarePlus size={16} strokeWidth={1.5} />}
          label="Feedback"
        />
        {/* Visible sign-out — same destination as the avatar menu in
            TitleBar, but discoverable without hunting for it. The
            collapsed icon-rail still gets the icon-only affordance. */}
        <NavItem
          collapsed={collapsed}
          active={false}
          onClick={() => {
            void performSignOut();
          }}
          icon={<LogOut size={16} strokeWidth={1.5} />}
          label="Sign out"
        />
      </div>
      {feedbackOpen && <FeedbackDialog onClose={() => setFeedbackOpen(false)} />}
    </aside>
  );
}

// ── Section label ─────────────────────────────────────────────────

/**
 * True below Tailwind's `md` breakpoint, where the sidebar is an overlay
 * drawer rather than a resizable rail. Kept in sync with the `max-md:`
 * classes on the <aside> — 768px is Tailwind's md.
 */
function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== 'undefined' && window.matchMedia('(max-width: 767px)').matches,
  );
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const mq = window.matchMedia('(max-width: 767px)');
    const onChange = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mq.addEventListener('change', onChange);
    setIsMobile(mq.matches);
    return () => mq.removeEventListener('change', onChange);
  }, []);
  return isMobile;
}

// ── NavItem ───────────────────────────────────────────────────────

/**
 * A plain left click navigates in place; a modified click (new tab, new
 * window) is left to the browser. Only a place gets this: Feedback and Sign
 * out are actions and stay buttons.
 */
function onNavClick(e: React.MouseEvent, go: () => void) {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  go();
}

function NavItem({
  collapsed,
  active,
  onClick,
  href,
  icon,
  label,
  badge,
  tag,
}: {
  collapsed: boolean;
  active: boolean;
  onClick: () => void;
  /** Set for a place in the app, which renders as a real link. */
  href?: string;
  icon: React.ReactNode;
  label: string;
  badge?: number;
  /** Small text pill after the label (e.g. "Soon" for a gated section). */
  tag?: string;
}) {
  const [hovered, setHovered] = useState(false);

  if (collapsed) {
    return (
      <div
        className="relative flex justify-center"
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
      >
        <NavControl
          href={href}
          active={active}
          onClick={onClick}
          // The collapsed rail is icon-only, so this label is the ONLY thing
          // a screen reader gets — and the dot below is pure decoration to it.
          // Without the count here, a collapsed-rail user has no indication
          // anything is waiting at all.
          aria-label={badge !== undefined && badge > 0 ? `${label}, ${badge} waiting` : label}
          className="relative flex items-center justify-center w-9 h-9 rounded-[var(--r-md)] transition-colors duration-150"
          style={{
            background: active ? 'var(--chrome-raised)' : 'transparent',
            color: active ? 'var(--chrome-ink)' : 'var(--chrome-ink-muted)',
          }}
          onMouseEnter={(e) => {
            if (active) return;
            const el = e.currentTarget as HTMLElement;
            el.style.background = 'var(--chrome-raised)';
            el.style.color = 'var(--chrome-ink)';
          }}
          onMouseLeave={(e) => {
            if (active) return;
            const el = e.currentTarget as HTMLElement;
            el.style.background = 'transparent';
            el.style.color = 'var(--chrome-ink-muted)';
          }}
        >
          {active && (
            <span
              aria-hidden
              className="absolute -left-2 top-1.5 bottom-1.5 w-[2px] rounded-full"
              style={{
                background: 'var(--chrome-accent)',
              }}
            />
          )}
          {icon}
          {badge !== undefined && badge > 0 && (
            <span
              aria-hidden="true"
              data-testid={`nav-dot-${label.toLowerCase()}`}
              className="absolute top-1 right-1 w-1.5 h-1.5 rounded-full"
              style={{ background: 'var(--chrome-accent)' }}
            />
          )}
        </NavControl>

        <CollapsedTooltip show={hovered} label={label} badge={badge} />
      </div>
    );
  }

  return (
    <NavControl
      href={href}
      active={active}
      onClick={onClick}
      // ⚠ An explicit name, because the badge otherwise REWRITES it. Adding
      // the count made this button's accessible name "Inbox 04" — and 04 is a
      // zero-padded display string, so a screen reader says "Inbox zero four".
      // Caught by a Playwright click timing out on `name: 'Inbox'`, which is
      // exactly what a person navigating by name would hit.
      aria-label={badge !== undefined && badge > 0 ? `${label}, ${badge} waiting` : label}
      className="group relative flex items-center gap-2.5 h-8 pl-4 pr-3 rounded-[var(--r-md)] text-left w-full transition-colors duration-150"
      style={{
        background: active ? 'var(--chrome-raised)' : 'transparent',
        color: active ? 'var(--chrome-ink)' : 'var(--chrome-ink-muted)',
        // --chrome-raised is WHITE on the light rail, so the active pill
        // needs a contact shadow to read as raised rather than as a hole
        // punched in the tint.
        boxShadow: active ? 'var(--shadow-sm)' : 'none',
        fontSize: 13,
        fontWeight: 500,
      }}
      onMouseEnter={(e) => {
        if (active) return;
        const el = e.currentTarget as HTMLElement;
        el.style.background = 'var(--chrome-raised)';
        el.style.color = 'var(--chrome-ink)';
      }}
      onMouseLeave={(e) => {
        if (active) return;
        const el = e.currentTarget as HTMLElement;
        el.style.background = 'transparent';
        el.style.color = 'var(--chrome-ink-muted)';
      }}
    >
      {/* PR #118 Phase E — bumped the active-nav left accent bar from
          2px to 3px wide and from top-1.5/bottom-1.5 to top-1/bottom-1
          so the active state is visible at a glance instead of
          easy-to-miss per the redesign brief. No colour change — the
          iris token stays the same. */}
      <span
        aria-hidden
        className="absolute left-[4px] top-1 bottom-1 w-[3px] rounded-full"
        style={{
          background: active ? 'var(--chrome-accent)' : 'transparent',
          transition: 'background var(--dur-base) var(--ease)',
        }}
      />
      <span
        style={{ color: active ? 'var(--chrome-ink)' : 'var(--chrome-ink-muted)' }}
      >
        {icon}
      </span>
      <span className="flex-1 truncate">{label}</span>
      {tag && (
        <span
          className=" uppercase rounded-[var(--r-xs)] px-1.5 py-0.5"
          style={{
            fontSize: 12,
            letterSpacing: '0.08em',
            color: 'var(--chrome-ink-muted)',
            background: 'var(--chrome-raised)',
            border: '1px solid var(--chrome-line-strong)',
          }}
        >
          {tag}
        </span>
      )}
      {badge !== undefined && badge > 0 && (
        <span
          aria-hidden="true"
          data-testid={`nav-badge-${label.toLowerCase()}`}
          className="tabular-nums"
          style={{
            fontSize: 12,
            fontWeight: 600,
            color: 'var(--chrome-accent)',
          }}
        >
          {badge}
        </span>
      )}
    </NavControl>
  );
}

/**
 * `<a href>` for a place, `<button>` for an action, with the same look.
 * `aria-current="page"` marks the active place for assistive tech; a link
 * needs it where a button relied on its styling alone.
 */
function NavControl({
  href,
  active,
  onClick,
  children,
  ...rest
}: {
  href?: string;
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  className: string;
  style: React.CSSProperties;
  'aria-label': string;
  onMouseEnter: React.MouseEventHandler<HTMLElement>;
  onMouseLeave: React.MouseEventHandler<HTMLElement>;
}) {
  if (href) {
    return (
      <a href={href} aria-current={active ? 'page' : undefined} onClick={(e) => onNavClick(e, onClick)} {...rest}>
        {children}
      </a>
    );
  }
  return (
    <button type="button" onClick={onClick} {...rest}>
      {children}
    </button>
  );
}

// ── Collapsed-state tooltip ───────────────────────────────────────

function CollapsedTooltip({
  show,
  label,
  badge,
}: {
  show: boolean;
  label: string;
  badge?: number;
}) {
  if (!show) return null;
  // Floats to the right of the collapsed rail, over the white content
  // area → light content tokens (gotcha 7), not chrome.
  return (
    <div
      className="absolute left-[calc(100%+10px)] top-1/2 -translate-y-1/2 z-[200] whitespace-nowrap flex items-center gap-2 px-2.5 py-1.5 rounded-[var(--r-sm)] border pointer-events-none fade-in"
      style={{
        background: 'var(--canvas-overlay)',
        borderColor: 'var(--line)',
        color: 'var(--ink)',
        boxShadow: 'var(--shadow-md)',
        fontSize: 12,
      }}
    >
      <span>{label}</span>
      {badge !== undefined && badge > 0 && (
        <span
          className="tabular-nums"
          style={{
            fontSize: 12,
            fontWeight: 600,
            color: 'var(--iris-hover)',
          }}
        >
          {badge}
        </span>
      )}
    </div>
  );
}

// ── Thread row with 3-dot hover menu ─────────────────────────────

function ThreadRow({
  title,
  createdAt,
  active,
  live = false,
  onSelect,
}: {
  title: string;
  createdAt: number;
  active: boolean;
  /** Live-watch PR — a run is in flight for this thread (pulse dot). */
  live?: boolean;
  onSelect: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const rowRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (rowRef.current && !rowRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [menuOpen]);

  return (
    <div className="relative group" ref={rowRef}>
      <button
        type="button"
        onClick={onSelect}
        className="flex flex-col gap-0.5 w-full px-3 pr-8 py-1.5 rounded-[var(--r-sm)] text-left transition-colors duration-150 overflow-hidden"
        style={{
          background: active ? 'var(--chrome-raised)' : 'transparent',
          color: active ? 'var(--chrome-ink)' : 'var(--chrome-ink-muted)',
        }}
        onMouseEnter={(e) => {
          if (active) return;
          const el = e.currentTarget as HTMLElement;
          el.style.background = 'var(--chrome-raised)';
          el.style.color = 'var(--chrome-ink)';
        }}
        onMouseLeave={(e) => {
          if (active) return;
          const el = e.currentTarget as HTMLElement;
          el.style.background = 'transparent';
          el.style.color = 'var(--chrome-ink-muted)';
        }}
      >
        <span className="w-full truncate text-[13px] leading-tight flex items-center gap-1.5">
          {live && (
            <span
              data-testid="thread-live-dot"
              aria-label="Agent running"
              title="Agent running"
              className="shrink-0 w-1.5 h-1.5 rounded-full animate-pulse"
              style={{ background: 'var(--chrome-accent)' }}
            />
          )}
          <span className="truncate">{title}</span>
        </span>
        <span
          className="text-[12px] leading-none"
          style={{ color: 'var(--chrome-ink-muted)' }}
        >
          {relativeTime(createdAt)}
        </span>
      </button>
      <button
        type="button"
        aria-label="Thread actions"
        onClick={(e) => {
          e.stopPropagation();
          setMenuOpen((v) => !v);
        }}
        className={`absolute right-1.5 top-1/2 -translate-y-1/2 flex items-center justify-center w-6 h-6 rounded-[var(--r-xs)] transition-opacity duration-150 ${
          menuOpen ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
        }`}
        style={{ color: 'var(--chrome-ink-muted)' }}
        onMouseEnter={(e) => {
          const el = e.currentTarget as HTMLElement;
          el.style.background = 'var(--chrome-raised)';
          el.style.color = 'var(--chrome-ink)';
        }}
        onMouseLeave={(e) => {
          const el = e.currentTarget as HTMLElement;
          el.style.background = 'transparent';
          el.style.color = 'var(--chrome-ink-muted)';
        }}
      >
        <MoreHorizontal size={13} strokeWidth={1.5} />
      </button>

      {/* Dropdown floats over the white content area → light content
          tokens (gotcha 7), not chrome — same as the TitleBar user menu. */}
      {menuOpen && (
        <div
          role="menu"
          className="absolute right-0 top-[calc(100%+4px)] w-[160px] rounded-[var(--r-md)] border fade-in z-20"
          style={{
            background: 'var(--canvas-overlay)',
            borderColor: 'var(--line)',
            boxShadow: 'var(--shadow-md)',
          }}
        >
          <ThreadMenuItem
            icon={<Pencil size={13} strokeWidth={1.5} />}
            label="Rename"
            onClick={() => setMenuOpen(false)}
          />
          <ThreadMenuItem
            icon={<Trash2 size={13} strokeWidth={1.5} />}
            label="Delete"
            danger
            onClick={() => setMenuOpen(false)}
          />
        </div>
      )}
    </div>
  );
}

function ThreadMenuItem({
  icon,
  label,
  danger,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  danger?: boolean;
  onClick: () => void;
}) {
  const baseColor = danger ? 'var(--danger)' : 'var(--ink-muted)';
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      className="flex items-center gap-2.5 w-full px-3 py-2 text-left text-[13px] transition-colors duration-150"
      style={{ color: baseColor }}
      onMouseEnter={(e) => {
        const el = e.currentTarget as HTMLElement;
        el.style.background = 'var(--canvas-hover)';
        if (!danger) el.style.color = 'var(--ink)';
      }}
      onMouseLeave={(e) => {
        const el = e.currentTarget as HTMLElement;
        el.style.background = 'transparent';
        el.style.color = baseColor;
      }}
    >
      <span style={{ color: danger ? 'var(--danger)' : 'var(--ink-subtle)' }}>
        {icon}
      </span>
      {label}
    </button>
  );
}

// ── Thread recency helpers ────────────────────────────────────────
//
// Replaces the old colored StatusDot: threads now disambiguate by a
// relative timestamp + recency grouping (Today / Earlier this week /
// Older), which does the visual job the dots did without the
// generic-template look.

function relativeTime(ts: number): string {
  const now = Date.now();
  const diff = Math.max(0, now - ts);
  const min = Math.floor(diff / 60000);
  if (min < 1) return 'just now';
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day === 1) return 'yesterday';
  if (day < 7) return `${day}d ago`;
  const wk = Math.floor(day / 7);
  return wk < 5 ? `${wk}w ago` : `${Math.floor(day / 30)}mo ago`;
}

interface RecencyGroup<T> {
  label: string;
  threads: T[];
}

function groupThreadsByRecency<T extends { createdAt: number }>(
  threads: T[],
): RecencyGroup<T>[] {
  const now = Date.now();
  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  const todayMs = startOfToday.getTime();
  const weekMs = todayMs - 6 * 24 * 60 * 60 * 1000;

  const today: T[] = [];
  const week: T[] = [];
  const older: T[] = [];
  for (const t of threads) {
    if (t.createdAt >= todayMs) today.push(t);
    else if (t.createdAt >= weekMs) week.push(t);
    else older.push(t);
  }
  return [
    { label: 'Today', threads: today },
    { label: 'Earlier this week', threads: week },
    { label: 'Older', threads: older },
  ].filter((g) => g.threads.length > 0);
}
