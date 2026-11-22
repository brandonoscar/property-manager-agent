/**
 * Command palette registry + dispatcher.
 *
 * Every palette entry funnels through ``executeCommand(id)`` so the UI
 * is purely presentational and we have a single hook for telemetry +
 * future things like keyboard hints, recent-command history, etc.
 */

import { toast } from 'sonner';
import { capturePaletteCommandExecuted } from '../services/analytics';
import { performSignOut } from '../services/auth';
import { useStore } from '../stores/store';

export type CommandGroup = 'Navigate' | 'Action';

export interface Command {
  /** Stable id used by the dispatcher and analytics. */
  id: string;
  /** Visible name in the palette. */
  label: string;
  /** Group the command renders under. */
  group: CommandGroup;
  /** Aliases the fuzzy filter should also match. */
  keywords?: string[];
  /**
   * Dev-only command — hidden from the palette in production builds so it
   * never surfaces to a customer. Still available to the operator via the
   * ``helixis_dev`` localStorage flag (see {@link devCommandsVisible}).
   */
  devOnly?: boolean;
  /** Actual side effect. Async so signOut and similar fit naturally. */
  run: () => void | Promise<void>;
}

/**
 * Whether dev-only commands should appear in the palette. True in dev builds,
 * or when the operator has opted in on any build with
 * ``localStorage.setItem('helixis_dev', '1')``. Guarded so it never throws in
 * SSR / restricted-storage contexts (#7 F5b).
 */
export function devCommandsVisible(): boolean {
  if (import.meta.env.DEV) return true;
  try {
    return localStorage.getItem('helixis_dev') === '1';
  } catch {
    return false;
  }
}

export const COMMANDS: Command[] = [
  // ── Navigate ────────────────────────────────────────────────────
  {
    id: 'nav.chat',
    label: 'Go to Chat',
    group: 'Navigate',
    keywords: ['chat', 'home', 'conversations'],
    run: () => {
      useStore.getState().setView('chat');
    },
  },
  {
    id: 'nav.inbox',
    label: 'Go to Inbox',
    group: 'Navigate',
    keywords: ['inbox', 'tasks', 'webhooks', 'events'],
    run: () => {
      useStore.getState().setView('inbox');
    },
  },
  {
    id: 'nav.activity',
    label: 'Go to Team Activity',
    group: 'Navigate',
    keywords: ['activity', 'log', 'history', 'audit', 'team'],
    run: () => {
      useStore.getState().openSettings('activity');
    },
  },
  {
    id: 'nav.operations',
    label: 'Go to Operations',
    group: 'Navigate',
    keywords: ['operations', 'properties', 'tenants', 'vendors', 'team', 'portfolio', 'analytics'],
    run: () => {
      useStore.getState().setView('properties');
    },
  },
  {
    id: 'nav.crm',
    label: 'Go to Leasing',
    group: 'Navigate',
    keywords: ['leasing', 'crm', 'leads', 'pipeline', 'prospects', 'contacts', 'renters', 'owners'],
    run: () => {
      useStore.getState().setView('crm');
    },
  },
  {
    id: 'nav.settings',
    label: 'Go to Settings',
    group: 'Navigate',
    keywords: ['settings', 'preferences', 'config', 'connectors'],
    run: () => {
      useStore.getState().setView('settings');
    },
  },
  {
    // PR #141 — dev-only orchestrator-runs inspection page. #7 F5b — hidden
    // from the prod palette (a customer shouldn't see a "Dev:" command); the
    // operator re-enables it with ``localStorage.setItem('helixis_dev','1')``.
    // The backend route still 404s in prod unless HELIXIS_DEV__SHOW_RUNS_PAGE
    // is set; the page renders an EmptyState on 404 rather than crashing.
    id: 'nav.dev_runs',
    label: 'Dev: Orchestrator runs',
    group: 'Navigate',
    keywords: ['dev', 'runs', 'orchestrator', 'telemetry', 'debug', 'trace'],
    devOnly: true,
    run: () => {
      useStore.getState().setView('dev-runs');
    },
  },

  // ── Action ──────────────────────────────────────────────────────
  {
    id: 'action.new_chat',
    label: 'New chat',
    group: 'Action',
    keywords: ['new', 'compose', 'start', 'task'],
    run: () => {
      const store = useStore.getState();
      // createThread() makes a fresh thread + selects it; mirrors the
      // "+ New" sidebar button in Sidebar.tsx.
      store.createThread();
      store.setView('chat');
    },
  },
  {
    id: 'action.sign_out',
    label: 'Sign out',
    group: 'Action',
    keywords: ['logout', 'log out', 'sign out', 'leave'],
    run: async () => {
      try {
        await performSignOut();
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Sign out failed';
        toast.error(`Sign out failed: ${message}`);
      }
    },
  },

  // ── Theme ───────────────────────────────────────────────────────
  // No theme-toggle command yet: main ships only the dark token palette,
  // so flipping a class on <html> would partially break the UI. The
  // command is intentionally OMITTED (not a no-op stub) until light mode
  // lands — re-add it here with a real theme store at that point.
];

/**
 * Look up a command by id and execute it. Always emits the
 * ``palette_command_executed`` analytics event before running so we
 * count attempts, not just successes.
 */
export async function executeCommand(id: string): Promise<void> {
  const cmd = COMMANDS.find((c) => c.id === id);
  if (!cmd) return;
  capturePaletteCommandExecuted(cmd.id, cmd.group);
  await cmd.run();
}
