import {
  Alert,
  Badge,
  Button,
  Card,
  Drawer,
  Grid,
  Group,
  JsonInput,
  NumberInput,
  Select,
  Stack,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { IconPlayerPlay, IconX } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { api, errorMessage } from '@/api';
import { usePageCommands } from '@/app/commandRegistry';
import { useTopicEvents } from '@/app/EventStreamContext';
import { DataTable, JsonViewer, StatCard } from '@/components';

export interface BacktestRow {
  id: string;
  started_utc: string;
  finished_utc: string | null;
  actor: string;
  kind: string;
  strategy: string;
  timerange: string;
  fee_bps: number | null;
  slippage_bps: number | null;
  status: string;
  error: string | null;
  metrics?: Record<string, unknown> | null;
  config_patch?: Record<string, unknown> | null;
}

interface QueuedResponse {
  id: string;
  status: string;
  kind: string;
  timerange: string;
}

const backtestApi = {
  list: () => api.get<BacktestRow[]>('/backtests'),
  detail: (id: string) => api.get<BacktestRow>(`/backtests/${id}`),
  submit: (body: Record<string, unknown>) => api.post<QueuedResponse>('/backtests', body),
  cancel: (id: string) => api.post<{ id: string; status: string }>(`/backtests/${id}/cancel`),
};

const STATUS_COLOUR: Record<string, string> = {
  queued: 'gray',
  running: 'blue',
  ok: 'teal',
  failed: 'red',
  cancelled: 'orange',
};

/**
 * Backtest Lab (spec 12 page 5).
 *
 * This is the *operator's* runner, deliberately separate from the evidence runner that
 * gates automated changes: a human exploring ideas must never be able to starve or
 * pollute the evidence behind a merge. Costs default to the TCA-measured numbers rather
 * than freqtrade's, so a promising curve here is a promising curve after costs.
 */
export default function BacktestLabPage() {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);
  const [progress, setProgress] = useState<Record<string, string>>({});

  const list = useQuery({
    queryKey: ['backtests'],
    queryFn: backtestApi.list,
    refetchInterval: 20_000,
  });

  useTopicEvents(['backtest'], (event) => {
    const payload = event.payload as Record<string, unknown>;
    const id = String(payload.id ?? '');
    if (!id) return;
    const stage = String(payload.event ?? '');
    setProgress((current) => ({
      ...current,
      [id]:
        stage === 'window' ? `window ${payload.index}/${payload.of}` : stage || String(payload.status ?? ''),
    }));
    void queryClient.invalidateQueries({ queryKey: ['backtests'] });
  });

  usePageCommands('backtest-lab', [
    {
      id: 'refresh',
      title: 'Refresh the backtest queue',
      run: (ctx) => {
        void list.refetch();
        ctx.close();
      },
    },
    {
      id: 'close-result',
      title: 'Close the backtest result drawer',
      run: (ctx) => {
        setSelected(null);
        ctx.close();
      },
    },
  ]);

  return (
    <Stack gap="lg">
      <div>
        <Title order={3}>Backtest Lab</Title>
        <Text size="sm" c="dimmed">
          Backtests and walk-forwards against history. They never touch the mode file, the
          live config or the databases the bots write.
        </Text>
      </div>

      <Grid>
        <Grid.Col span={{ base: 12, lg: 5 }}>
          <NewBacktestForm onQueued={() => void list.refetch()} />
        </Grid.Col>
        <Grid.Col span={{ base: 12, lg: 7 }}>
          <Card withBorder radius="md" padding="lg">
            <Title order={5} mb="sm">
              Queue and history
            </Title>
            <DataTable
              rows={list.data ?? []}
              loading={list.isLoading}
              rowKey={(row) => row.id}
              onRowClick={(row) => setSelected(row.id)}
              emptyTitle="No backtests yet"
              emptyDescription="Queue one on the left; progress arrives over SSE, one event per window."
              columns={[
                {
                  key: 'kind',
                  header: 'Kind',
                  render: (row) => (
                    <Badge size="xs" variant="light">
                      {row.kind === 'walk_forward' ? 'walk-forward' : 'backtest'}
                    </Badge>
                  ),
                },
                { key: 'strategy', header: 'Strategy', render: (row) => row.strategy },
                { key: 'timerange', header: 'Timerange', render: (row) => (
                  <Text size="xs" ff="monospace">{row.timerange}</Text>
                ) },
                {
                  key: 'status',
                  header: 'Status',
                  render: (row) => (
                    <Group gap={6}>
                      <Badge size="xs" color={STATUS_COLOUR[row.status] ?? 'gray'} variant="light">
                        {row.status}
                      </Badge>
                      {progress[row.id] && row.status === 'running' ? (
                        <Text size="xs" c="dimmed">
                          {progress[row.id]}
                        </Text>
                      ) : null}
                    </Group>
                  ),
                },
                {
                  key: 'started',
                  header: 'Started',
                  render: (row) => <Text size="xs">{row.started_utc}</Text>,
                  sortValue: (row) => row.started_utc,
                },
                {
                  key: 'cancel',
                  header: '',
                  render: (row) =>
                    row.status === 'queued' || row.status === 'running' ? (
                      <Button
                        size="compact-xs"
                        variant="subtle"
                        color="red"
                        leftSection={<IconX size={12} />}
                        onClick={(event) => {
                          event.stopPropagation();
                          void backtestApi.cancel(row.id).then(() => list.refetch());
                        }}
                      >
                        Cancel
                      </Button>
                    ) : null,
                },
              ]}
            />
          </Card>
        </Grid.Col>
      </Grid>

      <ResultDrawer id={selected} onClose={() => setSelected(null)} />
    </Stack>
  );
}

