/**
 * Signals — the tiered pipeline, end to end (spec 12 page 7).
 *
 * Funnel (detected → screened → validated → valid → planned → acted) with conversions, a
 * filterable list, a detail drawer that shows the feature table, the screener's output
 * with its provider and model, the validator's verdict WITH its counter-evidence and
 * invalidation, and the linked run/proposal. Actions: Scan now, Revalidate, Inject manual
 * signal, Mark noise.
 *
 * The page refetches on the `signal` and `validation` SSE topics, so a scan started
 * anywhere (cron, this button, the CLI) lands here without a reload.
 */
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Card,
  Group,
  Loader,
  Paper,
  Select,
  SimpleGrid,
  Stack,
  Text,
  TextInput,
  Title,
  Tooltip,
} from '@mantine/core';
import { IconAlertTriangle, IconPlus, IconRadar2, IconRefresh } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useMemo, useState } from 'react';

import { useApi } from '@/app/ApiContext';
import { usePageCommands } from '@/app/commandRegistry';
import { useTopicEvents } from '@/app/EventStreamContext';
import { DataTable, EmptyState, StatCard, type DataTableColumn } from '@/components';
import { formatNumber, formatRelative } from '@/lib/format';

import {
  DIRECTION_COLOR,
  STATUS_COLOR,
  signalKeys,
  signalsApi,
  type Direction,
  type SignalFilters,
  type SignalRow,
} from './api';
import { FunnelBar } from './components/FunnelBar';
import { ManualSignalModal } from './components/ManualSignalModal';
import { SignalDrawer } from './components/SignalDrawer';

const STATUS_OPTIONS = [
  'candidate',
  'screened',
  'screened_out',
  'validating',
  'valid',
  'invalid',
  'uncertain',
  'blocked',
  'planned',
  'acted',
  'expired',
  'error',
].map((value) => ({ value, label: value }));

const WINDOW_OPTIONS = [
  { value: '6', label: 'last 6 h' },
  { value: '24', label: 'last 24 h' },
  { value: '72', label: 'last 3 d' },
  { value: '168', label: 'last 7 d' },
];

