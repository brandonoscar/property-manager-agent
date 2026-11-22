/**
 * TitleBar — mobile hamburger (2026-08-15 audit, debt #1).
 *
 * Below md the sidebar is an off-canvas drawer and the hamburger in the
 * app shell is the ONLY way to open navigation — `setSidebarOpen(true)`
 * was previously never called anywhere, making every page a dead end at
 * phone widths. These tests pin the opener's existence and wiring; the
 * md:hidden visibility split is CSS (not assertable in jsdom).
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { TitleBar } from './TitleBar';

describe('TitleBar mobile nav opener', () => {
  it('renders the hamburger and fires onOpenNav on click', () => {
    const onOpenNav = vi.fn();
    render(<TitleBar onOpenNav={onOpenNav} />);
    const button = screen.getByTestId('titlebar-open-nav');
    expect(button).toBeInTheDocument();
    expect(button).toHaveAccessibleName('Open navigation');
    // Mobile-only: hidden at the same md breakpoint the sidebar uses
    // for its drawer mode.
    expect(button.className).toContain('md:hidden');
    fireEvent.click(button);
    expect(onOpenNav).toHaveBeenCalledTimes(1);
  });

  it('renders no opener when the shell does not provide one', () => {
    render(<TitleBar />);
    expect(screen.queryByTestId('titlebar-open-nav')).toBeNull();
  });

  it('keeps the desktop collapse toggle, marked desktop-only', () => {
    render(<TitleBar onOpenNav={vi.fn()} />);
    const toggle = screen.getByLabelText(/collapse sidebar|expand sidebar/i);
    // Collapse is meaningless when the sidebar is an overlay drawer,
    // so the toggle hides below md rather than confusing the phone UI.
    expect(toggle.className).toContain('max-md:hidden');
  });
});
