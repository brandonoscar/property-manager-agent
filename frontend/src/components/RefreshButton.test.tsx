/**
 * The button, and the corpus of surfaces that need one.
 *
 * ⚠ The behavioural half is small on purpose; the half that matters is the
 * SWEEP. A refresh button is the kind of thing that gets added to three pages
 * and forgotten on the fourth, and the omission is invisible — a page that
 * never refetches looks exactly like a page whose data has not changed
 * (gotcha 45: the failure mode is "silently permits less").
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { RefreshButton } from './RefreshButton';

const SOURCES = import.meta.glob('../**/*.tsx', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

/** Every page-level list surface, and where its refresh lives. */
const LIST_SURFACES = [
  'InboxPage.tsx',
  'ActivityPage.tsx',
  'OperationsPage.tsx',
  'CrmPage.tsx',
  'PropertiesPage.tsx',
];

function source(suffix: string): string {
  const key = Object.keys(SOURCES).find((k) => k.endsWith(suffix));
  expect(key, `${suffix} not found in the glob`).toBeTruthy();
  return SOURCES[key as string];
}

describe('RefreshButton', () => {
  it('names the surface, not just the verb', () => {
    // Three of these in one view read as "Refresh, Refresh, Refresh" to
    // somebody listing a page's controls.
    render(<RefreshButton onClick={() => {}} busy={false} label="activity" />);
    expect(screen.getByLabelText('Refresh activity')).toBeTruthy();
  });

  it('spins and refuses a second click while a reload is in flight', () => {
    const onClick = vi.fn();
    const { rerender } = render(
      <RefreshButton onClick={onClick} busy={false} label="activity" />,
    );
    fireEvent.click(screen.getByLabelText('Refresh activity'));
    expect(onClick).toHaveBeenCalledTimes(1);

    rerender(<RefreshButton onClick={onClick} busy label="activity" />);
    const button = screen.getByLabelText('Refresh activity') as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    // The spin IS the answer to "did that do anything" — a reload returning
    // identical rows is otherwise indistinguishable from a missed click.
    expect(button.querySelector('.animate-spin')).toBeTruthy();
  });
});

describe('the surfaces that need one', () => {
  it('is reading real sources', () => {
    expect(Object.keys(SOURCES).length).toBeGreaterThan(30);
  });

  it('every page-level list can be reloaded without leaving it', () => {
    const missing = LIST_SURFACES.filter((f) => !/RefreshCw|RefreshButton/.test(source(f)));
    expect(
      missing,
      'these show data a webhook or a colleague changes seconds later, and ' +
        'the only way to see it is to navigate away and back',
    ).toEqual([]);
  });

  it('the CRM refresh reflects a REAL fetch, not a timer', () => {
    // ⚠ This one is worth pinning because the easy version is a lie: the CRM's
    // views keep their old rows on screen while refetching, so a fixed-duration
    // spin would be a claim about a request nobody measured. The overview view
    // reports its own fetch instead.
    const crm = source('CrmPage.tsx');
    expect(crm).toMatch(/onLoadingChange=\{setRefreshing\}/);
    expect(crm).toMatch(/busy=\{refreshing\}/);
    expect(crm).not.toMatch(/busy=\{false\}/);
    // ⚠ And the setter has to be REAL state. A mutation that kept both lines
    // above while stubbing setRefreshing to a no-op passed every assertion —
    // the wiring read correctly and the button never spun. A source guard
    // cannot see semantics (gotcha 62), so it has to pin the declaration too.
    expect(crm, 'setRefreshing is not backed by state — the spin is dead').toMatch(
      /const \[refreshing, setRefreshing\] = useState/,
    );
  });
});
