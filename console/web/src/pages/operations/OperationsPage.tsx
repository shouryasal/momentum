import { Alert, Button, Grid, Group, Stack, Tabs, Text, Title } from '@mantine/core';
import {
  IconClockHour4,
  IconCalendarTime,
  IconDeviceDesktop,
  IconFileText,
  IconServer2,
} from '@tabler/icons-react';
import { useCallback, useEffect, useState } from 'react';

import { usePageCommands, type CommandContext } from '@/app/commandRegistry';
import { StatCard } from '@/components';
import { formatRelative } from '@/lib/format';

import {
  opsApi,
  type BackupListing,
  type ContainerRow,
  type CrontabStatus,
  type DbStat,
  type HealthSnapshot,
  type HostSummary,
  type Incident,
  type JobRow,
  type LogFile,
  type SystemdUnit,
} from './api';
import { HostPanel } from './components/HostPanel';
import { JobsPanel } from './components/JobsPanel';
import { LogViewer } from './components/LogViewer';
import { SchedulePanel } from './components/SchedulePanel';
import { BackupsPanel, ContainersPanel, DbPanel, IncidentsPanel } from './components/SystemPanel';

interface PageState {
  jobs: JobRow[];
  host: HostSummary | null;
  health: HealthSnapshot | null;
  crontab: CrontabStatus | null;
  units: SystemdUnit[];
  containers: ContainerRow[];
  backups: BackupListing | null;
  incidents: Incident[];
  db: DbStat[];
  logs: LogFile[];
}

const EMPTY: PageState = {
  jobs: [],
  host: null,
  health: null,
  crontab: null,
  units: [],
  containers: [],
  backups: null,
  incidents: [],
  db: [],
  logs: [],
};

const OPS_TABS = [
  ['jobs', 'Jobs'],
  ['host', 'Host readiness'],
  ['schedules', 'Crontab & units'],
  ['containers', 'Containers'],
  ['backups', 'Backups'],
  ['incidents', 'Incidents'],
  ['logs', 'Logs'],
] as const;

/**
 * Operations — the page you open when something is wrong with the *machine* rather than
 * with a trade.
 *
 * It answers, in order: is the host fit to run unattended (ext4, no sleep, keep-alive task,
 * timezone, docker); did every job fire when it should have; does the installed crontab
 * still match the config; are the containers up; are backups landing; and what do the logs
 * say. Everything here is read-only except "Run now", crontab install and container
 * restart, and each of those goes through the same wrapper cron uses.
 */
