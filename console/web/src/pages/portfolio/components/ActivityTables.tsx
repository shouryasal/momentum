import { Badge, Button, Group, Stack, Text } from '@mantine/core';

import { DataTable, type DataTableColumn } from '@/components';

import type { FillRow, OrderRow } from '../api';

function SimBadge({ mode }: { mode: string | null }) {
  if ((mode ?? 'test') === 'live') return null;
  return (
    <Badge size="xs" color="blue" variant="light">
      SIM
    </Badge>
  );
}

export function OrdersTable({ rows, onCancel, loading }: {
  rows: OrderRow[];
  onCancel?: (row: OrderRow) => void;
  loading?: boolean;
}) {
  const columns: Array<DataTableColumn<OrderRow>> = [
    { key: 'ts', header: 'Time (UTC)', sortValue: (r) => r.ts_utc,
      render: (r) => <Text size="xs" ff="monospace">{r.ts_utc}</Text> },
    {
      key: 'pair',
      header: 'Pair',
      sortValue: (r) => r.pair,
      render: (r) => (
        <Group gap={6}>
          <Text size="xs">{r.pair}</Text>
          <SimBadge mode={r.mode} />
        </Group>
      ),
    },
    { key: 'side', header: 'Side', sortValue: (r) => r.side,
      render: (r) => (
        <Badge size="xs" color={r.side === 'buy' ? 'teal' : 'orange'}>{r.side}</Badge>
      ) },
    { key: 'type', header: 'Type', render: (r) => <Text size="xs">{r.order_type}</Text> },
    { key: 'amount', header: 'Amount', align: 'right',
      render: (r) => <Text size="xs" ff="monospace">{r.amount ?? '—'}</Text> },
    { key: 'price', header: 'Price', align: 'right',
      render: (r) => <Text size="xs" ff="monospace">{r.price ?? '—'}</Text> },
    { key: 'status', header: 'Status', sortValue: (r) => r.status,
      render: (r) => <Text size="xs">{r.status}</Text> },
    {
      key: 'actions',
      header: '',
      render: (r) =>
        onCancel && r.status === 'open' ? (
          <Button size="compact-xs" variant="light" color="orange"
            onClick={() => onCancel(r)}>
            Cancel
          </Button>
        ) : null,
    },
  ];
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(r) => String(r.id)}
      loading={loading}
      maxHeight={360}
      dense
      emptyTitle="No orders"
      emptyDescription="Nothing has been submitted for this sleeve yet."
    />
  );
}

/** Slippage against the decision-time quote, in bps — the TCA anchor the gate captured. */
export function slippageBps(row: FillRow): number | null {
  const quote = row.side === 'buy' ? row.quote_ask : row.quote_bid;
  if (!quote || !row.fill_price) return null;
  const sign = row.side === 'buy' ? 1 : -1;
  return ((row.fill_price - quote) / quote) * 10_000 * sign;
}

export function FillsTable({ rows, loading }: { rows: FillRow[]; loading?: boolean }) {
  const columns: Array<DataTableColumn<FillRow>> = [
    { key: 'ts', header: 'Time (UTC)', sortValue: (r) => r.ts_utc,
      render: (r) => <Text size="xs" ff="monospace">{r.ts_utc}</Text> },
    {
      key: 'pair',
      header: 'Pair',
      sortValue: (r) => r.pair,
      render: (r) => (
        <Group gap={6}>
          <Text size="xs">{r.pair}</Text>
          <SimBadge mode={r.mode} />
        </Group>
      ),
    },
    { key: 'side', header: 'Side', sortValue: (r) => r.side,
      render: (r) => (
        <Badge size="xs" color={r.side === 'buy' ? 'teal' : 'orange'}>{r.side}</Badge>
      ) },
    { key: 'amount', header: 'Amount', align: 'right',
      render: (r) => <Text size="xs" ff="monospace">{r.fill_amount}</Text> },
    { key: 'price', header: 'Price', align: 'right',
      render: (r) => <Text size="xs" ff="monospace">{r.fill_price}</Text> },
    {
      key: 'fee',
      header: 'Fee',
      align: 'right',
      render: (r) => (
        <Text size="xs" ff="monospace">
          {r.fee_amount ?? '—'} {r.fee_currency ?? ''}
        </Text>
      ),
    },
    {
      key: 'slippage',
      header: 'Slippage (bps)',
      align: 'right',
      sortValue: (r) => slippageBps(r),
      render: (r) => {
        const bps = slippageBps(r);
        return (
          <Stack gap={0} align="flex-end">
            <Text size="xs" ff="monospace" c={bps !== null && bps > 0 ? 'orange' : undefined}>
              {bps === null ? '—' : bps.toFixed(1)}
            </Text>
          </Stack>
        );
      },
    },
  ];
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(r) => String(r.id)}
      loading={loading}
      maxHeight={360}
      dense
      emptyTitle="No fills"
      emptyDescription="No order has filled for this sleeve yet."
    />
  );
}
