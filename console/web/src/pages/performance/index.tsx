import { AreaChart, LineChart } from '@mantine/charts';
import {
  Alert,
  Badge,
  Card,
  Grid,
  Group,
  Loader,
  SegmentedControl,
  Skeleton,
  Stack,
  Table,
  Tabs,
  Text,
  Title,
} from '@mantine/core';
import { IconInfoCircle } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';

import { useApi } from '@/app/ApiContext';
import { usePageCommands } from '@/app/commandRegistry';
import { DataTable, EmptyState, ErrorAlert, StatCard } from '@/components';

import {
  COMPARE_FIELDS,
  formatMetric,
  type Caveat,
  type RunMetrics,
  type SeriesPoint,
} from '../test-lab/api';

interface NavResponse {
  sleeve: string;
  run_id: string | null;
  resolution: string;
  points: SeriesPoint[];
  benchmark: SeriesPoint[];
}

interface SummaryResponse {
  sleeves: Array<{
    sleeve: string;
    run_id: string | null;
    mode?: string;
    label?: string | null;
    metrics: RunMetrics | null;
  }>;
  caveats: Record<string, Caveat>;
}

interface WhatIfPoint {
  date_utc: string;
  nav_usdt: number;
  turnover: number | null;
  cost_usdt: number | null;
  last_proposal_run_id: string | null;
}

interface AttributionRow {
  tag: string;
  trades: number;
  pnl_usdt: number;
  fees_usdt: number;
}

/**
 * The page reads its client from the context rather than the module singleton, so a test
 * can drive it with a fake `fetch` exactly as the shell does.
 */
function perfApi(client: ReturnType<typeof useApi>) {
  return {
    nav: (sleeve: string, res: string) => client.get<NavResponse>('/perf/nav', { sleeve, res }),
    summary: () => client.get<SummaryResponse>('/perf/summary'),
    whatif: () => client.get<WhatIfPoint[]>('/perf/whatif'),
    attribution: (sleeve: string) =>
      client.get<AttributionRow[]>('/perf/attribution', { sleeve }),
  };
}

/**
 * Performance (spec 12 page 3).
 *
 * Earn is measured against holding BTC, so every series here is drawn beside the benchmark
 * and the headline number is the *excess* return. The what-if tab answers the separate
 * question the analyst half is accountable for: what would the proposals alone have
 * earned, followed exactly, with the measured costs?
 */
