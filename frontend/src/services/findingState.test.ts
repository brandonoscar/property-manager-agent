/**
 * What a risk card says when the problem behind it was already fixed.
 *
 * A detector finding becomes an Inbox card once (`bridged_at`, so a
 * resolved->new reactivation cannot re-spam the Inbox) while the condition
 * keeps moving underneath it. Measured on live data before this existed: 81
 * risk cards, 35 of them (43%) fully healed and still sitting there, 9 more
 * overstating their count.
 *
 * These pin the SENTENCE, because the sentence is the whole feature -- the
 * backend can compute the state perfectly and a card that renders nothing has
 * still told the reader a false thing.
 */
import { describe, expect, it } from 'vitest';
import { findingStateLabel, type FindingState } from './inbox';

const state = (over: Partial<FindingState>): FindingState => ({
  state: 'open',
  total: 1,
  open: 1,
  resolved: 0,
  dismissed: 0,
  unknown: 0,
  ...over,
});

describe('findingStateLabel', () => {
  it('says so when every finding behind the card has healed', () => {
    const label = findingStateLabel(
      state({ state: 'all_resolved', total: 4, open: 0, resolved: 4 }),
    );
    expect(label).toBe('All 4 resolved since this was raised');
  });

  it('does not say "all 1" for a single finding', () => {
    const label = findingStateLabel(
      state({ state: 'all_resolved', total: 1, open: 0, resolved: 1 }),
    );
    expect(label).toBe('Resolved since this was raised');
  });

  it('gives the remainder when only some healed, because the title still says four', () => {
    const label = findingStateLabel(
      state({ state: 'partly_resolved', total: 4, open: 2, resolved: 2 }),
    );
    expect(label).toBe('2 of 4 resolved since this was raised');
  });

  it('renders NOTHING on a card that is still accurate', () => {
    // A badge on every row is one people stop reading, and then it is absent
    // exactly when it matters (gotcha 52).
    expect(findingStateLabel(state({ state: 'open', total: 3, open: 3 }))).toBeNull();
  });

  it('renders nothing for a card whose findings could not be read', () => {
    // "We could not check" is not something a property manager can act on, and
    // showing doubt on a card that may be perfectly current trains them to
    // ignore the badge that matters. The API still reports it.
    expect(
      findingStateLabel(state({ state: 'unknown', total: 2, open: 1, unknown: 1 })),
    ).toBeNull();
  });

  it('renders nothing for every non-risk card', () => {
    // The backend omits the key entirely on Buildium, email and comms rows.
    expect(findingStateLabel(undefined)).toBeNull();
  });
});
