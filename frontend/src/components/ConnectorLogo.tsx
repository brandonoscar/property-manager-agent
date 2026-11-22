/**
 * Connector logo — colored letter tile per brand.
 *
 * We used to fetch real brand logos from Clearbit's free Logo API, but
 * Clearbit shut down the public Logo API in late 2024 and DNS for
 * ``logo.clearbit.com`` no longer resolves at all. The hung <img>
 * requests blocked the Connectors page from rendering (browsers don't
 * fire ``onError`` for DNS-pending loads, so the row never gave up
 * waiting). We dropped the dependency entirely. Letter tiles are
 * theme-consistent in the dark UI and never break.
 */

import type { CSSProperties, ReactNode } from 'react';

interface BrandStyle {
  letters: string;
  bg: string;
  fg: string;
}

/**
 * Real brand marks, drawn INLINE.
 *
 * ⚠ Never re-introduce a fetched logo. The module note above records why:
 * Clearbit's Logo API went away, its DNS stopped resolving, and the hung
 * <img> loads blocked the whole Connectors page from rendering — browsers do
 * not fire onError while a DNS lookup is still pending, so the row waited
 * forever. Inline SVG cannot hang, cannot 404 and needs no network.
 *
 * Google Drive / Docs / Sheets had no entry at all and were rendering the '?'
 * fallback tile.
 */
const BRAND_MARKS: Record<string, (size: number) => ReactNode> = {
  buildium: (size) => (
    // Buildium's own mark, supplied by the founder from their asset (the
    // Illustrator wrapper — ns_extend namespaces, <metadata>, the .st0 style
    // class — stripped; the path data is byte-for-byte theirs).
    //
    // ⚠ fillRule is evenodd as INSURANCE, not as a fix. Both subpaths live
    // in one path: the 100x100 green field, then the glyph knocked out of
    // it. Measured by shoelace on this exact data — field -10000 (CW),
    // glyph +1077 (CCW) — so the windings are opposite and the SVG default
    // (nonzero) renders it correctly today. evenodd knocks the glyph out
    // whichever way it winds, so a future re-export with a flipped glyph
    // cannot turn the mark into a solid green square. That failure would
    // read as a rendering quirk rather than a bug, which is why it is worth
    // one attribute to make impossible.
    <svg width={size} height={size} viewBox="0 0 100 100" aria-hidden="true">
      <path
        fill="#70B37F"
        fillRule="evenodd"
        d="M0,0v100h100V0H0z M54.4,49v34l-16.6-5c-1.1-0.3-2-1.6-2-2.7V49h-7l20.8-26.2c0.7-0.9,2.3-1.8,3.4-2.1l18.2-4.4L45.4,49H54.4z"
      />
    </svg>
  ),
  google_drive: (size) => (
    <svg width={size} height={size} viewBox="0 0 87.3 78" aria-hidden="true">
      <path d="m6.6 66.85 3.85 6.65c.8 1.4 1.95 2.5 3.3 3.3l13.75-23.8H0c0 1.55.4 3.1 1.2 4.5z" fill="#0066da" />
      <path d="M43.65 25 29.9 1.2c-1.35.8-2.5 1.9-3.3 3.3l-25.4 44a9.06 9.06 0 0 0-1.2 4.5h27.5z" fill="#00ac47" />
      <path d="M73.55 76.8c1.35-.8 2.5-1.9 3.3-3.3l1.6-2.75 7.65-13.25c.8-1.4 1.2-2.95 1.2-4.5h-27.502l5.852 11.5z" fill="#ea4335" />
      <path d="M43.65 25 57.4 1.2C56.05.4 54.5 0 52.9 0H34.4c-1.6 0-3.15.45-4.5 1.2z" fill="#00832d" />
      <path d="M59.8 53H27.5L13.75 76.8c1.35.8 2.9 1.2 4.5 1.2h50.8c1.6 0 3.15-.45 4.5-1.2z" fill="#2684fc" />
      <path d="M73.4 26.5 60.7 4.5c-.8-1.4-1.95-2.5-3.3-3.3L43.65 25l16.15 28h27.45c0-1.55-.4-3.1-1.2-4.5z" fill="#ffba00" />
    </svg>
  ),
  gmail: (size) => (
    // The envelope, in Gmail's own construction: two side panels, the two
    // fold triangles, and the red M across the middle.
    <svg width={size} height={size} viewBox="0 0 52 40" aria-hidden="true">
      <path d="M3.5 40h9V18L0 8.5v28A3.5 3.5 0 0 0 3.5 40" fill="#4285f4" />
      <path d="M39.5 40h9a3.5 3.5 0 0 0 3.5-3.5v-28L39.5 18" fill="#34a853" />
      <path d="M39.5 3.5V18L52 8.5V5.25c0-3.01-3.44-4.73-5.85-2.925" fill="#fbbc04" />
      <path d="M12.5 18V3.5L26 13.625 39.5 3.5V18L26 28.125" fill="#ea4335" />
      <path d="M0 5.25V8.5L12.5 18V3.5L5.85 2.325C3.44.52 0 2.24 0 5.25" fill="#c5221f" />
    </svg>
  ),
  google_calendar: (size) => (
    // ⚠ The FAVICON, not the full app icon. Google Calendar's product icon
    // carries four colored corner accents; at the 20-36px this renders at
    // they are a smear, and I could not verify their exact arrangement from
    // this container (no egress). The white card with a blue rule and the
    // date numeral is what the favicon actually is, and it is the half that
    // makes it recognisable. Drawing the corners from memory would be
    // inventing a company's mark, which is worse than drawing less of it.
    <svg width={size} height={size} viewBox="0 0 48 48" aria-hidden="true">
      <rect x="4" y="6" width="40" height="38" rx="4" fill="#fff" stroke="#dadce0" strokeWidth="2" />
      <path d="M4 10a4 4 0 0 1 4-4h32a4 4 0 0 1 4 4v4H4z" fill="#1a73e8" />
      <text
        x="24"
        y="35"
        textAnchor="middle"
        fontFamily="system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
        fontSize="20"
        fontWeight="600"
        fill="#1a73e8"
      >
        31
      </text>
    </svg>
  ),
  google_docs: (size) => (
    <svg width={size} height={size} viewBox="0 0 48 64" aria-hidden="true">
      <path d="M29 0H6a6 6 0 0 0-6 6v52a6 6 0 0 0 6 6h36a6 6 0 0 0 6-6V19z" fill="#4285f4" />
      <path d="M29 0v13a6 6 0 0 0 6 6h13z" fill="#a1c2fa" />
      <g fill="#fff">
        <rect x="11" y="28" width="26" height="3" rx="1.5" />
        <rect x="11" y="36" width="26" height="3" rx="1.5" />
        <rect x="11" y="44" width="17" height="3" rx="1.5" />
      </g>
    </svg>
  ),
  google_sheets: (size) => (
    <svg width={size} height={size} viewBox="0 0 48 64" aria-hidden="true">
      <path d="M29 0H6a6 6 0 0 0-6 6v52a6 6 0 0 0 6 6h36a6 6 0 0 0 6-6V19z" fill="#0f9d58" />
      <path d="M29 0v13a6 6 0 0 0 6 6h13z" fill="#8ed1b1" />
      <path
        d="M11 28h26v20H11z M11 34.5h26 M11 41h26 M19.7 28v20 M28.4 28v20"
        fill="none"
        stroke="#fff"
        strokeWidth="2.6"
      />
    </svg>
  ),
};

