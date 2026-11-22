/**
 * Vim-style two-key chord navigation: press ``g``, then within 800ms
 * press ``c | i | a | s`` to jump views.
 *
 * Disabled while the focus is in an editable element (input, textarea,
 * contenteditable) so typing "g" in the chat box doesn't hijack
 * navigation. The caller can pass ``isPaletteOpen`` to also disable
 * chords while ⌘K is up.
 */

import { useEffect, useRef } from 'react';
import { toast } from 'sonner';
import { captureNavChordUsed, captureViewChanged } from '../services/analytics';
import { useStore } from '../stores/store';

const CHORD_TIMEOUT_MS = 800;
const HINT_ID = 'nav-chord-hint';

const CHORD_TO_VIEW: Record<string, 'chat' | 'inbox' | 'settings'> = {
  c: 'chat',
  i: 'inbox',
  s: 'settings',
};

/** Chords that open Settings on a named section. `g a` kept its meaning when
 *  Team Activity moved off the sidebar into Settings — the muscle memory is
 *  older than the layout, so the shortcut follows the destination. */
const CHORD_TO_SETTINGS_SECTION: Record<string, string> = { a: 'activity' };

function isEditable(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true;
  return target.isContentEditable;
}

export function useNavChord(options: { disabled?: boolean } = {}): void {
  const armedAtRef = useRef<number | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (options.disabled) return;
      if (isEditable(e.target)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      const key = e.key.toLowerCase();
      const now = Date.now();
      const armedAt = armedAtRef.current;
      const armed = armedAt !== null && now - armedAt <= CHORD_TIMEOUT_MS;

      if (!armed && key === 'g') {
        // Arm the chord. Show a low-noise hint that auto-dismisses.
        armedAtRef.current = now;
        toast('Press c, i, a, or s…', {
          id: HINT_ID,
          duration: CHORD_TIMEOUT_MS,
        });
        return;
      }

      if (armed) {
        // Always disarm after a single follow-up keypress, regardless of
        // whether it was a recognised chord — chord state is sticky
        // otherwise.
        armedAtRef.current = null;

        // A settings-section chord resolves to the settings VIEW; the
        // section rides alongside it.
        const section = CHORD_TO_SETTINGS_SECTION[key];
        const view = section ? ('settings' as const) : CHORD_TO_VIEW[key];
        if (!view) {
          toast.dismiss(HINT_ID);
          return;
        }

        e.preventDefault();
        toast.dismiss(HINT_ID);

        const store = useStore.getState();
        const previous = store.view;
        // ⚠ A section chord must still act when Settings is ALREADY open —
        // `g a` from the Account tab has to move to Team Activity, and the
        // same-view early return would swallow it.
        if (previous === view && !section) return;
        if (section) store.openSettings(section);
        else store.setView(view);
        captureNavChordUsed(key, previous);
        captureViewChanged(view);
      }
    };

    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [options.disabled]);
}
