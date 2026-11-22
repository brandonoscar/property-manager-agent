/**
 * The frontend half of a boundary test (rule 15).
 *
 * `mirrorNullableFields.json` lists every response field the backend may send
 * as `null`; `tests/unit/test_rentvine_blind_surfaces.py` derives it from the
 * Pydantic models and fails if it drifts. This test parses the TypeScript
 * interfaces and fails if any listed field's type does not accept `null`. A
 * field typed `number` for a value the backend sends as `null` is how a
 * Rentvine company got "$0 delinquent": `null > 0` is false, so the page fell
 * into its "$0" branch.
 *
 * ⚠ Parsed with oxc (`rolldown/utils`, which Vite already ships), not the
 * TypeScript compiler: TypeScript 7 has no JavaScript API, so
 * `ts.createSourceFile` is undefined here.
 */

import { parseSync } from 'rolldown/utils';
import { describe, expect, it } from 'vitest';

import nullable from './mirrorNullableFields.json';
import operationsSource from './operations.ts?raw';
import propertiesSource from './properties.ts?raw';

// The parser's ESTree shape, only as far as this test reads it.
interface Node {
  type: string;
  [key: string]: unknown;
}

function interfaces(name: string, source: string): Map<string, Node[]> {
  const out = new Map<string, Node[]>();
  const { program } = parseSync(name, source) as unknown as { program: { body: Node[] } };
  for (const stmt of program.body) {
    const decl = (stmt.type === 'ExportNamedDeclaration' ? stmt.declaration : stmt) as Node | null;
    if (decl?.type !== 'TSInterfaceDeclaration') continue;
    const id = decl.id as { name: string };
    out.set(id.name, (decl.body as { body: Node[] }).body);
  }
  return out;
}

const declared = new Map([
  ...interfaces('properties.ts', propertiesSource),
  ...interfaces('operations.ts', operationsSource),
]);

function acceptsNull(type: Node | undefined): boolean {
  if (!type) return false;
  if (type.type === 'TSNullKeyword') return true;
  if (type.type === 'TSUnionType') return (type.types as Node[]).some(acceptsNull);
  if (type.type === 'TSParenthesizedType') return acceptsNull(type.typeAnnotation as Node);
  return false;
}

function members(name: string): Map<string, Node | undefined> {
  const out = new Map<string, Node | undefined>();
  for (const m of declared.get(name) ?? []) {
    if (m.type !== 'TSPropertySignature') continue;
    const key = m.key as { name: string };
    out.set(key.name, (m.typeAnnotation as { typeAnnotation?: Node } | null)?.typeAnnotation);
  }
  return out;
}

describe('the TypeScript types accept every null the backend sends', () => {
  const entries = Object.entries(nullable as Record<string, string[]>);

  it('reads the interfaces it names', () => {
    expect(entries.length).toBeGreaterThanOrEqual(7);
    for (const [name] of entries) expect(members(name).size, name).toBeGreaterThan(0);
  });

  it('can tell a nullable type from a plain one', () => {
    // Guards the guard: a reader that called everything nullable would pass
    // every per-interface check below.
    const summary = members('CockpitSummary');
    expect(acceptsNull(summary.get('delinquent_total'))).toBe(true);
    expect(acceptsNull(summary.get('unit_count'))).toBe(false);
  });

  for (const [name, fields] of entries) {
    it(name, () => {
      const types = members(name);
      const missing = fields.filter((field) => !types.has(field));
      const wrong = fields.filter((field) => types.has(field) && !acceptsNull(types.get(field)));
      expect({ missing, wrong }).toEqual({ missing: [], wrong: [] });
    });
  }
});
