/**
 * Minimal 5-field cron support for the `cron` form widget (spec 12: "CronInput with
 * preview").  Only what crontab(5) needs for `ops.schedules`: `*`, `a`, `a-b`, `*\/n`,
 * `a-b/n` and comma lists, in minute / hour / day-of-month / month / day-of-week order.
 * No `@reboot`, no names, no `L`/`#` — those are rejected with a readable message.
 */

export interface CronParts {
  minute: number[];
  hour: number[];
  dayOfMonth: number[];
  month: number[];
  dayOfWeek: number[];
  /** True when the field was literally `*` (matters for the dom/dow OR rule). */
  domRestricted: boolean;
  dowRestricted: boolean;
}

export class CronParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'CronParseError';
  }
}

const FIELD_RANGES: Array<[string, number, number]> = [
  ['minute', 0, 59],
  ['hour', 0, 23],
  ['day of month', 1, 31],
  ['month', 1, 12],
  ['day of week', 0, 6],
];

function parseField(raw: string, name: string, min: number, max: number): number[] {
  const values = new Set<number>();
  for (const chunk of raw.split(',')) {
    const piece = chunk.trim();
    if (piece === '') throw new CronParseError(`empty ${name} field`);
    const [rangePart, stepPart] = piece.split('/');
    let step = 1;
    if (stepPart !== undefined) {
      step = Number(stepPart);
      if (!Number.isInteger(step) || step <= 0) {
        throw new CronParseError(`bad step "${stepPart}" in ${name}`);
      }
    }
    let lo: number;
    let hi: number;
    if (rangePart === '*' || rangePart === undefined) {
      lo = min;
      hi = max;
    } else if (rangePart.includes('-')) {
      const [a, b] = rangePart.split('-');
      lo = Number(a);
      hi = Number(b);
    } else {
      lo = Number(rangePart);
      hi = stepPart === undefined ? lo : max;
    }
    if (!Number.isInteger(lo) || !Number.isInteger(hi)) {
      throw new CronParseError(`bad ${name} value "${piece}"`);
    }
    if (lo < min || hi > max || lo > hi) {
      throw new CronParseError(`${name} "${piece}" is outside ${min}-${max}`);
    }
    for (let value = lo; value <= hi; value += step) values.add(value);
  }
  return [...values].sort((a, b) => a - b);
}

export function parseCron(expression: string): CronParts {
  const fields = expression.trim().split(/\s+/);
  if (fields.length !== 5) {
    throw new CronParseError(`expected 5 fields, got ${fields.length}`);
  }
  const parsed = fields.map((field, index) => {
    const spec = FIELD_RANGES[index];
    if (!spec) throw new CronParseError('too many fields');
    const [name, min, max] = spec;
    return parseField(field, name, min, max);
  });
  const [minute, hour, dayOfMonth, month, dayOfWeek] = parsed as [
    number[],
    number[],
    number[],
    number[],
    number[],
  ];
  return {
    minute,
    hour,
    dayOfMonth,
    month,
    dayOfWeek,
    domRestricted: fields[2] !== '*',
    dowRestricted: fields[4] !== '*',
  };
}

export function isValidCron(expression: string): boolean {
  try {
    parseCron(expression);
    return true;
  } catch {
    return false;
  }
}

function matches(parts: CronParts, date: Date): boolean {
  if (!parts.minute.includes(date.getUTCMinutes())) return false;
  if (!parts.hour.includes(date.getUTCHours())) return false;
  if (!parts.month.includes(date.getUTCMonth() + 1)) return false;
  const domHit = parts.dayOfMonth.includes(date.getUTCDate());
  const dowHit = parts.dayOfWeek.includes(date.getUTCDay());
  // crontab(5): when both day fields are restricted, either one matching is enough.
  if (parts.domRestricted && parts.dowRestricted) return domHit || dowHit;
  if (parts.domRestricted) return domHit;
  if (parts.dowRestricted) return dowHit;
  return true;
}

/**
 * The next `count` firing times at or after `from`, as UTC `Date`s.
 * Returns fewer entries when nothing fires inside the 400-day search horizon.
 */
export function nextCronFires(expression: string, count = 5, from: Date = new Date()): Date[] {
  const parts = parseCron(expression);
  const cursor = new Date(from.getTime());
  cursor.setUTCSeconds(0, 0);
  cursor.setUTCMinutes(cursor.getUTCMinutes() + 1);
  const limit = 400 * 24 * 60; // minutes
  const hits: Date[] = [];
  for (let step = 0; step < limit && hits.length < count; step += 1) {
    if (matches(parts, cursor)) hits.push(new Date(cursor.getTime()));
    cursor.setUTCMinutes(cursor.getUTCMinutes() + 1);
  }
  return hits;
}

export function describeCron(expression: string): string {
  try {
    const parts = parseCron(expression);
    const every = (values: number[], max: number) =>
      values.length === max ? 'every' : values.join(',');
    return `minute ${every(parts.minute, 60)}, hour ${every(parts.hour, 24)}`;
  } catch (error) {
    return error instanceof Error ? error.message : 'invalid cron';
  }
}
