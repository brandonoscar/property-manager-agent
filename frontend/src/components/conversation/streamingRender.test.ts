/**
 * The answer must not "glitch in" while it streams.
 *
 * Two causes, two pins:
 * - Motion's `layout` prop on a bubble answers every size change with a scale
 *   transform, and a streaming bubble changes size on every wrapped line, so
 *   the text squashed and stretched as it grew. Guarded on the AST (rule 17),
 *   so this comment quoting the prop cannot trip it.
 * - Half-typed markdown ("**Apex Plumb") rendered as literal asterisks and
 *   then snapped to bold. `settleStreamingMarkdown` closes it for the render.
 */

import { parseSync } from 'rolldown/utils';
import { describe, expect, it } from 'vitest';

import { settleStreamingMarkdown } from './AssistantBubble';
import assistantSource from './AssistantBubble.tsx?raw';
import userSource from './UserBubble.tsx?raw';

interface Node {
  type: string;
  [key: string]: unknown;
}

/** Every JSX attribute name on a `motion.*` element in the file. */
function motionAttributes(name: string, source: string): string[] {
  const { program } = parseSync(name, source) as unknown as { program: Node };
  const found: string[] = [];
  const walk = (node: unknown) => {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) {
      node.forEach(walk);
      return;
    }
    const n = node as Node;
    if (n.type === 'JSXOpeningElement') {
      const el = n.name as Node;
      if (el.type === 'JSXMemberExpression' && (el.object as { name?: string }).name === 'motion') {
        for (const attr of n.attributes as Node[]) {
          if (attr.type === 'JSXAttribute') found.push((attr.name as { name: string }).name);
        }
      }
    }
    for (const key of Object.keys(n)) walk(n[key]);
  };
  walk(program);
  return found;
}

describe('bubbles have no layout animation', () => {
  it.each([
    ['AssistantBubble.tsx', assistantSource],
    ['UserBubble.tsx', userSource],
  ])('%s', (name, source) => {
    const attrs = motionAttributes(name, source);
    // Non-empty proves the walk found the motion element at all; a parse that
    // found nothing would otherwise pass this by measuring nothing.
    expect(attrs).toContain('initial');
    expect(attrs).not.toContain('layout');
  });
});

describe('settleStreamingMarkdown', () => {
  it('closes an open bold phrase', () => {
    expect(settleStreamingMarkdown('Call **Apex Plumb')).toBe('Call **Apex Plumb**');
  });

  it('closes before trailing space, so the closing pair still counts', () => {
    expect(settleStreamingMarkdown('Call **Apex ')).toBe('Call **Apex** ');
  });

  it('drops a marker with nothing after it yet', () => {
    expect(settleStreamingMarkdown('Call **')).toBe('Call ');
  });

  it('leaves balanced text alone', () => {
    const done = 'Apex has **14** open and `WO-12` is late.';
    expect(settleStreamingMarkdown(done)).toBe(done);
  });

  it('closes inline code and an open fence', () => {
    expect(settleStreamingMarkdown('See `WO-1')).toBe('See `WO-1`');
    expect(settleStreamingMarkdown('```\nrow 1')).toBe('```\nrow 1\n```');
  });
});
