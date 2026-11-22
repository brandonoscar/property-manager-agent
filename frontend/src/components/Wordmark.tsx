/**
 * The name, set the way occupella.com sets it: lowercase "occupella" in
 * Fraunces. Lowercase is the mark, not a style choice; "Occupella" in the
 * title face is a different logo. The site's copy is `Wordmark` in
 * helixis-onboarding `src/Site.tsx`; change one, change both.
 *
 * TODO(brandon): provide the lowercase Fraunces "occupella" SVG. Until then
 * this is live text in Fraunces; swapping the SVG in is this one element.
 */
export function Wordmark({ size = 22 }: { size?: number }) {
  return (
    <span
      data-testid="wordmark"
      style={{
        color: 'var(--ink)',
        fontFamily: 'var(--font-wordmark)',
        fontOpticalSizing: 'auto',
        fontSize: size,
        fontWeight: 600,
        letterSpacing: '-0.015em',
        lineHeight: 1,
      }}
    >
      occupella
    </span>
  );
}
