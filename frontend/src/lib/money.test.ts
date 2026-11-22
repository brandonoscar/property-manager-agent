/**
 * Both directions, because a money parser is wrong in two ways.
 *
 * The obvious one is refusing something real ("$1,250" is what people paste).
 * The expensive one is ACCEPTING something and producing a different number —
 * `"1,25"` becoming 125 is a hundredfold error on a rent offer, made by the
 * software, and it looks exactly like a successful parse from the outside.
 */

import { describe, expect, it } from 'vitest';

import { formatMoney, parseMoney } from './money';

describe('parseMoney', () => {
  it('reads the shapes people actually type', () => {
    expect(parseMoney('1250')).toBe(1250);
    expect(parseMoney('1250.50')).toBe(1250.5);
    expect(parseMoney('1250.5')).toBe(1250.5);
    expect(parseMoney('.5')).toBe(0.5);
  });

  it('reads the shapes people PASTE, which the number input drops', () => {
    // Each of these leaves <input type="number"> holding the empty string
    // while the characters stay on screen — the failure this replaces.
    expect(parseMoney('$1,250')).toBe(1250);
    expect(parseMoney('$1,250.50')).toBe(1250.5);
    expect(parseMoney(' $2,400.00 ')).toBe(2400);
    expect(parseMoney('$1,234,567.89')).toBe(1234567.89);
  });

  it('REFUSES an ambiguous comma group rather than guessing', () => {
    // 1.25 to a European reader, 125 to a comma-stripper. Guessing here is a
    // hundredfold error entered by the software.
    expect(parseMoney('1,25')).toBeNull();
    expect(parseMoney('1,2')).toBeNull();
    expect(parseMoney('1,2500')).toBeNull();
    expect(parseMoney('1,,250')).toBeNull();
    expect(parseMoney(',250')).toBeNull();
  });

  it('returns null for blank and for junk', () => {
    expect(parseMoney('')).toBeNull();
    expect(parseMoney('   ')).toBeNull();
    expect(parseMoney('abc')).toBeNull();
    expect(parseMoney('12abc')).toBeNull();
    expect(parseMoney('1.2.3')).toBeNull();
    expect(parseMoney('$')).toBeNull();
  });

  it('keeps a negative, because a credit is a real amount', () => {
    expect(parseMoney('-50')).toBe(-50);
    expect(parseMoney('-$1,250.00')).toBeNull(); // sign before the $ only
    expect(parseMoney('$-1,250.00')).toBe(-1250);
  });
});

describe('formatMoney', () => {
  it('writes what a US reader expects', () => {
    expect(formatMoney(1250)).toBe('$1,250.00');
    expect(formatMoney(1250.5)).toBe('$1,250.50');
    expect(formatMoney(0)).toBe('$0.00');
  });

  it('renders absence as absence, never as zero', () => {
    // "$0.00" for an unknown amount is a false statement about money — the
    // same rule bill_amount follows on the backend (gotcha 25).
    expect(formatMoney(null)).toBe('—');
    expect(formatMoney(undefined)).toBe('—');
    expect(formatMoney(NaN)).toBe('—');
  });

  it('round-trips through parseMoney', () => {
    for (const n of [0, 1250, 1250.5, 1234567.89]) {
      expect(parseMoney(formatMoney(n))).toBe(n);
    }
  });
});
