/**
 * Every surface that reads `needsReauth` must also read `stateUnknown`.
 *
 * "Reconnect" and "Connect" are both instructions the customer can carry out,
 * and neither can change a deployment that cannot reach Composio at all. So a
 * surface that branches on the lapsed-grant flag and has never heard of the
 * unreadable-state one tells somebody with a healthy account to fix it — the
 * defect this change closes, waiting to be re-introduced on a third surface.
 *
 * Behavioural tests cover the two surfaces that exist today
 * (`ComposioOAuthDetail.stateUnknown.test.tsx`, the backend route tests). They
 * are structurally blind to a FOURTH one written next month, which is gotcha
 * 45's "a control whose failure mode is silently permits more must read the
 * CORPUS" — so this one reads the files.
 *
 * ⚠ `import.meta.glob(..., '?raw')`, never `node:fs`: the frontend tsconfig
 * carries no node types, so the fs spelling runs green under vitest and turns
 * `tsc --noEmit` RED (gotcha 97).
 */

import { describe, expect, it } from 'vitest';

const SOURCES = import.meta.glob('../**/*.{ts,tsx}', {
  eager: true,
  query: '?raw',
  import: 'default',
}) as Record<string, string>;

/** Test files declare these flags in fixtures; they are not surfaces. */
function isProduction(path: string): boolean {
  return !/\.test\.tsx?$/.test(path);
}

describe('the unreadable-connection state reaches every reauth surface', () => {
  const readers = Object.entries(SOURCES)
    .filter(([path]) => isProduction(path))
    .filter(([, src]) => src.includes('needsReauth'));

  it('finds the surfaces it is meant to police', () => {
    // A scan that finds zero is a claim to check, not a result
    // (gotchas 49/100/101). Both known readers must be present, or the glob
    // is wrong and every assertion below passes vacuously.
    const paths = readers.map(([p]) => p);
    expect(paths.some((p) => p.endsWith('SettingsPage.tsx'))).toBe(true);
    expect(paths.some((p) => p.endsWith('ComposioOAuthDetail.tsx'))).toBe(true);
  });

  it('every reauth reader also reads stateUnknown', () => {
    const blind = readers
      .filter(([, src]) => !src.includes('stateUnknown'))
      .map(([path]) => path);
    expect(blind, `these branch on needsReauth and cannot see an unreachable integration: ${blind.join(', ')}`).toEqual([]);
  });
});
