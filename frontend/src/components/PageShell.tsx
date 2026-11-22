/**
 * Standard main-surface page shell.
 *
 * Solves the layout-asymmetry bug across Activity, Webhooks, and
 * Settings: the 260px Sidebar is pinned-left, but page content used
 * to stretch full-width with mirrored padding — making the visual
 * center of the viewport sit well right of the content midpoint, and
 * everything appear to crowd against the sidebar.
 *
 * This shell mirrors `ChatThread`'s correct pattern:
 *
 *   <main-surface>
 *     <header h-12 px-12 mx-auto max-w-1100 />
 *     <body  px-12 mx-auto max-w-1100 />
 *
 * Header gets the same gutter as the body so dots / chips / titles
 * align vertically with the content rows below them. Pages opt into
 * a different `maxWidth` when they need it (Settings uses 1200 to
 * fit its 220px secondary rail without squishing the detail pane).
 */

import type { ReactNode } from 'react';

interface PageShellProps {
  /** Header band — usually a title plus right-aligned filter chips. */
  header: ReactNode;
  /** Body content. The shell handles its outer horizontal gutters; do
   *  not add page-level `px-*` inside. */
  children: ReactNode;
  /** Override the inner column max width. Defaults to 1100px to match
   *  ChatThread. */
  maxWidth?: number;
}

export function PageShell({
  header,
  children,
  maxWidth = 1100,
}: PageShellProps) {
  return (
    <div className="flex-1 flex flex-col overflow-hidden min-h-0">
      <div
        className="h-12 shrink-0 border-b"
        style={{ borderColor: 'var(--line)' }}
      >
        <div
          className="h-full flex items-center px-12 mx-auto w-full"
          style={{ maxWidth: `${maxWidth}px` }}
        >
          {header}
        </div>
      </div>
      <div className="flex-1 overflow-y-auto">
        <div
          className="px-12 mx-auto w-full"
          style={{ maxWidth: `${maxWidth}px` }}
        >
          {children}
        </div>
      </div>
    </div>
  );
}
