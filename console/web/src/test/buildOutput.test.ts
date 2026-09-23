import { readFileSync } from 'node:fs';
import { basename, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

/**
 * The console serves the SPA from `console/static` (spec 5.1), so the bundle has to land
 * there and nowhere else.  A build that writes somewhere else fails silently: the server
 * keeps serving the previous bundle and the operator sees stale behaviour with no error.
 */
const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

describe('build output', () => {
  it('emits into console/static', () => {
    const config = readFileSync(resolve(WEB_ROOT, 'vite.config.ts'), 'utf8');
    const outDir = /outDir:\s*'([^']+)'/.exec(config)?.[1];
    expect(outDir).toBe('../static');
    // Relative to console/web that is console/static.  The `earn-web` helper mirrors
    // console/web alone, so the resolved path is only meaningful inside the repository.
    const inRepo = basename(resolve(WEB_ROOT, '..')) === 'console';
    if (inRepo) {
      expect(resolve(WEB_ROOT, outDir as string).replace(/\\/g, '/')).toMatch(/\/console\/static$/);
    }
  });
});