export default function SignalsPage() {
  const client = useApi();
  const queryClient = useQueryClient();
  const api = useMemo(() => signalsApi(client), [client]);

  const [status, setStatus] = useState<string | null>(null);
  const [detector, setDetector] = useState<string | null>(null);
  const [pair, setPair] = useState('');
  const [windowHours, setWindowHours] = useState('24');
  const [selected, setSelected] = useState<string | null>(null);
  const [manualOpen, setManualOpen] = useState(false);

  const filters: SignalFilters = useMemo(
    () => ({
      status: status ?? undefined,
      detector: detector ?? undefined,
      pair: pair.trim() || undefined,
      since_hours: Number(windowHours),
      limit: 200,
    }),
    [status, detector, pair, windowHours],
  );

  const hours = Number(windowHours);
  const listQuery = useQuery({
    queryKey: signalKeys.list(filters),
    queryFn: () => api.list(filters),
  });
  const funnelQuery = useQuery({
    queryKey: signalKeys.funnel(hours, 30),
    queryFn: () => api.funnel(hours, 30),
  });

  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: signalKeys.all });
  }, [queryClient]);

  useTopicEvents(['signal', 'validation'], invalidate);

  const scanNow = useMutation({ mutationFn: () => api.scanNow(), onSuccess: invalidate });
  const markNoise = useMutation({
    mutationFn: (signalId: string) => api.label(signalId, 'noise'),
    onSuccess: invalidate,
  });

  usePageCommands('signals', [
    {
      id: 'scan-now',
      title: 'Scan now',
      subtitle: 'Run the detector sweep and screen whatever it finds',
      keywords: ['detector', 'sweep'],
      run: (ctx) => {
        scanNow.mutate();
        ctx.close();
      },
    },
    {
      id: 'manual',
      title: 'Inject a manual signal',
      subtitle: 'Hand the validator a pair and a thesis of your own',
      run: (ctx) => {
        setManualOpen(true);
        ctx.close();
      },
    },
    {
      id: 'clear-filters',
      title: 'Clear the signal filters',
      run: (ctx) => {
        setStatus(null);
        setDetector(null);
        setPair('');
        ctx.close();
      },
    },
  ]);

  const rows = listQuery.data?.signals ?? [];
  const detectors = useMemo(
    () => Array.from(new Set(rows.map((r) => r.detector))).sort().map((d) => ({ value: d, label: d })),
    [rows],
  );

  const columns: Array<DataTableColumn<SignalRow>> = useMemo(
    () => [
      {
        key: 'ts',
        header: 'Detected',
        width: 130,
        sortValue: (r) => r.ts_utc,
        render: (r) => (
          <Tooltip label={r.ts_utc} withArrow>
            <Text size="sm">{formatRelative(r.ts_utc)}</Text>
          </Tooltip>
        ),
      },
      {
        key: 'detector',
        header: 'Detector',
        sortValue: (r) => r.detector,
        render: (r) => (
          <Group gap={6} wrap="nowrap">
            <Text size="sm" fw={500}>
              {r.detector}
            </Text>
            {r.fast_path ? (
              <Tooltip label="Fast path: skipped the screener and the validator, never the guards" withArrow>
                <Badge size="xs" color="orange" variant="light">
                  fast
                </Badge>
              </Tooltip>
            ) : null}
          </Group>
        ),
      },
      {
        key: 'pair',
        header: 'Pair',
        width: 110,
        sortValue: (r) => r.pair ?? '',
        render: (r) => <Text size="sm">{r.pair ?? '—'}</Text>,
      },
      {
        key: 'direction',
        header: 'Direction',
        width: 100,
        render: (r) =>
          r.direction ? (
            <Badge size="sm" variant="light" color={DIRECTION_COLOR[r.direction as Direction]}>
              {r.direction}
            </Badge>
          ) : (
            <Text size="sm">—</Text>
          ),
      },
      {
        key: 'score',
        header: 'Score',
        align: 'right',
        width: 150,
        sortValue: (r) => r.strength,
        render: (r) => (
          <Tooltip
            withArrow
            label={`detector ${formatNumber(r.detector_score, 2)} · screener ${
              r.screen_score === null ? 'n/a' : formatNumber(r.screen_score, 2)
            }`}
          >
            <Text size="sm" ff="monospace">
              {formatNumber(r.strength, 2)}
            </Text>
          </Tooltip>
        ),
      },
      {
        key: 'status',
        header: 'Status',
        width: 160,
        sortValue: (r) => r.status,
        render: (r) => (
          <Group gap={6} wrap="nowrap">
            <Badge size="sm" color={STATUS_COLOR[r.status] ?? 'gray'} variant="light">
              {r.status}
            </Badge>
            {r.status_reason ? (
              <Tooltip label={r.status_reason} withArrow multiline w={320}>
                <Text size="xs" c="dimmed" lineClamp={1} style={{ maxWidth: 90 }}>
                  {r.status_reason}
                </Text>
              </Tooltip>
            ) : null}
          </Group>
        ),
      },
      {
        key: 'actions',
        header: '',
        width: 80,
        align: 'right',
        render: (r) => (
          <Group gap={4} justify="flex-end" wrap="nowrap">
            <Tooltip label="Mark noise" withArrow>
              <ActionIcon
                variant="subtle"
                color="gray"
                aria-label={`mark ${r.signal_id} noise`}
                onClick={(event) => {
                  event.stopPropagation();
                  markNoise.mutate(r.signal_id);
                }}
              >
                <IconAlertTriangle size={16} />
              </ActionIcon>
            </Tooltip>
          </Group>
        ),
      },
    ],
    [markNoise],
  );

  const counts = funnelQuery.data?.counts;
  const screen = funnelQuery.data?.screen;

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-end">
        <div>
          <Title order={2}>Signals</Title>
          <Text c="dimmed" size="sm">
            Deterministic detectors, a cheap screener with host-verified evidence, and a strong
            validator. Nothing here places an order — a valid signal only asks the planner to look.
          </Text>
        </div>
        <Group gap="sm">
          <Button
            leftSection={<IconPlus size={16} />}
            variant="default"
            onClick={() => setManualOpen(true)}
          >
            Inject signal
          </Button>
          <Button
            leftSection={scanNow.isPending ? <Loader size={14} /> : <IconRadar2 size={16} />}
            onClick={() => scanNow.mutate()}
            disabled={scanNow.isPending}
          >
            Scan now
          </Button>
          <Tooltip label="Refresh" withArrow>
            <ActionIcon variant="default" size="lg" aria-label="refresh" onClick={invalidate}>
              <IconRefresh size={18} />
            </ActionIcon>
          </Tooltip>
        </Group>
      </Group>

      {scanNow.isSuccess && !scanNow.data?.spawned ? (
        <Alert color="orange" title="Scan could not be spawned">
          The console could not start the scanner process. Check the Operations page for the job
          lock and the envwrap script.
        </Alert>
      ) : null}

      <SimpleGrid cols={{ base: 2, sm: 3, lg: 6 }} spacing="sm">
        <StatCard label="Detected" value={counts?.detected ?? '—'} loading={funnelQuery.isLoading} />
        <StatCard label="Screened" value={counts?.screened ?? '—'} loading={funnelQuery.isLoading} />
        <StatCard label="Validated" value={counts?.validated ?? '—'} loading={funnelQuery.isLoading} />
        <StatCard label="Valid" value={counts?.valid ?? '—'} loading={funnelQuery.isLoading} />
        <StatCard label="Planned" value={counts?.planned ?? '—'} loading={funnelQuery.isLoading} />
        <StatCard label="Acted" value={counts?.acted ?? '—'} loading={funnelQuery.isLoading} />
      </SimpleGrid>

      <Card withBorder radius="md" padding="md">
        <Stack gap="sm">
          <Group justify="space-between">
            <Title order={5}>Funnel</Title>
            {screen && screen.screen_unavailable > 0 ? (
              <Badge color="orange" variant="light">
                {screen.screen_unavailable} scored on the detector alone
              </Badge>
            ) : null}
          </Group>
          <FunnelBar counts={counts} loading={funnelQuery.isLoading} />
        </Stack>
      </Card>

      <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="sm">
        <RateTable
          title="Hit rate by detector"
          caption="Resolved outcomes only — an unresolved signal is not a miss."
          rows={funnelQuery.data?.by_detector ?? []}
          loading={funnelQuery.isLoading}
        />
        <RateTable
          title="Hit rate by validator model"
          caption="What the strong model's confidence was actually worth."
          rows={funnelQuery.data?.by_model ?? []}
          loading={funnelQuery.isLoading}
        />
      </SimpleGrid>

      <Paper withBorder radius="md" p="sm">
        <Group gap="sm" wrap="wrap">
          <Select
            label="Status"
            placeholder="any"
            data={STATUS_OPTIONS}
            value={status}
            onChange={setStatus}
            clearable
            w={170}
          />
          <Select
            label="Detector"
            placeholder="any"
            data={detectors}
            value={detector}
            onChange={setDetector}
            clearable
            w={180}
          />
          <TextInput
            label="Pair"
            placeholder="BTC/USDT"
            value={pair}
            onChange={(e) => setPair(e.currentTarget.value)}
            w={150}
          />
          <Select
            label="Window"
            data={WINDOW_OPTIONS}
            value={windowHours}
            onChange={(v) => setWindowHours(v ?? '24')}
            w={150}
          />
        </Group>
      </Paper>

      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.signal_id}
        loading={listQuery.isLoading}
        onRowClick={(r) => setSelected(r.signal_id)}
        emptyTitle="No signals in this window"
        emptyDescription="Detectors run every five minutes and after every ingest. Quiet is the normal state."
        maxHeight={620}
      />

      <SignalDrawer
        signalId={selected}
        onClose={() => setSelected(null)}
        onChanged={invalidate}
      />
      <ManualSignalModal
        opened={manualOpen}
        onClose={() => setManualOpen(false)}
        onCreated={(id) => {
          invalidate();
          setSelected(id);
        }}
      />
    </Stack>
  );
}

