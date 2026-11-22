/**
 * A voicemail transcript was rendered in quotation marks.
 *
 * `CrmPage` showed `“{m.transcription}”`. Quotes are a claim that these are the
 * caller's words; automatic speech recognition does not make that claim and
 * routinely gets exactly the tokens a property manager acts on wrong — a rent
 * amount, a unit number. Twilio sends no per-word confidence to gate on, so the
 * controls are to stop presenting a guess as a quotation and to name the
 * figures worth checking against the recording.
 *
 * ⚠ The false-positive direction is what decides whether this survives real
 * traffic. A scan over bare digits fires on every voicemail carrying a phone
 * number or a date — nearly all of them — and a caveat that appears everywhere
 * is one people stop reading. Every case below has its negative twin.
 *
 * ⚠ Mirrors `src/helixis/comms/transcript.py`; the wording is pinned across
 * both by `tests/unit/test_transcript_caveat.py`.
 */

import { describe, expect, it } from 'vitest';

import { TRANSCRIPT_LABEL, figuresIn, transcriptCaveat } from './transcript';

describe('figuresIn', () => {
  it.each([
    "Hi, it's about the rent, I can pay $1,500 on Friday.",
    'The invoice came to $15,000 and I want to dispute it.',
    'I owe $850.00 right?',
  ])('flags a dollar amount: %s', (text) => {
    expect(figuresIn(text)).toContain('an amount');
  });

  it.each([
    'This is unit 4B calling about the heat.',
    "I'm in apartment 12, the lock is broken.",
    'Suite 208 needs someone today.',
  ])('flags a labelled unit number: %s', (text) => {
    expect(figuresIn(text)).toContain('a unit number');
  });

  it.each([
    'Hi, this is Dana, please call me back when you get a chance.',
    'Call me on 555 123 4567 tomorrow.',
    "I'll be there on the 15th of next month.",
    'It has been broken for 3 days now.',
    '',
  ])('leaves an ordinary voicemail alone: %s', (text) => {
    expect(figuresIn(text)).toEqual([]);
    expect(transcriptCaveat(text)).toBeNull();
  });

  it('does not treat a bare number as an amount', () => {
    // "1500" in speech is as likely a door code, a year or a square footage.
    expect(figuresIn('the meter reads 1500')).toEqual([]);
  });

  it('does not treat a bare alphanumeric as a unit', () => {
    expect(figuresIn('my policy number is 4B7')).toEqual([]);
  });

  it('handles null and undefined', () => {
    expect(figuresIn(null)).toEqual([]);
    expect(figuresIn(undefined)).toEqual([]);
    expect(transcriptCaveat(null)).toBeNull();
  });
});

describe('transcriptCaveat', () => {
  it('names what to check and where to check it', () => {
    const line = transcriptCaveat('unit 4B owes $1,500');
    expect(line).toContain('an amount');
    expect(line).toContain('a unit number');
    // A caveat with no way to settle it is just doubt.
    expect(line).toContain('recording');
  });

  it('never echoes the figure itself', () => {
    // Repeating a possibly-wrong number prints it a second time, somewhere it
    // looks more official than the transcript it came from.
    expect(transcriptCaveat('I can pay $15,000 on Friday')).not.toContain('15,000');
  });

  it('reads naturally with one figure', () => {
    expect(transcriptCaveat('I can pay $500')).toContain('It contains an amount —');
  });

  it('has a label that does not claim the words are the callers', () => {
    expect(TRANSCRIPT_LABEL).toBe('Automatic transcription');
  });
});
