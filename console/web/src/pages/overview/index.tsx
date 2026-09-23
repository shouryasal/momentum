import { LineChart } from '@mantine/charts';
import {
  Alert,
  Badge,
  Card,
  Grid,
  Group,
  Loader,
  Progress,
  SimpleGrid,
  Stack,
  Text,
  Title,
  Tooltip,
} from '@mantine/core';
import { IconAlertTriangle, IconFlag, IconSkull } from '@tabler/icons-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useMemo } from 'react';
import { Link } from 'react-router-dom';

import { errorMessage } from '@/api';
import { usePageCommands } from '@/app/commandRegistry';
import { useTopicEvents } from '@/app/EventStreamContext';
import { DataTable, EmptyState, StatCard } from '@/components';

import { PendingEffectsBanner } from '../settings/components/PendingEffectsBanner';
import { overviewApi, overviewKeys, type NavCard } from './api';

const REFETCH_TOPICS = ['nav', 'gate', 'signal', 'config', 'mode', 'kill', 'run'] as const;

/**
 * Overview (spec 12 page 1): "is Earn fine right now?" in one screen.
 *
 * Every number is a read of what actually happened — NAV points, gate decisions, signals,
 * config saves — never a cached summary, so a panel that is empty means nothing happened,
 * not that something failed.
 */
