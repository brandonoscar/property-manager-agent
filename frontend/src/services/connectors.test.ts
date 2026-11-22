/**
 * Tests for the connectors service — capability sectioning and the
 * rowsByCapability filter that drives the Settings → Connectors page.
 */

import { describe, expect, it } from 'vitest';

import { rowsByCapability, type Connector } from './connectors';

function row(
  id: string,
  capabilities: Connector['capabilities'],
  overrides: Partial<Connector> = {},
): Connector {
  return {
    id,
    name: id,
    category: 'property_management',
    description: '',
    capabilities,
    authMethod: 'api_key',
    ...overrides,
  };
}

describe('rowsByCapability', () => {
  it('returns rows that include the api capability', () => {
    const rows = [
      row('buildium', ['api', 'webhook']),
      row('zillow', ['webhook']),
      row('gmail', ['api']),
    ];
    const apis = rowsByCapability(rows, 'api');
    expect(apis.map((c) => c.id)).toEqual(['buildium', 'gmail']);
  });

  it('returns rows that include the webhook capability', () => {
    const rows = [
      row('buildium', ['api', 'webhook']),
      row('zillow', ['webhook']),
      row('gmail', ['api']),
    ];
    const webhooks = rowsByCapability(rows, 'webhook');
    expect(webhooks.map((c) => c.id)).toEqual(['buildium', 'zillow']);
  });

  it('a connector that lists both capabilities appears in both filters', () => {
    const buildium = row('buildium', ['api', 'webhook']);
    expect(rowsByCapability([buildium], 'api')).toEqual([buildium]);
    expect(rowsByCapability([buildium], 'webhook')).toEqual([buildium]);
  });

  it('rows with no matching capability are excluded', () => {
    const rows = [row('only-api', ['api'])];
    expect(rowsByCapability(rows, 'webhook')).toEqual([]);
  });
});
