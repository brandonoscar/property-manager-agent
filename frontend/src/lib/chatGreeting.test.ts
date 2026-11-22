/**
 * The greeting guarantee: fifteen greetings, and none of them can render a
 * blank where a name should be.
 */

import { describe, expect, it } from 'vitest';

import { firstNameFrom, greetingText, greetings } from './chatGreeting';

describe('greetings', () => {
  it('has fifteen, all different', () => {
    const list = greetings(9);
    expect(list).toHaveLength(15);
    expect(new Set(list.map((g) => g.plain)).size).toBe(15);
  });

  it('every nameless form is a whole sentence with no empty slot', () => {
    for (const hour of [8, 14, 20]) {
      for (const g of greetings(hour)) {
        expect(g.plain).not.toMatch(/undefined|null|,\s*[?.!]?$|,\s*,|\s[?.!]$|—/);
        expect(g.plain.trim()).toBe(g.plain);
      }
    }
  });

  it('every named form carries the name exactly once', () => {
    for (const g of greetings(9)) {
      const text = g.named('Dana');
      expect(text.split('Dana')).toHaveLength(2);
      expect(text).not.toMatch(/—/);
    }
  });

  it('reads the clock it is shown at', () => {
    expect(greetingText(0, null, 8)).toBe('Good morning');
    expect(greetingText(0, null, 13)).toBe('Good afternoon');
    expect(greetingText(0, 'Dana', 19)).toBe('Good evening, Dana');
  });

  it('wraps any index into the list', () => {
    expect(greetingText(15, null, 9)).toBe(greetingText(0, null, 9));
    expect(greetingText(-1, null, 9)).toBe(greetingText(14, null, 9));
  });
});

describe('firstNameFrom', () => {
  it('uses a name the account carries', () => {
    expect(firstNameFrom({ first_name: 'dana' })).toBe('Dana');
    expect(firstNameFrom({ full_name: 'Dana Whitfield' })).toBe('Dana');
    expect(firstNameFrom({ name: "Anne-Marie O'Neil" })).toBe('Anne-Marie');
  });

  it('greets nobody by a word that is not a name', () => {
    expect(firstNameFrom(undefined)).toBeNull();
    expect(firstNameFrom({})).toBeNull();
    expect(firstNameFrom({ full_name: 'team@acme.com' })).toBeNull();
    expect(firstNameFrom({ full_name: '4B Holdings' })).toBeNull();
    expect(firstNameFrom({ full_name: '  ' })).toBeNull();
    expect(firstNameFrom({ first_name: 'J' })).toBeNull();
  });

  it('never reads the email', () => {
    // The TOS fields are all signup writes today; an email in metadata must
    // not become a greeting.
    expect(firstNameFrom({ email: 'jane.q.public@gmail.com', tos_version: '1' })).toBeNull();
  });
});
