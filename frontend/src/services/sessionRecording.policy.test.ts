/**
 * What the product collects cannot change without the policy changing with it.
 *
 * ⚠ THE DEFECT THIS EXISTS FOR IS NOT A LEAK — IT IS A COLLECTION CHANGE
 * NOBODY DECIDED. `disable_session_recording: false` is the posthog-js
 * default and does NOT mean "record": it means "record if the project says
 * so". The project toggle lives in a web UI, one click from anyone who opens
 * it to look at something else, so until 2026-09-15 whether Occupella
 * recorded every authenticated property manager's screen — the screens
 * holding tenant names, addresses and email bodies — hung on a dashboard
 * setting with no code change, no review, and no edit to privacy.html.
 *
 * Masking (`RECORDING_PRIVACY`) is what stops that being a leak, and it is
 * correctly configured. This guard is about the other half: a person reading
 * the privacy policy must be reading something true.
 *
 * ⚠ IT DOES NOT GRADE THE POLICY'S PROSE, and that is the whole reason it
 * works. Asserting "the policy discloses recording" needs a phrase set, and
 * every phrase that discloses recording also appears in a sentence DENYING it
 * — "there is no session replay in Occupella" contains "session replay". That
 * is the 414/416 false-positive class this repo has recorded five times
 * (gotchas 52/65/86/102/113): a compliant text quotes the phrase in order to
 * reject it, the guard fires on the best sentence in the document, and the
 * guard gets switched off.
 *
 * So it pins the DENIAL in both directions instead. Recording off → the
 * denial must be there, because it is a claim we are making and it must stay
 * true. Recording on → the denial must be GONE, which forces an edit to
 * privacy.html in the same change; whoever deletes those sentences is the
 * person writing the disclosure that replaces them. No prose is graded and
 * there is nothing to false-positive on.
 */
import { describe, expect, it } from 'vitest';

import analyticsSource from './analytics.ts?raw';
import privacyHtml from '../../public/legal/privacy.html?raw';

/**
 * The file with its prose removed.
 *
 * ⚠ REQUIRED, not hygiene. The comment above `disable_session_recording`
 * explains what `false` would mean and quotes the word repeatedly — a scan
 * over the raw file is reading the sentence that explains the setting, which
 * is how a guard ends up grading its own documentation (gotchas 40/59/91/114,
 * and it caught the author of this file once already in the same iteration).
 *
 * The line-comment pattern spares `://` so the PostHog host default survives;
 * it would not change this result, but a stripper that mangles string
 * literals is one nobody can reuse.
 */
function withoutComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/[^\n]*/g, '$1');
}

/** The policy as a reader sees it: no tags, whitespace collapsed. */
function visibleText(html: string): string {
  return html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');
}

/**
 * Every place the setting is decided, comments removed.
 *
 * ⚠ A FUNCTION SO THE CANARY BELOW IS TESTABLE. Inlined, "the guard fails
 * loudly when it cannot find the setting" is a property no mutation can reach
 * — against the real file the match count is 1 under every spelling of the
 * assertion, so weakening it to `>= 0` changes no output and scores ALIVE for
 * a reason that is about the corpus rather than the code (gotcha 58). Pulled
 * out, the zero-match and two-match cases are ordinary inputs.
 */
function recordingSettings(src: string): string[] {
  return [...withoutComments(src).matchAll(/disable_session_recording:\s*(true|false)/g)].map(
    (m) => m[1],
  );
}

/**
 * Sentences that are TRUE only while nothing records.
 *
 * Both are load-bearing and they say different things — the first is about
 * video, the second names the technique — so a disclosure that replaced only
 * one of them would leave the other standing and false.
 */
const DENIALS = [
  'We do not record your screen.',
  'There is no session replay in Occupella',
];

describe('session recording and the privacy policy', () => {
  const settings = recordingSettings(analyticsSource);

  it('reads one real setting out of the real file', () => {
    // ⚠ A scan that finds zero is a claim to check, not a result — and every
    // assertion below passes vacuously against an empty match set or an empty
    // `?raw` import (gotchas 49/100/101, made twice in one week by somebody
    // who had just re-read them). Two matches would mean the value is set in
    // two places, and this guard would be grading whichever it found first.
    expect(analyticsSource.length).toBeGreaterThan(1000);
    expect(privacyHtml.length).toBeGreaterThan(1000);
    expect(settings).toHaveLength(1);
  });

  it('ignores the prose that explains the setting', () => {
    // The distinguishing input the real file cannot supply: `analytics.ts`
    // happens to carry exactly one real assignment, so an extractor that read
    // comments too would score identically against it. The comment above that
    // line quotes `false` repeatedly, which is what this separates.
    expect(
      recordingSettings(`
        /* disable_session_recording: false would mean "ask the project". */
        // disable_session_recording: false
        init({ disable_session_recording: true });
      `),
    ).toEqual(['true']);
  });

  it('fails loudly rather than vacuously when the setting is gone', () => {
    // The canary's whole job. Inlined it was untestable — with the real file
    // the count is 1 under every spelling of the assertion, so weakening it to
    // `>= 0` changed no output and scored ALIVE for a reason about the corpus
    // rather than the code (gotcha 58).
    expect(recordingSettings('init({ persistence: "localStorage" });')).toEqual([]);
  });

  it('keeps the policy and the setting saying the same thing', () => {
    const recordingBlocked = settings[0] === 'true';
    const prose = visibleText(privacyHtml);

    for (const denial of DENIALS) {
      const present = prose.includes(denial);
      if (recordingBlocked) {
        expect(
          present,
          `analytics.ts blocks session recording, so the privacy policy has to keep ` +
            `saying so — the sentence "${denial}" is gone from privacy.html. If the ` +
            `product now records, flip disable_session_recording in the same change.`,
        ).toBe(true);
      } else {
        expect(
          present,
          `analytics.ts allows session recording and privacy.html still tells ` +
            `customers "${denial}". Either set disable_session_recording back to ` +
            `true, or replace that sentence with the disclosure of what is now ` +
            `recorded — in this change, not a follow-up.`,
        ).toBe(false);
      }
    }
  });

  it('still masks everything, for the day the setting is flipped', () => {
    // The masking is inert while recording is blocked, which is exactly how an
    // unused config gets tidied away — and then the flip later arrives
    // unmasked. `analytics.test.ts` pins the flags themselves; this pins that
    // the init still spreads them, so the two halves cannot drift apart.
    expect(withoutComments(analyticsSource)).toContain('...RECORDING_PRIVACY');
  });
});
