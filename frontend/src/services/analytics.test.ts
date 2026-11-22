/**
 * Guards the PostHog session-recording privacy posture.
 *
 * Session recordings capture rendered DOM. For a property manager that DOM
 * carries tenant/owner names, addresses, and email bodies, so recordings must
 * mask all text + element attributes before anything reaches PostHog. This
 * flipped to `false` once (unmasked recordings keyed to real user emails); the
 * exported constant makes the safe posture a tested contract that can't
 * silently regress.
 */

import { describe, expect, it } from 'vitest';

import { RECORDING_PRIVACY } from './analytics';

describe('PostHog recording privacy', () => {
  it('masks all text and element attributes in session recordings', () => {
    expect(RECORDING_PRIVACY.mask_all_text).toBe(true);
    expect(RECORDING_PRIVACY.mask_all_element_attributes).toBe(true);
  });
});
