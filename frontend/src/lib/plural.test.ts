/**
 * The plural helper, and the words it is trusted to guess.
 *
 * ⚠ The interesting half is the OVER-derivation. A "-y → -ies" rule that does
 * not check the preceding letter turns "day" into "daies" and "key" into
 * "keies" — plausible-looking code, wrong on screen, and nothing throws. Every
 * rule below is asserted in both directions for that reason: the words it must
 * change AND the words it must leave alone.
 */

import { describe, expect, it } from 'vitest';

import { count, plural, pluralWord } from './plural';

describe('pluralWord', () => {
  it('adds -s to ordinary nouns', () => {
    expect(pluralWord('unit')).toBe('units');
    expect(pluralWord('lease')).toBe('leases');
    expect(pluralWord('tenant')).toBe('tenants');
  });

  it('turns a consonant + y into -ies', () => {
    expect(pluralWord('property')).toBe('properties');
    expect(pluralWord('company')).toBe('companies');
  });

  it('leaves a VOWEL + y alone', () => {
    // The half the naive rule gets wrong, and both of these appear in this
    // product's copy.
    expect(pluralWord('day')).toBe('days');
    expect(pluralWord('key')).toBe('keys');
  });

  it('adds -es after a sibilant', () => {
    expect(pluralWord('address')).toBe('addresses');
    expect(pluralWord('box')).toBe('boxes');
    expect(pluralWord('match')).toBe('matches');
  });

  it('uses the lookup for irregulars rather than a rule', () => {
    expect(pluralWord('person')).toBe('people');
    expect(pluralWord('is')).toBe('are');
  });

  it('keeps a leading capital on an irregular', () => {
    expect(pluralWord('Person')).toBe('People');
  });
});

describe('plural', () => {
  it('returns the singular for exactly one', () => {
    expect(plural(1, 'property')).toBe('property');
  });

  it('returns the plural for zero', () => {
    // "0 property" is the other half people forget — zero takes the plural in
    // English, and a count of zero is common in an empty account.
    expect(plural(0, 'property')).toBe('properties');
  });

  it('takes an explicit plural when the phrase changes shape', () => {
    expect(plural(1, 'item needs', 'items need')).toBe('item needs');
    expect(plural(4, 'item needs', 'items need')).toBe('items need');
  });

  it('REFUSES to guess a multi-word phrase', () => {
    // "work order" → "work orders" needs the head noun, and guessing which
    // word is the head is how you get "works order". Refusing makes the
    // caller say it; the throw is a build-time-ish failure in dev, not a
    // wrong string in front of a customer.
    expect(() => plural(2, 'work order')).toThrow(/pass it explicitly/);
  });

  it('does not refuse a phrase when the plural is supplied', () => {
    expect(plural(2, 'work order', 'work orders')).toBe('work orders');
  });
});

describe('count', () => {
  it('puts the number and the right noun together', () => {
    expect(count(1, 'property')).toBe('1 property');
    expect(count(3, 'property')).toBe('3 properties');
    expect(count(0, 'unit')).toBe('0 units');
  });

  it('groups thousands, because these sit beside figures that do', () => {
    expect(count(1200, 'answer')).toBe('1,200 answers');
  });
});
