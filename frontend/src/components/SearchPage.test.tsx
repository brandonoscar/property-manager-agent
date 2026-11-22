/**
 * SearchPage — the web-search entry point, gated by the desktop shell.
 *
 * Contract: inside the Occupella desktop app (window.helixis present) Search shows
 * the search box, and results open in a REAL browser tab via window.open (the
 * shell turns that into a chrome tab). In a plain browser (no window.helixis)
 * Search is replaced by the desktop-download gate — no web-search fallback.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import { SearchPage } from './SearchPage';

afterEach(() => {
  vi.restoreAllMocks();
  delete (window as unknown as { helixis?: unknown }).helixis;
});

describe('SearchPage — inside the desktop shell', () => {
  beforeEach(() => {
    // The shell injects window.helixis via its preload bridge.
    (window as unknown as { helixis?: unknown }).helixis = {};
  });

  it('opens results in a new tab with the encoded query on submit', () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    render(<SearchPage />);

    const input = screen.getByLabelText('Search the web') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'zillow rentals 10128' } });
    fireEvent.click(screen.getByLabelText('Search'));

    expect(openSpy).toHaveBeenCalledTimes(1);
    const [url, target, features] = openSpy.mock.calls[0];
    expect(url).toBe('https://duckduckgo.com/?q=zillow%20rentals%2010128');
    expect(target).toBe('_blank');
    // noopener so the results page can't reach back into the app window.
    expect(String(features)).toContain('noopener');
  });

  it('clears the box after a search so it is ready for the next one', () => {
    vi.spyOn(window, 'open').mockReturnValue(null);
    render(<SearchPage />);

    const input = screen.getByLabelText('Search the web') as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'plumbers near me' } });
    fireEvent.submit(input.closest('form')!);

    expect(input.value).toBe('');
  });

  it('does nothing on an empty / whitespace-only query', () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null);
    render(<SearchPage />);

    fireEvent.submit(
      (screen.getByLabelText('Search the web') as HTMLInputElement).closest('form')!,
    );
    fireEvent.change(screen.getByLabelText('Search the web'), {
      target: { value: '   ' },
    });
    fireEvent.submit(
      (screen.getByLabelText('Search the web') as HTMLInputElement).closest('form')!,
    );

    expect(openSpy).not.toHaveBeenCalled();
  });
});

describe('SearchPage — plain browser (download gate)', () => {
  beforeEach(() => {
    // The download gate's mount effect looks up the latest release; stub fetch
    // so it resolves offline (404 → "coming soon" state).
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 404 }));
  });

  it('shows the desktop-download gate, not a search box', () => {
    render(<SearchPage />);

    // The web-only search box is gone…
    expect(screen.queryByLabelText('Search the web')).toBeNull();
    // …and the download gate copy is present.
    expect(screen.getByText(/lives on your desktop/i)).toBeTruthy();
  });
});
