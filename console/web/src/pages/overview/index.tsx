/**
 * Home: the money, and nothing else.
 *
 * The owner wrote the specification for this screen themselves, and it is the whole brief:
 *
 *   *"on main page i only want to see seed money, ongoing holdings, current value of total
 *   money, maybe a table of each holding showing value profit loss and a table of
 *   transactions done, if i double click i should see reasoning"* — and, about everything
 *   else, *"i will not do things from ui, hide other things for now"*.
 *
 * So Home is four things, in this order:
 *
 *   1. What was put in.
 *   2. What it is worth now, up or down in money and percent, and one line against simply
 *      having held BTC.
 *   3. One row per holding: how much, what it cost, what it is worth, profit or loss.
 *   4. Every buy and sell, newest first, each marked as simulated, demo or real money.
 *
 * And nothing else. No health widget, no incident list, no schedule, no funnel counts, no
 * gate tallies, no "what changed today", no safety strip. Every one of those still exists
 * — `HomeDetail.tsx` holds them and they are still reachable by URL — they are simply not
 * what a person sees when they open this console.
 *
 * Depth is a double click. Single click selects a row; double click opens the reasoning
 * pane beside it with what the local model noticed, what Claude concluded and why, the
 * plan it produced and what the safety check decided. The open row lives in the URL, so
 * the reasoning is deep-linkable.
 *
 * `overview.test.tsx` fails the day a technical block comes back.
 */
import { Alert, Badge, Card, Group, Loader, Stack, Text } from '@mantine/core';
import { IconAlertTriangle } from '@tabler/icons-react';
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useState } from 'react';
import { Link } from 'react-router-dom';

import { errorMessage } from '@/api';
import { usePageCommands } from '@/app/commandRegistry';
import { useDetailRoute } from '@/app/detailParam';
import { useTopicEvents } from '@/app/EventStreamContext';
import { DataTable, DetailPane, EmptyState, MasterDetail, PageIntro, StatCard, type DataTableColumn } from '@/components';
import { formatUtcStamp } from '@/lib/format';
import { sleeveFullName } from '@/lib/plain';
import { routeBlurb } from '@/routes';

import type { PortfolioPayload } from '../portfolio/api';
import { ControlCard } from './ControlCard';
import { HomeDetail, HOME_DETAIL_SUBTITLE, HOME_DETAIL_TITLE, LEGACY_DETAIL_KINDS } from './HomeDetail';
import { MoneyBadge } from './MoneyBadge';
import { HOME_SLEEVES, overviewApi, overviewKeys } from './api';
import {
  benchmarkLine,
  demoLine,
  gainPhrase,
  holdingRows,
  lastRunFor,
  money,
  seedHint,
  signedMoney,
  signedPct,
  totals,
  transactionRows,
  type HoldingRow,
  type TransactionRow,
} from './money';
import { Reasoning, type ReasoningTarget } from './Reasoning';

const REFETCH_TOPICS = ['nav', 'gate', 'signal', 'config', 'mode', 'kill', 'run'] as const;

/** The two kinds of row a double click opens, plus the kinds old links still carry. */
export const HOME_DETAIL_KINDS = ['holding', 'transaction', ...LEGACY_DETAIL_KINDS] as const;

function amountText(value: number): string {
  if (!Number.isFinite(value)) return '—';
  const digits = Math.abs(value) >= 1 ? 4 : 8;
  return String(Number(value.toFixed(digits)));
}

