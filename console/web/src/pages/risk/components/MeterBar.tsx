import { Group, Progress, Stack, Text, Tooltip } from '@mantine/core';

import type { LimitUnit, Meter } from '../api';

const UNIT_FORMAT: Record<string, (v: number) => string> = {
  fraction: (v) => `${(v * 100).toFixed(1)}%`,
  pct: (v) => `${v.toFixed(1)}%`,
  usdt: (v) => `${v.toFixed(2)} USDT`,
  count: (v) => v.toFixed(0),
  minutes: (v) => `${v.toFixed(0)} min`,
};

export function fmt(value: number, unit: LimitUnit | string = 'fraction'): string {
  if (!Number.isFinite(value)) return '—';
  return (UNIT_FORMAT[unit] ?? UNIT_FORMAT.fraction)(value);
}

/** Amber from 70% of a limit, red at or past it — the same thresholds everywhere. */
function tone(pct: number): string {
  if (pct >= 0.9) return 'red';
  if (pct >= 0.7) return 'yellow';
  return 'teal';
}

export interface MeterBarProps {
  label: string;
  meter: Meter;
  unit?: LimitUnit | string;
  /** A floor (usdt_floor) is healthy ABOVE its limit, so "used" is read the other way. */
  floor?: boolean;
  help?: string;
}

export function MeterBar({ label, meter, unit = 'fraction', floor = false, help }:
MeterBarProps) {
  const raw = Number.isFinite(meter.pct) ? meter.pct : 0;
  const pct = Math.max(0, Math.min(raw, 1));
  const breached = floor ? meter.headroom < 0 : meter.used > meter.limit;
  return (
    <Stack gap={4} data-testid={`meter-${label}`}>
      <Group justify="space-between" gap="xs">
        <Tooltip label={help ?? label} disabled={!help} withArrow multiline w={280}>
          <Text size="sm" fw={500}>
            {label}
          </Text>
        </Tooltip>
        <Text size="sm" c={breached ? 'red' : undefined} ff="monospace">
          {fmt(meter.used, unit)} / {fmt(meter.limit, unit)}
        </Text>
      </Group>
      <Progress
        value={pct * 100}
        color={breached ? 'red' : tone(pct)}
        aria-label={`${label} utilisation`}
      />
      <Text size="xs" c="dimmed">
        {floor ? 'above the floor by ' : 'headroom '}
        {fmt(meter.headroom, unit)}
      </Text>
    </Stack>
  );
}
