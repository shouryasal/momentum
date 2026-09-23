import { ActionIcon, Badge, Button, Code, Group, Stack, Text, Tooltip } from '@mantine/core';
import { IconPlayerPlay, IconRefresh } from '@tabler/icons-react';
import { useCallback, useState } from 'react';

import { DataTable, type DataTableColumn } from '@/components';
import { formatDuration, formatRelative, formatUtcStamp } from '@/lib/format';

import { opsApi, type JobRow } from '../api';

const STATUS_COLOUR: Record<string, string> = {
  ok: 'teal',
  rerun: 'yellow',
  failed: 'red',
  running: 'blue',
};

function statusBadge(row: JobRow) {
  if (row.lock_held) return <Badge color="blue">running</Badge>;
  if (!row.last_status) return <Badge color="gray">no runs</Badge>;
  return <Badge color={STATUS_COLOUR[row.last_status] ?? 'gray'}>{row.last_status}</Badge>;
}

export interface JobsPanelProps {
  jobs: JobRow[];
  loading: boolean;
  onChanged: () => void;
}

/**
 * The jobs table. "Run now" launches the *cron line's own* wrapper (flock + timeout +
 * envwrap), detached — the same guarantees the schedule has, so it can never double-run
 * a job cron is already running.
 */
export function JobsPanel({ jobs, loading, onChanged }: JobsPanelProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const run = useCallback(
    async (job: string) => {
      setBusy(job);
      setMessage(null);
      try {
        const result = await opsApi.runJob(job);
        setMessage(`${job} started (pid ${result.pid ?? '?'}) — output in logs/${result.log}`);
        onChanged();
      } catch (error) {
        setMessage(`${job}: ${(error as Error).message}`);
      } finally {
        setBusy(null);
      }
    },
    [onChanged],
  );

  const columns: Array<DataTableColumn<JobRow>> = [
    {
      key: 'job',
      header: 'Job',
      sortValue: (row) => row.job,
      render: (row) => (
        <Stack gap={0}>
          <Text fw={600}>{row.job}</Text>
          <Text size="xs" c="dimmed">
            logs/{row.log}
          </Text>
        </Stack>
      ),
    },
    {
      key: 'schedule',
      header: 'Schedule (Gulf)',
      render: (row) => (
        <Stack gap={2}>
          {row.crons.map((cron) => (
            <Code key={cron}>{cron}</Code>
          ))}
        </Stack>
      ),
    },
    {
      key: 'next',
      header: 'Next fire',
      sortValue: (row) => row.next_fire_utc ?? '',
      render: (row) => (
        <Tooltip label={formatUtcStamp(row.next_fire_utc)} withArrow>
          <Text size="sm">{row.next_fire_utc ? formatRelative(row.next_fire_utc) : '—'}</Text>
        </Tooltip>
      ),
    },
    {
      key: 'last',
      header: 'Last run',
      sortValue: (row) => row.last_started_utc ?? '',
      render: (row) => (
        <Group gap="xs">
          {statusBadge(row)}
          <Text size="xs" c="dimmed">
            {row.last_started_utc ? formatRelative(row.last_started_utc) : '—'}
          </Text>
        </Group>
      ),
    },
    {
      key: 'duration',
      header: 'Duration',
      align: 'right',
      sortValue: (row) => row.duration_s ?? -1,
      render: (row) => (row.duration_s === null ? '—' : formatDuration(row.duration_s * 1000)),
    },
    {
      key: 'deadline',
      header: 'Timeout',
      align: 'right',
      sortValue: (row) => row.deadline_s,
      render: (row) => `${row.deadline_s}s`,
    },
    {
      key: 'run',
      header: '',
      align: 'right',
      render: (row) =>
        row.runnable ? (
          <Tooltip
            label={row.lock_held ? 'a run is already in flight' : 'run now (detached)'}
            withArrow
          >
            <ActionIcon
              variant="light"
              aria-label={`run ${row.job}`}
              loading={busy === row.job}
              disabled={row.lock_held}
              onClick={() => void run(row.job)}
            >
              <IconPlayerPlay size={16} />
            </ActionIcon>
          </Tooltip>
        ) : null,
    },
  ];

  return (
    <Stack gap="sm">
      <Group justify="space-between">
        <Text size="sm" c="dimmed">
          Every job runs under flock (no overlap), timeout (its deadline) and envwrap (its
          own secret allowlist). Research fires once per <Code>research.slots</Code> entry.
        </Text>
        <Button
          size="xs"
          variant="subtle"
          leftSection={<IconRefresh size={14} />}
          onClick={onChanged}
        >
          Refresh
        </Button>
      </Group>
      {message ? <Text size="sm">{message}</Text> : null}
      <DataTable
        columns={columns}
        rows={jobs}
        rowKey={(row) => row.job}
        loading={loading}
        emptyTitle="No schedules"
        emptyDescription="config/earn.yaml ops.schedules is empty."
      />
    </Stack>
  );
}

export default JobsPanel;
