/**
 * Rows and panels operated with the keyboard, exercised rather than scanned.
 *
 * ⚠ `keyboardReachable.test.tsx` sweeps the corpus and can only see that the
 * ATTRIBUTES are present. A mutation proved that is not enough: deleting
 * `e.preventDefault()` from the Space handler left it green, and the whole
 * point of preventing the default on Space is that the browser otherwise
 * scrolls the page — moving the table out from under the row being activated.
 * jsdom does not scroll, so `defaultPrevented` is the only observable form of
 * it, exactly as with the focus trap.
 *
 * Same reason for the DetailShell case: losing its Escape handler broke no
 * test at all, because nothing had ever pressed Escape at it.
 */

import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { OpsTable } from './operations/OpsTable';
import { DetailShell } from './connectors/DetailShell';

interface Row {
  id: string;
  name: string;
}

function table(onRowClick?: (row: Row) => void) {
  return render(
    <OpsTable<Row>
      columns={[{ key: 'name', label: 'Name', value: (r) => r.name }]}
      rows={[{ id: 'a', name: 'Redbud Plumbing' }]}
      rowKey={(r) => r.id}
      onRowClick={onRowClick}
      emptyText="Nothing here."
      testId="ops"
    />,
  );
}

describe('an operations table row', () => {
  it('opens on Enter', () => {
    const open = vi.fn();
    table(open);
    fireEvent.keyDown(screen.getByTestId('ops-row-a'), { key: 'Enter' });
    expect(open).toHaveBeenCalledWith({ id: 'a', name: 'Redbud Plumbing' });
  });

  it('opens on Space, and stops the page scrolling out from under it', () => {
    const open = vi.fn();
    table(open);
    const row = screen.getByTestId('ops-row-a');
    const event = new KeyboardEvent('keydown', {
      key: ' ',
      bubbles: true,
      cancelable: true,
    });
    fireEvent(row, event);
    expect(open).toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(true);
  });

  it('is a tab stop only when it actually does something', () => {
    // A row with no click handler that still takes focus is a stop a keyboard
    // user has to pass for no reward — the cycle gets longer and nothing is
    // gained.
    table(undefined);
    expect(screen.getByTestId('ops-row-a').getAttribute('tabindex')).toBeNull();
  });

  it('is a tab stop when it does', () => {
    table(vi.fn());
    expect(screen.getByTestId('ops-row-a').getAttribute('tabindex')).toBe('0');
  });

  it('ignores keys that are not activation', () => {
    const open = vi.fn();
    table(open);
    fireEvent.keyDown(screen.getByTestId('ops-row-a'), { key: 'a' });
    expect(open).not.toHaveBeenCalled();
  });
});

describe('a connector panel', () => {
  it('closes on Escape', () => {
    // This shell is behind every connector panel — Buildium API, webhooks,
    // Composio, SMS. Before this the only dismissal that did not require
    // finding the X was clicking the backdrop, which is a mouse affordance
    // with no keyboard equivalent.
    const onClose = vi.fn();
    render(
      <DetailShell title="Buildium" subtitle="API credentials" onClose={onClose}>
        <p>body</p>
      </DetailShell>,
    );
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });

  it('does not close on any other key', () => {
    const onClose = vi.fn();
    render(
      <DetailShell title="Buildium" subtitle="API credentials" onClose={onClose}>
        <p>body</p>
      </DetailShell>,
    );
    fireEvent.keyDown(window, { key: 'Enter' });
    expect(onClose).not.toHaveBeenCalled();
  });
});
