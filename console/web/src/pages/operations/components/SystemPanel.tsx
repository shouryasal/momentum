import { Alert, Badge, Button, Card, Code, Grid, Group, Stack, Text } from '@mantine/core';
import { IconDatabase, IconDeviceFloppy, IconPlugConnected } from '@tabler/icons-react';
import { useCallback, useState } from 'react';

import { errorMessage } from '@/api';
import { ConfirmDialog, DataTable, StatCard, type DataTableColumn } from '@/components';
import { formatRelative } from '@/lib/format';

import {
  opsApi,
  type BackupListing,
  type ContainerRow,
  type DbStat,
  type DestProbe,
  type Incident,
} from '../api';

function mib(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
}

export function BackupsPanel({
  backups,
  loading,
  onChanged,
}: {
  backups: BackupListing | null;
  loading: boolean;
  onChanged: () => void;
}) {
  const [probe, setProbe] = useState<DestProbe | null>(null);
  const [busy, setBusy] = useState(false);

  const test = useCallback(async (which: 'dest' | 'mirror') => {
    setBusy(true);
    try {
      setProbe(await opsApi.testDest(which));
    } finally {
      setBusy(false);
    }
  }, []);

  const run = useCallback(async () => {
    setBusy(true);
    try {
      await opsApi.runBackup();
      onChanged();
    } finally {
      setBusy(false);
    }
  }, [onChanged]);

  if (loading || !backups) return <Text c="dimmed">Reading the backup destination…</Text>;

  return (
    <Stack gap="sm">
      <Group justify="space-between" align="flex-end">
        <Stack gap={2}>
          <Text fw={600}>Destination</Text>
          <Code>{backups.dest}</Code>
          {backups.mirror_dest ? (
            <Text size="xs" c="dimmed">
              mirror: <Code>{backups.mirror_dest}</Code> (best effort — a failure warns, never
              alerts)
            </Text>
          ) : null}
        </Stack>
        <Group gap="xs">
          <Button size="xs" variant="light" loading={busy} onClick={() => void test('dest')}>
            Test destination
          </Button>
          {backups.mirror_dest ? (
            <Button size="xs" variant="subtle" loading={busy} onClick={() => void test('mirror')}>
              Test mirror
            </Button>
          ) : null}
          <Button
            size="xs"
            leftSection={<IconDeviceFloppy size={14} />}
            loading={busy}
            onClick={() => void run()}
          >
            Back up now
          </Button>
        </Group>
      </Group>

      {probe ? (
        <Alert color={probe.ok ? 'teal' : 'red'} title={`${probe.which}: ${probe.detail}`}>
          <Code>{probe.path ?? '(not configured)'}</Code>
          {probe.free_gb != null ? ` · ${probe.free_gb.toFixed(1)} GB free` : ''}
        </Alert>
      ) : null}

      <DataTable
        columns={
          [
            { key: 'date', header: 'Date', sortValue: (r) => r.date, render: (r) => r.date },
            {
              key: 'size',
              header: 'Size',
              align: 'right',
              sortValue: (r) => r.bytes,
              render: (r) => mib(r.bytes),
            },
            { key: 'path', header: 'Path', render: (r) => <Code>{r.path}</Code> },
          ] as Array<DataTableColumn<BackupListing['entries'][number]>>
        }
        rows={backups.entries}
        rowKey={(r) => r.date}
        emptyTitle="No backups yet"
        emptyDescription={`Nothing at ${backups.dest}. "Back up now" writes the first one.`}
        maxHeight={260}
      />
      <Text size="xs" c="dimmed">
        Retention: {backups.keep_daily} daily, {backups.keep_weekly} weekly.
      </Text>
    </Stack>
  );
}

