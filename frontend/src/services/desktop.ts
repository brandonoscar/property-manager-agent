/**
 * Desktop-app detection + release lookup.
 *
 * The Occupella browser (the Electron shell, repo `brandonoscar/helixiselectron`)
 * is what turns "search" into real chrome tabs + a docked copilot. The web app
 * only gets a plain new-tab. So the Search surface gates on whether it's running
 * inside the shell and, when it isn't, offers the desktop download.
 *
 * Detection: the shell hosts the deployed web app with its preload bridge
 * attached, which injects `window.helixis` (see helixiselectron copilot.ts).
 * A plain browser has no such object — a reliable, synchronous signal.
 *
 * Download link: the electron repo is PUBLIC, so we resolve the latest release
 * straight from GitHub's API in the browser. The call comes from the user's own
 * IP, so the 60/hr anonymous limit is per-user (a non-issue) and there's no
 * backend to deploy or rate-limit centrally.
 */

const RELEASES_API =
  'https://api.github.com/repos/brandonoscar/helixiselectron/releases/latest';
export const RELEASES_PAGE =
  'https://github.com/brandonoscar/helixiselectron/releases';

export type DesktopOS = 'mac' | 'windows' | 'linux' | 'other';

/** True when running inside the Occupella desktop shell (preload-injected). */
export function isDesktopApp(): boolean {
  return typeof window !== 'undefined' && 'helixis' in window;
}

/** Best-effort OS guess for labelling the primary download button. */
export function detectOS(): DesktopOS {
  if (typeof navigator === 'undefined') return 'other';
  const ua = `${navigator.userAgent} ${navigator.platform ?? ''}`.toLowerCase();
  if (/mac|iphone|ipad|ipod/.test(ua)) return 'mac';
  if (/win/.test(ua)) return 'windows';
  if (/linux|x11/.test(ua)) return 'linux';
  return 'other';
}

export interface DesktopRelease {
  /** The release tag, e.g. "v0.1.0". */
  version: string;
  /** The GitHub Release page — fallback when no OS asset matches. */
  releaseUrl: string;
  /** Direct installer URLs by platform, when that platform's asset exists. */
  assets: Partial<Record<Exclude<DesktopOS, 'other'>, string>>;
}

interface GithubAsset {
  name: string;
  browser_download_url: string;
}
interface GithubRelease {
  tag_name: string;
  html_url: string;
  assets: GithubAsset[];
}

/** Map an installer filename to the platform it targets, or null. */
function platformOf(name: string): Exclude<DesktopOS, 'other'> | null {
  const n = name.toLowerCase();
  if (n.endsWith('.dmg')) return 'mac';
  if (n.endsWith('.exe')) return 'windows';
  if (n.endsWith('.appimage')) return 'linux';
  return null;
}

/**
 * Fetch the latest published release, or null when none exists yet (the API
 * 404s until the first `v*` tag is cut) or the call fails. Never throws — the
 * caller renders a "coming soon" state on null.
 */
export async function fetchLatestRelease(
  signal?: AbortSignal,
): Promise<DesktopRelease | null> {
  try {
    const res = await fetch(RELEASES_API, {
      headers: { Accept: 'application/vnd.github+json' },
      signal,
    });
    if (!res.ok) return null; // 404 = no release published yet
    const data = (await res.json()) as GithubRelease;
    const assets: DesktopRelease['assets'] = {};
    for (const a of data.assets ?? []) {
      const p = platformOf(a.name);
      // First asset per platform wins (there is only one installer each).
      if (p && !assets[p]) assets[p] = a.browser_download_url;
    }
    return {
      version: data.tag_name,
      releaseUrl: data.html_url,
      assets,
    };
  } catch {
    return null;
  }
}

/** The direct installer URL for an OS, falling back to the release page. */
export function downloadUrlFor(release: DesktopRelease, os: DesktopOS): string {
  if (os !== 'other' && release.assets[os]) return release.assets[os] as string;
  return release.releaseUrl;
}

/** Human label for a platform. */
export function osLabel(os: DesktopOS): string {
  return { mac: 'Mac', windows: 'Windows', linux: 'Linux', other: 'desktop' }[os];
}
