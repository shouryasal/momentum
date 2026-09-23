/**
 * Decisions — research runs, proposals, approvals and traces (spec 12 page 8).
 *
 * Three tabs over one dataset: the runs table (stages, requested vs SERVED model,
 * provider, switches, escalation reasons, effort, auth source, cost, tokens, and the
 * signal that fired it), the proposals timeline as stacked target weights, and the
 * approvals queue with live countdowns. The detail drawer carries the trace viewer.
 *
 * "Requested vs served" is the column that matters: a run that silently fell back to a
 * weaker model is a different decision, and this is where that shows.
 */
import {
  ActionIcon,
  Badge,
  Button,
  Group,
  Loader,
  Progress,
  Select,
  Stack,
  Table,
  Tabs,
  Text,
  Title,
  Tooltip,
} from '@mantine/core';
import { IconPlayerPlay, IconRefresh } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useMemo, useState } from 'react';

import { useApi } from '@/app/ApiContext';
import { usePageCommands } from '@/app/commandRegistry';
import { useTopicEvents } from '@/app/EventStreamContext';
import { DataTable, EmptyState, type DataTableColumn } from '@/components';
import { formatNumber, formatRelative, formatUsd, formatUtcStamp } from '@/lib/format';

import { RunDrawer } from './components/RunDrawer';
import { TargetsTimeline } from './components/TargetsTimeline';
import { STATUS_COLOR, decisionKeys, decisionsApi, type PendingApproval, type RunSummary } from './api';

const WINDOWS = [
  { value: '7', label: 'last 7 days' },
  { value: '30', label: 'last 30 days' },
  { value: '90', label: 'last 90 days' },
];

export default function DecisionsPage() {
  const client = useApi();
  const queryClient = useQueryClient();
  const api = useMemo(() => decisionsApi(client), [client]);
  const [days, setDays] = useState('30');
  const [selected, setSelected] = useState<string | null>(null);
  const [tab, setTab] = useState<string | null>('runs');

  const windowDays = Number(days);
  const runs = useQuery({
    queryKey: decisionKeys.runs(windowDays),
    queryFn: () => api.runs(windowDays),
  });
  const proposals = useQuery({
    queryKey: decisionKeys.proposals(windowDays),
    queryFn: () => api.proposals(windowDays),
  });
  const pending = useQuery({ queryKey: decisionKeys.pending, queryFn: () => api.pending() });

  const invalidate = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: decisionKeys.all });
  }, [queryClient]);

  useTopicEvents(['run', 'proposal', 'approval'], invalidate);

  const runNow = useMutation({ mutationFn: () => api.runResearch(), onSuccess: invalidate });

  usePageCommands('decisions', [
    {
      id: 'run-research',
      title: 'Run research now',
      subtitle: 'Start an off-schedule research run',
      keywords: ['proposal', 'decide'],
      run: (ctx) => {
        runNow.mutate();
        ctx.close();
      },
    },
    {
      id: 'approvals',
      title: 'Show the approvals queue',
      subtitle: 'Proposals waiting on you, with their countdowns',
      run: (ctx) => {
        setTab('approvals');
        ctx.close();
      },
    },
    {
      id: 'window-90',
      title: 'Widen the window to 90 days',
      run: (ctx) => {
        setDays('90');
        ctx.close();
      },
    },
  ]);

  const columns: Array<DataTableColumn<RunSummary>> = useMemo(
    () => [
      {
        key: 'started',
        header: 'Started',
        width: 150,
        sortValue: (r) => r.started_utc,
        render: (r) => (
          <Tooltip label={r.started_utc} withArrow>
            <Text size="sm">{formatRelative(r.started_utc)}</Text>
          </Tooltip>
        ),
      },
      {
        key: 'run_id',
        header: 'Run',
        render: (r) => (
          <Text size="sm" ff="monospace">
            {r.run_id}
          </Text>
        ),
      },
      {
        key: 'models',
        header: 'Requested → served',
        render: (r) => {
          const decide = r.stages.find((s) => s.stage === 'decide');
          if (!decide) return <Text size="sm">—</Text>;
          const mismatch =
            decide.served_model && decide.requested_model && decide.served_model !== decide.requested_model;
          return (
            <Group gap={6} wrap="nowrap">
              <Text size="sm">{decide.requested_model ?? '—'}</Text>
              <Text size="sm" c="dimmed">
                →
              </Text>
              <Text size="sm" c={mismatch ? 'orange' : undefined} fw={mismatch ? 600 : 400}>
                {decide.served_model ?? '—'}
              </Text>
              {decide.provider ? (
                <Badge size="xs" variant="light">
                  {decide.provider}
                </Badge>
              ) : null}
            </Group>
          );
        },
      },
      {
        key: 'trigger',
        header: 'Trigger',
        width: 190,
        render: (r) => (
          <Group gap={4} wrap="nowrap">
            {r.signal_id ? (
              <Badge size="xs" color="violet" variant="light">
                signal
              </Badge>
            ) : null}
            <Text size="xs" c="dimmed" lineClamp={1}>
              {r.trigger_reason ?? 'scheduled'}
            </Text>
          </Group>
        ),
      },
      {
        key: 'escalated',
        header: 'Escalated',
        width: 100,
        render: (r) =>
          r.escalated ? (
            <Badge size="sm" color="violet" variant="light">
              yes
            </Badge>
          ) : (
            <Text size="sm" c="dimmed">
              no
            </Text>
          ),
      },
      {
        key: 'cost',
        header: 'Cost',
        align: 'right',
        width: 90,
        sortValue: (r) => r.cost_usd ?? 0,
        render: (r) => (
          <Text size="sm" ff="monospace">
            {formatUsd(r.cost_usd)}
          </Text>
        ),
      },
      {
        key: 'status',
        header: 'Status',
        width: 110,
        render: (r) => (
          <Badge size="sm" color={STATUS_COLOR[r.status] ?? 'gray'} variant="light">
            {r.status}
          </Badge>
        ),
      },
    ],
    [],
  );

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-end">
        <div>
          <Title order={2}>Decisions</Title>
          <Text c="dimmed" size="sm">
            What was asked, which model actually answered, and what it proposed. The gate decides
            whether a proposal may be executed — this page only records how it was reached.
          </Text>
        </div>
        <Group gap="sm">
          <Select data={WINDOWS} value={days} onChange={(v) => setDays(v ?? '30')} w={160} />
          <Button
            leftSection={runNow.isPending ? <Loader size={14} /> : <IconPlayerPlay size={16} />}
            onClick={() => runNow.mutate()}
            disabled={runNow.isPending}
          >
            Run research now
          </Button>
          <Tooltip label="Refresh" withArrow>
            <ActionIcon variant="default" size="lg" aria-label="refresh" onClick={invalidate}>
              <IconRefresh size={18} />
            </ActionIcon>
          </Tooltip>
        </Group>
      </Group>

      <Tabs value={tab} onChange={setTab} keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="runs">Research runs</Tabs.Tab>
          <Tabs.Tab value="proposals">Proposals</Tabs.Tab>
          <Tabs.Tab value="approvals">
            Approvals
            {pending.data?.pending.length ? (
              <Badge ml={6} size="xs" color="orange" circle>
                {pending.data.pending.length}
              </Badge>
            ) : null}
          </Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="runs" pt="sm">
          <DataTable
            columns={columns}
            rows={runs.data?.runs ?? []}
            rowKey={(r) => r.run_id}
            loading={runs.isLoading}
            onRowClick={(r) => setSelected(r.run_id)}
            emptyTitle="No research runs in this window"
            emptyDescription="Runs fire at the configured slots and whenever a validated signal passes the guards."
            maxHeight={640}
          />
        </Tabs.Panel>

        <Tabs.Panel value="proposals" pt="sm">
          <TargetsTimeline
            proposals={proposals.data?.proposals ?? []}
            loading={proposals.isLoading}
            onSelect={setSelected}
          />
        </Tabs.Panel>

        <Tabs.Panel value="approvals" pt="sm">
          <ApprovalsQueue rows={pending.data?.pending ?? []} loading={pending.isLoading} onSelect={setSelected} />
        </Tabs.Panel>
      </Tabs>

      <RunDrawer runId={selected} onClose={() => setSelected(null)} />
    </Stack>
  );
}

