import { LineChart } from '@mantine/charts';
import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  Grid,
  Group,
  NumberInput,
  Stack,
  Table,
  Tabs,
  Text,
  Textarea,
  TextInput,
  Title,
} from '@mantine/core';
import { IconFlask, IconInfoCircle, IconRefresh } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';

import { errorMessage } from '@/api';
import { usePageCommands } from '@/app/commandRegistry';
import { useSession } from '@/app/SessionContext';
import { ConfirmDialog, DataTable, EmptyState, StatCard } from '@/components';

import {
  COMPARE_FIELDS,
  formatMetric,
  MAX_COMPARE,
  MIN_COMPARE,
  RESET_PHRASE,
  testLabApi,
  type RunSummary,
  type SleeveRunRow,
} from './api';

const SLEEVES = ['a', 'b'] as const;

/**
 * Test Lab (spec 12 page 4).
 *
 * TEST is a Freqtrade dry-run on **live** market data: the whole pipeline runs, only the
 * exchange submission is simulated. That is why every P&L number on this page carries the
 * measured TCA caveat — dry-run fills never queue, never partially fill and never slip, so
 * paper results are optimistic by a knowable amount and the page says by how much.
 */
export default function TestLabPage() {
  const [compare, setCompare] = useState<string[]>([]);
  const runs = useQuery({ queryKey: ['testruns'], queryFn: () => testLabApi.runs() });

  usePageCommands('test-lab', [
    {
      id: 'refresh',
      title: 'Refresh the test runs',
      run: (ctx) => {
        void runs.refetch();
        ctx.close();
      },
    },
    {
      id: 'clear-compare',
      title: 'Clear the run comparison',
      subtitle: 'Unselect every run in the compare overlay',
      run: (ctx) => {
        setCompare([]);
        ctx.close();
      },
    },
  ]);

  return (
    <Stack gap="lg">
      <div>
        <Title order={3}>Test Lab</Title>
        <Text size="sm" c="dimmed">
          Test runs on live market data with simulated fills. Resetting archives the run —
          nothing is ever deleted.
        </Text>
      </div>

      <Grid>
        {SLEEVES.map((sleeve) => (
          <Grid.Col key={sleeve} span={{ base: 12, lg: 6 }}>
            <ActiveRunCard sleeve={sleeve} />
          </Grid.Col>
        ))}
      </Grid>

      <Tabs defaultValue="history">
        <Tabs.List>
          <Tabs.Tab value="history">Run history</Tabs.Tab>
          <Tabs.Tab value="compare">Compare ({compare.length})</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="history" pt="md">
          <RunHistory
            rows={runs.data ?? []}
            loading={runs.isLoading}
            selected={compare}
            onToggle={(runId) =>
              setCompare((current) =>
                current.includes(runId)
                  ? current.filter((id) => id !== runId)
                  : current.length >= MAX_COMPARE
                    ? current
                    : [...current, runId],
              )
            }
          />
        </Tabs.Panel>
        <Tabs.Panel value="compare" pt="md">
          <ComparePanel runIds={compare} onClear={() => setCompare([])} />
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}

// --------------------------------------------------------------------------- active run

function ActiveRunCard({ sleeve }: { sleeve: string }) {
  const queryClient = useQueryClient();
  const summary = useQuery({
    queryKey: ['testruns', 'summary', sleeve],
    queryFn: () => testLabApi.summary(sleeve),
    refetchInterval: 30_000,
  });
  const [wizard, setWizard] = useState(false);

  const data = summary.data;
  if (!data?.run) {
    return (
      <Card withBorder radius="md" padding="lg">
        <Title order={4} mb="sm">
          Sleeve {sleeve.toUpperCase()}
        </Title>
        <EmptyState
          compact
          title="No active run"
          description="A run opens when the sleeve is reset or transitions mode."
          action={{ label: 'Open a run', onClick: () => setWizard(true) }}
        />
        <ResetWizard
          sleeve={sleeve}
          summary={data ?? null}
          opened={wizard}
          onClose={() => setWizard(false)}
          onDone={() => {
            void queryClient.invalidateQueries({ queryKey: ['testruns'] });
          }}
        />
      </Card>
    );
  }

  const metrics = data.metrics;
  return (
    <Card withBorder radius="md" padding="lg">
      <Stack gap="md">
        <Group justify="space-between" align="flex-start">
          <div>
            <Group gap="xs">
              <Title order={4}>Sleeve {sleeve.toUpperCase()}</Title>
              <Badge variant="light" color={data.run.mode === 'test' ? 'blue' : 'orange'}>
                {data.run.mode}
              </Badge>
              {data.run.label ? <Badge variant="outline">{data.run.label}</Badge> : null}
            </Group>
            <Text size="sm" c="dimmed">
              {data.run.run_id} · seed {data.run.seed_usdt.toLocaleString()} USDT
              {data.days !== null ? ` · day ${Math.floor(data.days)}` : ''}
            </Text>
          </div>
          <Button
            size="xs"
            variant="light"
            leftSection={<IconRefresh size={14} />}
            onClick={() => setWizard(true)}
          >
            Reset run
          </Button>
        </Group>

        {data.pending.reset_required ? (
          <Alert color="yellow" variant="light" title="Seed change pending">
            The configured seed is {data.pending.configured_seed_usdt.toLocaleString()} USDT but
            this run started with {data.pending.current_seed_usdt?.toLocaleString()}. Freqtrade
            binds its wallet to the run, so the new seed applies only after a reset.
          </Alert>
        ) : null}

        <Grid>
          <Grid.Col span={6}>
            <StatCard
              label="Return"
              value={formatMetric(metrics?.return_pct, '%')}
              delta={
                metrics?.excess_return_pct !== null && metrics?.excess_return_pct !== undefined
                  ? {
                      value: `${formatMetric(metrics.excess_return_pct, '%')} vs BTC`,
                      positive: metrics.excess_return_pct >= 0,
                    }
                  : null
              }
            />
          </Grid.Col>
          <Grid.Col span={6}>
            <StatCard label="Max drawdown" value={formatMetric(metrics?.max_drawdown_pct, '%')} />
          </Grid.Col>
          <Grid.Col span={6}>
            <StatCard label="Trades" value={metrics?.trades ?? 0} hint="Closed round trips" />
          </Grid.Col>
          <Grid.Col span={6}>
            <StatCard label="Gate rejects" value={metrics?.gate_rejects ?? 0} />
          </Grid.Col>
        </Grid>

        {data.caveat ? <CaveatBanner text={data.caveat.text} /> : null}

        <ResetWizard
          sleeve={sleeve}
          summary={data}
          opened={wizard}
          onClose={() => setWizard(false)}
          onDone={() => {
            void queryClient.invalidateQueries({ queryKey: ['testruns'] });
          }}
        />
      </Stack>
    </Card>
  );
}

export function CaveatBanner({ text }: { text: string }) {
  return (
    <Alert variant="light" color="gray" icon={<IconInfoCircle size={16} />}>
      <Text size="xs">{text}</Text>
    </Alert>
  );
}

// --------------------------------------------------------------------------- reset

function ResetWizard({
  sleeve,
  summary,
  opened,
  onClose,
  onDone,
}: {
  sleeve: string;
  summary: RunSummary | null;
  opened: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const { stepUpActive } = useSession();
  const [seed, setSeed] = useState<number | ''>(summary?.pending.configured_seed_usdt ?? '');
  const [label, setLabel] = useState('');
  const [notes, setNotes] = useState('');

  const reset = useMutation({
    mutationFn: () =>
      testLabApi.reset(sleeve, {
        seed_usdt: seed === '' ? null : Number(seed),
        label: label || null,
        notes: notes || null,
        confirm_phrase: RESET_PHRASE,
      }),
    onSuccess: () => {
      onDone();
      onClose();
    },
  });

  return (
    <ConfirmDialog
      opened={opened}
      onClose={onClose}
      title={`Reset sleeve ${sleeve.toUpperCase()}`}
      description={
        <Stack gap={4}>
          <Text size="sm">
            The current run is closed with its final metrics and a snapshot of its run-scoped
            risk state, and a new run opens with a fresh Freqtrade database and fresh anchors.
          </Text>
          <Text size="sm" c="dimmed">
            Nothing is deleted: {summary?.run?.run_id ?? 'the current run'} stays fully
            inspectable, and its database stays on disk.
          </Text>
        </Stack>
      }
      confirmPhrase={RESET_PHRASE}
      requireStepUp
      stepUpSatisfied={stepUpActive}
      confirmLabel="Reset run"
      danger
      onConfirm={async () => {
        await reset.mutateAsync();
      }}
    >
      <Stack gap="sm">
        <NumberInput
          label="Seed for the new run (USDT)"
          value={seed}
          onChange={(value) => setSeed(value === '' ? '' : Number(value))}
          min={1}
          thousandSeparator
        />
        <TextInput
          label="Label"
          placeholder="e.g. wider stops"
          value={label}
          onChange={(event) => setLabel(event.currentTarget.value)}
        />
        <Textarea
          label="Notes"
          placeholder="What is this run testing?"
          value={notes}
          onChange={(event) => setNotes(event.currentTarget.value)}
          autosize
          minRows={2}
        />
        {reset.isError ? (
          <Alert color="red" variant="light">
            {errorMessage(reset.error)}
          </Alert>
        ) : null}
      </Stack>
    </ConfirmDialog>
  );
}

// --------------------------------------------------------------------------- history

function RunHistory({
  rows,
  loading,
  selected,
  onToggle,
}: {
  rows: SleeveRunRow[];
  loading: boolean;
  selected: string[];
  onToggle: (runId: string) => void;
}) {
  return (
    <DataTable
      rows={rows}
      loading={loading}
      rowKey={(row) => row.run_id}
      emptyTitle="No runs yet"
      emptyDescription="A run is created by a reset or a mode transition."
      columns={[
        {
          key: 'select',
          header: '',
          width: 40,
          render: (row) => (
            <Checkbox
              checked={selected.includes(row.run_id)}
              onChange={() => onToggle(row.run_id)}
              aria-label={`compare ${row.run_id}`}
            />
          ),
        },
        { key: 'run', header: 'Run', render: (row) => <Text size="sm">{row.run_id}</Text>,
          sortValue: (row) => row.run_id },
        { key: 'sleeve', header: 'Sleeve', render: (row) => row.sleeve.toUpperCase() },
        {
          key: 'mode',
          header: 'Mode',
          render: (row) => (
            <Badge size="xs" variant="light" color={row.mode === 'test' ? 'blue' : 'orange'}>
              {row.mode}
              {row.submode ? `·${row.submode}` : ''}
            </Badge>
          ),
        },
        { key: 'seed', header: 'Seed', align: 'right',
          render: (row) => row.seed_usdt.toLocaleString(), sortValue: (row) => row.seed_usdt },
        { key: 'started', header: 'Started', render: (row) => <Text size="xs">{row.started_utc}</Text>,
          sortValue: (row) => row.started_utc },
        { key: 'ended', header: 'Ended', render: (row) => <Text size="xs">{row.ended_utc ?? '—'}</Text> },
        {
          key: 'status',
          header: 'Status',
          render: (row) => (
            <Badge size="xs" variant="light" color={row.status === 'active' ? 'teal' : 'gray'}>
              {row.status}
            </Badge>
          ),
        },
        { key: 'label', header: 'Label', render: (row) => row.label ?? '—' },
      ]}
    />
  );
}

// --------------------------------------------------------------------------- compare

function ComparePanel({ runIds, onClear }: { runIds: string[]; onClear: () => void }) {
  const enabled = runIds.length >= MIN_COMPARE && runIds.length <= MAX_COMPARE;
  const compare = useQuery({
    queryKey: ['testruns', 'compare', runIds.join(',')],
    queryFn: () => testLabApi.compare(runIds),
    enabled,
  });

  const chartData = useMemo(() => {
    const series = compare.data?.series ?? {};
    const points = new Map<number, Record<string, number>>();
    for (const [runId, list] of Object.entries(series)) {
      if (runId.includes(':benchmark')) continue;
      for (const point of list) {
        const day = Math.round(point.days * 4) / 4;
        const row = points.get(day) ?? { day };
        row[runId] = point.index;
        points.set(day, row);
      }
    }
    return [...points.values()].sort((a, b) => (a.day ?? 0) - (b.day ?? 0));
  }, [compare.data]);

  if (!enabled) {
    return (
      <EmptyState
        icon={<IconFlask size={24} />}
        title={`Select ${MIN_COMPARE}–${MAX_COMPARE} runs`}
        description="Tick runs in the history table to overlay their NAV, compare their metrics and see what changed in the config between them."
      />
    );
  }

  if (compare.isError) {
    return (
      <Alert color="red" variant="light" title="Comparison failed">
        {errorMessage(compare.error)}
      </Alert>
    );
  }

  const data = compare.data;
  return (
    <Stack gap="lg">
      <Group justify="space-between">
        <Text size="sm" c="dimmed">
          NAV normalised to 100 at each run&apos;s first point, so runs with different seeds are
          directly comparable.
        </Text>
        <Button size="xs" variant="subtle" onClick={onClear}>
          Clear selection
        </Button>
      </Group>

      <Card withBorder radius="md" padding="md">
        <LineChart
          h={300}
          data={chartData}
          dataKey="day"
          withDots={false}
          curveType="linear"
          series={runIds.map((runId, index) => ({
            name: runId,
            color: ['blue.6', 'teal.6', 'grape.6', 'orange.6', 'cyan.6'][index] ?? 'gray.6',
          }))}
          xAxisLabel="Days into the run"
          yAxisLabel="Index (100 = start)"
        />
      </Card>

      <Card withBorder radius="md" padding="md">
        <Title order={5} mb="sm">
          Metrics
        </Title>
        <Table striped withTableBorder>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Metric</Table.Th>
              {data?.metrics.map((metric) => (
                <Table.Th key={metric.run_id} style={{ textAlign: 'right' }}>
                  {metric.label ?? metric.run_id}
                </Table.Th>
              ))}
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {COMPARE_FIELDS.map((field) => (
              <Table.Tr key={String(field.key)}>
                <Table.Td>{field.label}</Table.Td>
                {data?.metrics.map((metric, index) => {
                  const delta = index === 0 ? null : data.deltas[metric.run_id]?.[String(field.key)];
                  const positive =
                    delta === null || delta === undefined
                      ? null
                      : field.better === 'low'
                        ? delta < 0
                        : delta > 0;
                  return (
                    <Table.Td key={metric.run_id} style={{ textAlign: 'right' }}>
                      <Text size="sm" span>
                        {formatMetric(metric[field.key], field.unit)}
                      </Text>
                      {delta !== null && delta !== undefined && field.better ? (
                        <Text
                          size="xs"
                          span
                          ml={6}
                          c={positive ? 'teal' : 'red'}
                        >
                          {delta > 0 ? '+' : ''}
                          {formatMetric(delta)}
                        </Text>
                      ) : null}
                    </Table.Td>
                  );
                })}
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      </Card>

      <Card withBorder radius="md" padding="md">
        <Title order={5} mb="sm">
          Config changes between these runs
        </Title>
        {data && data.config_diff.length > 0 ? (
          <DataTable
            rows={data.config_diff}
            rowKey={(row, index) => `${row.ts_utc}-${index}`}
            columns={[
              { key: 'ts', header: 'When', render: (row) => <Text size="xs">{row.ts_utc}</Text> },
              { key: 'file', header: 'File', render: (row) => row.file },
              {
                key: 'paths',
                header: 'Changed',
                render: (row) => (
                  <Text size="xs" ff="monospace">
                    {row.changed_paths.join(', ')}
                  </Text>
                ),
              },
              { key: 'reason', header: 'Reason', render: (row) => row.reason ?? '—' },
            ]}
          />
        ) : (
          <Text size="sm" c="dimmed">
            No config saves landed between these runs — the difference is the strategy&apos;s
            own behaviour, not a setting.
          </Text>
        )}
      </Card>
    </Stack>
  );
}
