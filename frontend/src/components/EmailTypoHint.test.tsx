/**
 * The "did you mean" line, and the two forms that need it for opposite reasons.
 *
 * ⚠ The half worth guarding is the SILENCE. A hint that fires on a correct
 * address is one people learn to ignore, and then it is not there on the day
 * it would have helped — so the assertions below spend as much effort on what
 * must NOT appear as on what must.
 *
 * ⚠ And the CORPUS half: this belongs on every form where a mistyped address
 * fails silently. On sign-in a typo announces itself (the password fails). On
 * an invite nothing fails at all — the address is well-formed, the API accepts
 * it, mail goes to a mailbox that does not exist, and the team screen shows an
 * "Invite pending" row for somebody who was never asked. A behavioural test of
 * the component cannot see a form that forgot to use it (gotcha 62).
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { EmailTypoHint } from './EmailTypoHint';

const SOURCES = import.meta.glob('./*.tsx', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

describe('EmailTypoHint', () => {
  it('offers the correction for a near-miss on a known domain', () => {
    render(<EmailTypoHint email="sam@gmial.com" onAccept={() => {}} />);
    expect(screen.getByTestId('email-typo-hint')).toHaveTextContent('sam@gmail.com');
  });

  it('applies the correction when accepted', () => {
    const onAccept = vi.fn();
    render(<EmailTypoHint email="sam@gmial.com" onAccept={onAccept} />);
    fireEvent.click(screen.getByRole('button'));
    expect(onAccept).toHaveBeenCalledWith('sam@gmail.com');
  });

  it('says NOTHING about a real company domain', () => {
    // The whole reason this is shippable. Every property manager's work
    // address is an unfamiliar domain, and "did you mean" under a correct
    // one is how the hint gets tuned out.
    for (const email of [
      'brandon@blueridgeproperties.com',
      'ops@occupella.com',
      'sam@gmail.com',
    ]) {
      const { container, unmount } = render(<EmailTypoHint email={email} onAccept={() => {}} />);
      expect(container.firstChild, `${email} was second-guessed`).toBeNull();
      unmount();
    }
  });

  it('says nothing mid-typing', () => {
    // Firing while somebody is halfway through a domain is noise, not help.
    for (const partial of ['', 'sam', 'sam@', 'sam@gm']) {
      const { container, unmount } = render(<EmailTypoHint email={partial} onAccept={() => {}} />);
      expect(container.firstChild, `fired on "${partial}"`).toBeNull();
      unmount();
    }
  });
});

describe('the forms that need it', () => {
  it('is reading real sources', () => {
    expect(Object.keys(SOURCES).length).toBeGreaterThan(20);
  });

  it('covers both places an address is typed for somebody else to receive', () => {
    const missing = ['LoginForm.tsx', 'InviteUsersDialog.tsx'].filter((f) => {
      const key = Object.keys(SOURCES).find((k) => k.endsWith(f));
      return !key || !/<EmailTypoHint\b/.test(SOURCES[key]);
    });
    expect(
      missing,
      'a mistyped address here fails silently — no error, and on the invite ' +
        'form an "Invite pending" row for somebody who was never asked',
    ).toEqual([]);
  });

  it('has exactly ONE definition of the hint', () => {
    // It lived inside LoginForm first. A second copy drifts, and the copy
    // that stops firing is the one nobody is looking at (gotcha 25).
    const definitions = Object.entries(SOURCES).filter(
      ([, src]) => /function EmailTypoHint\(/.test(src),
    );
    expect(definitions.map(([p]) => p)).toHaveLength(1);
  });
});
