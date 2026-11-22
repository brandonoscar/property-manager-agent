/**
 * Settings explains a choice, or it says nothing.
 *
 * The Account pane had a paragraph under almost every row — "Ends your session
 * on every device, including this one" beneath a button that says *Sign out
 * everywhere*; "Quote this if you report a problem" beneath a build number;
 * "Removes your workspace and everything in it — chat history, Inbox, uploaded
 * documents and the cached copy of your Buildium records…" beneath *Delete
 * account*. Sixteen rows, each restating its own label, several of them in the
 * register of a privacy notice. It read as a product that did not trust the
 * person using it, and as legal-heavy on a screen nobody visits for legal
 * reasons.
 *
 * Founder call, 2026-09-08: **a description survives only where the CHOICE is
 * genuinely non-obvious.** That is three places — picking a plan, connecting an
 * account or a webhook, and team roles, where "Manager" versus "Member" is a
 * real decision with consequences somebody cannot guess.
 *
 * ⚠ A CORPUS guard rather than a render test. The failure mode is a new row
 * arriving with a helpful sentence attached — nobody would notice, every test
 * would stay green, and the pane drifts back one row at a time. This counts
 * them.
 */
import { describe, expect, it } from 'vitest';

import source from './SettingsPage.tsx?raw';
import privacyHtml from '../../public/legal/privacy.html?raw';

/**
 * Every `description=` a component in this file passes, with the panel it
 * belongs to left out of the question — what matters is HOW MANY there are and
 * that each survivor is deliberate.
 */
function descriptions(src: string): string[] {
  return [...src.matchAll(/^\s*description=/gm)].map((m) => m[0].trim());
}

describe('the Account pane', () => {
  it('carries no explanatory sub-lines at all', () => {
    // Everything from `function AccountPanel` to the next top-level section.
    const start = source.indexOf('function AccountPanel()');
    const end = source.indexOf('// ── Legal', start);
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
    expect(descriptions(source.slice(start, end))).toEqual([]);
  });

  it('sends the security rows to their own section, not a heading', () => {
    // ⚠ Grouping under a heading was the FIRST attempt and it did not shorten
    // the list — password and two-factor still sat below a build number and an
    // export button. They are their own rail entry now, so `SecurityPanel` has
    // to exist, be routable, and be the only place those rows are rendered.
    expect(source).toContain('function SecurityPanel()');
    expect(source).toContain('{section === "security" && <SecurityPanel />}');
    expect(source).toContain('id: "security"');
    // The one group that legitimately stays inside Account.
    expect(source).toContain('<GroupHeader title="Workspace" />');
    // ⚠ The rows MOVED; a copy left behind on Account would render two
    // password buttons and both would work.
    const accountPane = source.slice(
      source.indexOf('function AccountPanel()'),
      source.indexOf('// ── Security'),
    );
    for (const row of ['<ChangePasswordRow />', '<TwoFactorRow />', '<ActiveSessionsRow />']) {
      expect(accountPane, `${row} is still on the Account pane`).not.toContain(row);
    }
  });

  it('shows the two things support asks for', () => {
    // "Which build are you on" was already here; "which account are you"
    // existed only inside the copied diagnostics blob, so answering the first
    // question still cost a round trip for the second.
    expect(source).toContain('label="Account ID"');
    expect(source).toContain('label="Version"');
  });
});

