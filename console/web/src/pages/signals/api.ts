/**
 * Typed wrappers over `/api/signals/*` (spec 5.2) plus the query keys the page uses.
 *
 * The shapes here mirror what `console/services/signals_service.py` returns. They are
 * deliberately permissive about fields the backend may add (`[key: string]: unknown` is
 * avoided; optional fields are used instead) so a new column in `signals` does not break
 * the build before the page renders it.
 */
import type { ApiClient } from '@/api';

export type SignalStatus =
  | 'candidate'
  | 'screened_out'
  | 'screened'
  | 'validating'
  | 'valid'
  | 'invalid'
  | 'uncertain'
  | 'blocked'
  | 'planned'
  | 'acted'
  | 'expired'
  | 'error';

export type Direction = 'up' | 'down' | 'risk' | 'neutral';

export interface SignalRow {
  signal_id: string;
  ts_utc: string;
  scan_id: string;
  source: string;
  detector: string;
  pair: string | null;
  direction: Direction | null;
  detector_score: number | null;
  screen_score: number | null;
  strength: number;
  fast_path: boolean;
  status: SignalStatus;
  status_reason: string | null;
  screen_provider: string | null;
  screen_model: string | null;
  screen_rationale: string | null;
  run_id: string | null;
  proposal_run_id: string | null;
  updated_utc: string;
}

export interface ValidationRow {
  id: number;
  signal_id: string;
  ts_utc: string;
  provider: string;
  model: string;
  verdict: 'valid' | 'invalid' | 'uncertain';
  confidence: number;
  horizon_hours: number | null;
  thesis: string | null;
  invalidation: string | null;
  escalated: boolean;
  cost_usd: number | null;
  latency_ms: number | null;
  pack_path: string | null;
  error: string | null;
  outcome_ret: number | null;
  outcome_hit: number | null;
  outcome_resolved_at: string | null;
  reasons: string[];
  counter_evidence: string[];
  suggested: { direction: string; pair?: string | null; conviction?: number | null } | null;
}

export interface SignalDetail extends SignalRow {
  features: Record<string, number | string | null>;
  detector_detail: Record<string, unknown>;
  news_refs: string[];
  blocked: string[];
  validations: ValidationRow[];
  run: Record<string, unknown> | null;
  proposal: Record<string, unknown> | null;
}

export interface FunnelCounts {
  detected: number;
  screened: number;
  validated: number;
  valid: number;
  planned: number;
  acted: number;
  screened_out: number;
  expired: number;
  errors: number;
}

export interface GroupStat {
  group: string;
  n: number;
  hits: number;
  hit_rate: number;
  avg_ret: number | null;
  avg_confidence: number | null;
}

export interface FunnelResponse {
  window_hours: number;
  counts: FunnelCounts;
  by_detector: GroupStat[];
  by_model: GroupStat[];
  screen: { by_model: Array<{ provider: string | null; model: string | null; n: number }>; screen_unavailable: number };
}

export interface SignalFilters {
  status?: string;
  detector?: string;
  pair?: string;
  since_hours?: number;
  limit?: number;
}

export function signalsApi(client: ApiClient) {
  return {
    list: (filters: SignalFilters = {}) =>
      client.get<{ signals: SignalRow[] }>('/signals', {
        status: filters.status ?? null,
        detector: filters.detector ?? null,
        pair: filters.pair ?? null,
        since_hours: filters.since_hours ?? null,
        limit: filters.limit ?? 100,
      }),
    detail: (signalId: string) => client.get<SignalDetail>(`/signals/${encodeURIComponent(signalId)}`),
    funnel: (hours = 24, days = 30) => client.get<FunnelResponse>('/signals/funnel', { hours, days }),
    scanNow: () => client.post<{ spawned: boolean; pid: number | null }>('/signals/scan-now'),
    revalidate: (signalId: string) =>
      client.post<{ spawned: boolean; pid: number | null }>(
        `/signals/${encodeURIComponent(signalId)}/revalidate`,
      ),
    manual: (body: { pair: string | null; direction: Direction; note: string }) =>
      client.post<{ signal_id: string }>('/signals/manual', body),
    label: (signalId: string, value: 'noise' | 'useful') =>
      client.post<{ ok: boolean; label: string }>(`/signals/${encodeURIComponent(signalId)}/label`, {
        label: value,
      }),
  };
}

export const signalKeys = {
  all: ['signals'] as const,
  list: (filters: SignalFilters) => ['signals', 'list', filters] as const,
  detail: (id: string) => ['signals', 'detail', id] as const,
  funnel: (hours: number, days: number) => ['signals', 'funnel', hours, days] as const,
};

/** The colour each status gets everywhere in the console, so the funnel reads at a glance. */
export const STATUS_COLOR: Record<SignalStatus, string> = {
  candidate: 'gray',
  screened_out: 'gray',
  screened: 'blue',
  validating: 'indigo',
  valid: 'teal',
  invalid: 'red',
  uncertain: 'yellow',
  blocked: 'orange',
  planned: 'violet',
  acted: 'green',
  expired: 'gray',
  error: 'red',
};

export const DIRECTION_COLOR: Record<Direction, string> = {
  up: 'teal',
  down: 'red',
  risk: 'orange',
  neutral: 'gray',
};
