/**
 * Tests for the desktop-download gate (DownloadHelixis).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import { DownloadHelixis } from './DownloadHelixis';
import * as analytics from '../services/analytics';

const RELEASE = {
  tag_name: 'v0.1.0',
  html_url: 'https://github.com/brandonoscar/helixiselectron/releases/tag/v0.1.0',
  assets: [
    { name: 'Occupella-0.1.0.dmg', browser_download_url: 'https://gh/Occupella.dmg' },
    { name: 'Occupella-Setup-0.1.0.exe', browser_download_url: 'https://gh/Occupella.exe' },
  ],
};

beforeEach(() => {
  vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)',
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('DownloadHelixis', () => {
  it('renders the primary download for the detected OS with the real asset URL', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(RELEASE), { status: 200 }),
    );

    render(<DownloadHelixis />);

    const btn = await screen.findByText(/Download for Mac/i);
    expect(btn.closest('a')?.getAttribute('href')).toBe('https://gh/Occupella.dmg');
    // Secondary platform link points at the Windows installer.
    expect(screen.getByText('Windows').closest('a')?.getAttribute('href')).toBe(
      'https://gh/Occupella.exe',
    );
    expect(screen.getByText(/Version v0\.1\.0/i)).toBeTruthy();
  });

  it('fires a PostHog event with the platform + availability on click', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(RELEASE), { status: 200 }),
    );
    const spy = vi.spyOn(analytics, 'captureDesktopDownloadClicked');

    render(<DownloadHelixis />);
    const btn = await screen.findByText(/Download for Mac/i);
    fireEvent.click(btn);

    expect(spy).toHaveBeenCalledWith('mac', true);
  });

  it('shows "coming soon" when no release is published yet', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 404 }));

    render(<DownloadHelixis />);

    await waitFor(() => expect(screen.getByText(/coming soon/i)).toBeTruthy());
    expect(screen.queryByText(/Download for Mac/i)).toBeNull();
    // The "View releases" fallback still links to the releases page.
    expect(screen.getByText(/View releases/i).closest('a')?.getAttribute('href')).toContain(
      '/releases',
    );
  });
});