function NewBacktestForm({ onQueued }: { onQueued: () => void }) {
  const [kind, setKind] = useState('backtest');
  const [sleeve, setSleeve] = useState('a');
  const [timerange, setTimerange] = useState('20240101-20241231');
  const [oosMonths, setOosMonths] = useState<number>(6);
  const [patch, setPatch] = useState('{}');

  const submit = useMutation({
    mutationFn: () =>
      backtestApi.submit({
        kind,
        sleeve,
        timerange,
        oos_months: oosMonths,
        config_patch: safeJson(patch),
      }),
    onSuccess: onQueued,
  });

  const patchValid = isJson(patch);

  return (
    <Card withBorder radius="md" padding="lg">
      <Stack gap="sm">
        <Title order={5}>New run</Title>
        <Select
          label="Kind"
          data={[
            { value: 'backtest', label: 'Backtest (one window)' },
            { value: 'walk_forward', label: 'Walk-forward (expanding windows)' },
          ]}
          value={kind}
          onChange={(value) => setKind(value ?? 'backtest')}
        />
        <Select
          label="Sleeve"
          data={[
            { value: 'a', label: 'A — rules' },
            { value: 'b', label: 'B — proposals' },
          ]}
          value={sleeve}
          onChange={(value) => setSleeve(value ?? 'a')}
        />
        <TextInput
          label="Timerange"
          description="Freqtrade format, e.g. 20240101-20241231 (the end may be left open)"
          value={timerange}
          onChange={(event) => setTimerange(event.currentTarget.value)}
        />
        {kind === 'walk_forward' ? (
          <NumberInput
            label="Out-of-sample window (months)"
            value={oosMonths}
            onChange={(value) => setOosMonths(Number(value) || 6)}
            min={1}
            max={24}
          />
        ) : null}
        <JsonInput
          label="Config patch"
          description="Freqtrade config overlay for this run only; it never touches the live config."
          value={patch}
          onChange={setPatch}
          autosize
          minRows={3}
          error={patchValid ? undefined : 'not valid JSON'}
          formatOnBlur
        />
        {submit.isError ? (
          <Alert color="red" variant="light">
            {errorMessage(submit.error)}
          </Alert>
        ) : null}
        <Button
          leftSection={<IconPlayerPlay size={16} />}
          onClick={() => submit.mutate()}
          loading={submit.isPending}
          disabled={!patchValid || !timerange}
        >
          Queue run
        </Button>
        <Text size="xs" c="dimmed">
          Costs default to the measured fee and slippage in <code>config/backtest.yaml</code>,
          not to freqtrade&apos;s optimistic defaults.
        </Text>
      </Stack>
    </Card>
  );
}

