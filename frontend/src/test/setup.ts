/**
 * Shared vitest setup — runs before every test file.
 *
 * Loads `@testing-library/jest-dom` matchers so component tests can
 * assert against `.toBeInTheDocument()` / `.toHaveTextContent(...)`
 * etc. Cleanup runs between tests automatically (RTL ≥ 16 wires this
 * up via the testing-library `afterEach` hook).
 *
 * Polyfills `window.matchMedia` so `useReducedMotion()` from motion/
 * react doesn't crash in jsdom (which doesn't ship one).
 */

import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';

if (typeof window !== 'undefined' && !window.matchMedia) {
  // Force ``prefers-reduced-motion: reduce`` true so framer-motion's
  // ``useReducedMotion()`` skips transforms + exit animations in
  // tests — otherwise the AnimatePresence step transitions tear
  // down the previous step asynchronously and assertions fight
  // against the 200ms animation window.
  window.matchMedia = (query: string): MediaQueryList =>
    ({
      matches: query.includes('prefers-reduced-motion'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList;
}

afterEach(() => {
  cleanup();
});
