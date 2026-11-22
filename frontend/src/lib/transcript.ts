/**
 * A voicemail transcript is a machine's guess, and it was rendered as quotes.
 *
 * `CrmPage` showed the transcription as `“{text}”`. Quotation marks are a
 * claim that these are the caller's words; automatic speech recognition does
 * not make that claim and routinely gets exactly the tokens a property manager
 * acts on wrong — a rent amount, a unit number.
 *
 * ⚠ There is no confidence value to gate on. Twilio's `transcribeCallback`
 * sends `TranscriptionStatus` (completed / failed) and the text, nothing
 * per-word, and the route already drops anything not completed. So the control
 * is to stop presenting a guess as a quotation, and to name the figures worth
 * checking against the recording — which is one click away in the same row.
 *
 * ⚠ Mirrors `src/helixis/comms/transcript.py`, which produces the same caveat
 * as a deterministic floor signal on the Inbox comms card. Two surfaces read
 * the same rule and gotcha 25 says fix every layer that has it; the wording is
 * pinned across both by `test_transcript_caveat_parity.py`, because two copies
 * of a sentence drift and the drift is invisible.
 */

/** Shown wherever a transcript is rendered, figures or not. */
export const TRANSCRIPT_LABEL = 'Automatic transcription';

/**
 * Figures a manager acts on directly.
 *
 * ⚠ Money is matched WITH its symbol and a unit number WITH its label. A scan
 * over bare digits would fire on every voicemail carrying a phone number or a
 * date — nearly all of them — and a caveat that appears everywhere is one
 * people stop reading.
 */
const FIGURE_PATTERNS: [string, RegExp][] = [
  ['an amount', /\$\s?\d[\d,]*(?:\.\d{1,2})?/],
  ['a unit number', /\b(?:unit|apt\.?|apartment|suite|ste\.?)\s*#?\s*[0-9]+[a-z]?\b/i],
];

/**
 * Which kinds of act-on-it figure this transcript contains — the LABELS, never
 * the matched values. Echoing a possibly-wrong number into the caveat would
 * repeat it somewhere it looks more official than the transcript does.
 */
export function figuresIn(text: string | null | undefined): string[] {
  const body = text ?? '';
  return FIGURE_PATTERNS.filter(([, re]) => re.test(body)).map(([label]) => label);
}

/**
 * The line to show, or `null` when the transcript carries no figures.
 *
 * ⚠ `null` is not "this transcript is accurate" — every transcript is a guess,
 * which is what `TRANSCRIPT_LABEL` says. This is the narrower claim: something
 * numeric is in here that a reader is likely to act on and ASR is likely to get
 * wrong.
 */
export function transcriptCaveat(text: string | null | undefined): string | null {
  const found = figuresIn(text);
  if (found.length === 0) return null;
  const what =
    found.length === 1
      ? found[0]
      : `${found.slice(0, -1).join(', ')} and ${found[found.length - 1]}`;
  return (
    "This text is an automatic transcription, not the caller's exact words. " +
    `It contains ${what} — check against the recording before acting on the figures.`
  );
}
