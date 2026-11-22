/**
 * Every modal overlay can be escaped and cannot be tabbed out of.
 *
 * ⚠ **A HAND-TYPED LIST IS WHAT PRODUCED THE GAP THIS GUARD CLOSES.**
 * `useFocusTrap` shipped with four dialogs wired and a docstring counting
 * "four of the five dialogs here". There were SEVEN overlays. The three it
 * never counted were the CRM lead drawer, the CRM "Add a lead" modal, and the
 * webhook "Forget the signing secret?" confirm — and two of those had no
 * keyboard dismissal AT ALL, so a keyboard user who opened them was inside a
 * form they could Tab straight out of and could not close. One of the two is
 * DESTRUCTIVE (saying yes breaks every inbound webhook until somebody pastes a
 * new secret). That is gotcha 45 exactly: a control whose failure mode is
 * "silently permits more" has to read the CORPUS, because the loud failure —
 * a dialog that should have been trapped and was — is the one you notice.
 *
 * ⚠ **AND `keyboardReachable.test.tsx` EXEMPTED THESE ON A FALSE PREMISE.**
 * Its header exempts every `fixed inset-0` backdrop from needing a tab stop,
 * on the stated grounds that "the keyboard equivalent is Escape, which those
 * dialogs have". Two of them did not. The exemption is right and its
 * justification was unchecked prose (gotcha 48) — this file is what makes it
 * true rather than asserted, and that test's header now points here.
 *
 * ⚠ **SCANNED PER COMPONENT, NOT PER FILE.** A file-level sweep passes the
 * moment the file contains the strings anywhere — so a fourth un-trapped
 * dialog added to `CrmPage.tsx` (which now holds two correct ones) would sail
 * through. That is the granularity bound gotcha 118 recorded against
 * `connectorUnknownState.corpus.test.ts`, closed here by splitting on function
 * boundaries and judging each component that renders an overlay on its own.
 */

import { describe, expect, it } from 'vitest';

const SOURCES = import.meta.glob('../**/*.tsx', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

/**
 * What makes something a modal overlay: a full-viewport positioned element.
 *
 * ⚠ Anchored on the ATTRIBUTE (`className=` then a quote, brace or backtick),
 * never on the bare phrase. Three files in this repo — including this one and
 * `keyboardReachable.test.tsx` — spell `fixed inset-0` inside prose in order to
 * explain the rule, and a guard that fires on the sentence documenting it is
 * the guard people delete (gotchas 40/59/114).
 */
const OVERLAY = /className=(?:"|\{"|\{`)[^"`]*fixed inset-0/;

/** A component block: `function Name(` / `export function Name(` at column 0. */
function components(src: string): { name: string; body: string }[] {
  const lines = src.split('\n');
  const starts: { name: string; line: number }[] = [];
  lines.forEach((line, i) => {
    const m = /^(?:export )?function ([A-Za-z0-9_]+)\s*\(/.exec(line);
    if (m) starts.push({ name: m[1], line: i });
  });
  return starts.map((s, i) => ({
    name: s.name,
    body: lines.slice(s.line, starts[i + 1]?.line ?? lines.length).join('\n'),
  }));
}

/**
 * A bare scrim is not a dialog. `App.tsx`'s mobile backdrop is a self-closing
 * `<div>` with no children — it holds no content, announces nothing, and
 * `role="dialog"` on it would be a promise about a thing that is not there.
 * Detected by SHAPE rather than allowlisted by name, so a second scrim needs no
 * maintenance and a scrim that grows content stops being exempt.
 */
function isBareScrim(body: string): boolean {
  const at = body.search(OVERLAY);
  if (at === -1) return false;
  // From the overlay's className to the end of that JSX element: a scrim closes
  // with `/>` before it ever opens a child tag.
  const after = body.slice(at);
  const selfClose = after.indexOf('/>');
  const childTag = after.indexOf('>\n');
  return selfClose !== -1 && (childTag === -1 || selfClose < childTag);
}

type Overlay = { file: string; component: string; body: string };

const OVERLAYS: Overlay[] = Object.entries(SOURCES)
  .filter(([path]) => !path.includes('.test.'))
  .flatMap(([file, src]) =>
    components(src)
      .filter((c) => OVERLAY.test(c.body) && !isBareScrim(c.body))
      .map((c) => ({ file, component: c.name, body: c.body })),
  );

describe('every modal overlay is operable by keyboard', () => {
  it('is looking at real files', () => {
    // A scan that finds zero is a claim to check, not a result (gotchas
    // 49/100/101). If the glob or the cwd breaks, this fails rather than
    // reporting a clean sweep over nothing.
    expect(Object.keys(SOURCES).length).toBeGreaterThan(50);
  });

  it('tells a real overlay from prose describing one', () => {
    // ⚠ PINNED DIRECTLY, because the CORPUS CANNOT DISTINGUISH THIS TODAY.
    // A mutation dropping the `className=` anchor back to the bare phrase
    // stayed green: measured, zero production .tsx mentions `fixed inset-0`
    // outside a className, so no input separates the two readings (gotcha 58 —
    // check whether the mutant changes any output before calling it a test
    // hole). The anchor is not redundant, it is defence against the FIRST
    // comment somebody writes explaining this rule — which is precisely how
    // gotchas 40/59/114 each began. So the distinguishing input is supplied
    // here rather than waited for.
    expect(OVERLAY.test('className="fixed inset-0 z-50 flex"')).toBe(true);
    expect(OVERLAY.test('className={`fixed inset-0 z-[99] ${open}`}')).toBe(true);
    expect(OVERLAY.test('// a backdrop is any fixed inset-0 element')).toBe(false);
    expect(OVERLAY.test(' * exempt by SHAPE (`fixed inset-0`), which is what')).toBe(false);
  });

  it('finds the overlays it is meant to judge', () => {
    // Pinned by NAME, both directions: a rename that silently drops a dialog
    // out of the sweep is the same defect as never having counted it.
    const found = OVERLAYS.map((o) => o.component).sort();
    expect(found).toEqual(
      [
        'AddLeadModal',
        'ConfirmForgetDialog',
        'DetailShell',
        'FeedbackDialog',
        'InviteUsersDialog',
        'LeadDrawer',
        'RenewalsSection',
      ].sort(),
    );
  });

  it.each(OVERLAYS.map((o) => [o.component, o] as const))(
    '%s traps Tab inside itself',
    (_name, overlay) => {
      // Without this the overlay is only VISUALLY modal: Tab walks into the
      // sidebar, the composer and every link behind the scrim, all of which
      // are focusable and none of which the user can see.
      expect(overlay.body).toMatch(/useFocusTrap\(/);
    },
  );

  it.each(OVERLAYS.map((o) => [o.component, o] as const))(
    '%s closes on Escape',
    (_name, overlay) => {
      // The backdrop click is a MOUSE affordance. Escape is the whole keyboard
      // equivalent, and `keyboardReachable` exempts these backdrops from
      // carrying a tab stop precisely because this holds.
      expect(overlay.body).toMatch(/e\.key === 'Escape'/);
    },
  );

  it.each(OVERLAYS.map((o) => [o.component, o] as const))(
    '%s announces itself as a dialog',
    (_name, overlay) => {
      // `aria-modal` is the promise that the rest of the page is inert; the
      // trap above is what keeps it. A label is what a screen reader reads on
      // arrival — without one it announces "dialog" and nothing else.
      expect(overlay.body).toMatch(/role="dialog"/);
      expect(overlay.body).toMatch(/aria-modal="true"/);
      expect(overlay.body).toMatch(/aria-label(?:ledby)?=/);
    },
  );
});
