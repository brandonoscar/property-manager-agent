/**
 * Reaching a person, and the two ways this surface could lie.
 *
 * 1. A `tel:` built from junk dials junk. Mirror phone values are whatever
 *    Buildium held in a free-text field, so the parser has to REFUSE rather
 *    than guess — and refusing everything is the equally-wrong direction,
 *    since 62 of 137 tenants do have a real number.
 * 2. A "Text" control here is a handoff to the manager's own messaging app,
 *    NOT an Occupella send. `comms/sms.py` is dark and `helixis_sms_consent`
 *    held ZERO rows when this shipped, so anything routed through our send
 *    path would fail closed for every recipient in the product. The guard
 *    below pins the href scheme for exactly that reason: the day someone
 *    "improves" this into a real send, they have to delete a test that says
 *    why not.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { ContactLinks, mailHref, telHref } from './ContactLinks';

describe('telHref refuses rather than guesses', () => {
  it('keeps a real number and strips its formatting', () => {
    expect(telHref('(555) 010-9999')).toBe('5550109999');
    expect(telHref('+1 555 010 9999')).toBe('+15550109999');
  });

  it('rejects the shapes a free-text field actually produces', () => {
    // Each of these would otherwise become a call button that dials nothing.
    for (const junk of ['', '   ', 'n/a', 'none', 'ext 4', '-', '1']) {
      expect(telHref(junk)).toBeNull();
    }
    expect(telHref(null)).toBeNull();
    expect(telHref(undefined)).toBeNull();
  });

  it('takes 7 digits, the shortest real subscriber number', () => {
    // ⚠ Both sides of the boundary. A rule that only rejects is a rule that
    // hides every phone number in the mirror.
    expect(telHref('010-9999')).toBe('0109999');
    expect(telHref('10-9999')).toBeNull();
  });
});

describe('mailHref', () => {
  it('accepts an ordinary address', () => {
    expect(mailHref(' keanu.alameda@example.com ')).toBe('keanu.alameda@example.com');
  });

  it('rejects what would open a compose window addressed to nothing', () => {
    for (const junk of ['', 'none', 'keanu@', '@example.com', 'keanu@example', 'a b@c.com']) {
      expect(mailHref(junk)).toBeNull();
    }
  });
});

describe('ContactLinks', () => {
  it('renders call, text and email when both channels are on file', () => {
    render(<ContactLinks phone="(555) 010-9999" email="a@b.com" />);
    expect(screen.getByTestId('contact-call').getAttribute('href')).toBe('tel:5550109999');
    expect(screen.getByTestId('contact-text').getAttribute('href')).toBe('sms:5550109999');
    expect(screen.getByTestId('contact-email').getAttribute('href')).toBe('mailto:a@b.com');
  });

  it('is a device handoff, never an Occupella send', () => {
    // The whole reason a Text control can exist while SMS is dark. If this
    // ever points at an API route, the consent gate has to come with it.
    render(<ContactLinks phone="5550109999" email="a@b.com" />);
    for (const id of ['contact-call', 'contact-text', 'contact-email']) {
      const href = screen.getByTestId(id).getAttribute('href') ?? '';
      expect(href).toMatch(/^(tel|sms|mailto):/);
    }
  });

  it('omits a channel rather than disabling it', () => {
    // 75 of 137 tenants have no phone. A greyed-out Call on three quarters of
    // a table reads as breakage, not as absence.
    render(<ContactLinks email="a@b.com" />);
    expect(screen.queryByTestId('contact-call')).toBeNull();
    expect(screen.queryByTestId('contact-text')).toBeNull();
    expect(screen.getByTestId('contact-email')).toBeTruthy();
  });

  it('renders nothing at all when there is no way to reach them', () => {
    const { container } = render(<ContactLinks email={null} phone={null} />);
    expect(container.textContent).toBe('');
  });

  it('a junk phone does not become a call button', () => {
    render(<ContactLinks phone="n/a" email="a@b.com" />);
    expect(screen.queryByTestId('contact-call')).toBeNull();
  });

  it('the detail variant shows the address, the row variant shows the verb', () => {
    const { unmount } = render(<ContactLinks email="a@b.com" variant="detail" />);
    expect(screen.getByTestId('contact-email').textContent).toContain('a@b.com');
    unmount();
    render(<ContactLinks email="a@b.com" />);
    expect(screen.getByTestId('contact-email').textContent).toContain('Email');
  });
});
