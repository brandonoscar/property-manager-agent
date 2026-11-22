import { describe, it, expect } from 'vitest';

import { parsePasswordFlow } from './authFlow';

describe('parsePasswordFlow', () => {
  it('detects an invite link', () => {
    expect(
      parsePasswordFlow('#access_token=abc&refresh_token=def&type=invite'),
    ).toBe('invite');
  });

  it('detects a recovery link', () => {
    expect(parsePasswordFlow('#access_token=abc&type=recovery')).toBe('recovery');
  });

  it('detects type=invite at the end of the hash', () => {
    expect(parsePasswordFlow('#expires_at=123&type=invite')).toBe('invite');
  });

  it('returns null for a normal sign-in / empty hash', () => {
    expect(parsePasswordFlow('')).toBeNull();
    expect(parsePasswordFlow('#access_token=abc&type=signup')).toBeNull();
    expect(parsePasswordFlow('#some-other-anchor')).toBeNull();
  });

  it('does not match a substring like type=invited', () => {
    expect(parsePasswordFlow('#type=invitedxyz')).toBeNull();
  });
});