export default function OverviewPage() {
  const queryClient = useQueryClient();
  const detail = useDetailRoute(HOME_DETAIL_KINDS);
  const [selected, setSelected] = useState<string | null>(null);

  const query = useQuery({
    queryKey: overviewKeys.bundle,
    queryFn: () => overviewApi.get(),
    refetchInterval: 60_000,
  });

  // One query per bot: a bot that is down must not blank the tables for the one that is up.
  const books = useQueries({
    queries: HOME_SLEEVES.map((sleeve) => ({
      queryKey: overviewKeys.portfolio(sleeve),
      queryFn: () => overviewApi.portfolio(sleeve),
      refetchInterval: 60_000,
      retry: false,
    })),
  });

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: overviewKeys.bundle });
    for (const sleeve of HOME_SLEEVES) {
      void queryClient.invalidateQueries({ queryKey: overviewKeys.portfolio(sleeve) });
    }
  }, [queryClient]);

  useTopicEvents(REFETCH_TOPICS, refresh);

  usePageCommands('overview', [
    {
      id: 'refresh',
      title: 'Refresh Home',
      subtitle: 'Re-read what it is worth, what it holds and what it has traded',
      run: (ctx) => {
        refresh();
        ctx.close();
      },
    },
  ]);

  // Two books of at most a hundred rows each: cheap enough to derive on every render, and
  // a memo keyed on a query result that is replaced wholesale would buy nothing.
  const payloads = books.map((book) => (book.data ?? null) as PortfolioPayload | null);
  const data = query.data ?? null;
  const holdings = holdingRows(payloads, data);
  const transactions = transactionRows(payloads);
  const booksLoading = books.some((book) => book.isLoading);

  if (query.isLoading) {
    return (
      <Group justify="center" p="xl">
        <Loader />
      </Group>
    );
  }

  if (query.isError || !data) {
    return <EmptyState title="Home unavailable" description={errorMessage(query.error)} />;
  }

  if (!data.ok) {
    return (
      <Stack gap="md">
        <Alert color="red" icon={<IconAlertTriangle size={18} />} title="Configuration is invalid">
          {data.config_error}
        </Alert>
        <Text size="sm">
          Nothing runs until the settings can be read. Fix them under{' '}
          <Link to="/settings">Setup</Link>.
        </Text>
      </Stack>
    );
  }

  const summary = totals(data);
  const demo = demoLine(data);
  const allTest = Object.values(data.mode?.sleeves ?? {}).every(
    (sleeve) => String(sleeve.state ?? '').toUpperCase() === 'TEST',
  );

  const openHolding = (row: HoldingRow) => detail.open('holding', row.key);
  const openTransaction = (row: TransactionRow) => detail.open('transaction', row.key);

  const target = reasoningTarget(detail.kind, detail.id, holdings, transactions);
  const detailPane = detail.opened && detail.kind && detail.id ? (
    <DetailPane
      title={
        target
          ? target.kind === 'holding'
            ? `Why it holds ${target.asset}`
            : `Why it traded ${target.asset}`
          : (HOME_DETAIL_TITLE[detail.kind] ?? 'Details')
      }
      subtitle={
        target
          ? 'What was noticed, what Claude concluded and why, the plan it produced, and what the safety check decided.'
          : (HOME_DETAIL_SUBTITLE[detail.kind] ?? 'Everything behind this line.')
      }
      rawId={target?.runId ?? null}
      onClose={detail.close}
    >
      {target ? (
        <Reasoning target={target} />
      ) : (
        <HomeDetail kind={detail.kind} id={detail.id} data={data} open={detail.open} />
      )}
    </DetailPane>
  ) : null;

  return (
    <MasterDetail detail={detailPane}>
      <Stack gap="md" data-testid="overview-page">
        <PageIntro title="Home" blurb={routeBlurb('overview')} />

        {/*
          0: is it running by itself, and how much does it do alone?

          Home was four things and the brief said so. The owner then asked a question this
          screen could not answer — "is there a button on the UI that starts the autonomous
          running?" — and the honest answer was no: every run so far had been typed by a
          human and nothing here said so. A number is meaningless if you do not know whether
          anything is producing it, so the control goes above the money rather than beside
          it. It is one block, not five: the liveness line, both bots with whose money and
          how much each does by itself, and the preview.
        */}
        <ControlCard />

        {/* 1 and 2: what went in, what it is worth, and the one BTC comparison. */}
        <Group align="stretch" gap="md" grow wrap="wrap" data-testid="money-cards">
          <StatCard
            label="Money put in"
            value={summary.seed === null ? 'Not set yet' : money(summary.seed)}
            hint={seedHint(data)}
          />
          <StatCard
            label="What it is worth now"
            value={summary.value === null ? 'Nothing recorded yet' : money(summary.value)}
            delta={
              summary.gain === null
                ? null
                : {
                    value: `${signedMoney(summary.gain)} (${signedPct(summary.gainFraction)})`,
                    positive: summary.gain >= 0,
                  }
            }
            hint={benchmarkLine(data)}
          />
        </Group>

        {/*
          One line, only while a demo account exists but nothing is running on it. It is a
          footnote about money that is not in play — the moment a bot is on demo this
          disappears, because the demo balances have become the two cards above.
        */}
        {demo === null ? null : (
          <Text size="xs" c="dimmed" data-testid="demo-line">
            {demo}
          </Text>
        )}

        {/* 3: one row per holding. */}
        <Card withBorder padding="md" radius="md" data-testid="holdings-card">
          <Stack gap="xs">
            <Text fw={600}>What it holds</Text>
            {booksLoading && holdings.length === 0 ? (
              <Group justify="center" p="md">
                <Loader size="sm" />
              </Group>
            ) : holdings.length === 0 ? (
              <Text size="sm" c="dimmed" data-testid="no-holdings">
                No holdings yet — every bit of the money is still cash.
              </Text>
            ) : (
              <DataTable
                columns={holdingColumns}
                rows={holdings}
                rowKey={(row) => row.key}
                onRowClick={(row) => setSelected(row.key)}
                onRowDoubleClick={openHolding}
                selectedKey={selected}
                caption="Double-click a row to see the reasoning behind it."
              />
            )}
          </Stack>
        </Card>

        {/* 4: every buy and sell, newest first. */}
        <Card withBorder padding="md" radius="md" data-testid="transactions-card">
          <Stack gap="xs">
            <Text fw={600}>What it has bought and sold</Text>
            {booksLoading && transactions.length === 0 ? (
              <Group justify="center" p="md">
                <Loader size="sm" />
              </Group>
            ) : transactions.length === 0 ? (
              <Text size="sm" c="dimmed" data-testid="no-transactions">
                {allTest
                  ? 'No transactions yet — the bots are in test mode, so nothing has been sent to an exchange.'
                  : 'No transactions yet.'}
              </Text>
            ) : (
              <DataTable
                columns={transactionColumns}
                rows={transactions}
                rowKey={(row) => row.key}
                onRowClick={(row) => setSelected(row.key)}
                onRowDoubleClick={openTransaction}
                selectedKey={selected}
                maxHeight={420}
                dense
                caption="Double-click a row to see the reasoning behind it."
              />
            )}
          </Stack>
        </Card>
      </Stack>
    </MasterDetail>
  );
}

