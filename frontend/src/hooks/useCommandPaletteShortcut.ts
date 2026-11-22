/**
 * Global ⌘K / Ctrl+K listener that toggles the command palette.
 *
 * Calls ``e.preventDefault()`` so the browser's built-in Find dialog
 * doesn't fight the shortcut on Chromium / Firefox.
 */

import { useEffect } from 'react';

export function useCommandPaletteShortcut(toggle: () => void): void {
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      const isModK =
        (e.key === 'k' || e.key === 'K') && (e.metaKey || e.ctrlKey);
      if (!isModK) return;
      e.preventDefault();
      toggle();
    };

    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [toggle]);
}
