/**
 * Every error a person has to act on is announced, and is not red-only.
 *
 * ⚠ Thirteen error messages in this app announced nothing and carried no signal
 * but colour. Both failures are silent by construction: on screen the message
 * is plainly there, so a sighted developer with normal colour vision sees a
 * working form. The people it fails are the two who cannot check for
 * themselves — somebody listening, for whom the submit silently did nothing,
 * and somebody who cannot separate `--danger` from `--ink` and reads the
 * failure as a caption.
 *
 * The second describe block is the one that matters long-term: it reads the
 * CORPUS, because the failure mode is "a new form quietly reopens the hole"
 * and no behavioural test of the component can see that (gotcha 45).
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { FormError } from './FormError';

describe('FormError', () => {
  it('is announced when it appears', () => {
    render(<FormError>That code was not accepted.</FormError>);
    expect(screen.getByRole('alert').textContent).toContain('That code was not accepted.');
  });

  it('carries a signal that is not colour', () => {
    const { container } = render(<FormError>Something failed.</FormError>);
    expect(container.querySelector('svg')).not.toBeNull();
  });

  it('hides that icon from a screen reader', () => {
    // `role="alert"` already reads the text out. Announcing the decoration too
    // spends the listener's attention on nothing.
    const { container } = render(<FormError>Something failed.</FormError>);
    expect(container.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true');
  });

  it('renders nothing when there is no error', () => {
    // A permanently-present empty alert region gets announced on unrelated
    // re-renders, which is how people learn to ignore the role.
    const { container } = render(<FormError>{null}</FormError>);
    expect(container.querySelector('[role="alert"]')).toBeNull();
  });
});

// The corpus sweep. Vite's import.meta.glob reads every component source as
// text, so a form added next month is covered without anybody remembering.
const SOURCES = import.meta.glob('./**/*.tsx', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

/** `{something && (…)}` blocks whose body is styled with a danger colour. */
function silentErrorBlocks(source: string): string[] {
  const found: string[] = [];
  const opener = /\{\s*\w*[Ee]rror\w*\s*&&/g;
  let m: RegExpExecArray | null;
  while ((m = opener.exec(source)) !== null) {
    const chunk = source.slice(m.index, m.index + 400);
    if (!chunk.includes('danger')) continue; // not an error display
    if (
      chunk.includes('role="alert"') ||
      chunk.includes('aria-live') ||
      chunk.includes('FormError')
    ) {
      continue;
    }
    found.push(source.slice(0, m.index).split('\n').length.toString());
  }
  return found;
}

describe('no form in the app fails silently', () => {
  it('finds error displays at all — otherwise this sweep proves nothing', () => {
    // A regex that matches zero blocks would report green forever. Assert the
    // instrument works before trusting its verdict.
    const total = Object.values(SOURCES).filter((s) => s.includes('FormError')).length;
    expect(total).toBeGreaterThan(3);
  });

  it('has no error message that is red and silent', () => {
    const offenders: string[] = [];
    for (const [path, source] of Object.entries(SOURCES)) {
      if (path.includes('.test.')) continue;
      for (const line of silentErrorBlocks(source)) offenders.push(`${path}:${line}`);
    }
    expect(
      offenders,
      'these error messages are announced to nobody and are distinguished only ' +
        'by colour. Use <FormError>, which carries role="alert" and an icon.',
    ).toEqual([]);
  });
});
