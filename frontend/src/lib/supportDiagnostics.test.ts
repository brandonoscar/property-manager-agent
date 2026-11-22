/**
 * The one property that matters here is what is NOT in the output.
 *
 * This block exists to be pasted — into an email, a chat, a screenshot, a
 * forum thread. Every other test in this file is about usefulness; the
 * secrets test is about whether the feature is safe to have at all, and it
 * asserts on the OUTPUT rather than on intent, because "I did not mean to
 * include a token" is not a property a reader can check.
 */

import { describe, expect, it } from 'vitest';

import { buildDiagnostics, forbiddenTermIn } from './supportDiagnostics';

const FULL = {
  userId: '11111111-1111-1111-1111-111111111111',
  email: 'sam@blueridgeproperties.com',
  companyId: '5ca1ab1e-0000-4000-8000-000000000002',
  companyRole: 'admin',
  companyCount: 1,
  appVersion: 'a1b2c3d4e5f6',
  userAgent: 'Mozilla/5.0 (Macintosh) Chrome/128',
  now: '2026-09-08T12:00:00.000Z',
  view: 'properties',
};

describe('nothing secret reaches the clipboard', () => {
  it('contains no token, key or credential word', () => {
    const offending = forbiddenTermIn(buildDiagnostics(FULL));
    expect(offending, `"${offending}" reached the support block`).toBeNull();
  });

  it('the guard actually fires — otherwise the test above proves nothing', () => {
    // Without this, a broken `forbiddenTermIn` returns null for everything and
    // the secrets test passes vacuously (gotcha 35: a check that cannot fail
    // is not a check).
    expect(forbiddenTermIn('access_token: abc123')).toBe('token');
    // ⚠ Asserts THAT it fires, not WHICH word it names. "Authorization: Bearer"
    // contains two forbidden terms and the function returns the first in list
    // order — pinning one of them would make a harmless reordering of the list
    // fail a test that is supposed to be about detection.
    expect(forbiddenTermIn('Authorization: Bearer x')).not.toBeNull();
    expect(forbiddenTermIn('sb-anon-key: eyJ...')).not.toBeNull();
    // And it stays quiet on ordinary text, or every block would read as unsafe.
    expect(forbiddenTermIn('Role: admin')).toBeNull();
  });
});

describe('it answers the questions support actually asks', () => {
  it.each([
    ['the workspace', FULL.companyId],
    ['the user', FULL.email],
    ['the build', FULL.appVersion],
    ['the role', 'admin'],
    ['the screen', 'properties'],
  ])('includes %s', (_label, value) => {
    expect(buildDiagnostics(FULL)).toContain(value);
  });

  it('is plain text, not JSON — a property manager pastes this into an email', () => {
    const text = buildDiagnostics(FULL);
    expect(text.startsWith('{')).toBe(false);
    expect(text.split('\n').length).toBeGreaterThan(5);
  });
});

describe('missing values render honestly rather than as "undefined"', () => {
  const EMPTY = {
    userId: null,
    email: null,
    companyId: null,
    companyRole: null,
    companyCount: 0,
    appVersion: '',
    userAgent: '',
    now: '2026-09-08T12:00:00.000Z',
    view: null,
  };

  it('never prints the words undefined or null', () => {
    const text = buildDiagnostics(EMPTY);
    expect(text).not.toContain('undefined');
    expect(text).not.toContain('null');
  });

  it('says "not signed in" rather than leaving the line blank', () => {
    expect(buildDiagnostics(EMPTY)).toContain('not signed in');
  });

  it('says "unknown" for a build it could not read', () => {
    expect(buildDiagnostics(EMPTY)).toContain('Build:     unknown');
  });
});