describe('what is left', () => {
  it('is exactly the three panes where a choice needs explaining', () => {
    // ⚠ The count is the assertion. Naming which strings survive would pass
    // whatever wording drifted in; counting them fails the moment a fourth
    // appears, which is how this pane filled up in the first place.
    // ⚠ Two, not three. Connectors lost its header AND description on
    // 2026-09-09: the rail already says Connectors, and the line under it —
    // "The accounts Occupella reads from and acts in on your behalf" — sat
    // directly above the APIs group's "Accounts Occupella reads and writes
    // through", which is the same sentence twice before a single row.
    const remaining = descriptions(source);
    expect(remaining).toHaveLength(2);

    for (const marker of [
      'Admins can change anything, including billing and the Buildium connection',
      'What Occupella has picked up about your company',
    ]) {
      expect(source, `the surviving description for "${marker}" is gone`).toContain(marker);
    }
    // The capability headings ARE the structure of that pane now, so the pane
    // still has to have them — dropping the title must not have taken the
    // groups with it.
    expect(source).toContain('<CapabilitySection');
  });

  it('does not describe the read-only knowledge page as editable', () => {
    // ⚠ The AI & Memory panel renders `content_md` and offers no delete or
    // edit control. Copy promising either would be a claim the screen cannot
    // honour — this product's own worst failure mode, pointed at a caption.
    const aiDescription = source.slice(source.indexOf('What Occupella has picked up'));
    const firstLine = aiDescription.slice(0, aiDescription.indexOf('"'));
    for (const verb of ['edit', 'delete', 'remove', 'forget']) {
      expect(firstLine.toLowerCase(), `the AI description promises "${verb}"`).not.toContain(verb);
    }
  });

  it('no longer offers the switch that duplicates a browser setting', () => {
    // ⚠ THIS ASSERTION USED TO COVER TWO ROWS AND WAS RIGHT ABOUT ONE.
    // Reduce motion duplicates `prefers-reduced-motion`, which the OS already
    // owns — a control with no purpose, correctly cut. Product analytics was
    // cut in the same pass on the same stated ground, and that ground was
    // false: it is the ONLY mechanism implementing the opt-out that
    // `frontend/public/legal/privacy.html` promises every customer by name.
    // An assertion holds a decision in place as firmly as it holds a defect,
    // and for eight days this one held a published privacy policy in the
    // wrong. Re-pointed rather than deleted — the reduce-motion half is a
    // real decision and still needs a guard.
    //
    // ⚠ ANCHORED ON CODE, NOT THE BARE NAME. The docstring above
    // `ProductAnalyticsRow` has to NAME `ReduceMotionRow` in order to explain
    // which of the two cuts was right — and a `not.toContain('ReduceMotion
    // Row')` fires on that sentence, which is how this guard failed against a
    // correct pane the moment the explanation was written. A comment can
    // mention an identifier; only JSX renders one and only a declaration
    // defines one (gotcha 40).
    expect(source).not.toContain('<ReduceMotionRow');
    expect(source).not.toContain('function ReduceMotionRow');
  });

  it('offers the opt-out the privacy policy sends people here to find', () => {
    // ⚠ BOTH SIDES, because each is fine on its own and the pair is what
    // fails. The policy's sentence — "Settings → Account → Product analytics"
    // — is only true while a row by that name exists on that pane, and the
    // row is only worth having while the policy points at it. Testing either
    // alone is what let them disagree (gotcha 28).
    // ⚠ `?raw`, never `node:fs` — the frontend tsconfig carries no node types,
    // so the fs spelling runs green under vitest and turns `tsc --noEmit` RED
    // (gotcha 97).
    const policy = privacyHtml;
    // Tags first, then whitespace: the policy wraps and marks up its own
    // sentences, so a raw substring scan misses a phrase split across a line
    // break or a <strong> (gotcha 77).
    const prose = policy.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ');

    const policyPointsHere = /Settings\s*(→|&rarr;)\s*Account\s*(→|&rarr;)\s*Product analytics/.test(
      prose,
    );
    expect(
      policyPointsHere,
      'the privacy policy stopped naming Settings → Account → Product analytics — ' +
        'if the control moved, move this sentence with it in the same change',
    ).toBe(true);

    // ⚠ `<ProductAnalyticsRow`, not the bare name — same reason as the
    // reduce-motion guard below, pointed the other way. The row's own
    // docstring names it, so `toContain('ProductAnalyticsRow')` stays green
    // with the row deleted from the pane and only the comment left behind,
    // which is precisely the failure this test exists to catch.
    expect(
      source,
      'privacy.html promises a "Product analytics" control on the Account pane and ' +
        'the pane does not render one — restore the row, or change the policy in the ' +
        'same change to name whatever replaced it',
    ).toContain('<ProductAnalyticsRow');
    expect(source).toContain('label="Product analytics"');
  });

  it('wires that row to the opt-out rather than to nothing', () => {
    // ⚠ THE WHOLE DEFECT WAS A WELL-TESTED FUNCTION WITH NO CALLER.
    // `setAnalyticsOptedOut` has eight assertions of its own in
    // analytics.optout.test.ts and was reached by nothing outside them, so a
    // row that renders and calls neither would restore the *appearance* of the
    // control and none of it — the same failure in a costume (gotcha 62). A
    // behavioural test of the helper structurally cannot see this.
    const start = source.indexOf('function ProductAnalyticsRow()');
    expect(start).toBeGreaterThan(-1);
    const body = source.slice(start, source.indexOf('\nfunction ', start + 1));

    expect(body, 'the row never writes the choice').toContain('setAnalyticsOptedOut(');
    // Without the read it renders "Turn off" to somebody who already turned it
    // off — a control that lies about the state it is in.
    expect(body, 'the row never reads the current choice').toContain('analyticsOptedOut()');
  });

  it('is read from a real file, not an empty string', () => {
    // The whole suite above passes trivially against ''. A `?raw` import that
    // silently resolves empty is not hypothetical here — it is what the
    // Tailwind plugin does to `.css`, measured the same day.
    expect(source.length).toBeGreaterThan(10_000);
    expect(descriptions(source).length).toBeGreaterThan(0);
  });
});
