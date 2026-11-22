/**
 * Nothing you can click is unreachable by keyboard.
 *
 * ⚠ A clickable `<div>` or `<tr>` is not a slow control — it is an ABSENT one.
 * Tab never lands on it, Enter has nothing to act on, and the feature is
 * mouse-only with no error and nothing on screen to suggest a problem. Two of
 * these were real: the operations table's rows and the dev-runs expander,
 * where the entire point of the table is opening a row.
 *
 * ⚠ **A backdrop is deliberately exempt, and that exemption is the reason this
 * sweep is not simply "no onClick on a div".** Eleven of the thirteen hits on
 * the first run were overlays whose click-to-dismiss is a mouse convenience;
 * the keyboard equivalent is Escape, which those dialogs have. Adding a tab
 * stop to a full-screen scrim would put a stop in the cycle that announces
 * nothing and does nothing a keyboard user can perceive — worse than leaving
 * it alone. So the exemption is by SHAPE (`fixed inset-0`), which is what
 * actually makes something an overlay, and the file lists what it skipped.
 *
 * ⚠ **"WHICH THOSE DIALOGS HAVE" WAS PROSE, AND IT WAS FALSE FOR TWO OF THEM.**
 * The sentence above is what licenses this whole exemption, and nothing checked
 * it: the CRM "Add a lead" modal and the webhook "Forget the signing secret?"
 * confirm each shipped with no Escape handler at all, so exempting their
 * backdrops left them with no keyboard dismissal by either route — on one of
 * them a destructive action. `modalKeyboard.corpus.test.tsx` now asserts the
 * premise (Escape + focus trap + dialog role, per COMPONENT), so this
 * exemption rests on a guard instead of on a claim (gotcha 48).
 */

import { describe, expect, it } from 'vitest';

const SOURCES = import.meta.glob('../**/*.tsx', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

/**
 * Opening tags of non-interactive elements, scanned with BALANCED braces.
 *
 * ⚠ A regex cannot do this, and the first cut proved it by flagging the very
 * row it had just been written to protect. `{[^{}]*}` stops at the first
 * nested brace, so an `onKeyDown={(e) => { if (…) { … } }}` handler truncates
 * the match before `tabIndex` is ever seen — the guard reports the fixed
 * element as broken. Walking the tag and counting braces is the only reading
 * that survives a real handler.
 */
const TAGS = /<(div|span|li|tr|td)\b/g;

function openingTag(source: string, from: number): string {
  let depth = 0;
  for (let i = from; i < source.length; i += 1) {
    const c = source[i];
    if (c === '{') depth += 1;
    else if (c === '}') depth -= 1;
    else if (c === '>' && depth === 0) return source.slice(from, i);
  }
  return source.slice(from);
}

function unreachable(source: string): string[] {
  const found: string[] = [];
  let m: RegExpExecArray | null;
  TAGS.lastIndex = 0;
  while ((m = TAGS.exec(source)) !== null) {
    const attrs = openingTag(source, m.index);
    if (!attrs.includes('onClick')) continue;
    // A full-screen overlay: dismissing by clicking the scrim. Escape is the
    // keyboard path and belongs to the dialog, not to this element.
    if (attrs.includes('fixed inset-0')) continue;
    // ⚠ `onClick={(e) => e.stopPropagation()}` is not a control. It is the
    // inner card of a dialog, stopping a backdrop click from closing what the
    // user is typing into. It activates nothing, so there is nothing for a
    // keyboard to reach — a tab stop here would be a stop that does nothing.
    if (/onClick=\{\(e\) => e\.stopPropagation\(\)\}/.test(attrs)) continue;
    const reachable =
      attrs.includes('onKeyDown') ||
      attrs.includes('onKeyPress') ||
      attrs.includes('tabIndex') ||
      attrs.includes('role="button"');
    if (reachable) continue;
    found.push(String(source.slice(0, m.index).split('\n').length));
  }
  return found;
}

describe('every clickable element can be operated by keyboard', () => {
  it('is looking at real files', () => {
    // A glob that resolves to nothing reports green forever. Assert the
    // instrument works before trusting its verdict.
    expect(Object.keys(SOURCES).length).toBeGreaterThan(30);
  });

  it('finds clickable non-button elements at all', () => {
    // If the regex silently stopped matching, the sweep below would pass on a
    // codebase full of mouse-only rows. The backdrops are the population that
    // guarantees this is non-zero.
    const anyClickable = Object.values(SOURCES).some(
      (s) => /<div\b[^<>]*onClick/s.test(s) || /<tr\b[^<>]*onClick/s.test(s),
    );
    expect(anyClickable).toBe(true);
  });

  it('has none that Tab cannot reach', () => {
    const offenders: string[] = [];
    for (const [path, source] of Object.entries(SOURCES)) {
      if (path.includes('.test.')) continue;
      for (const line of unreachable(source)) offenders.push(`${path}:${line}`);
    }
    expect(
      offenders,
      'these respond to a click and to nothing on the keyboard. Give them ' +
        'tabIndex={0}, role="button" and an onKeyDown handling Enter and ' +
        'Space — or make them a real <button>.',
    ).toEqual([]);
  });
});
