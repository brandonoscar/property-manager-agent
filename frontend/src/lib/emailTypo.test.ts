/**
 * Both directions matter, and the second one matters MORE.
 *
 * A missed typo costs one person one confused afternoon. A false suggestion
 * on a real work address tells a property manager their own company email is
 * wrong — in the product they are deciding whether to trust with their books.
 * So every "must stay silent" case here is load-bearing, and the bar for
 * adding a domain to the list is that a near-miss of it is more likely a slip
 * than a real host.
 */

import { describe, expect, it } from 'vitest';

import { suggestEmail } from './emailTypo';

describe('catches the typos that actually happen', () => {
  it.each([
    ['sam@gmial.com', 'sam@gmail.com'],
    ['sam@gmai.com', 'sam@gmail.com'],
    ['sam@gnail.com', 'sam@gmail.com'],
    ['sam@yahooo.com', 'sam@yahoo.com'],
    ['sam@hotmial.com', 'sam@hotmail.com'],
    ['sam@outlok.com', 'sam@outlook.com'],
    ['sam@iclould.com', 'sam@icloud.com'],
  ])('%s → %s', (input, expected) => {
    expect(suggestEmail(input)).toBe(expected);
  });

  it('fixes a mistyped ending on a domain it has never heard of', () => {
    // The NAME is left alone — only `con` → `com`.
    expect(suggestEmail('sam@blueridgeproperties.con')).toBe(
      'sam@blueridgeproperties.com',
    );
    expect(suggestEmail('sam@blueridgeproperties.cmo')).toBe(
      'sam@blueridgeproperties.com',
    );
  });

  it('returns the WHOLE address, so accepting it is one click and not a retype', () => {
    expect(suggestEmail('first.last+tag@gmial.com')).toBe('first.last+tag@gmail.com');
  });
});

describe('stays silent where a suggestion would be an insult', () => {
  it('never second-guesses a real company domain', () => {
    // This is the case that decides whether the feature ships at all.
    for (const email of [
      'sam@blueridgeproperties.com',
      'sam@occupella.com',
      'sam@bmw.de',
      'sam@acme.io',
      'sam@a-very-unusual-name.co.uk',
      'sam@buildium.com',
    ]) {
      expect(suggestEmail(email), email).toBeNull();
    }
  });

  it('does not "correct" a known domain into another known domain', () => {
    // gmail.com and googlemail.com are both real and both in the list.
    expect(suggestEmail('sam@gmail.com')).toBeNull();
    expect(suggestEmail('sam@me.com')).toBeNull();
    expect(suggestEmail('sam@live.com')).toBeNull();
  });

  it('leaves an unfamiliar ending alone rather than guessing at it', () => {
    // `dev` and `ai` are not one character from anything known, so the rule
    // declines instead of inventing a domain the person never typed.
    expect(suggestEmail('sam@occupella.dev')).toBeNull();
    expect(suggestEmail('sam@occupella.ai')).toBeNull();
  });

  it('says nothing while somebody is still typing', () => {
    for (const partial of ['', 's', 'sam', 'sam@', 'sam@g', 'sam@gmail', '@gmail.com']) {
      expect(suggestEmail(partial), JSON.stringify(partial)).toBeNull();
    }
  });

  it('is case- and whitespace-insensitive on the domain', () => {
    expect(suggestEmail('  Sam@GMIAL.COM  ')).toBe('Sam@gmail.com');
  });

  it('reads the LAST @, so a local part containing one cannot break it', () => {
    expect(suggestEmail('"weird@local"@gmial.com')).toBe('"weird@local"@gmail.com');
  });
});

describe('never throws on input a person can produce', () => {
  it.each([['@@@'], ['a@b'], ['a@.'], ['@.com'], ['sam@' + 'x'.repeat(300) + '.com']])(
    'handles %s',
    (input) => {
      expect(() => suggestEmail(input)).not.toThrow();
    },
  );
});