export default function OverviewPage() {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: overviewKeys.bundle,
    queryFn: () => overviewApi.get(),
    refetchInterval: 60_000,
  });

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: overviewKeys.bundle });
  }, [queryClient]);

  useTopicEvents(REFETCH_TOPICS, refresh);

  usePageCommands('overview', [
    {
      id: 'refresh',
      title: 'Refresh the overview',
      subtitle: 'Re-read NAV, gate activity and what changed today',
      run: (ctx) => {
        refresh();
        ctx.close();
      },
    },
  ]);

  if (query.isLoading) {
    return (
      <Group justify="center" p="xl">
        <Loader />
      </Group>
    );
  }

  if (query.isError || !query.data) {
    return <EmptyState title="Overview unavailable" description={errorMessage(query.error)} />;
  }

  const data = query.data;

  if (!data.ok) {
    return (
      <Stack gap="md">
        <Alert color="red" icon={<IconAlertTriangle size={18} />} title="Configuration is invalid">
          {data.config_error}
        </Alert>
        <Text size="sm">
          Every job loads <code>config/earn.yaml</code>; nothing runs until it parses. Fix it on{' '}
          <Link to="/settings">Settings</Link>.
        </Text>
      </Stack>
    );
  }

  const cards = data.nav?.cards ?? [];
  const benchmark = cards.find((card) => card.sleeve === 'benchmark');

  return (
    <Stack gap="md" data-testid="overview-page">
      <Group justify="space-between" align="flex-end">
        <Title order={3}>Overview</Title>
        <Group gap="xs">
          {data.mode.kill.engaged ? (
            <Badge color="red" leftSection={<IconSkull size={12} />}>
              KILL engaged
            </Badge>
          ) : null}
          {!data.mode.verified ? (
            <Tooltip label={`mode state ${data.mode.reason}: every sleeve reads TEST`}>
              <Badge color="orange">mode unverified</Badge>
            </Tooltip>
          ) : null}
          {data.bless && !data.bless.ok ? (
            <Badge color="orange">config not blessed ({data.bless.reason})</Badge>
          ) : null}
          <Text size="xs" c="dimmed">
            {data.generated_utc}
          </Text>
        </Group>
      </Group>

      <PendingEffectsBanner {...(data.banner ? { initial: data.banner } : {})} />

      <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }}>
        {cards.map((card) => (
          <NavStat key={card.sleeve} card={card} mode={data.mode.sleeves[card.sleeve]} />
        ))}
        <StatCard
          label="Pending approvals"
          value={data.approvals?.count ?? 0}
          hint="Proposals and held changes waiting on a human"
        />
      </SimpleGrid>

      <Grid>
        <Grid.Col span={{ base: 12, lg: 8 }}>
          <Card withBorder padding="md">
            <Stack gap="xs">
              <Text fw={600}>NAV vs BTC (7 days)</Text>
              <NavChart series={data.nav_series?.series ?? {}} />
            </Stack>
          </Card>
        </Grid.Col>
        <Grid.Col span={{ base: 12, lg: 4 }}>
          <Card withBorder padding="md" h="100%">
            <Stack gap="sm">
              <Text fw={600}>Exposure vs caps</Text>
              {(data.exposure?.sleeves ?? []).length === 0 ? (
                <Text size="sm" c="dimmed">
                  No positions yet.
                </Text>
              ) : null}
              {(data.exposure?.sleeves ?? []).map((sleeve) => (
                <Stack key={sleeve.sleeve} gap={4}>
                  <Group justify="space-between">
                    <Text size="sm" fw={600}>
                      Sleeve {sleeve.sleeve.toUpperCase()}
                    </Text>
                    <Text size="xs" c="dimmed">
                      gross {pct(sleeve.gross)} / {pct(sleeve.gross_cap)}
                    </Text>
                  </Group>
                  <Progress
                    value={Math.min(100, (sleeve.gross_util ?? 0) * 100)}
                    color={(sleeve.gross_util ?? 0) > 0.9 ? 'red' : 'blue'}
                  />
                  {sleeve.assets.map((asset) => (
                    <Group key={asset.asset} justify="space-between" gap="xs">
                      <Text size="xs">{asset.asset}</Text>
                      <Progress
                        value={Math.min(100, (asset.util ?? 0) * 100)}
                        style={{ flex: 1 }}
                        color={(asset.util ?? 0) > 0.9 ? 'orange' : 'teal'}
                      />
                      <Text size="xs" c="dimmed">
                        {pct(asset.weight)} / {pct(asset.cap)}
                      </Text>
                    </Group>
                  ))}
                </Stack>
              ))}
              {data.limits ? (
                <Text size="xs" c="dimmed">
                  Daily stop {pct(data.limits.daily_loss_stop)} · monthly stop{' '}
                  {pct(data.limits.monthly_loss_stop)} · USDT floor {pct(data.limits.usdt_floor)}
                </Text>
              ) : null}
            </Stack>
          </Card>
        </Grid.Col>
      </Grid>

      <Grid>
        <Grid.Col span={{ base: 12, md: 6 }}>
          <Card withBorder padding="md" h="100%">
            <Stack gap="xs">
              <Group justify="space-between">
                <Text fw={600}>Gate today</Text>
                <Group gap={6}>
                  <Badge variant="light">{data.gate?.allow ?? 0} allowed</Badge>
                  <Badge color="orange">{data.gate?.reject ?? 0} rejected</Badge>
                  <Badge color="red">{data.gate?.breach ?? 0} breaches</Badge>
                </Group>
              </Group>
              <DataTable
                dense
                rows={data.gate?.recent ?? []}
                rowKey={(row) => String(row.id)}
                emptyTitle="Nothing rejected today"
                emptyDescription="Every order the gate saw was allowed."
                columns={[
                  { key: 'ts', header: 'When', render: (row) => row.ts_utc.slice(11, 19) },
                  { key: 'sleeve', header: 'Sleeve', render: (row) => row.sleeve },
                  { key: 'pair', header: 'Pair', render: (row) => row.pair },
                  { key: 'reason', header: 'Reason', render: (row) => row.reason },
                  {
                    key: 'sev',
                    header: '',
                    render: (row) => (
                      <Badge size="xs" color={row.severity === 'breach' ? 'red' : 'orange'}>
                        {row.severity}
                      </Badge>
                    ),
                  },
                ]}
              />
            </Stack>
          </Card>
        </Grid.Col>

        <Grid.Col span={{ base: 12, md: 6 }}>
          <Card withBorder padding="md" h="100%">
            <Stack gap="xs">
              <Text fw={600}>Signal funnel (24 h)</Text>
              {(data.funnel?.stages ?? []).map((stage) => {
                const top = data.funnel?.stages[0]?.count ?? 0;
                return (
                  <Group key={stage.stage} gap="xs">
                    <Text size="xs" w={80}>
                      {stage.stage}
                    </Text>
                    <Progress
                      value={top ? (stage.count / top) * 100 : 0}
                      style={{ flex: 1 }}
                      color="indigo"
                    />
                    <Text size="xs" w={28} ta="right">
                      {stage.count}
                    </Text>
                  </Group>
                );
              })}
              <Text fw={600} mt="sm">
                Last research run
              </Text>
              {data.research?.last ? (
                <Stack gap={2}>
                  <Text size="sm">{data.research.last.run_id}</Text>
                  <Group gap={6}>
                    <Badge size="xs" variant="light">
                      {data.research.last.served_model ?? 'unknown model'}
                    </Badge>
                    {data.research.last.provider ? (
                      <Badge size="xs" variant="light">
                        {data.research.last.provider}
                      </Badge>
                    ) : null}
                    {data.research.last.escalated ? (
                      <Badge size="xs" color="grape">
                        escalated
                      </Badge>
                    ) : null}
                    <Badge
                      size="xs"
                      color={data.research.last.status === 'success' ? 'teal' : 'red'}
                    >
                      {data.research.last.status}
                    </Badge>
                  </Group>
                  {data.research.last.escalation_reasons.length > 0 ? (
                    <Text size="xs" c="dimmed">
                      {data.research.last.escalation_reasons.join(', ')}
                    </Text>
                  ) : null}
                </Stack>
              ) : (
                <Text size="sm" c="dimmed">
                  No research run recorded yet.
                </Text>
              )}
            </Stack>
          </Card>
        </Grid.Col>
      </Grid>

      <Grid>
        <Grid.Col span={{ base: 12, md: 6 }}>
          <Card withBorder padding="md" h="100%">
            <Stack gap="xs">
              <Text fw={600}>Next scheduled jobs</Text>
              <DataTable
                dense
                rows={data.schedule?.jobs ?? []}
                rowKey={(row) => row.job}
                emptyTitle="No schedule"
                columns={[
                  { key: 'job', header: 'Job', render: (row) => row.job },
                  { key: 'cron', header: 'Cron', render: (row) => <code>{row.cron}</code> },
                  { key: 'next', header: 'Next', render: (row) => row.next_fire_local },
                ]}
              />
              <Text fw={600} mt="sm">
                Open incidents
              </Text>
              <DataTable
                dense
                rows={data.incidents ?? []}
                rowKey={(row) => String(row.id)}
                emptyTitle="No open incidents"
                columns={[
                  { key: 'kind', header: 'Kind', render: (row) => row.kind },
                  {
                    key: 'sev',
                    header: '',
                    render: (row) => (
                      <Badge size="xs" color={row.severity === 'critical' ? 'red' : 'yellow'}>
                        {row.severity}
                      </Badge>
                    ),
                  },
                  { key: 'detail', header: 'Detail', render: (row) => row.detail },
                ]}
              />
            </Stack>
          </Card>
        </Grid.Col>

        <Grid.Col span={{ base: 12, md: 6 }}>
          <Card withBorder padding="md" h="100%">
            <Stack gap="xs">
              <Group justify="space-between">
                <Text fw={600}>What changed today</Text>
                <Group gap={4}>
                  {(data.flags?.active ?? []).map((flag) => (
                    <Badge key={flag} size="xs" color="yellow" leftSection={<IconFlag size={10} />}>
                      {flag}
                    </Badge>
                  ))}
                </Group>
              </Group>
              <DataTable
                dense
                rows={data.changed_today?.config ?? []}
                rowKey={(row) => String(row.id)}
                emptyTitle="No config changes today"
                emptyDescription="Config only changes through the console, and every change is here."
                columns={[
                  { key: 'ts', header: 'When', render: (row) => row.ts_utc.slice(11, 19) },
                  { key: 'file', header: 'File', render: (row) => row.file },
                  {
                    key: 'paths',
                    header: 'Paths',
                    render: (row) => (
                      <Text size="xs" ff="monospace">
                        {row.changed_paths.join(', ')}
                      </Text>
                    ),
                  },
                  { key: 'actor', header: 'Actor', render: (row) => row.actor },
                ]}
              />
              <DataTable
                dense
                rows={data.changed_today?.changes ?? []}
                rowKey={(row) => row.change_id}
                emptyTitle="No merged changes today"
                columns={[
                  { key: 'id', header: 'Change', render: (row) => row.change_id },
                  { key: 'kind', header: 'Kind', render: (row) => row.kind },
                  { key: 'target', header: 'Target', render: (row) => row.target },
                  { key: 'status', header: 'Status', render: (row) => row.status },
                ]}
              />
              <Text fw={600} mt="sm">
                Provider usage (24 h)
              </Text>
              <DataTable
                dense
                rows={data.provider?.usage ?? []}
                rowKey={(row) => row.provider ?? 'unknown'}
                emptyTitle="No model calls in the last day"
                columns={[
                  { key: 'p', header: 'Provider', render: (row) => row.provider ?? 'unknown' },
                  { key: 'calls', header: 'Calls', align: 'right', render: (row) => row.calls },
                  {
                    key: 'ok',
                    header: 'OK',
                    align: 'right',
                    render: (row) => `${row.ok}/${row.calls}`,
                  },
                  {
                    key: 'cost',
                    header: 'Cost',
                    align: 'right',
                    render: (row) => `$${(row.cost ?? 0).toFixed(2)}`,
                  },
                ]}
              />
            </Stack>
          </Card>
        </Grid.Col>
      </Grid>

      {benchmark ? null : (
        <Text size="xs" c="dimmed">
          The benchmark curve appears once nav_tick has written its first hold-BTC point.
        </Text>
      )}
    </Stack>
  );
}

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `${(value * 100).toFixed(1)}%`;
}

