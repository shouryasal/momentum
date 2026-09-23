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
  Textarea,
  Tooltip,
} from '@mantine/core';
import { IconPlayerPlay, IconRefresh } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useMemo, useState } from 'react';

import { shellKeys } from '@/api';
import { useApi } from '@/app/ApiContext';
import { usePageCommands } from '@/app/commandRegistry';
import { useDetailSelection } from '@/app/detailParam';
import { useTopicEvents } from '@/app/EventStreamContext';
import {
  ConfirmDialog,
  DataTable,
  DetailPane,
  EmptyState,
  MasterDetail,
  PageIntro,
  type DataTableColumn,
} from '@/components';
import { formatNumber, formatRelative, formatUsd, formatUtcStamp } from '@/lib/format';
import { routeBlurb } from '@/routes';

import { RunDetailBody } from './components/RunDrawer';
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
  // The open row lives in `?detail=run:<id>`, so a refresh, the back button and a pasted
  // link all land on the same decision.
  const selection = useDetailSelection('run');
  const selected = selection.id;
  const setSelected = selection.open;
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

  /**
   * Approve / reject.
   *
   * The queue used to be read-only, so `LIVE_PROPOSE` had no console path at all: the
   * badge in the header was "a call to action" with nothing behind it. Both keys are
   * invalidated on success — the page's own `pending` query and the shell's badge.
   */
  const decide = useMutation({
    mutationFn: (vars: { runId: string; decision: 'approve' | 'reject'; note: string }) =>
      vars.decision === 'approve'
        ? api.approve(vars.runId, vars.note)
        : api.reject(vars.runId, vars.note),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: decisionKeys.pending });
      void queryClient.invalidateQueries({ queryKey: shellKeys.approvals });
      invalidate();
    },
  });

  usePageCommands('decisions', [
    {
      id: 'run-research',
      title: 'Think about it now',
      subtitle: 'Make a decision now instead of waiting for the next scheduled one',
      keywords: ['proposal', 'decide', 'research'],
      run: (ctx) => {
        runNow.mutate();
        ctx.close();
      },
    },
    {
      id: 'approvals',
      title: 'Show what is waiting on you',
      subtitle: "Plans the AI has made that need your yes or no, with how long is left",
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
        header: 'Reference',
        render: (r) => (
          <Text size="sm" ff="monospace">
            {r.run_id}
          </Text>
        ),
      },
      {
        key: 'models',
        header: 'Model asked for → model that answered',
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
        header: 'Why it ran',
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
        header: 'Retried on a stronger model',
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
    <MasterDetail
      detail={
        selected ? (
          <DetailPane
            title={`Decision of ${formatRelative(
              runs.data?.runs.find((r) => r.run_id === selected)?.started_utc ?? null,
            )}`}
            subtitle="What was noticed, what Claude concluded, and what the safety check did about it."
            rawId={selected}
            onClose={selection.close}
          >
            <RunDetailBody runId={selected} />
          </DetailPane>
        ) : null
      }
    >
    <Stack gap="md">
      <PageIntro
        title="Decisions"
        blurb={routeBlurb('decisions')}
        actions={
          <>
            <Select data={WINDOWS} value={days} onChange={(v) => setDays(v ?? '30')} w={160} />
            <Button
              leftSection={runNow.isPending ? <Loader size={14} /> : <IconPlayerPlay size={16} />}
              onClick={() => runNow.mutate()}
              disabled={runNow.isPending}
            >
              Think about it now
            </Button>
            <Tooltip label="Refresh" withArrow>
              <ActionIcon variant="default" size="lg" aria-label="refresh" onClick={invalidate}>
                <IconRefresh size={18} />
              </ActionIcon>
            </Tooltip>
          </>
        }
      >
        <Text size="xs" c="dimmed">
          Click any row to open the whole story beside it — nothing here can place an order on
          its own.
        </Text>
      </PageIntro>

      <Tabs value={tab} onChange={setTab} keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="runs">Every decision</Tabs.Tab>
          <Tabs.Tab value="proposals">What it planned to hold</Tabs.Tab>
          <Tabs.Tab value="approvals">
            Waiting on you
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
            emptyTitle="No decisions in this window"
            emptyDescription="The system thinks at the scheduled times, and whenever an idea it spotted survives a second look."
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
          <ApprovalsQueue
            rows={pending.data?.pending ?? []}
            loading={pending.isLoading}
            onSelect={setSelected}
            onDecide={(runId, decision, note) =>
              decide.mutateAsync({ runId, decision, note })
            }
            deciding={decide.isPending}
          />
        </Tabs.Panel>
      </Tabs>
    </Stack>
    </MasterDetail>
  );
}

function ApprovalsQueue({
  rows,
  loading,
  onSelect,
  onDecide,
  deciding,
}: {
  rows: PendingApproval[];
  loading: boolean;
  onSelect: (runId: string) => void;
  onDecide: (runId: string, decision: 'approve' | 'reject', note: string) => Promise<unknown>;
  deciding: boolean;
}) {
  const [pendingDecision, setPendingDecision] = useState<{
    runId: string;
    decision: 'approve' | 'reject';
  } | null>(null);
  const [note, setNote] = useState('');

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
    <>
      <Table striped highlightOnHover withTableBorder>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>Run</Table.Th>
          <Table.Th>Created</Table.Th>
          <Table.Th>Module</Table.Th>
          <Table.Th>Confidence</Table.Th>
          <Table.Th>Expires in</Table.Th>
          <Table.Th>Decision</Table.Th>
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
              <Table.Td onClick={(event) => event.stopPropagation()}>
                <Group gap="xs" wrap="nowrap">
                  <Button
                    size="compact-xs"
                    disabled={deciding || left <= 0}
                    onClick={() =>
                      setPendingDecision({ runId: row.run_id, decision: 'approve' })
                    }
                    data-testid={`approve-${row.run_id}`}
                  >
                    Approve
                  </Button>
                  <Button
                    size="compact-xs"
                    variant="default"
                    disabled={deciding}
                    onClick={() =>
                      setPendingDecision({ runId: row.run_id, decision: 'reject' })
                    }
                    data-testid={`reject-${row.run_id}`}
                  >
                    Reject
                  </Button>
                </Group>
              </Table.Td>
            </Table.Tr>
          );
        })}
      </Table.Tbody>
      </Table>

      <ConfirmDialog
        opened={pendingDecision !== null}
        onClose={() => {
          setPendingDecision(null);
          setNote('');
        }}
        title={
          pendingDecision?.decision === 'approve'
            ? `Approve ${pendingDecision.runId}?`
            : `Reject ${pendingDecision?.runId ?? ''}?`
        }
        confirmLabel={pendingDecision?.decision === 'approve' ? 'Approve' : 'Reject'}
        danger={pendingDecision?.decision === 'approve'}
        description={
          pendingDecision?.decision === 'approve'
            ? 'Writes an HMAC-signed approval file the bot verifies before it acts on this proposal. It expires with the proposal.'
            : 'Removes any approval file for this run. The proposal is recorded rejected and the bot will not act on it.'
        }
        onConfirm={async () => {
          if (!pendingDecision) return;
          await onDecide(pendingDecision.runId, pendingDecision.decision, note);
          setPendingDecision(null);
          setNote('');
        }}
      >
        <Textarea
          label="Note"
          description="Why, for the audit trail. Optional."
          value={note}
          onChange={(event) => setNote(event.currentTarget.value)}
          autosize
          minRows={2}
          data-testid="approval-note"
        />
      </ConfirmDialog>
    </>
  );
}