export function ContainersPanel({
  containers,
  loading,
  onChanged,
}: {
  containers: ContainerRow[];
  loading: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);

  /**
   * `POST /api/ops/containers/{service}/restart` is step-up guarded.
   *
   * It used to fire from the Restart button's bare `onClick`, so outside the step-up
   * window every click painted "re-enter the console token to continue" into a red alert
   * with no way to step up from this panel — and inside the window a freqtrade container
   * restart was one unconfirmed click. `ConfirmDialog` supplies both.
   */
  const restart = useCallback(
    async (service: string) => {
      setBusy(service);
      setError(null);
      try {
        await opsApi.restartContainer(service);
        onChanged();
      } catch (e) {
        setError(errorMessage(e));
        throw e; // the dialog shows it too, and stays open
      } finally {
        setBusy(null);
      }
    },
    [onChanged],
  );

  return (
    <Stack gap="sm">
      {error ? <Alert color="red">{error}</Alert> : null}
      {loading ? <Text c="dimmed">Reading docker…</Text> : null}
      {containers.map((c) => (
        <Card key={c.service} withBorder padding="sm" radius="md">
          <Group justify="space-between">
            <Stack gap={0}>
              <Text fw={600}>{c.service}</Text>
              <Text size="xs" c="dimmed">
                sleeve {c.sleeve} · {c.api}
              </Text>
            </Stack>
            <Group gap="xs">
              <Badge color={c.state === 'running' ? 'teal' : 'red'}>{c.state}</Badge>
              <Button
                size="xs"
                variant="light"
                leftSection={<IconPlugConnected size={14} />}
                loading={busy === c.service}
                onClick={() => setConfirming(c.service)}
                data-testid={`restart-${c.service}`}
              >
                Restart
              </Button>
            </Group>
          </Group>
        </Card>
      ))}
      <ConfirmDialog
        opened={confirming !== null}
        onClose={() => setConfirming(null)}
        title={`Restart ${confirming ?? ''}?`}
        confirmLabel="Restart"
        requireStepUp
        danger
        description="The bot stops and comes back on the same generated config. Open positions are left alone; entries pause while it is down. It takes the ops lock."
        onConfirm={async () => {
          if (confirming) await restart(confirming);
          setConfirming(null);
        }}
      />
    </Stack>
  );
}

export function IncidentsPanel({
  incidents,
  loading,
  onChanged,
}: {
  incidents: Incident[];
  loading: boolean;
  onChanged: () => void;
}) {
  const columns: Array<DataTableColumn<Incident>> = [
    { key: 'kind', header: 'Kind', sortValue: (r) => r.kind, render: (r) => <Badge>{r.kind}</Badge> },
    { key: 'detail', header: 'Detail', render: (r) => r.detail ?? '' },
    {
      key: 'opened',
      header: 'Opened',
      sortValue: (r) => r.opened_at,
      render: (r) => formatRelative(r.opened_at),
    },
    {
      key: 'close',
      header: '',
      align: 'right',
      render: (r) => (
        <Button
          size="xs"
          variant="subtle"
          onClick={() => {
            void opsApi.closeIncident(r.id).then(onChanged);
          }}
        >
          Close
        </Button>
      ),
    },
  ];
  return (
    <DataTable
      columns={columns}
      rows={incidents}
      rowKey={(r) => String(r.id)}
      loading={loading}
      emptyTitle="No open incidents"
      emptyDescription="The watchdog opens one for a stale feed, a missed run, a mode mismatch or a dead backup destination."
    />
  );
}

export function DbPanel({ stats, loading }: { stats: DbStat[]; loading: boolean }) {
  if (loading) return <Text c="dimmed">Reading the databases…</Text>;
  return (
    <Grid>
      {stats.map((s) => (
        <Grid.Col key={s.name} span={{ base: 12, sm: 4 }}>
          <StatCard
            label={s.name}
            value={mib(s.bytes)}
            icon={<IconDatabase size={18} />}
            hint={
              s.error
                ? s.error
                : s.journal_mode
                  ? `${s.journal_mode} · WAL ${mib(s.wal_bytes ?? 0)}`
                  : s.free_bytes != null
                    ? `${mib(s.free_bytes)} free`
                    : s.path
            }
          />
        </Grid.Col>
      ))}
    </Grid>
  );
}
