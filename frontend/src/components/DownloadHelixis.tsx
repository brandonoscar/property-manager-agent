/**
 * DownloadHelixis — the Search surface shown when the web app is NOT running
 * inside the Occupella desktop shell.
 *
 * The real browser experience (chrome tabs, address bar, the docked copilot)
 * only exists in the desktop app, so a plain-browser user who reaches Search is
 * offered the download instead of a web-only search box. See services/desktop.ts
 * for detection + release lookup; SearchPage branches on isDesktopApp().
 */

import { useEffect, useState } from 'react';
import { Download } from 'lucide-react';

import { HelixisMark } from './HelixisMark';
import { captureDesktopDownloadClicked } from '../services/analytics';
import {
  type DesktopOS,
  type DesktopRelease,
  detectOS,
  downloadUrlFor,
  fetchLatestRelease,
  osLabel,
  RELEASES_PAGE,
} from '../services/desktop';

/** The two platforms offered as secondary links, given the primary OS. */
const SECONDARY: Record<DesktopOS, Exclude<DesktopOS, 'other'>[]> = {
  mac: ['windows', 'linux'],
  windows: ['mac', 'linux'],
  linux: ['mac', 'windows'],
  other: ['mac', 'windows'],
};

export function DownloadHelixis() {
  const [release, setRelease] = useState<DesktopRelease | null>(null);
  const [loading, setLoading] = useState(true);
  const os = detectOS();

  useEffect(() => {
    const ctrl = new AbortController();
    fetchLatestRelease(ctrl.signal)
      .then(setRelease)
      .finally(() => setLoading(false));
    return () => ctrl.abort();
  }, []);

  const onDownload = (platform: DesktopOS) =>
    captureDesktopDownloadClicked(platform, Boolean(release));

  return (
    <div className="flex flex-col items-center justify-center h-full px-6">
      <div className="w-full max-w-[420px] flex flex-col items-center -mt-16 text-center">
        <div className="flex items-center gap-3 mb-3">
          <HelixisMark size={38} />
          <span
            className="text-[28px] font-semibold tracking-tight"
            style={{ color: 'var(--ink)' }}
          >
            Occupella
          </span>
        </div>

        <p className="text-[16px]" style={{ color: 'var(--ink)' }}>
          The Occupella browser lives on your desktop.
        </p>
        <p className="mt-1 mb-7 text-[14px]" style={{ color: 'var(--ink-muted)' }}>
          Search with real tabs, an address bar, and your copilot docked
          alongside — in the desktop app.
        </p>

        {loading ? (
          <div className="h-11 text-[14px]" style={{ color: 'var(--ink-subtle)' }}>
            Checking for the latest version…
          </div>
        ) : release ? (
          <>
            <a
              href={downloadUrlFor(release, os)}
              onClick={() => onDownload(os)}
              className="flex items-center justify-center gap-2 h-10 px-5 rounded-[var(--r-pill)] text-[14px] font-medium transition-colors duration-150"
              style={{ background: 'var(--iris)', color: 'var(--canvas)' }}
            >
              <Download size={17} strokeWidth={1.75} />
              Download for {osLabel(os === 'other' ? 'mac' : os)}
            </a>
            <div
              className="mt-3 flex items-center gap-3 text-[13px]"
              style={{ color: 'var(--ink-subtle)' }}
            >
              {SECONDARY[os].map((p) => (
                <a
                  key={p}
                  href={downloadUrlFor(release, p)}
                  onClick={() => onDownload(p)}
                  className="hover:underline"
                  style={{ color: 'var(--iris-hover)' }}
                >
                  {osLabel(p)}
                </a>
              ))}
            </div>
            <p className="mt-4 text-[12px]" style={{ color: 'var(--ink-subtle)' }}>
              Version {release.version}
            </p>
          </>
        ) : (
          <div className="flex flex-col items-center">
            <div
              className="flex items-center justify-center gap-2 h-10 px-5 rounded-[var(--r-pill)] text-[14px] font-medium opacity-60"
              style={{ background: 'var(--canvas-elevated)', color: 'var(--ink-muted)' }}
            >
              <Download size={17} strokeWidth={1.75} />
              Desktop app coming soon
            </div>
            <a
              href={RELEASES_PAGE}
              onClick={() => onDownload(os)}
              className="mt-3 text-[13px] hover:underline"
              style={{ color: 'var(--iris-hover)' }}
            >
              View releases
            </a>
          </div>
        )}
      </div>
    </div>
  );
}