/* --------------------------------------------------------------------------- the columns */

const holdingColumns: Array<DataTableColumn<HoldingRow>> = [
  {
    key: 'asset',
    header: 'Coin',
    sortValue: (row) => row.asset,
    render: (row) => (
      <Group gap={6} wrap="nowrap">
        <Text size="sm" fw={500}>
          {row.asset}
        </Text>
        <MoneyBadge mode={row.mode} />
      </Group>
    ),
  },
  {
    key: 'amount',
    header: 'How much',
    align: 'right',
    sortValue: (row) => row.amount,
    render: (row) => (
      <Text size="sm" ff="monospace">
        {amountText(row.amount)}
      </Text>
    ),
  },
  {
    key: 'cost',
    header: 'What it cost',
    align: 'right',
    sortValue: (row) => row.costTotal,
    render: (row) => (
      <Stack gap={0} align="flex-end">
        <Text size="sm">{row.costTotal === null ? 'not recorded' : money(row.costTotal)}</Text>
        {row.costEach === null ? null : (
          <Text size="xs" c="dimmed">
            {money(row.costEach)} each
          </Text>
        )}
      </Stack>
    ),
  },
  {
    key: 'value',
    header: 'What it is worth',
    align: 'right',
    sortValue: (row) => row.valueNow,
    render: (row) => (
      <Text size="sm">{row.valueNow === null ? 'not recorded' : money(row.valueNow)}</Text>
    ),
  },
  {
    key: 'gain',
    header: 'Profit or loss',
    align: 'right',
    sortValue: (row) => row.gain,
    render: (row) => (
      <Text size="sm" c={row.gain === null ? undefined : row.gain >= 0 ? 'teal' : 'red'}>
        {gainPhrase(row.gain, row.gainFraction)}
      </Text>
    ),
  },
];