function NavStat({ card, mode }: { card: NavCard; mode?: { state: string } }) {
  const label =
    card.sleeve === 'benchmark' ? 'Benchmark (hold BTC)' : `Sleeve ${card.sleeve.toUpperCase()}`;
  const delta =
    card.day_pct === null || card.day_pct === undefined
      ? null
      : { value: `${card.day_pct > 0 ? '+' : ''}${card.day_pct.toFixed(2)}% today`,
          positive: card.day_pct >= 0 };
  return (
    <StatCard
      label={label}
      value={card.nav_usdt === null ? '—' : `$${card.nav_usdt.toLocaleString()}`}
      delta={delta}
      hint={
        card.run_id
          ? `${mode?.state ?? card.mode ?? ''} · run ${card.run_id} · since start ${
              card.run_pct === null ? '—' : `${card.run_pct.toFixed(2)}%`
            }`
          : 'No NAV points yet'
      }
    />
  );
}

function NavChart({ series }: { series: Record<string, Array<{ ts: string; nav: number }>> }) {
  const data = useMemo(() => {
    const byTs = new Map<string, Record<string, number | string>>();
    for (const [sleeve, points] of Object.entries(series)) {
      for (const point of points) {
        const row = byTs.get(point.ts) ?? { ts: point.ts.slice(5, 16) };
        row[sleeve] = point.nav;
        byTs.set(point.ts, row);
      }
    }
    return [...byTs.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([, row]) => row);
  }, [series]);

  if (data.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        No NAV points in the last 7 days.
      </Text>
    );
  }

  return (
    <LineChart
      h={240}
      data={data}
      dataKey="ts"
      withDots={false}
      curveType="linear"
      series={[
        { name: 'a', color: 'blue.6' },
        { name: 'b', color: 'grape.6' },
        { name: 'benchmark', color: 'gray.5' },
      ]}
    />
  );
}