function ResultDrawer({ id, onClose }: { id: string | null; onClose: () => void }) {
  const detail = useQuery({
    queryKey: ['backtests', id],
    queryFn: () => backtestApi.detail(id as string),
    enabled: Boolean(id),
  });
  const row = detail.data;
  const metrics = (row?.metrics ?? {}) as Record<string, unknown>;
  const windows = (metrics.windows as Array<Record<string, unknown>>) ?? [];

  return (
    <Drawer opened={Boolean(id)} onClose={onClose} position="right" size="xl" title={id ?? ''}>
      {row ? (
        <Stack gap="md">
          <Group>
            <Badge color={STATUS_COLOUR[row.status] ?? 'gray'}>{row.status}</Badge>
            <Text size="sm">{row.strategy}</Text>
            <Text size="sm" ff="monospace">
              {row.timerange}
            </Text>
            <Text size="xs" c="dimmed">
              {row.fee_bps} bps fee + {row.slippage_bps} bps slippage
            </Text>
          </Group>

          {row.error ? (
            <Alert color="red" variant="light" title="Failed">
              {row.error}
            </Alert>
          ) : null}

          <Grid>
            <Grid.Col span={6}>
              <StatCard label="Total profit" value={fmt(metrics.profit_total_pct, '%')} />
            </Grid.Col>
            <Grid.Col span={6}>
              <StatCard label="Max drawdown" value={fmt(metrics.max_drawdown_pct, '%')} />
            </Grid.Col>
            <Grid.Col span={6}>
              <StatCard label="Worst window" value={fmt(metrics.worst_window_pct, '%')} />
            </Grid.Col>
            <Grid.Col span={6}>
              <StatCard label="Trades" value={fmt(metrics.trades)} />
            </Grid.Col>
          </Grid>

          {windows.length > 0 ? (
            <DataTable
              rows={windows}
              rowKey={(w, index) => String(w.window ?? index)}
              columns={[
                { key: 'window', header: 'Window', render: (w) => String(w.window ?? '') },
                { key: 'profit', header: 'Profit %', align: 'right',
                  render: (w) => fmt(w.profit_total_pct, '%') },
                { key: 'dd', header: 'Max DD %', align: 'right',
                  render: (w) => fmt(w.max_drawdown_pct, '%') },
                { key: 'trades', header: 'Trades', align: 'right', render: (w) => fmt(w.trades) },
                { key: 'error', header: '', render: (w) => (
                  w.parse_error ? <Text size="xs" c="orange">{String(w.parse_error)}</Text> : null
                ) },
              ]}
            />
          ) : null}

          {row.config_patch && Object.keys(row.config_patch).length > 0 ? (
            <div>
              <Text size="sm" fw={600} mb={4}>
                Config patch
              </Text>
              <JsonViewer value={row.config_patch} />
            </div>
          ) : null}
        </Stack>
      ) : null}
    </Drawer>
  );
}

function fmt(value: unknown, unit = ''): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') return `${value.toFixed(2)}${unit}`;
  return String(value);
}

function isJson(text: string): boolean {
  try {
    JSON.parse(text || '{}');
    return true;
  } catch {
    return false;
  }
}

function safeJson(text: string): Record<string, unknown> {
  try {
    return JSON.parse(text || '{}') as Record<string, unknown>;
  } catch {
    return {};
  }
}
