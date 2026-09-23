/**
 * Operations page API surface: `/api/ops/*` and `/api/logs/*`.
 *
 * Types mirror the router's responses exactly (`console/routers/ops.py`,
 * `console/routers/logs.py`); the transport is the shared client from `@/api`.
 */

import { api } from '@/api';

export type CheckStatus = 'ok' | 'warn' | 'fail';

export interface JobRow {
  job: string;
  /** One expression per fire time — research has one per `research.slots` entry. */
  crons: string[];
  deadline_s: number;
  artifact: string;
  next_fire_utc: string | null;
  last_fire_utc: string | null;
  last_status: string | null;
  last_started_utc: string | null;
  last_finished_utc: string | null;
  duration_s: number | null;
  lock_held: boolean;
  runnable: boolean;
  log: string;
}

export interface JobRunRow {
  job: string;
  scheduled_for: string;
  started_at: string | null;
  finished_at: string | null;
  status: string | null;
  rerun_count: number;
  detached_pid: number | null;
  rerun_started_utc: string | null;
}

export interface RunJobResult {
  id: number | null;
  job: string;
  pid: number | null;
  status: string;
  started_utc: string;
  log: string;
  argv: string[];
}

export interface CrontabStatus {
  rendered: string;
  diff: string;
  in_sync: boolean;
  templates: Array<{ path: string; reason: string; diff: string }>;
  templates_in_sync: boolean;
}

export interface SystemdUnit {
  name: string;
  path: string;
  rendered: string;
  active: string;
  enabled: string;
  installed: boolean;
}

export interface HostCheck {
  name: string;
  status: CheckStatus;
  detail: string;
  fix: string;
  /** A failing blocking check also blocks the live preflight. */
  blocking: boolean;
  data: Record<string, unknown>;
}

export interface HostSummary {
  ok: boolean;
  blocking_failures: string[];
  counts: Partial<Record<CheckStatus, number>>;
  checks: HostCheck[];
}

export interface HealthSnapshot {
  as_of: string;
  freshness_minutes: Record<string, number | null>;
  data_age_minutes: number | null;
  staleness_limit_min: number;
  open_incidents: number;
  undelivered_alerts: number | null;
}

export interface Incident {
  id: number;
  opened_at: string;
  kind: string;
  detail: string | null;
  resolved_at: string | null;
}

export interface BackupListing {
  dest: string;
  dest_exists: boolean;
  mirror_dest: string | null;
  keep_daily: number;
  keep_weekly: number;
  entries: Array<{ date: string; path: string; bytes: number }>;
}

export interface DestProbe {
  which: string;
  ok: boolean;
  path: string | null;
  detail: string;
  free_gb?: number | null;
}

export interface ContainerRow {
  sleeve: string;
  service: string;
  state: string;
  api: string;
}

export interface DbStat {
  name: string;
  path: string;
  exists: boolean;
  bytes: number;
  wal_bytes?: number;
  journal_mode?: string | null;
  page_count?: number | null;
  free_bytes?: number;
  total_bytes?: number;
  error?: string;
}

export interface LogFile {
  name: string;
  bytes: number;
  modified_utc: string;
}

export interface LogTail {
  name: string;
  bytes: number;
  lines: number;
  /** Already redacted server-side; the client never sees a raw secret. */
  text: string;
}

export const opsApi = {
  jobs: () => api.get<{ jobs: JobRow[] }>('/ops/jobs'),
  jobRuns: (job: string) => api.get<{ job: string; runs: JobRunRow[] }>(`/ops/jobs/${job}/runs`),
  runJob: (job: string, slot?: string) =>
    api.post<RunJobResult>(`/ops/jobs/${job}/run${slot ? `?slot=${encodeURIComponent(slot)}` : ''}`),
  crontab: () => api.get<CrontabStatus>('/ops/schedules/crontab'),
  installCrontab: () =>
    api.post<{ crontab_diff: string; units: string[]; staged_in: string; sudo: string[] }>(
      '/ops/schedules/install',
    ),
  systemd: () => api.get<{ units: SystemdUnit[] }>('/ops/systemd'),
  host: () => api.get<HostSummary>('/ops/host'),
  health: () => api.get<HealthSnapshot>('/ops/health'),
  dbStats: () => api.get<{ databases: DbStat[] }>('/ops/db'),
  incidents: (includeResolved = false) =>
    api.get<{ incidents: Incident[] }>('/ops/incidents', { include_resolved: includeResolved }),
  closeIncident: (id: number) =>
    api.post<{ id: number; closed: boolean }>(`/ops/incidents/${id}/close`),
  backups: () => api.get<BackupListing>('/ops/backups'),
  runBackup: () => api.post<RunJobResult>('/ops/backups/run'),
  testDest: (which: 'dest' | 'mirror') =>
    api.post<DestProbe>(`/ops/backups/test-dest?which=${which}`),
  containers: () => api.get<{ containers: ContainerRow[] }>('/ops/containers'),
  restartContainer: (service: string) =>
    api.post<{ service: string; ok: boolean; detail: string }>(
      `/ops/containers/${service}/restart`,
    ),
  logs: () => api.get<{ logs: LogFile[] }>('/logs'),
  logTail: (name: string, tail: number) => api.get<LogTail>(`/logs/${name}`, { tail }),
};