export function OperationsPage() {
  const [state, setState] = useState<PageState>(EMPTY);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<string | null>('jobs');

  const load = useCallback(async () => {
    setError(null);
    const results = await Promise.allSettled([
      opsApi.jobs(),
      opsApi.host(),
      opsApi.health(),
      opsApi.crontab(),
      opsApi.systemd(),
      opsApi.containers(),
      opsApi.backups(),
      opsApi.incidents(),
      opsApi.dbStats(),
      opsApi.logs(),
    ]);
    const value = <T,>(index: number, fallback: T): T =>
      results[index]?.status === 'fulfilled'
        ? ((results[index] as PromiseFulfilledResult<T>).value as T)
        : fallback;
    const failures = results.filter((r) => r.status === 'rejected');
    if (failures.length) {
      setError(
        `${failures.length} panel(s) failed to load: ` +
          failures
            .map((f) => (f as PromiseRejectedResult).reason?.message ?? 'unknown error')
            .join('; '),
      );
    }
    setState({
      jobs: value(0, { jobs: [] as JobRow[] }).jobs,
      host: value<HostSummary | null>(1, null),
      health: value<HealthSnapshot | null>(2, null),
      crontab: value<CrontabStatus | null>(3, null),
      units: value(4, { units: [] as SystemdUnit[] }).units,
      containers: value(5, { containers: [] as ContainerRow[] }).containers,
      backups: value<BackupListing | null>(6, null),
      incidents: value(7, { incidents: [] as Incident[] }).incidents,
      db: value(8, { databases: [] as DbStat[] }).databases,
      logs: value(9, { logs: [] as LogFile[] }).logs,
    });
    setLoading(false);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const nextFire = state.jobs
    .map((j) => j.next_fire_utc)
    .filter((v): v is string => Boolean(v))
    .sort()[0];
  const running = state.jobs.filter((j) => j.lock_held).length;
  const dataAge = state.health?.data_age_minutes;
  const stale =
    dataAge != null && state.health != null && dataAge > state.health.staleness_limit_min;

  usePageCommands('operations', [
    {
      id: 'reload',
      title: 'Reload every operations panel',
      run: (ctx) => {
        setLoading(true);
        void load();
        ctx.close();
      },
    },
    ...OPS_TABS.map(([value, label]) => ({
      id: `tab-${value}`,
      title: `Operations: ${label}`,
      run: (ctx: CommandContext) => {
        setTab(value);
        ctx.close();
      },
    })),
  ]);

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-end">
        <Stack gap={0}>
          <Title order={2}>Operations</Title>
          <Text c="dimmed" size="sm">
            Jobs, schedules, containers, host readiness, backups and logs.
          </Text>
        </Stack>
        <Button
          variant="default"
          size="xs"
          loading={loading}
          onClick={() => {
            setLoading(true);
            void load();
          }}
        >
          Refresh
        </Button>
      </Group>

      {error ? <Alert color="yellow">{error}</Alert> : null}

      <Grid>
        <Grid.Col span={{ base: 12, sm: 3 }}>
          <StatCard
            label="Host"
            value={state.host ? (state.host.ok ? 'ready' : 'blocked') : '—'}
            icon={<IconDeviceDesktop size={18} />}
            hint={state.host?.blocking_failures.join(', ') || 'no blocking problems'}
            loading={loading}
          />
        </Grid.Col>
        <Grid.Col span={{ base: 12, sm: 3 }}>
          <StatCard
            label="Data age"
            value={dataAge == null ? 'unknown' : `${dataAge.toFixed(0)} min`}
            icon={<IconClockHour4 size={18} />}
            hint={
              state.health
                ? `entries block above ${state.health.staleness_limit_min} min`
                : undefined
            }
            delta={stale ? { value: 'stale — entries blocked', positive: false } : null}
            loading={loading}
          />
        </Grid.Col>
        <Grid.Col span={{ base: 12, sm: 3 }}>
          <StatCard
            label="Next job"
            value={nextFire ? formatRelative(nextFire) : '—'}
            icon={<IconCalendarTime size={18} />}
            hint={running ? `${running} running now` : 'nothing in flight'}
            loading={loading}
          />
        </Grid.Col>
        <Grid.Col span={{ base: 12, sm: 3 }}>
          <StatCard
            label="Open incidents"
            value={state.incidents.length}
            icon={<IconServer2 size={18} />}
            hint={
              state.health?.undelivered_alerts
                ? `${state.health.undelivered_alerts} alerts queued for delivery`
                : 'alert outbox empty'
            }
            loading={loading}
          />
        </Grid.Col>
      </Grid>

      <Tabs value={tab} onChange={setTab} keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="jobs">Jobs</Tabs.Tab>
          <Tabs.Tab value="host">Host readiness</Tabs.Tab>
          <Tabs.Tab value="schedules">Crontab &amp; units</Tabs.Tab>
          <Tabs.Tab value="containers">Containers</Tabs.Tab>
          <Tabs.Tab value="backups">Backups</Tabs.Tab>
          <Tabs.Tab value="incidents">Incidents</Tabs.Tab>
          <Tabs.Tab value="logs" leftSection={<IconFileText size={14} />}>
            Logs
          </Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="jobs" pt="md">
          <JobsPanel jobs={state.jobs} loading={loading} onChanged={() => void load()} />
        </Tabs.Panel>
        <Tabs.Panel value="host" pt="md">
          <HostPanel host={state.host} loading={loading} />
        </Tabs.Panel>
        <Tabs.Panel value="schedules" pt="md">
          <SchedulePanel
            crontab={state.crontab}
            units={state.units}
            loading={loading}
            onChanged={() => void load()}
          />
        </Tabs.Panel>
        <Tabs.Panel value="containers" pt="md">
          <ContainersPanel
            containers={state.containers}
            loading={loading}
            onChanged={() => void load()}
          />
        </Tabs.Panel>
        <Tabs.Panel value="backups" pt="md">
          <BackupsPanel
            backups={state.backups}
            loading={loading}
            onChanged={() => void load()}
          />
        </Tabs.Panel>
        <Tabs.Panel value="incidents" pt="md">
          <Stack gap="md">
            <IncidentsPanel
              incidents={state.incidents}
              loading={loading}
              onChanged={() => void load()}
            />
            <DbPanel stats={state.db} loading={loading} />
          </Stack>
        </Tabs.Panel>
        <Tabs.Panel value="logs" pt="md">
          <LogViewer logs={state.logs} loading={loading} />
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}

export default OperationsPage;
