import { api } from '@/api';

export interface SleeveRunRow {
  run_id: string;
  sleeve: string;
  mode: string;
  submode: string | null;
  seed_usdt: number;
  started_utc: string;
  ended_utc: string | null;
  status: string;
  strategy: string;
  config_sha: string;
  label: string | null;
  notes: string | null;
  ft_db_path: string;
  final_metrics_json: string | null;
}

export interface RunMetrics {
  run_id: string;
  sleeve: string;
  mode: string;
  label: string | null;
  seed_usdt: number;
  days: number;
  points: number;
  nav_start: number | null;
  nav_end: number | null;
  return_pct: number | null;
  cagr_pct: number | null;
  max_drawdown_pct: number | null;
  volatility_pct: number | null;
  sharpe: number | null;
  sortino: number | null;
  trades: number;
  win_rate: number | null;
  profit_factor: number | null;
  avg_win_usdt: number | null;
  avg_loss_usdt: number | null;
  exposure_pct: number | null;
  turnover_usdt: number;
  fees_usdt: number;
  gate_rejects: number;
  gate_breaches: number;
  signals_total: number;
  signals_acted: number;
  signal_conversion: number | null;
  decisions: number;
  llm_cost_usd: number;
  cost_per_decision_usd: number | null;
  benchmark_return_pct: number | null;
  excess_return_pct: number | null;
}

export interface Caveat {
  simulated: boolean;
  assumed_bps: number;
  measured_bps: number | null;
  gap_bps: number | null;
  text: string;
}

export interface PendingReset {
  sleeve: string;
  run_id: string | null;
  current_seed_usdt: number | null;
  configured_seed_usdt: number;
  reset_required: boolean;
}

export interface RunSummary {
  sleeve: string;
  run: SleeveRunRow | null;
  days: number | null;
  metrics: RunMetrics | null;
  pending: PendingReset;
  caveat: Caveat | null;
}

export interface SeriesPoint {
  ts_utc: string;
  days: number;
  nav: number;
  index: number;
}

export interface TradeRow {
  pair: string;
  opened_utc: string;
  closed_utc: string;
  amount: number;
  entry_price: number;
  exit_price: number;
  fees_usdt: number;
  pnl_usdt: number;
  win: boolean;
}

export interface RunDetail {
  run: SleeveRunRow;
  metrics: RunMetrics;
  caveat: Caveat;
  series: SeriesPoint[];
  benchmark: SeriesPoint[];
  trades: TradeRow[];
}

export interface ConfigChange {
  ts_utc: string;
  file: string;
  changed_paths: string[];
  reason: string | null;
  actor: string;
}

export interface CompareResponse {
  run_ids: string[];
  metrics: RunMetrics[];
  series: Record<string, SeriesPoint[]>;
  deltas: Record<string, Record<string, number | null>>;
  config_diff: ConfigChange[];
}

export interface ResetResponse {
  run_id: string;
  previous_run_id: string | null;
  seed_usdt: number;
  steps: Array<{ step: string; status: string; detail?: string }>;
}

export const MIN_COMPARE = 2;
export const MAX_COMPARE = 5;
export const RESET_PHRASE = 'RESET';

export const testLabApi = {
  runs: (sleeve?: string) => api.get<SleeveRunRow[]>('/testruns', sleeve ? { sleeve } : undefined),
  summary: (sleeve: string) => api.get<RunSummary>(`/testruns/summary/${sleeve}`),
  detail: (runId: string) => api.get<RunDetail>(`/testruns/${encodeURIComponent(runId)}`),
  compare: (ids: string[]) => api.get<CompareResponse>('/testruns/compare', { ids: ids.join(',') }),
  reset: (
    sleeve: string,
    body: { seed_usdt?: number | null; label?: string | null; notes?: string | null; confirm_phrase: string },
  ) => api.post<ResetResponse>(`/testruns/${sleeve}/reset`, body),
};

/** The metrics the compare table shows, in the order they answer "was this better?". */
export const COMPARE_FIELDS: Array<{ key: keyof RunMetrics; label: string; unit?: string; better?: 'high' | 'low' }> = [
  { key: 'return_pct', label: 'Return', unit: '%', better: 'high' },
  { key: 'excess_return_pct', label: 'vs BTC', unit: '%', better: 'high' },
  { key: 'cagr_pct', label: 'CAGR', unit: '%', better: 'high' },
  { key: 'max_drawdown_pct', label: 'Max drawdown', unit: '%', better: 'high' },
  { key: 'volatility_pct', label: 'Volatility', unit: '%', better: 'low' },
  { key: 'sharpe', label: 'Sharpe', better: 'high' },
  { key: 'sortino', label: 'Sortino', better: 'high' },
  { key: 'trades', label: 'Trades' },
  { key: 'win_rate', label: 'Win rate', better: 'high' },
  { key: 'profit_factor', label: 'Profit factor', better: 'high' },
  { key: 'exposure_pct', label: 'Exposure', unit: '%' },
  { key: 'turnover_usdt', label: 'Turnover', unit: ' USDT', better: 'low' },
  { key: 'fees_usdt', label: 'Fees', unit: ' USDT', better: 'low' },
  { key: 'gate_rejects', label: 'Gate rejects', better: 'low' },
  { key: 'gate_breaches', label: 'Gate breaches', better: 'low' },
  { key: 'signal_conversion', label: 'Signal→trade' },
  { key: 'cost_per_decision_usd', label: 'Cost / decision', unit: ' USD', better: 'low' },
];

export function formatMetric(value: unknown, unit?: string): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') {
    const rounded = Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(2);
    return `${rounded}${unit ?? ''}`;
  }
  return String(value);
}
