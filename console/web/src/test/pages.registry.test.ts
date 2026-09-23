import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { ROUTES, pageModuleKey } from '../routes';

/**
 * Integration guards for the page registry.
 *
 * These read the source tree rather than rendering: mounting twenty-one pages would need
 * twenty-one sets of API fixtures, and what is being checked here is structural — that
 * every route in spec §12 resolves to a page a package actually shipped, and that every
 * page puts its own actions into the Ctrl-K palette.
 */
const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '..');

/** `/src/pages/risk/RiskPage.tsx` -> the file on disk. */
function onDisk(moduleKey: string): string {
  return join(SRC, '..', moduleKey.replace(/^\//, ''));
}

/** The entry module plus everything it imports from its own folder, concatenated. */
function pageSources(moduleKey: string): string {
  const entry = onDisk(moduleKey);
  const folder = dirname(entry);
  const seen = new Set<string>();
  const parts: string[] = [];

  const visit = (file: string) => {
    if (seen.has(file) || !existsSync(file)) return;
    seen.add(file);
    const text = readFileSync(file, 'utf8');
    parts.push(text);
    for (const match of text.matchAll(/from '(\.[^']*)'/g)) {
      const relative = match[1] as string;
      for (const suffix of ['.tsx', '.ts', '/index.tsx', '/index.ts']) {
        const candidate = resolve(dirname(file), `${relative}${suffix}`);
        if (candidate.startsWith(folder) && existsSync(candidate)) {
          visit(candidate);
          break;
        }
      }
    }
  };

  visit(entry);
  return parts.join('\n');
}

describe('page registry', () => {
  it('has a shipped page module for every route in spec 12', () => {
    const missing = ROUTES.filter((route) => pageModuleKey(route.id) === null).map((r) => r.id);
    expect(missing, 'these routes would render the not-built-yet placeholder').toEqual([]);
  });

  it('registers Ctrl-K entries from every page', () => {
    const silent: string[] = [];
    for (const route of ROUTES) {
      const key = pageModuleKey(route.id);
      if (!key) continue;
      const registers = new RegExp(`usePageCommands\\(\\s*'${route.id}'`).test(pageSources(key));
      if (!registers) silent.push(route.id);
    }
    expect(silent, 'every page owns its palette entries (spec 12, Ctrl-K)').toEqual([]);
  });

  it('leaves the SSE stream to the shell — no page opens its own EventSource', () => {
    const offenders: string[] = [];
    for (const route of ROUTES) {
      const key = pageModuleKey(route.id);
      if (!key) continue;
      const source = pageSources(key);
      if (/\buseEventStream\b|new EventSource\(/.test(source)) offenders.push(route.id);
    }
    expect(offenders, 'pages subscribe through useTopicEvents, not their own connection').toEqual(
      [],
    );
  });
});
