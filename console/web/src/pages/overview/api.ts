/** Overview page transport (spec 12 page 1): one bundle, refetched on the SSE topics. */

import { api } from '@/api';

export interface NavCard {
  sleeve: string;
  run_id: string | null;
  mode: string | null;
  ts_utc: string | null;
  nav_usdt: number | null;
  cash_usdt: number | null;
  reserved_usdt: number | null;
  open_trades: number | null;
  positions: Record<string, { value_usdt?: number } | number>;
  day_pct: number | null;
  week_pct: number | null;
  run_pct: number | null;
  run_started_utc: string | null;
  points?: number;
}

export interface ExposureAsset {
  asset: string;
  weight: number;
  cap: number;
  util: number | null;
}

export interface ExposureSleeve {
  sleeve: string;
  gross: number;
  gross_cap: number;
  gross_util: number | null;
  assets: ExposureAsset[];
}

export interface GatePanel {
  since_utc: string;
  allow: number;
  reject: number;
  breach: number;
  recent: Array<{
    id: number;
    ts_utc: string;
    sleeve: string;
    pair: string;
    reason: string;
    severity: string;
    callback: string;
  }>;
}

export interface FunnelPanel {
  since_utc: string;
  by_status: Record<string, number>;
  stages: Array<{ stage: string; count: number }>;
}

export interface ResearchPanel {
  last: {
    run_id: string;
    stage: string;
    started_utc: string;
    finished_utc: string | null;
    status: string;
    requested_model: string | null;
    served_model: string | null;
    provider: string | null;
    escalated: boolean;
    escalation_reasons: string[];
    cost_usd: number | null;
    trigger_reason: string | null;
    signal_id: string | null;
  } | null;
  proposal?: { run_id: string; approval_status: string | null; signal_id: string | null } | null;
}

export interface ScheduleJob {
  job: string;
  cron: string;
  next_fire_local: string;
  next_fire_utc: string;
  deadline_s: number | null;
  artifact: string | null;
}

export interface SleeveModeState {
  state: string;
  submode: string | null;
  run_id: string | null;
  seed_usdt: number | null;
}

export interface OverviewPayload {
  ok: boolean;
  config_error?: string;
  generated_utc?: string;
  mode: {
    verified: boolean;
    reason: string;
    phase: string;
    sleeves: Record<string, SleeveModeState>;
    kill: { engaged: boolean };
  };
  nav?: { cards: NavCard[] };
  nav_series?: { since_utc: string; series: Record<string, Array<{ ts: string; nav: number }>> };
  exposure?: { sleeves: ExposureSleeve[] };
  gate?: GatePanel;
  funnel?: FunnelPanel;
  research?: ResearchPanel;
  schedule?: { jobs: ScheduleJob[]; timezone: string };
  provider?: {
    since_utc: string;
    usage: Array<{ provider: string; calls: number; cost: number; ok: number }>;
    rate_limit: Record<string, unknown>;
    recent_switches: Array<Record<string, unknown>>;
  };
  changed_today?: {
    since_utc: string;
    config: Array<{
      id: number;
      ts_utc: string;
      actor: string;
      file: string;
      reason: string | null;
      changed_paths: string[];
      protected_changed: number;
      applied: number;
    }>;
    changes: Array<{
      change_id: string;
      kind: string;
      target: string;
      status: string;
      decided_at: string | null;
      decided_by: string | null;
    }>;
  };
  incidents?: Array<{
    id: number;
    opened_utc: string;
    kind: string;
    subkind: string | null;
    severity: string;
    detail: string;
  }>;
  approvals?: {
    proposals: Array<{ run_id: string; approval_status: string }>;
    changes: Array<{ change_id: string; kind: string; target: string; status: string }>;
    count: number;
  };
  flags?: { active: string[] };
  bless?: { ok: boolean; reason: string; changed: string[] };
  limits?: {
    daily_loss_stop: number;
    monthly_loss_stop: number;
    max_gross_exposure: number;
    usdt_floor: number;
    max_trades_per_day: number;
  };
  banner?: {
    pending: Array<{ effect: string; title: string; detail: string; auto_applicable: boolean;
      since_utc: string; source: string; reason: string | null; audit_id: number | null }>;
    count: number;
    needs_reset: boolean;
    applicable: string[];
  };
}

export const overviewApi = {
  get: () => api.get<OverviewPayload>('/overview'),
  nav: (days: number) =>
    api.get<{ since_utc: string; series: Record<string, Array<{ ts: string; nav: number }>> }>(
      '/overview/nav',
      { days },
    ),
};

export const overviewKeys = {
  bundle: ['overview'] as const,
  nav: (days: number) => ['overview', 'nav', days] as const,
};
