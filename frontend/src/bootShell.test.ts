/**
 * The first-paint shell, and the one thing that removes it.
 *
 * ⚠ Nothing deletes `#boot` explicitly — it disappears because
 * `createRoot(container).render()` replaces the container's children. That is
 * the whole design (a removal step is a step that can fail), and it is also
 * exactly the kind of coupling that breaks silently: move the shell out of
 * `#root`, or mount React somewhere else, and the loading bar stays on screen
 * FOREVER, sitting on top of a working app. Nothing throws and no test that
 * looks at either file alone can see it.
 *
 * So this reads BOTH sides (gotcha 28): the shell is inside the container, and
 * the container is the one React mounts into.
 */

import { describe, expect, it } from 'vitest';

import html from '../index.html?raw';
import mainSource from './main.tsx?raw';

describe('the boot shell', () => {
  it('renders something before the bundle arrives', () => {
    // The failure this exists to stop is a blank page on a cold load, which
    // reads as broken rather than slow.
    expect(html).toContain('id="boot"');
    expect(html).toContain('Occupella</span>');
  });

  it('sits INSIDE the element React mounts into', () => {
    const rootOpen = html.indexOf('<div id="root">');
    const boot = html.indexOf('id="boot"');
    const rootClose = html.indexOf('</div>', boot);
    expect(rootOpen).toBeGreaterThan(-1);
    expect(boot).toBeGreaterThan(rootOpen);
    expect(rootClose).toBeGreaterThan(boot);
  });

  it('mounts React into that same element, which is what clears the shell', () => {
    expect(mainSource).toContain(`getElementById('root')`);
    expect(mainSource).toContain('createRoot');
  });

  it('needs no request of its own', () => {
    // A shell fetched over the network has not solved anything: the network is
    // the thing being waited on. The styles are inline for that reason, and a
    // stylesheet <link> added for the shell would quietly undo it.
    const head = html.slice(0, html.indexOf('</head>'));
    expect(head).toContain('<style>');
    expect(head).toContain('#boot {');
  });

  it('does not animate for somebody who asked it not to', () => {
    expect(html).toContain('prefers-reduced-motion');
  });

  it('is hidden from screen readers', () => {
    // It is on screen for a fraction of a second and then the entire document
    // is replaced underneath. Announcing that is noise, not information.
    expect(html).toMatch(/id="boot"[^>]*aria-hidden="true"/);
  });
});
