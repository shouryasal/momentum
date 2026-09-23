/**
 * Turning an edited form value back into JSON-pointer patch operations.
 *
 * The server edits YAML surgically: a `replace` at an existing pointer is spliced into the
 * file so every comment outside that node survives byte for byte. So the client sends the
 * smallest possible set of ops — one per changed leaf, or one per changed array — and never
 * the whole document, which would reflow the file.
 */

import type { PatchOp } from './api';

export function pointerOf(path: Array<string | number>): string {
  if (path.length === 0) return '';
  return path
    .map((part) => `/${String(part).replace(/~/g, '~0').replace(/\//g, '~1')}`)
    .join('');
}

export function dottedOf(path: Array<string | number>): string {
  return path.map(String).join('.');
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((item, i) => deepEqual(item, b[i]));
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const ka = Object.keys(a);
    const kb = Object.keys(b);
    if (ka.length !== kb.length) return false;
    return ka.every((key) => key in b && deepEqual(a[key], b[key]));
  }
  return false;
}

/**
 * `before` → `after` as patch ops.
 *
 * Objects are walked key by key; an array is replaced whole (a one-line flow sequence is
 * still a surgical edit server-side, and per-index ops would break when the length moves).
 */
export function patchFrom(before: unknown, after: unknown, base: Array<string | number> = []): PatchOp[] {
  if (deepEqual(before, after)) return [];

  if (isPlainObject(before) && isPlainObject(after)) {
    const ops: PatchOp[] = [];
    for (const key of Object.keys(before)) {
      if (!(key in after)) {
        ops.push({ op: 'remove', path: pointerOf([...base, key]) });
      }
    }
    for (const [key, value] of Object.entries(after)) {
      if (!(key in before)) {
        ops.push({ op: 'add', path: pointerOf([...base, key]), value });
        continue;
      }
      ops.push(...patchFrom(before[key], value, [...base, key]));
    }
    return ops;
  }

  return [{ op: 'replace', path: pointerOf(base), value: after }];
}

/** Dotted paths a patch touches — what the preview drawer highlights before the server answers. */
export function touchedPaths(ops: PatchOp[]): string[] {
  return ops.map((op) =>
    op.path
      .split('/')
      .slice(1)
      .map((part) => part.replace(/~1/g, '/').replace(/~0/g, '~'))
      .join('.'),
  );
}

export function getAtDotted(value: unknown, dotted: string): unknown {
  if (!dotted) return value;
  let node: unknown = value;
  for (const part of dotted.split('.')) {
    if (Array.isArray(node)) {
      node = node[Number(part)];
    } else if (isPlainObject(node)) {
      node = node[part];
    } else {
      return undefined;
    }
  }
  return node;
}

export function setAtDotted<T>(root: T, dotted: string, next: unknown): T {
  if (!dotted) return next as T;
  const parts = dotted.split('.');
  const clone = (node: unknown, depth: number): unknown => {
    const key = parts[depth] as string;
    const last = depth === parts.length - 1;
    if (Array.isArray(node)) {
      const copy = [...node];
      copy[Number(key)] = last ? next : clone(copy[Number(key)], depth + 1);
      return copy;
    }
    const source = isPlainObject(node) ? node : {};
    return { ...source, [key]: last ? next : clone(source[key], depth + 1) };
  };
  return clone(root, 0) as T;
}

/**
 * The change the operator is about to review *and* save — derived once, used by both.
 *
 * Preview and save used to key on different conditions (`tab === 'raw'` vs
 * `tab === 'raw' && rawDirty`), so form edits reviewed from the Raw tab were previewed as
 * the unchanged file — empty diff, no effects, `requires_stepup: false` — and then saved as
 * the patch. The operator confirmed a blank diff and wrote something else.
 */
export function changeBodyFor(args: {
  rawDirty: boolean;
  rawDraft: string | null;
  ops: PatchOp[];
}): { patch?: PatchOp[]; raw?: string } {
  return args.rawDirty ? { raw: args.rawDraft ?? '' } : { patch: args.ops };
}
