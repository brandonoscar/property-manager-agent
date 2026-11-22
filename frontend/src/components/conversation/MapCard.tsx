/**
 * Static-map renderer (maps #539).
 *
 * A location tool (``properties_near`` / ``find_places_near_property``) emits a
 * ``map`` event with its geocoded points; this renders the resulting static
 * image inline in the conversation stream. ``data.url`` is our OWN key-free
 * ``/api/v1/maps/static`` proxy path — the Google key never reaches the browser
 * (see ``api/routes/maps.py``), and the <img> loads it same-origin with no auth
 * header needed.
 *
 * Deliberately dumb: one bordered, rounded image with an optional caption,
 * sized to the bubble band like TabularContent. No live JS map, no external
 * script — just an image the browser caches.
 */

import type { MapData } from './types';

interface MapCardProps {
  data: MapData;
  /** Test hook prefix for the rendered card. */
  testIdPrefix?: string;
}

export function MapCard({ data, testIdPrefix = 'map' }: MapCardProps) {
  if (!data?.url) return null;
  const count = Array.isArray(data.markers) ? data.markers.length : 0;
  const alt = data.caption || `Map with ${count} location${count === 1 ? '' : 's'}`;

  return (
    <div
      data-testid={`${testIdPrefix}-root`}
      data-marker-count={count}
      className="my-3 max-w-[420px] rounded-[var(--r-md)] overflow-hidden"
      style={{
        background: 'var(--canvas-elevated)',
        border: '1px solid var(--line)',
      }}
    >
      <img
        data-testid={`${testIdPrefix}-img`}
        src={data.url}
        alt={alt}
        loading="lazy"
        className="block w-full h-auto"
        style={{ maxWidth: '100%' }}
      />
      {data.caption ? (
        <div
          data-testid={`${testIdPrefix}-caption`}
          className="px-3 py-2 text-[13px]"
          style={{ color: 'var(--ink-muted)', borderTop: '1px solid var(--line)' }}
        >
          {data.caption}
        </div>
      ) : null}
    </div>
  );
}
