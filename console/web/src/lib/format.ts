/** Formatting helpers.  Timestamps are UTC ISO-8601 with `Z` everywhere (CLAUDE.md). */

export const GULF_TZ = 'Asia/Dubai';

const utcTime = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

const gulfTime = new Intl.DateTimeFormat('en-GB', {
  timeZone: GULF_TZ,
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
});

const utcStamp = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

export function toDate(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === '') return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatUtcClock(value: string | number | Date): string {
  const date = toDate(value);
  return date ? utcTime.format(date) : '--:--:--';
}

export function formatGulfClock(value: string | number | Date): string {
  const date = toDate(value);
  return date ? gulfTime.format(date) : '--:--:--';
}

export function formatUtcStamp(value: string | number | Date | null | undefined): string {
  const date = toDate(value);
  if (!date) return '—';
  return `${utcStamp.format(date).replace(',', '')}Z`;
}

/** Compact "3m ago" / "in 12m" style relative time. */
export function formatRelative(
  value: string | number | Date | null | undefined,
  now: Date = new Date(),
): string {
  const date = toDate(value);
  if (!date) return '—';
  const deltaMs = date.getTime() - now.getTime();
  const abs = Math.abs(deltaMs);
  const units: Array<[number, string]> = [
    [86_400_000, 'd'],
    [3_600_000, 'h'],
    [60_000, 'm'],
    [1_000, 's'],
  ];
  for (const [ms, suffix] of units) {
    if (abs >= ms) {
      const amount = Math.floor(abs / ms);
      return deltaMs >= 0 ? `in ${amount}${suffix}` : `${amount}${suffix} ago`;
    }
  }
  return 'now';
}

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—';
  const total = Math.floor(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

export function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return value.toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function formatUsd(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${value < 0 ? '-' : ''}$${formatNumber(Math.abs(value), digits)}`;
}

/** `0.0425` -> `4.25%`.  Fractions are the storage unit (`x-unit: fraction`). */
export function formatFractionPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${formatNumber(value * 100, digits)}%`;
}

export function formatPct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${formatNumber(value, digits)}%`;
}

export function sleeveLabel(sleeve: string): string {
  return sleeve.toUpperCase();
}

export function truncate(text: string, max = 80): string {
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}
