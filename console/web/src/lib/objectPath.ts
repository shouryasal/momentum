/** Immutable get/set/delete on nested plain objects and arrays, addressed by a path. */

export type PathSegment = string | number;
export type ObjectPath = readonly PathSegment[];

/** Dotted display form used by validation errors and Ctrl-K: `risk.max_dd.a`, `pairs[0]`. */
export function pathToString(path: ObjectPath): string {
  return path.reduce<string>((acc, segment) => {
    if (typeof segment === 'number') return `${acc}[${segment}]`;
    return acc === '' ? segment : `${acc}.${segment}`;
  }, '');
}

export function getAt(source: unknown, path: ObjectPath): unknown {
  let cursor: unknown = source;
  for (const segment of path) {
    if (cursor === null || cursor === undefined) return undefined;
    if (typeof segment === 'number') {
      if (!Array.isArray(cursor)) return undefined;
      cursor = cursor[segment];
    } else {
      if (typeof cursor !== 'object') return undefined;
      cursor = (cursor as Record<string, unknown>)[segment];
    }
  }
  return cursor;
}

function cloneContainer(value: unknown, segment: PathSegment): unknown {
  if (typeof segment === 'number') return Array.isArray(value) ? [...value] : [];
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return { ...(value as Record<string, unknown>) };
  }
  return {};
}

export function setAt<T>(source: T, path: ObjectPath, value: unknown): T {
  if (path.length === 0) return value as T;
  const [head, ...rest] = path as [PathSegment, ...PathSegment[]];
  const container = cloneContainer(source, head);
  const child = getAt(source, [head]);
  const nextValue = rest.length === 0 ? value : setAt(child, rest, value);
  if (typeof head === 'number') {
    const arr = container as unknown[];
    arr[head] = nextValue;
    return arr as unknown as T;
  }
  const obj = container as Record<string, unknown>;
  obj[head] = nextValue;
  return obj as unknown as T;
}

export function deleteAt<T>(source: T, path: ObjectPath): T {
  if (path.length === 0) return source;
  const [head, ...rest] = path as [PathSegment, ...PathSegment[]];
  if (rest.length === 0) {
    if (typeof head === 'number') {
      const arr = Array.isArray(source) ? [...(source as unknown[])] : [];
      arr.splice(head, 1);
      return arr as unknown as T;
    }
    const obj = { ...((source ?? {}) as Record<string, unknown>) };
    delete obj[head];
    return obj as unknown as T;
  }
  const child = getAt(source, [head]);
  return setAt(source, [head], deleteAt(child, rest));
}
