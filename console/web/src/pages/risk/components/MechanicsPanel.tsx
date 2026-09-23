import { Badge, Card, Code, Group, SimpleGrid, Stack, Text } from '@mantine/core';
import type { Mechanics } from '../api';

interface Row {
  label: string;
  value: string;
  path: string;
}

function fraction(value: unknown): string {
  return typeof value === 'number' ? `${(value * 100).toFixed(2)}%` : String(value ?? '—');
}

function pick(node: unknown, key: string): unknown {
  return node && typeof node === 'object' ? (node as Record<string, unknown>)[key] : undefined;
}

function rows(m: Mechanics): Row[] {
  const t = m.trading as Record<string, unknown>;
  const sl = pick(t, 'stoploss');
  const tp = pick(t, 'take_profit');
  const dca = pick(t, 'dca');
  const pyr = pick(t, 'pyramid');
  const reb = pick(t, 'rebalance');
  const ot = pick(t, 'order_types');
  const base = `trading.sleeves.${m.sleeve}`;
  const ladder = pick(tp, 'ladder');
  return [
    { label: 'sizing mode', value: String(pick(t, 'sizing_mode') ?? '—'),
      path: `${base}.sizing_mode` },
    { label: 'order types',
      value: `entry ${String(pick(ot, 'entry'))} · exit ${String(pick(ot, 'exit'))}`,
      path: `${base}.order_types` },
    { label: 'fixed stop', value: fraction(pick(sl, 'fixed_pct')),
      path: `${base}.stoploss.fixed_pct` },
    { label: 'stop on exchange', value: String(pick(sl, 'on_exchange') ?? '—'),
      path: `${base}.stoploss.on_exchange` },
    { label: 'trailing',
      value: pick(pick(sl, 'trailing'), 'enabled')
        ? `on · ${fraction(pick(pick(sl, 'trailing'), 'distance_pct'))} from ` +
          `${fraction(pick(pick(sl, 'trailing'), 'activate_profit_pct'))}`
        : 'off',
      path: `${base}.stoploss.trailing` },
    { label: 'ATR stop',
      value: pick(pick(sl, 'atr'), 'enabled')
        ? `on · ${String(pick(pick(sl, 'atr'), 'mult'))}× ATR` +
          `(${String(pick(pick(sl, 'atr'), 'period'))})`
        : 'off',
      path: `${base}.stoploss.atr` },
    { label: 're-entry cooldown',
      value: `${String(pick(sl, 'reentry_cooldown_hours') ?? 0)} h`,
      path: `${base}.stoploss.reentry_cooldown_hours` },
    { label: 'TP ladder',
      value: Array.isArray(ladder) && ladder.length
        ? `${ladder.length} rung(s)`
        : 'off',
      path: `${base}.take_profit.ladder` },
    { label: 'DCA',
      value: pick(dca, 'enabled')
        ? `on · ${String(pick(dca, 'max_adds'))} adds @ ${fraction(pick(dca, 'step_pct'))}`
        : 'off',
      path: `${base}.dca` },
    { label: 'pyramid',
      value: pick(pyr, 'enabled')
        ? `on · ${String(pick(pyr, 'max_adds'))} adds @ ` +
          `${fraction(pick(pyr, 'trigger_profit_pct'))}`
        : 'off',
      path: `${base}.pyramid` },
    { label: 'rebalance',
      value: `every ${String(pick(reb, 'min_interval_hours') ?? 0)} h · band from ` +
        `${String(pick(reb, 'band_source') ?? 'execution')}`,
      path: `${base}.rebalance` },
  ];
}

export interface MechanicsPanelProps {
  mechanics: Mechanics;
  onOpenSetting?: (path: string) => void;
}

/** Read-only: the effective mechanics for this sleeve, each deep-linking into Settings. */
export function MechanicsPanel({ mechanics, onOpenSetting }: MechanicsPanelProps) {
  return (
    <Card withBorder padding="md">
      <Group justify="space-between" mb="sm">
        <Text fw={600}>Effective trading mechanics</Text>
        <Group gap="xs">
          <Badge variant="light">{mechanics.timeframe}</Badge>
          <Badge variant="light" color={mechanics.mode === 'live' ? 'red' : 'blue'}>
            {mechanics.mode.toUpperCase()}
          </Badge>
          <Badge variant="light" color="gray">
            {mechanics.startup_candles} startup candles
          </Badge>
        </Group>
      </Group>
      <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="xs">
        {rows(mechanics).map((row) => (
          <Stack key={row.label} gap={2}>
            <Text size="xs" c="dimmed">{row.label}</Text>
            <Text size="sm">{row.value}</Text>
            <Code
              style={onOpenSetting ? { cursor: 'pointer' } : undefined}
              onClick={onOpenSetting ? () => onOpenSetting(row.path) : undefined}
            >
              {row.path}
            </Code>
          </Stack>
        ))}
      </SimpleGrid>
    </Card>
  );
}
