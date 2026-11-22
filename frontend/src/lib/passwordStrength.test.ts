/**
 * The meter is decoration; `requirements()` is the rule.
 *
 * ⚠ **This file lost ten assertions on 2026-09-09 and they are worth naming.**
 * They pinned an entropy model that ranked `outer marina drum kettle` above
 * `Password1!`, discounted dictionary words, and refused to call a wordlist
 * password strong. Every one of those properties was true and defensible. The
 * founder asked for "a standard UI, not the CIA database one", so the model is
 * gone and so are its tests — deleting a guard for a property the product no
 * longer claims is correct; leaving them to fail, or weakening them until they
 * pass, would be how a suite stops meaning anything.
 *
 * What is pinned now is smaller and is the part that can actually hurt
 * somebody: the submit gate, and the meter never contradicting it.
 */

import { describe, expect, it } from 'vitest';

import {
  firstUnmet,
  meetsRequirements,
  MIN_LENGTH,
  requirements,
  strength,
} from './passwordStrength';

describe('the meter never argues with the checklist', () => {
  it('does not call a password Weak after it satisfied every printed rule', () => {
    // ⚠ THE bug this rewrite exists to prevent. The form prints three rules;
    // clearing them and being told "Weak" is a form picking a fight with the
    // person filling it in. The shortest password the checklist accepts:
    const floor = 'Abcdefg1';
    expect(meetsRequirements(floor)).toBe(true);
    expect(strength(floor).label).not.toBe('Weak');
  });

  it.each(['Abcdefg1', 'Basketball1', 'Marigold7', 'Password1!'])(
    'rates %s at least Medium once it clears the floor',
    (password) => {
      expect(meetsRequirements(password)).toBe(true);
      expect(strength(password).score).toBeGreaterThanOrEqual(2);
    },
  );
});

describe('the three bands', () => {
  it('is silent on an empty field — a form you have not touched is not an error', () => {
    expect(strength('').score).toBe(0);
  });

  it('calls something short and plain Weak', () => {
    expect(strength('abcdef').label).toBe('Weak');
  });

  it('needs BOTH length and variety for the top band', () => {
    // 12+ chars but only one kind of character.
    expect(strength('abcdefghijklmnop').label).toBe('Medium');
    // Three kinds but under 12 characters.
    expect(strength('Abcdefg1').label).toBe('Medium');
    // Both.
    expect(strength('Abcdefghijk1').label).toBe('Strong');
  });

  it('never reports a band outside 0-3', () => {
    for (const p of ['', 'a', 'Abcdefg1', 'Mn7#qLx9$wTz'.repeat(20)]) {
      const { score } = strength(p);
      expect(score).toBeGreaterThanOrEqual(0);
      expect(score).toBeLessThanOrEqual(3);
    }
  });
});

describe('the floor is the floor', () => {
  it.each([
    ['Abcdefg', 'seven characters'],
    ['abcdefgh1', 'no capital'],
    ['Abcdefghi', 'no number'],
  ])('refuses %s (%s)', (password) => {
    expect(meetsRequirements(password)).toBe(false);
  });

  it('accepts the minimum that satisfies all three', () => {
    expect(meetsRequirements('Abcdefg1')).toBe(true);
    expect(requirements('Abcdefg1').every((r) => r.met)).toBe(true);
  });
});

describe('the disabled-button explanation names ONE thing', () => {
  it('leads with length while it is the blocker', () => {
    expect(firstUnmet('Ab1')).toBe(`Use at least ${MIN_LENGTH} characters.`);
  });

  it('then names the next unmet box, not all of them at once', () => {
    expect(firstUnmet('abcdefghij')).toBe('Add one capital letter.');
    expect(firstUnmet('Abcdefghij')).toBe('Add one number.');
  });

  it('says nothing once every box is ticked', () => {
    expect(firstUnmet('Abcdefghij1')).toBeNull();
  });
});

describe('it never crashes on input a person can actually produce', () => {
  it.each([
    ['        ', 'spaces only'],
    ['🔐🔐🔐🔐🔐🔐🔐🔐', 'emoji'],
    ['ñ'.repeat(20), 'non-ascii'],
    ['a'.repeat(500), 'a paste'],
  ])('handles %s (%s)', (input) => {
    const s = strength(input);
    expect(s.score).toBeGreaterThanOrEqual(0);
    expect(s.score).toBeLessThanOrEqual(4);
  });
});

/**
 * The bands have to be REACHABLE, and the thresholds were calibrated for raw
 * entropy while the checklist floor made half of them unreachable.
 *
 * ⚠ **Measured 2026-09-09, not reasoned about.** Over random passwords that
 * pass the checklist (8-15 chars), the old thresholds (30/45/60) produced
 * **0% Weak and 2% Fair** — an 8-character password with the required capital
 * and digit is already ~47.6 bits, so it landed on Good before content was
 * considered. A meter with two live bands is a meter nobody learns anything
 * from, and the founder's report was exactly that: "it never hits like a
 * yellow mode".
 */
/**
 * The wordlist shape, and why `COMMON` could never have covered it.
 *
 * ⚠ `COMMON` lists `football` and `baseball` and NOT `basketball`, so
 * `Basketball1` scored **4/Strong** — top band, same as a 12-character random
 * string. Adding `basketball` fixes one password and leaves the next sport
 * missing. Enumerating words is unwinnable; pricing the SHAPE is not.
 */