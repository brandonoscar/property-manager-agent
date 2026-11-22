/**
 * Unit tests for desktop-app detection + release lookup (services/desktop.ts).
 */

import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  detectOS,
  downloadUrlFor,
  fetchLatestRelease,
  isDesktopApp,
  osLabel,
  type DesktopRelease,
} from './desktop';

afterEach(() => {
  vi.restoreAllMocks();
  delete (window as unknown as { helixis?: unknown }).helixis;
});

describe('isDesktopApp', () => {
  it('is false in a plain browser', () => {
    expect(isDesktopApp()).toBe(false);
  });

  it('is true when the shell injected window.helixis', () => {
    (window as unknown as { helixis?: unknown }).helixis = {};
    expect(isDesktopApp()).toBe(true);
  });
});

describe('detectOS', () => {
  const setUA = (ua: string) =>
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(ua);

  it('detects mac', () => {
    setUA('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)');
    expect(detectOS()).toBe('mac');
  });

  it('detects windows', () => {
    setUA('Mozilla/5.0 (Windows NT 10.0; Win64; x64)');
    expect(detectOS()).toBe('windows');
  });

  it('detects linux', () => {
    setUA('Mozilla/5.0 (X11; Linux x86_64)');
    expect(detectOS()).toBe('linux');
  });
});

describe('fetchLatestRelease', () => {
  const release = (assets: { name: string; browser_download_url: string }[]) =>
    new Response(
      JSON.stringify({ tag_name: 'v0.1.0', html_url: 'https://gh/rel', assets }),
      { status: 200 },
    );

  it('maps installer assets to platforms by extension', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      release([
        { name: 'Occupella-0.1.0.dmg', browser_download_url: 'https://gh/mac.dmg' },
        { name: 'Occupella-Setup-0.1.0.exe', browser_download_url: 'https://gh/win.exe' },
        { name: 'Occupella-0.1.0.AppImage', browser_download_url: 'https://gh/lin.AppImage' },
        { name: 'latest.yml', browser_download_url: 'https://gh/latest.yml' },
      ]),
    );

    const r = await fetchLatestRelease();
    expect(r).toEqual({
      version: 'v0.1.0',
      releaseUrl: 'https://gh/rel',
      assets: {
        mac: 'https://gh/mac.dmg',
        windows: 'https://gh/win.exe',
        linux: 'https://gh/lin.AppImage',
      },
    });
  });

  it('returns null when no release is published yet (404)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 404 }));
    expect(await fetchLatestRelease()).toBeNull();
  });

  it('returns null (never throws) on a network error', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('offline'));
    expect(await fetchLatestRelease()).toBeNull();
  });
});

describe('downloadUrlFor', () => {
  const rel: DesktopRelease = {
    version: 'v0.1.0',
    releaseUrl: 'https://gh/rel',
    assets: { mac: 'https://gh/mac.dmg' },
  };

  it('returns the OS-matched installer when present', () => {
    expect(downloadUrlFor(rel, 'mac')).toBe('https://gh/mac.dmg');
  });

  it('falls back to the release page when the OS asset is missing', () => {
    expect(downloadUrlFor(rel, 'windows')).toBe('https://gh/rel');
    expect(downloadUrlFor(rel, 'other')).toBe('https://gh/rel');
  });
});

describe('osLabel', () => {
  it('labels each platform', () => {
    expect(osLabel('mac')).toBe('Mac');
    expect(osLabel('windows')).toBe('Windows');
    expect(osLabel('linux')).toBe('Linux');
  });
});