/**
 * Marks that ARE the tile — a full-bleed square with the glyph knocked out —
 * rather than a glyph floating on transparency.
 *
 * The Google marks are the second kind, so they inset to 82% and sit on the
 * page background. Buildium's is the first kind: inset, it reads as a green
 * square that is mysteriously smaller than every letter tile beside it, with
 * sharp corners where those are rounded. These render edge to edge and get
 * the tile's own corner radius.
 */
const FULL_BLEED_MARKS = new Set(['buildium']);

const BRAND_STYLES: Record<string, BrandStyle> = {
  // No `buildium` entry: BRAND_MARKS is consulted first and now carries the
  // real mark, so a letter tile here would be unreachable code claiming to
  // be a fallback.
  appfolio: { letters: 'A', bg: '#00A1DF', fg: '#FFFFFF' },
  yardi: { letters: 'Y', bg: '#D22B2B', fg: '#FFFFFF' },
  rentmanager: { letters: 'RM', bg: '#1F3A68', fg: '#FFFFFF' },
  // Neutral tokens, not brand colours: neither company's palette could be
  // checked from where these were written, and a guessed colour would claim
  // to be their mark. Without an entry both rendered the '?' fallback.
  rentvine: { letters: 'RV', bg: 'var(--ink-muted)', fg: 'var(--canvas)' },
  propertyware: { letters: 'PW', bg: 'var(--ink-muted)', fg: 'var(--canvas)' },
  doorloop: { letters: 'DL', bg: 'var(--ink-muted)', fg: 'var(--canvas)' },
  zillow: { letters: 'Z', bg: '#006AFF', fg: '#FFFFFF' },
  apartments: { letters: 'Ap', bg: '#E55A2B', fg: '#FFFFFF' },
  realtor: { letters: 'R', bg: '#C82127', fg: '#FFFFFF' },
  gmail: { letters: 'G', bg: '#EA4335', fg: '#FFFFFF' },
  // Neutral on purpose: '#F22F46' is Twilio's brand red and the mark read
  // as their logo in a white-labeled product.
  twilio: { letters: 'SMS', bg: '#4B5563', fg: '#FFFFFF' },
  google_calendar: { letters: '31', bg: '#1A73E8', fg: '#FFFFFF' },
};

const FALLBACK: BrandStyle = {
  letters: '?',
  bg: 'var(--bg-active)',
  fg: 'var(--text-secondary)',
};

export function ConnectorLogo({ id, size = 36 }: { id: string; size?: number }) {
  const mark = BRAND_MARKS[id];
  if (mark) {
    const fullBleed = FULL_BLEED_MARKS.has(id);
    return (
      <span
        className={
          'inline-flex items-center justify-center flex-shrink-0' +
          (fullBleed ? ' rounded-sm overflow-hidden shadow-sm' : '')
        }
        style={{ width: size, height: size }}
      >
        {mark(fullBleed ? size : Math.round(size * 0.82))}
      </span>
    );
  }
  const style = BRAND_STYLES[id] ?? FALLBACK;
  const tileStyle: CSSProperties = {
    width: size,
    height: size,
    background: style.bg,
    color: style.fg,
    fontSize: Math.max(12, size * 0.42),
  };
  return (
    <span
      className="inline-flex items-center justify-center rounded-sm font-bold flex-shrink-0 tracking-tight shadow-sm"
      style={tileStyle}
    >
      {style.letters}
    </span>
  );
}