export default function PerformancePage() {
  const client = useApi();
  const perf = useMemo(() => perfApi(client), [client]);
  const [sleeve, setSleeve] = useState('b');
  const [res, setRes] = useState('hour');

  const summary = useQuery({ queryKey: ['perf', 'summary'], queryFn: perf.summary });
  const nav = useQuery({
    queryKey: ['perf', 'nav', sleeve, res],
    queryFn: () => perf.nav(sleeve, res),
  });

  usePageCommands('performance', [
    {
      id: 'sleeve-a',
      title: 'Performance: sleeve A',
      run: (ctx) => {
        setSleeve('a');
        ctx.close();
      },
    },
    {
      id: 'sleeve-b',
      title: 'Performance: sleeve B',
      run: (ctx) => {
        setSleeve('b');
        ctx.close();
      },
    },
    {
      id: 'daily',
      title: 'Performance: daily resolution',
      keywords: ['resolution', 'day'],
      run: (ctx) => {
        setRes('day');
        ctx.close();
      },
    },
  ]);

  const sleeveSummary = summary.data?.sleeves.find((row) => row.sleeve === sleeve);
  const caveat = summary.data?.caveats?.[sleeve];

  const chartData = useMemo(() => {
    const points = nav.data?.points ?? [];
    const bench = new Map((nav.data?.benchmark ?? []).map((p) => [p.ts_utc, p.index]));
    return points.map((point) => ({
      ts: point.ts_utc.slice(0, 16).replace('T', ' '),
      sleeve: point.index,
      benchmark: bench.get(point.ts_utc) ?? null,
      drawdown: 0,
    }));
  }, [nav.data]);

  const withDrawdown = useMemo(() => {
    let peak = -Infinity;
    return chartData.map((row) => {
      peak = Math.max(peak, row.sleeve);
      return { ...row, drawdown: peak > 0 ? (row.sleeve / peak - 1) * 100 : 0 };
    });
  }, [chartData]);

  return (
    <Stack gap="lg">
      <Group justify="space-between" align="flex-start">
        <div>
          <Title order={3}>Performance</Title>
          <Text size="sm" c="dimmed">
            NAV against buy-and-hold BTC — the only comparison that decides whether Earn earns
            its place.
          </Text>
        </div>
        <Group>
          <SegmentedControl
            value={sleeve}
            onChange={setSleeve}
            data={[
              { value: 'a', label: 'Sleeve A' },
              { value: 'b', label: 'Sleeve B' },
            ]}
          />
          <SegmentedControl
            value={res}
            onChange={setRes}
            data={[
              { value: 'raw', label: 'Raw' },
              { value: 'hour', label: 'Hourly' },
              { value: 'day', label: 'Daily' },
            ]}
          />
        </Group>
      </Group>

      <ErrorAlert error={summary.error} title="Could not load the headline metrics">
        {' '}
        Metrics are computed from `nav_points` and `fills`; a failure here means the journal
        could not be read, not that the numbers are zero.
      </ErrorAlert>
      <ErrorAlert error={nav.error} title="Could not load the NAV series" />

      {caveat ? (
        <Alert variant="light" color={caveat.simulated ? 'yellow' : 'gray'} icon={<IconInfoCircle size={16} />}>
          {caveat.text}
        </Alert>
      ) : null}

      <Grid data-testid="perf-stats">
        {summary.isLoading ? (
          [0, 1, 2, 3].map((i) => (
            <Grid.Col span={{ base: 6, md: 3 }} key={i}>
              <Skeleton height={92} radius="md" data-testid="stat-skeleton" />
            </Grid.Col>
          ))
        ) : (
          <Grid.Col span={12} p={0}>
            <Grid>
              <Grid.Col span={{ base: 6, md: 3 }}>
                <StatCard
                  label="Return"
                  value={formatMetric(sleeveSummary?.metrics?.return_pct, '%')}
                  delta={
                    sleeveSummary?.metrics?.excess_return_pct != null
                      ? {
                          value: `${formatMetric(
                            sleeveSummary.metrics.excess_return_pct,
                            '%',
                          )} vs BTC`,
                          positive: sleeveSummary.metrics.excess_return_pct >= 0,
                        }
                      : null
                  }
                />
              </Grid.Col>
              <Grid.Col span={{ base: 6, md: 3 }}>
                <StatCard
                  label="Max drawdown"
                  value={formatMetric(sleeveSummary?.metrics?.max_drawdown_pct, '%')}
                  hint="Smaller drawdowns are the whole thesis"
                />
              </Grid.Col>
              <Grid.Col span={{ base: 6, md: 3 }}>
                <StatCard label="Sharpe" value={formatMetric(sleeveSummary?.metrics?.sharpe)} />
              </Grid.Col>
              <Grid.Col span={{ base: 6, md: 3 }}>
                <StatCard
                  label="Fees"
                  value={formatMetric(sleeveSummary?.metrics?.fees_usdt, ' USDT')}
                  hint="Measured, not assumed"
                />
              </Grid.Col>
            </Grid>
          </Grid.Col>
        )}
      </Grid>

      <Card withBorder radius="md" padding="md">
        <Group justify="space-between" mb="sm">
          <Title order={5}>NAV vs BTC</Title>
          {nav.data?.run_id ? (
            <Badge variant="light">{nav.data.run_id}</Badge>
          ) : null}
        </Group>
        {nav.isLoading ? (
          <Group justify="center" h={280} data-testid="nav-loading">
            <Loader size="sm" />
          </Group>
        ) : nav.isError ? (
          <EmptyState
            compact
            title="The NAV series did not load"
            description="See the message above — an empty chart and a failed read look the same, so this page refuses to draw one."
          />
        ) : withDrawdown.length > 0 ? (
          <>
            <LineChart
              h={280}
              data={withDrawdown}
              dataKey="ts"
              withDots={false}
              curveType="linear"
              series={[
                { name: 'sleeve', label: `Sleeve ${sleeve.toUpperCase()}`, color: 'blue.6' },
                { name: 'benchmark', label: 'Hold BTC', color: 'gray.5' },
              ]}
              yAxisLabel="Index (100 = run start)"
            />
            <AreaChart
              h={110}
              mt="xs"
              data={withDrawdown}
              dataKey="ts"
              withDots={false}
              series={[{ name: 'drawdown', label: 'Drawdown %', color: 'red.5' }]}
              yAxisLabel="Drawdown %"
            />
          </>
        ) : (
          <EmptyState
            compact
            title="No NAV points yet"
            description="nav_tick writes a point every 15 minutes once a run is active."
          />
        )}
      </Card>

      <Tabs defaultValue="metrics">
        <Tabs.List>
          <Tabs.Tab value="metrics">Metrics</Tabs.Tab>
          <Tabs.Tab value="attribution">Attribution</Tabs.Tab>
          <Tabs.Tab value="whatif">Proposals alone</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="metrics" pt="md">
          <MetricsTable rows={summary.data?.sleeves ?? []} loading={summary.isLoading} />
        </Tabs.Panel>

        <Tabs.Panel value="attribution" pt="md">
          <AttributionPanel sleeve={sleeve} />
        </Tabs.Panel>

        <Tabs.Panel value="whatif" pt="md">
          <WhatIfPanel />
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}

function MetricsTable({
  rows,
  loading,
}: {
  rows: SummaryResponse['sleeves'];
  loading?: boolean;
}) {
  const withMetrics = rows.filter((row) => row.metrics);
  if (loading) {
    return <Skeleton height={220} radius="md" data-testid="metrics-skeleton" />;
  }
  if (withMetrics.length === 0) {
    return <EmptyState title="No active runs" description="Metrics appear once a run is open." />;
  }
  return (
    <Card withBorder radius="md" padding="md">
      <Table striped withTableBorder>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Metric</Table.Th>
            {withMetrics.map((row) => (
              <Table.Th key={row.sleeve} style={{ textAlign: 'right' }}>
                Sleeve {row.sleeve.toUpperCase()}
                {row.label ? ` · ${row.label}` : ''}
              </Table.Th>
            ))}
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {COMPARE_FIELDS.map((field) => (
            <Table.Tr key={String(field.key)}>
              <Table.Td>{field.label}</Table.Td>
              {withMetrics.map((row) => (
                <Table.Td key={row.sleeve} style={{ textAlign: 'right' }}>
                  {formatMetric(row.metrics?.[field.key], field.unit)}
                </Table.Td>
              ))}
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Card>
  );
}

function AttributionPanel({ sleeve }: { sleeve: string }) {
  const client = useApi();
  const perf = useMemo(() => perfApi(client), [client]);
  const rows = useQuery({
    queryKey: ['perf', 'attribution', sleeve],
    queryFn: () => perf.attribution(sleeve),
  });
  return (
    <Card withBorder radius="md" padding="md">
      <Text size="sm" c="dimmed" mb="sm">
        Round-trip P&amp;L grouped by what the gate decided at entry — trend, DCA, take-profit,
        stop. Untagged fills predate the tag.
      </Text>
      <ErrorAlert error={rows.error} title="Could not load the attribution" />
      <DataTable
        rows={rows.data ?? []}
        loading={rows.isLoading}
        rowKey={(row) => row.tag}
        emptyTitle="No closed trades yet"
        columns={[
          { key: 'tag', header: 'Tag', render: (row) => <Badge variant="light">{row.tag}</Badge> },
          { key: 'trades', header: 'Trades', align: 'right', render: (row) => row.trades,
            sortValue: (row) => row.trades },
          {
            key: 'pnl',
            header: 'P&L (USDT)',
            align: 'right',
            render: (row) => (
              <Text size="sm" c={row.pnl_usdt >= 0 ? 'teal' : 'red'}>
                {row.pnl_usdt.toFixed(2)}
              </Text>
            ),
            sortValue: (row) => row.pnl_usdt,
          },
          { key: 'fees', header: 'Fees (USDT)', align: 'right',
            render: (row) => row.fees_usdt.toFixed(2) },
        ]}
      />
    </Card>
  );
}

function WhatIfPanel() {
  const client = useApi();
  const perf = useMemo(() => perfApi(client), [client]);
  const rows = useQuery({ queryKey: ['perf', 'whatif'], queryFn: perf.whatif });
  const data = (rows.data ?? []).map((point) => ({
    date: point.date_utc,
    nav: point.nav_usdt,
  }));
  const total = rows.data?.reduce((sum, point) => sum + (point.cost_usdt ?? 0), 0) ?? 0;
  return (
    <Card withBorder radius="md" padding="md">
      <Group justify="space-between" mb="sm">
        <Title order={5}>What the proposals alone would have earned</Title>
        <Badge variant="light">{total.toFixed(2)} USDT of modelled costs</Badge>
      </Group>
      <Text size="sm" c="dimmed" mb="sm">
        Every valid proposal applied at the first closed 4h candle at or after its timestamp —
        no look-ahead — with the TCA-measured fee and slippage charged on every rebalance.
      </Text>
      <ErrorAlert error={rows.error} title="Could not load the what-if curve" />
      {rows.isLoading ? (
        <Skeleton height={260} radius="md" data-testid="whatif-skeleton" />
      ) : rows.isError ? null : data.length > 0 ? (
        <LineChart
          h={260}
          data={data}
          dataKey="date"
          withDots={false}
          curveType="linear"
          series={[{ name: 'nav', label: 'Proposals-only NAV', color: 'grape.6' }]}
        />
      ) : (
        <EmptyState
          compact
          title="No what-if track yet"
          description="The simulator rebuilds after every daily NAV job."
        />
      )}
    </Card>
  );
}
