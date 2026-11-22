/**
 * Every property-management system gets its own logo on Settings → Connectors.
 *
 * Rentvine (live) and Propertyware had no entry and rendered the '?' fallback,
 * the tile that means "we don't know this connector".
 */

import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';

import { ConnectorLogo } from './ConnectorLogo';
import { PMS } from '../lib/pms';
import { PMS_CONNECTOR_IDS } from '../services/connectors';

const FALLBACK_LETTERS = '?';

describe('ConnectorLogo', () => {
  it('draws the fallback for an id it does not know', () => {
    const { container } = render(<ConnectorLogo id="not_a_connector" />);
    expect(container.textContent).toBe(FALLBACK_LETTERS);
  });

  it.each([...new Set([...Object.keys(PMS), ...PMS_CONNECTOR_IDS])])(
    'gives the %s card a mark or a letter tile, never the fallback',
    (id) => {
      const { container } = render(<ConnectorLogo id={id} />);
      const drawn = container.querySelector('svg') !== null;
      const letters = container.textContent ?? '';
      expect(drawn || (letters !== '' && letters !== FALLBACK_LETTERS)).toBe(true);
    },
  );
});
