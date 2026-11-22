/**
 * maps #539 — MapCard renders a location tool's static map inline.
 *
 * The map arrives as a ``map`` SSE event whose ``url`` is our key-free
 * ``/api/v1/maps/static`` proxy path. These pin the contract: the <img> points
 * at that proxy (never a provider URL / key), the caption renders, and a
 * missing url renders nothing rather than a broken image.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { MapCard } from './MapCard';
import type { MapData } from './types';

const DATA: MapData = {
  url: '/api/v1/maps/static?size=600x400&markers=42.36,-71.06|42.35,-71.05',
  caption: 'Your properties near Boston, MA',
  markers: [
    { lat: 42.36, lng: -71.06 },
    { lat: 42.35, lng: -71.05 },
  ],
};

describe('MapCard', () => {
  it('renders an <img> pointed at the key-free proxy', () => {
    render(<MapCard data={DATA} testIdPrefix="map-1" />);
    const img = screen.getByTestId('map-1-img') as HTMLImageElement;
    expect(img.tagName).toBe('IMG');
    expect(img.getAttribute('src')).toBe(DATA.url);
    // No provider host / API key ever reaches the DOM.
    expect(img.getAttribute('src')).not.toContain('googleapis');
    expect(img.getAttribute('src')).not.toContain('key=');
  });

  it('renders the caption and exposes the marker count', () => {
    render(<MapCard data={DATA} testIdPrefix="map-2" />);
    expect(screen.getByTestId('map-2-caption').textContent).toBe(
      'Your properties near Boston, MA',
    );
    expect(screen.getByTestId('map-2-root').getAttribute('data-marker-count')).toBe('2');
  });

  it('uses the caption as the image alt text (a11y)', () => {
    render(<MapCard data={DATA} testIdPrefix="map-3" />);
    expect(screen.getByTestId('map-3-img').getAttribute('alt')).toBe(DATA.caption);
  });

  it('renders nothing when url is missing', () => {
    const { container } = render(
      <MapCard data={{ url: '', markers: [] }} testIdPrefix="map-4" />,
    );
    expect(container.firstChild).toBeNull();
  });
});
