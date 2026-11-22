/**
 * groupRowsByDay — the rail's organizing scheme for large inboxes:
 * TODAY / YESTERDAY / THIS WEEK / EARLIER buckets, empty buckets dropped,
 * unparseable timestamps sunk to EARLIER (never silently lost).
 */
import { describe, expect, it } from 'vitest';

import { groupRowsByDay } from './InboxPage';
import type { InboxRow } from '../services/inbox';

/**
 * ⚠ These fixtures were UTC-DEPENDENT and the suite could not see it.
 *
 * `groupRowsByDay` buckets by LOCAL day, which is correct — an inbox row's
 * `event_time` is an instant, and "today" means the reader's today. But every
 * timestamp below was written and reasoned about in UTC, so the expectations
 * only held on a host running in UTC. When `vite.config.ts` pinned
 * `TZ=America/Los_Angeles` (so the suite could see the calendar-date bug in
 * coverage items 65/344), row `b` at `01:00Z` turned out to be 18:00 the
 * PREVIOUS day in Los Angeles and the "Today" bucket lost it.
 *
 * The code was right and the test was wrong. Times are now chosen so each row
 * lands in its intended LOCAL bucket, with the local time written beside it.
 */
const NOW = new Date('2026-08-20T15:00:00Z'); // 08:00 Thu 20 Aug, Los Angeles

function row(id: string, eventTime: string): InboxRow {
  return { id, event_time: eventTime } as InboxRow;
}

describe('groupRowsByDay', () => {
  it('buckets by local day and preserves order within buckets', () => {
    const groups = groupRowsByDay(
      [
        row('a', '2026-08-20T14:00:00Z'), // 07:00 Thu 20 — today
        row('b', '2026-08-20T10:00:00Z'), // 03:00 Thu 20 — today
        row('c', '2026-08-19T22:00:00Z'), // 15:00 Wed 19 — yesterday
        row('d', '2026-08-16T09:00:00Z'), // 02:00 Sun 16 — this week
        row('e', '2026-07-01T09:00:00Z'), // 02:00 Wed 1 Jul — earlier
      ],
      NOW,
    );
    expect(groups.map((g) => g.label)).toEqual(['Today', 'Yesterday', 'This week', 'Earlier']);
    expect(groups[0].rows.map((r) => r.id)).toEqual(['a', 'b']);
  });

  it('drops empty buckets', () => {
    const groups = groupRowsByDay([row('a', '2026-08-20T14:00:00Z')], NOW);
    expect(groups.map((g) => g.label)).toEqual(['Today']);
  });

  it('sinks unparseable timestamps to Earlier instead of losing them', () => {
    const groups = groupRowsByDay([row('junk', 'not-a-date')], NOW);
    expect(groups).toEqual([{ label: 'Earlier', rows: [expect.objectContaining({ id: 'junk' })] }]);
  });
});