function ApprovalsQueue({
  rows,
  loading,
  onSelect,
}: {
  rows: PendingApproval[];
  loading: boolean;
  onSelect: (runId: string) => void;
}) {
  if (loading) return <Loader />;
  if (rows.length === 0) {
    return (
      <EmptyState
        title="Nothing awaiting approval"
        description="In LIVE·PROPOSE a proposal lands in proposals/pending and waits here until a human approves it."
      />
    );
  }
  return (
    <Table striped highlightOnHover withTableBorder>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Run</Table.Th>
          <Table.Th>Created</Table.Th>
          <Table.Th>Module</Table.Th>
          <Table.Th>Confidence</Table.Th>
          <Table.Th>Expires in</Table.Th>
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {rows.map((row) => {
          const left = row.seconds_left ?? 0;
          const hours = Math.floor(left / 3600);
          const minutes = Math.floor((left % 3600) / 60);
          return (
            <Table.Tr
              key={row.run_id}
              style={{ cursor: 'pointer' }}
              onClick={() => onSelect(row.run_id)}
            >
              <Table.Td>
                <Text size="sm" ff="monospace">
                  {row.run_id}
                </Text>
              </Table.Td>
              <Table.Td>
                <Text size="sm">{formatUtcStamp(row.ts_utc)}</Text>
              </Table.Td>
              <Table.Td>{row.module ?? '—'}</Table.Td>
              <Table.Td>{formatNumber(row.confidence, 2)}</Table.Td>
              <Table.Td>
                <Stack gap={2}>
                  <Text size="sm" c={left < 1800 ? 'red' : undefined}>
                    {left <= 0 ? 'expired' : `${hours}h ${minutes}m`}
                  </Text>
                  <Progress value={Math.min(100, (left / (6 * 3600)) * 100)} size="xs" />
                </Stack>
              </Table.Td>
            </Table.Tr>
          );
        })}
      </Table.Tbody>
    </Table>
  );
}
