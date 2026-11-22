/**
 * The web app's list of property-management systems (lib/pms.ts).
 *
 * `tests/unit/test_pms_registry.py` pins this file against the server's
 * registry. These pin what the callers rely on: an id the build does not know
 * is `null` (Buildium behaviour), never a guess.
 */
import { describe, expect, it } from 'vitest';

import { PMS, pmsInfo } from './pms';

describe('pmsInfo', () => {
  it('names every system the server can send', () => {
    expect(pmsInfo('buildium')?.name).toBe('Buildium');
    expect(pmsInfo('rentvine')?.name).toBe('Rentvine');
    expect(pmsInfo('rentmanager')?.name).toBe('Rent Manager');
    expect(pmsInfo('propertyware')?.name).toBe('Propertyware');
  });

  it('reads a missing or unknown id as null, not as some system', () => {
    expect(pmsInfo(null)).toBeNull();
    expect(pmsInfo(undefined)).toBeNull();
    expect(pmsInfo('')).toBeNull();
    expect(pmsInfo('appfolio')).toBeNull();
    // An inherited property is not a system.
    expect(pmsInfo('toString')).toBeNull();
  });

  it('gives live tools to Buildium alone', () => {
    expect(Object.values(PMS).filter((p) => p.liveTools).map((p) => p.id)).toEqual(['buildium']);
  });

  it('keys every entry by its own id', () => {
    for (const [key, info] of Object.entries(PMS)) expect(info.id).toBe(key);
  });
});
