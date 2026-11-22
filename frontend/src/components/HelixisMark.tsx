/**
 * The Occupella mark (the ◐), matching the browser chrome's logo. Shared by the
 * Search surface and the desktop-download gate so the wordmark reads
 * identically on both. Swap the glyph for the real logo when it lands.
 */

export function HelixisMark({ size = 30 }: { size?: number }) {
  return (
    <span
      aria-hidden
      className="leading-none"
      style={{ color: 'var(--iris)', fontSize: `${size}px` }}
    >
      ◐
    </span>
  );
}