function RateTable({
  title,
  caption,
  rows,
  loading,
}: {
  title: string;
  caption: string;
  rows: Array<{ group: string; n: number; hits: number; hit_rate: number; avg_ret: number | null }>;
  loading: boolean;
}) {
  if (!loading && rows.length === 0) {
    return (
      <Card withBorder radius="md" padding="md">
        <Stack gap="xs">
          <Title order={5}>{title}</Title>
          <EmptyState
            compact
            title="No resolved outcomes yet"
            description="Outcomes resolve after the signal's horizon; the first numbers appear a day in."
          />
        </Stack>
      </Card>
    );
  }
  return (
    <Card withBorder radius="md" padding="md">
      <Stack gap="xs">
        <Title order={5}>{title}</Title>
        <DataTable
          dense
          loading={loading}
          rows={rows}
          rowKey={(r) => r.group}
          caption={caption}
          columns={[
            { key: 'group', header: 'Group', render: (r) => <Text size="sm">{r.group}</Text> },
            { key: 'n', header: 'n', align: 'right', render: (r) => <Text size="sm">{r.n}</Text> },
            {
              key: 'hit',
              header: 'Hit rate',
              align: 'right',
              sortValue: (r) => r.hit_rate,
              render: (r) => (
                <Text size="sm" ff="monospace">
                  {`${(r.hit_rate * 100).toFixed(0)}%`}
                </Text>
              ),
            },
            {
              key: 'ret',
              header: 'Avg return',
              align: 'right',
              render: (r) => (
                <Text size="sm" ff="monospace" c={(r.avg_ret ?? 0) >= 0 ? 'teal' : 'red'}>
                  {formatNumber(r.avg_ret, 2)}%
                </Text>
              ),
            },
          ]}
        />
      </Stack>
    </Card>
  );
}
