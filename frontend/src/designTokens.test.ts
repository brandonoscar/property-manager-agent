/**
 * The app's type and colour stay on occupella.com's system (2026-09-29).
 *
 *   · Nothing is set below 12px. The scale is 12/13/14/16/20/28.
 *   · No Geist, no Playfair, no #1E73BC (the old blue). The faces are IBM
 *     Plex Sans, Fraunces and IBM Plex Mono; the blue is #1957A0.
 *
 * ⚠ Reads CODE, not text: TSX is parsed with oxc (`rolldown/utils`, which Vite
 * ships; TypeScript 7 has no JS API) and only string literals, template text,
 * `fontSize` values, JSX `fontSize` attributes and the numbers in any
 * `*FONT*` size table are checked. A comment that explains the rule by quoting the
 * old value must not trip it (gotcha 40).
 *
 * globals.css is checked by tests/unit/test_frontend_design_tokens.py: vitest
 * hands a CSS import back as an empty string, `?raw` included.
 */

import { parseSync } from 'rolldown/utils';
import { describe, expect, it } from 'vitest';

const SOURCES = import.meta.glob(['./**/*.tsx', './**/*.ts', '!./**/*.test.ts', '!./**/*.test.tsx'], {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

const MIN_PX = 12;
const RETIRED = /geist|playfair|#1e73bc/i;
const TEXT_PX = /\btext-\[(\d+(?:\.\d+)?)px\]/g;

interface Node {
  type: string;
  [key: string]: unknown;
}

function walk(node: unknown, visit: (n: Node) => void): void {
  if (!node || typeof node !== 'object') return;
  if (Array.isArray(node)) {
    for (const child of node) walk(child, visit);
    return;
  }
  const n = node as Node;
  if (typeof n.type === 'string') visit(n);
  for (const key of Object.keys(n)) {
    if (key === 'parent') continue;
    walk(n[key], visit);
  }
}

function keyName(key: unknown): string | null {
  const k = key as { type?: string; name?: string; value?: unknown } | null;
  if (!k) return null;
  if (k.type === 'Identifier') return k.name ?? null;
  if (k.type === 'Literal' && typeof k.value === 'string') return k.value;
  return null;
}

/** Every problem in one file, as `file: what`. */
function problems(file: string, source: string): string[] {
  const out: string[] = [];
  const { program } = parseSync(file, source) as unknown as { program: Node };
  const checkText = (text: string) => {
    for (const m of text.matchAll(TEXT_PX)) {
      if (Number(m[1]) < MIN_PX) out.push(`${file}: text-[${m[1]}px]`);
    }
    if (RETIRED.test(text)) out.push(`${file}: "${text.slice(0, 60)}"`);
  };
  walk(program, (n) => {
    if (n.type === 'Literal' && typeof n.value === 'string') checkText(n.value);
    if (n.type === 'TemplateElement') checkText(String((n.value as { cooked?: string }).cooked ?? ''));
    if (n.type === 'Property' && keyName(n.key) === 'fontSize') {
      const v = n.value as { type?: string; value?: unknown };
      if (v?.type === 'Literal' && typeof v.value === 'number' && v.value < MIN_PX) {
        out.push(`${file}: fontSize: ${v.value}`);
      }
    }
    // A size table (`const _FONT_PX = { sm: 12, md: 14 }`): every number in it.
    if (n.type === 'VariableDeclarator' && /font/i.test((n.id as { name?: string })?.name ?? '')) {
      walk(n.init, (m) => {
        if (m.type === 'Literal' && typeof m.value === 'number' && m.value < MIN_PX) {
          out.push(`${file}: ${(n.id as { name: string }).name} ${m.value}`);
        }
      });
    }
    if (n.type === 'JSXAttribute' && (n.name as { name?: string })?.name === 'fontSize') {
      const v = n.value as { type?: string; value?: unknown };
      if (v?.type === 'Literal' && Number(v.value) < MIN_PX) out.push(`${file}: fontSize="${v.value}"`);
    }
  });
  return out;
}

describe('type and colour follow the site', () => {
  it('reads a real corpus', () => {
    expect(Object.keys(SOURCES).length).toBeGreaterThan(100);
  });

  it('sets nothing below 12px and names no retired face or colour, in code', () => {
    const found = Object.entries(SOURCES).flatMap(([file, src]) => problems(file, src));
    expect(found).toEqual([]);
  });

  it('the checker itself catches each kind of violation', () => {
    const bad = [
      `const a = <div className="text-[11px]" />;`,
      `const b = { fontSize: 10.5 };`,
      `const c = <text fontSize="10" />;`,
      `const d = { fontFamily: 'Geist Variable' };`,
      'const e = `color: #1E73BC`;',
      `const _FONT_PX = { sm: 10.5, md: 12 };`,
    ];
    for (const src of bad) expect(problems('x.tsx', src), src).not.toEqual([]);
    expect(problems('x.tsx', `// fontSize: 10 and Geist, in a comment\nconst ok = { fontSize: 12 };`)).toEqual([]);
  });
});