const transactionColumns: Array<DataTableColumn<TransactionRow>> = [
  {
    key: 'when',
    header: 'When',
    sortValue: (row) => row.when,
    render: (row) => <Text size="xs">{formatUtcStamp(row.when)}</Text>,
  },
  {
    key: 'asset',
    header: 'Coin',
    sortValue: (row) => row.asset,
    render: (row) => (
      <Group gap={6} wrap="nowrap">
        <Text size="xs">{row.asset}</Text>
        <MoneyBadge mode={row.mode} />
      </Group>
    ),
  },
  {
    key: 'side',
    header: 'Bought or sold',
    sortValue: (row) => row.side,
    render: (row) => (
      <Badge size="xs" color={row.side === 'buy' ? 'teal' : 'orange'} variant="light">
        {row.side === 'buy' ? 'Bought' : 'Sold'}
      </Badge>
    ),
  },
  {
    key: 'amount',
    header: 'How much',
    align: 'right',
    sortValue: (row) => row.amount,
    render: (row) => (
      <Text size="xs" ff="monospace">
        {amountText(row.amount)}
      </Text>
    ),
  },
  {
    key: 'price',
    header: 'Price each',
    align: 'right',
    sortValue: (row) => row.price,
    render: (row) => <Text size="xs">{money(row.price)}</Text>,
  },
  {
    key: 'value',
    header: 'Value',
    align: 'right',
    sortValue: (row) => row.value,
    render: (row) => <Text size="xs">{money(row.value)}</Text>,
  },
  {
    key: 'realised',
    header: 'Profit or loss on the sale',
    align: 'right',
    sortValue: (row) => row.realised,
    render: (row) =>
      row.side === 'buy' ? (
        <Text size="xs" c="dimmed">
          —
        </Text>
      ) : (
        <Text size="xs" c={row.realised === null ? undefined : row.realised >= 0 ? 'teal' : 'red'}>
          {row.realised === null ? 'not worked out' : gainPhrase(row.realised, row.realisedFraction)}
        </Text>
      ),
  },
];

/* ------------------------------------------------------------- what the pane is open on */

/** The open row, turned into everything the reasoning pane needs. */
export function reasoningTarget(
  kind: string | null,
  id: string | null,
  holdings: HoldingRow[],
  transactions: TransactionRow[],
): ReasoningTarget | null {
  if (!kind || !id) return null;

  if (kind === 'holding') {
    const row = holdings.find((item) => item.key === id);
    if (!row) return null;
    return {
      kind: 'holding',
      sleeve: row.sleeve,
      pair: row.pair,
      asset: row.asset,
      runId: lastRunFor(transactions, row),
      mode: row.mode,
      facts: [
        { label: 'Held by', value: sleeveFullName(row.sleeve) },
        { label: 'How much', value: `${amountText(row.amount)} ${row.asset}` },
        {
          label: 'What it cost',
          value: row.costTotal === null ? 'not recorded' : money(row.costTotal),
          ...(row.costEach === null ? {} : { meaning: `${money(row.costEach)} each, on average` }),
        },
        { label: 'What it is worth', value: row.valueNow === null ? 'not recorded' : money(row.valueNow) },
        { label: 'Profit or loss', value: gainPhrase(row.gain, row.gainFraction) },
      ],
    };
  }

  if (kind === 'transaction') {
    const row = transactions.find((item) => item.key === id);
    if (!row) return null;
    return {
      kind: 'transaction',
      sleeve: row.sleeve,
      pair: row.pair,
      asset: row.asset,
      runId: row.runId,
      mode: row.mode,
      facts: [
        { label: 'Traded by', value: sleeveFullName(row.sleeve) },
        { label: 'When', value: formatUtcStamp(row.when), meaning: 'Times are UTC everywhere.' },
        {
          label: row.side === 'buy' ? 'Bought' : 'Sold',
          value: `${amountText(row.amount)} ${row.asset} at ${money(row.price)}`,
          meaning: `${money(row.value)} in total`,
        },
        ...(row.side === 'sell'
          ? [
              {
                label: 'Profit or loss on the sale',
                value:
                  row.realised === null
                    ? 'not worked out — the buys behind it are older than the trades on record'
                    : gainPhrase(row.realised, row.realisedFraction),
              },
            ]
          : []),
      ],
    };
  }

  return null;
}
