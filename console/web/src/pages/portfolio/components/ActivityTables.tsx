import { Badge, Button, Group, Stack, Text, Tooltip } from '@mantine/core';

import { DataTable, type DataTableColumn } from '@/components';

import type { FillRow, OrderRow } from '../api';
import { whyOf } from '../why';

function SimBadge({ mode }: { mode: string | null }) {
  if ((mode ?? 'test') === 'live') return null;
  return (
    <Badge size="xs" color="blue" variant="light">
      SIM
    </Badge>
  );
}

export function OrdersTable({ rows, onCancel, loading, onSelect }: {
  rows: OrderRow[];
  onCancel?: (row: OrderRow) => void;
  loading?: boolean;
  /** Opens this order's story in the detail pane. */
  onSelect?: (row: OrderRow) => void;
}) {
  const columns: Array<DataTableColumn<OrderRow>> = [
    { key: 'ts', header: 'Time (UTC)', sortValue: (r) => r.ts_utc,
      render: (r) => <Text size="xs" ff="monospace">{r.ts_utc}</Text> },
    {
      key: 'pair',
      header: 'Coin',
      sortValue: (r) => r.pair,
      render: (r) => (
        <Group gap={6}>
          <Text size="xs">{r.pair}</Text>
          <SimBadge mode={r.mode} />
        </Group>
      ),
    },
    { key: 'side', header: 'Buy or sell', sortValue: (r) => r.side,
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
          <Button
            size="compact-xs"
            variant="light"
            color="orange"
            // The row itself opens the detail pane; cancelling must not do both.
            onClick={(event) => {
              event.stopPropagation();
              onCancel(r);
            }}
          >
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
      {...(onSelect ? { onRowClick: onSelect } : {})}
      maxHeight={360}
      dense
      emptyTitle="No orders yet"
      emptyDescription="This bot has not asked the exchange to do anything yet."
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

export function FillsTable({ rows, loading, onSelect }: {
  rows: FillRow[];
  loading?: boolean;
  /** Opens this trade's story in the detail pane. */
  onSelect?: (row: FillRow) => void;
}) {
  const columns: Array<DataTableColumn<FillRow>> = [
    { key: 'ts', header: 'Time (UTC)', sortValue: (r) => r.ts_utc,
      render: (r) => <Text size="xs" ff="monospace">{r.ts_utc}</Text> },
    {
      key: 'pair',
      header: 'Coin',
      sortValue: (r) => r.pair,
      render: (r) => (
        <Group gap={6}>
          <Text size="xs">{r.pair}</Text>
          <SimBadge mode={r.mode} />
        </Group>
      ),
    },
    { key: 'side', header: 'Buy or sell', sortValue: (r) => r.side,
      render: (r) => (
        <Badge size="xs" color={r.side === 'buy' ? 'teal' : 'orange'}>{r.side}</Badge>
      ) },
    {
      key: 'why',
      header: 'Why, and who',
      sortValue: (r) => whyOf(r).short,
      render: (r) => {
        const why = whyOf(r);
        return (
          <Tooltip label={why.long} withArrow multiline w={320}>
            <Text
              size="xs"
              fw={why.event ? 600 : undefined}
              c={why.event ? (why.who === 'human' ? 'red' : 'orange') : why.who === 'unknown' ? 'dimmed' : undefined}
              data-testid={`fill-why-${r.id}`}
            >
              {why.short}
            </Text>
          </Tooltip>
        );
      },
    },
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
      header: 'Price vs the plan',
      align: 'right',
      sortValue: (r) => slippageBps(r),
      render: (r) => {
        const bps = slippageBps(r);
        return (
          <Tooltip
            withArrow
            multiline
            w={280}
            label={
              bps === null
                ? 'No decision-time price was recorded for this trade.'
                : bps > 0
                  ? `You paid ${(bps / 100).toFixed(3)}% more than the price the decision assumed.`
                  : `You did ${Math.abs(bps / 100).toFixed(3)}% better than the price the decision assumed.`
            }
          >
            <Stack gap={0} align="flex-end">
              <Text size="xs" ff="monospace" c={bps !== null && bps > 0 ? 'orange' : undefined}>
                {bps === null ? '—' : `${bps > 0 ? '+' : ''}${(bps / 100).toFixed(3)}%`}
              </Text>
              <Text size="xs" c="dimmed">
                {bps === null ? 'not recorded' : bps > 0 ? 'worse than planned' : 'better than planned'}
              </Text>
            </Stack>
          </Tooltip>
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
      {...(onSelect ? { onRowClick: onSelect } : {})}
      maxHeight={360}
      dense
      emptyTitle="Nothing has traded yet"
      emptyDescription="No order this bot placed has traded yet."
    />
  );
}
