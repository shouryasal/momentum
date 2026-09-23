import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { PY_CONTRACT_MODELS, SSE_TOPICS } from '../api/contracts';

/**
 * Drift guard for the hand-written DTO mirror.
 *
 * `console/contracts.py` is outside `console/web`, so it is only visible when the tests
 * run inside the repository (the `earn-web` workspace mirrors `console/web` alone).  When
 * the file cannot be found the comparison is skipped and says so; the structural checks
 * below always run.
 */
function locateContracts(): string | null {
  const fromEnv = process.env.EARN_CONTRACTS_PY;
  if (fromEnv && existsSync(fromEnv)) return fromEnv;

  let dir = dirname(fileURLToPath(import.meta.url));
  for (let depth = 0; depth < 8; depth += 1) {
    for (const candidate of [join(dir, 'contracts.py'), join(dir, 'console', 'contracts.py')]) {
      if (existsSync(candidate)) return candidate;
    }
    const parent = resolve(dir, '..');
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

/** Field names per `class X(...):` block, in declaration order. */
export function parsePydanticModels(source: string): Map<string, string[]> {
  const models = new Map<string, string[]>();
  let current: string | null = null;
  for (const rawLine of source.split(/\r?\n/)) {
    const classMatch = /^class\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(/.exec(rawLine);
    if (classMatch) {
      current = classMatch[1] ?? null;
      if (current) models.set(current, []);
      continue;
    }
    if (rawLine.trim() !== '' && !/^\s/.test(rawLine)) {
      current = null;
      continue;
    }
    if (!current) continue;
    const fieldMatch = /^ {4}([a-z_][A-Za-z0-9_]*)\s*:/.exec(rawLine);
    if (!fieldMatch) continue;
    const name = fieldMatch[1] as string;
    if (name.startsWith('_') || name === 'model_config') continue;
    models.get(current)?.push(name);
  }
  return models;
}

const contractsPath = locateContracts();
const compare = contractsPath ? it : it.skip;

describe('console/contracts.py mirror', () => {
  it('describes every mirrored model with at least one field', () => {
    expect(Object.keys(PY_CONTRACT_MODELS).length).toBeGreaterThan(20);
    for (const [name, fields] of Object.entries(PY_CONTRACT_MODELS)) {
      expect(fields.length, `${name} has no fields`).toBeGreaterThan(0);
      expect(new Set(fields).size, `${name} repeats a field`).toBe(fields.length);
      for (const field of fields) expect(field).toMatch(/^[a-z][a-z0-9_]*$/);
    }
  });

  it('pins the SSE topic list of spec 5.3', () => {
    expect([...SSE_TOPICS]).toEqual([
      'alert',
      'health',
      'kill',
      'mode',
      'transition',
      'bot',
      'nav',
      'order',
      'fill',
      'gate',
      'signal',
      'validation',
      'run',
      'proposal',
      'approval',
      'provider_switch',
      'config',
      'change',
      'job',
      'reconcile',
      'backtest',
    ]);
  });

  it('parses pydantic classes out of a python source', () => {
    const parsed = parsePydanticModels(
      ['class KillState(BaseModel):', '    engaged: bool', '    reason: str | None = None', '', 'KILL = 1', ''].join('\n'),
    );
    expect(parsed.get('KillState')).toEqual(['engaged', 'reason']);
  });

  compare('mirrors every DTO that console/contracts.py exports', () => {
    // The mirror is only a contract if it is complete: a DTO added on the python side with
    // no entry here would be invisible to the field-name check below, which is exactly the
    // drift this file exists to catch.  `_Dto` is the shared base, not a payload.
    const source = readFileSync(contractsPath as string, 'utf8');
    const python = [...parsePydanticModels(source).keys()].filter((name) => !name.startsWith('_'));
    const unmirrored = python.filter((name) => !(name in PY_CONTRACT_MODELS));
    expect(
      unmirrored,
      'console/contracts.py grew a DTO that src/api/contracts.ts does not mirror',
    ).toEqual([]);
  });

  compare('matches the field names of every mirrored DTO', () => {
    const source = readFileSync(contractsPath as string, 'utf8');
    const python = parsePydanticModels(source);

    const missing: string[] = [];
    const drifted: string[] = [];
    for (const [name, fields] of Object.entries(PY_CONTRACT_MODELS)) {
      const actual = python.get(name);
      if (!actual) {
        missing.push(name);
        continue;
      }
      const expectedSet = [...fields].sort();
      const actualSet = [...actual].sort();
      if (JSON.stringify(expectedSet) !== JSON.stringify(actualSet)) {
        drifted.push(`${name}: ts=[${expectedSet.join(',')}] py=[${actualSet.join(',')}]`);
      }
    }
    expect(
      { missing, drifted },
      `src/api/contracts.ts drifted from ${contractsPath} — update the mirror`,
    ).toEqual({ missing: [], drifted: [] });
  });
});

if (!contractsPath) {
  // Visible in the vitest output so a skipped comparison is never silent.
  console.warn(
    '[contracts.drift] console/contracts.py not found — set EARN_CONTRACTS_PY to compare the DTO mirror.',
  );
}
