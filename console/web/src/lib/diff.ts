/** Line-level diff used by `DiffView` (config previews, change diffs, prompt versions). */

export type DiffKind = 'context' | 'add' | 'del';

export interface DiffLine {
  kind: DiffKind;
  text: string;
  leftNo: number | null;
  rightNo: number | null;
}

export interface DiffStats {
  added: number;
  removed: number;
  changed: boolean;
}

function splitLines(text: string): string[] {
  if (text === '') return [];
  return text.replace(/\r\n/g, '\n').replace(/\n$/, '').split('\n');
}

/** Longest common subsequence table, O(n*m); inputs here are config files, not repos. */
function lcsMatrix(a: string[], b: string[]): number[][] {
  const table: number[][] = Array.from({ length: a.length + 1 }, () =>
    new Array<number>(b.length + 1).fill(0),
  );
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      const row = table[i] as number[];
      const next = table[i + 1] as number[];
      row[j] = a[i] === b[j] ? (next[j + 1] as number) + 1 : Math.max(next[j] as number, row[j + 1] as number);
    }
  }
  return table;
}

export function diffLines(before: string, after: string): DiffLine[] {
  const a = splitLines(before);
  const b = splitLines(after);
  const table = lcsMatrix(a, b);
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      out.push({ kind: 'context', text: a[i] as string, leftNo: i + 1, rightNo: j + 1 });
      i += 1;
      j += 1;
    } else if ((table[i + 1]?.[j] ?? 0) >= (table[i]?.[j + 1] ?? 0)) {
      out.push({ kind: 'del', text: a[i] as string, leftNo: i + 1, rightNo: null });
      i += 1;
    } else {
      out.push({ kind: 'add', text: b[j] as string, leftNo: null, rightNo: j + 1 });
      j += 1;
    }
  }
  while (i < a.length) {
    out.push({ kind: 'del', text: a[i] as string, leftNo: i + 1, rightNo: null });
    i += 1;
  }
  while (j < b.length) {
    out.push({ kind: 'add', text: b[j] as string, leftNo: null, rightNo: j + 1 });
    j += 1;
  }
  return out;
}

export function diffStats(lines: DiffLine[]): DiffStats {
  const added = lines.filter((line) => line.kind === 'add').length;
  const removed = lines.filter((line) => line.kind === 'del').length;
  return { added, removed, changed: added + removed > 0 };
}

/** Collapse long runs of unchanged lines, keeping `context` lines around each hunk. */
export function collapseContext(lines: DiffLine[], context = 3): Array<DiffLine | 'gap'> {
  const keep = new Set<number>();
  lines.forEach((line, index) => {
    if (line.kind === 'context') return;
    for (let k = index - context; k <= index + context; k += 1) {
      if (k >= 0 && k < lines.length) keep.add(k);
    }
  });
  const out: Array<DiffLine | 'gap'> = [];
  let gapOpen = false;
  lines.forEach((line, index) => {
    if (keep.has(index)) {
      out.push(line);
      gapOpen = false;
    } else if (!gapOpen) {
      out.push('gap');
      gapOpen = true;
    }
  });
  return out;
}
