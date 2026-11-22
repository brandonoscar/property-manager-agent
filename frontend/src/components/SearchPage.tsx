/**
 * Search — the "look something up on the web" surface.
 *
 * Hybrid-product behaviour: the search BOX lives in the Occupella app (so the
 * entry point is here, in the left nav, Occupella-branded), but the RESULTS open
 * as a real browser tab — never embedded inside the app. We use a plain
 * ``window.open``: inside the Occupella Electron browser the shell's
 * window-open handler turns that into a real chrome tab (tabs + address bar);
 * in a normal browser it's just a new tab. Either way the user "lands in the
 * browser, ready to read results" instead of having a web page stuffed into
 * the app chrome.
 *
 * Engine: DuckDuckGo for now (no API key, clean results page). When the
 * search-engine decision lands (leaning Brave Search API for a Occupella-branded
 * results page), only SEARCH_ENGINE_URL changes.
 */

import { useRef, useState } from 'react';
import { Search as SearchIcon, ArrowUpRight } from 'lucide-react';

import { HelixisMark } from './HelixisMark';
import { DownloadHelixis } from './DownloadHelixis';
import { isDesktopApp } from '../services/desktop';

/** Results-page URL builder. Swap this one line for a different engine. */
const SEARCH_ENGINE_URL = (q: string) =>
  `https://duckduckgo.com/?q=${encodeURIComponent(q)}`;

export function SearchPage() {
  // The full browser experience (real tabs, address bar, docked copilot) only
  // exists in the Occupella desktop shell. In a plain browser, gate Search behind
  // the desktop download instead of offering a web-only search box.
  if (!isDesktopApp()) return <DownloadHelixis />;
  return <WebSearch />;
}

function WebSearch() {
  const [query, setQuery] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  const runSearch = (raw: string) => {
    const q = raw.trim();
    if (!q) return;
    // Open results in a real browser tab (see file header). ``noopener`` so the
    // results page can't reach back into the app window.
    window.open(SEARCH_ENGINE_URL(q), '_blank', 'noopener,noreferrer');
    setQuery('');
    inputRef.current?.focus();
  };

  return (
    <div className="flex flex-col items-center justify-center h-full px-6">
      <div className="w-full max-w-[640px] flex flex-col items-center -mt-16">
        {/* Wordmark */}
        <div className="flex items-center gap-3 mb-2">
          <HelixisMark />
          <span
            className="text-[28px] font-semibold tracking-tight"
            style={{ color: 'var(--ink)' }}
          >
            Occupella
          </span>
        </div>
        <p
          className="mb-7 text-[14px]"
          style={{ color: 'var(--ink-muted)' }}
        >
          Search the web — results open in a new browser tab.
        </p>

        {/* Search box */}
        <form
          className="w-full"
          onSubmit={(e) => {
            e.preventDefault();
            runSearch(query);
          }}
        >
          <div
            className="flex items-center gap-3 w-full h-12 px-4 rounded-[var(--r-lg)] border transition-colors duration-150 focus-within:border-[var(--iris)]"
            style={{
              background: 'var(--canvas-elevated)',
              borderColor: 'var(--line)',
            }}
          >
            <SearchIcon
              size={18}
              strokeWidth={1.5}
              style={{ color: 'var(--ink-subtle)' }}
            />
            <input
              ref={inputRef}
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search the web"
              autoFocus
              aria-label="Search the web"
              className="flex-1 bg-transparent outline-none text-[16px]"
              style={{ color: 'var(--ink)' }}
            />
            <button
              type="submit"
              aria-label="Search"
              disabled={!query.trim()}
              className="flex items-center justify-center w-8 h-8 rounded-[var(--r-md)] transition-colors duration-150 disabled:opacity-40"
              style={{ background: 'var(--iris-soft)', color: 'var(--iris-hover)' }}
            >
              <ArrowUpRight size={17} strokeWidth={1.75} />
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
