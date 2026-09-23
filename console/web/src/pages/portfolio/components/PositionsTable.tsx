import { Badge, Group, Progress, Stack, Text, Tooltip } from '@mantine/core';

import { DataTable, type DataTableColumn } from '@/components';

import type { Position } from '../api';

const pct = (v: number) => (Number.isFinite(v) ? `${(v * 100).toFixed(2)}%` : '—');
const usdt = (v: number) => (Number.isFinite(v) ? v.toFixed(2) : '—');

/** Weight against its own cap, in one bar — the cap is the gate's `weight_cap` check. */
function WeightCell({ position }: { position: Position }) {
  const cap = position.weight_cap || 1;
  const used = Math.min(position.weight / cap, 1);
  return (
    <Stack gap={2} miw={140}>
      <Group justify="space-between" gap={4}>
        <Text size="xs">{pct(position.weight)}</Text>
        <Text size="xs" c="dimmed">cap {pct(cap)}</Text>
      </Group>
      <Progress value={used * 100} color={used >= 0.95 ? 'red' : 'teal'} size="sm" />
    </Stack>
  );
}

export function PositionsTable({ positions, loading, onSelect }: {
  positions: Position[];
  loading?: boolean;
  /** Opens this coin's story in the detail pane. */
  onSelect?: (row: Position) => void;
}) {
  const columns: Array<DataTableColumn<Position>> = [
    {
      key: 'pair',
      header: 'Coin',
      sortValue: (row) => row.pair,
      render: (row) => (
        <Group gap={6}>
          <Text size="sm" fw={500}>{row.pair}</Text>
          {row.sim ? (
            <Tooltip label="Test mode: these orders are simulated" withArrow>
              <Badge size="xs" color="blue" variant="light">SIM</Badge>
            </Tooltip>
          ) : null}
        </Group>
      ),
    },
    { key: 'amount', header: 'Amount', sortValue: (row) => row.amount, align: 'right',
      render: (row) => <Text size="sm" ff="monospace">{row.amount}</Text> },
    { key: 'entry', header: 'Avg entry', align: 'right',
      render: (row) => <Text size="sm" ff="monospace">{usdt(row.avg_entry)}</Text> },
    { key: 'mark', header: 'Mark', align: 'right',
      render: (row) => <Text size="sm" ff="monospace">{usdt(row.mark)}</Text> },
    {
      key: 'upnl',
      header: 'Up or down',
      align: 'right',
      sortValue: (row) => row.upnl_usdt,
      render: (row) => (
        <Text size="sm" ff="monospace" c={row.upnl_usdt >= 0 ? 'teal' : 'red'}>
          {usdt(row.upnl_usdt)} ({pct(row.upnl_pct)})
        </Text>
      ),
    },
    { key: 'weight', header: 'Share of the pot vs the most allowed', sortValue: (row) => row.weight,
      render: (row) => <WeightCell position={row} /> },
    {
      key: 'entries',
      header: 'Buys used',
      align: 'center',
      sortValue: (row) => row.entries_used,
      render: (row) => (
        <Tooltip label="Initial entry plus DCA and pyramid adds (risk.max_entries_per_trade)"
          withArrow>
          <Badge size="sm" variant="light"
            color={row.entries_used >= row.entries_max ? 'orange' : 'gray'}>
            {row.entries_used} / {row.entries_max}
          </Badge>
        </Tooltip>
      ),
    },
    {
      key: 'stop',
      header: 'Stop',
      align: 'right',
      render: (row) => (
        <Stack gap={0}>
          <Text size="sm" ff="monospace">{usdt(row.stop_price)}</Text>
          <Text size="xs" c="dimmed">
            {pct(row.stop_from_open)}
            {row.trailing_active ? ' · trailing' : ''}
          </Text>
        </Stack>
      ),
    },
    {
      key: 'tp',
      header: 'Next profit-taking step',
      render: (row) =>
        row.next_tp_rung ? (
          <Text size="xs">
            tp{row.next_tp_rung.index + 1} @ {pct(row.next_tp_rung.at_profit_pct)} · sell{' '}
            {pct(row.next_tp_rung.sell_fraction)}
          </Text>
        ) : (
          <Text size="xs" c="dimmed">
            {row.tp_rungs_fired.length ? 'ladder done' : 'no ladder'}
          </Text>
        ),
    },
  ];
  return (
    <DataTable
      columns={columns}
      rows={positions}
      rowKey={(row) => row.pair}
      loading={loading}
      {...(onSelect ? { onRowClick: onSelect } : {})}
      emptyTitle="No open positions"
      emptyDescription="This bot holds nothing right now, or it is not running."
    />
  );
}
