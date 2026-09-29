/** Overview page transport (spec 12 page 1): one bundle, refetched on the SSE topics. */

import { api } from '@/api';

import type { RunDetail } from '../decisions/api';
import type { PortfolioPayload, SleevePot } from '../portfolio/api';
import type { GateDecisionsPayload } from '../risk/api';

/** Both bots' pots added up — only when they are on the same kind of money. */
export interface PotTotal {
  seed_usdt: number;
  cumulative_net_usdt: number;
  gain_usdt: number;
  gain_pct: number | null;
  realised_all_runs_usdt: number;
  realised_current_run_usdt: number;
  realised_earlier_runs_usdt: number;
  open_mark_usdt: number;
  fees_usdt: number;
  gross_usdt: number;
  fully_priced: boolean;
  unpriced: string[];
  restarts: number;
  runs: number;
  ledger_nav_usdt: number | null;
  ledger_gap_usdt: number | null;
}

/**
 * The cumulative pot, read from the bots' own databases rather than the 15-minute ledger.
 *
 * The ledger (`nav.cards`) restarts from the seed whenever a bot gets a fresh database —
 * on 2026-09-23 23:45Z that hid a 69.77 loss. `total` is `null` when the bots are on
 * different kinds of money (`mixed`) or when the reader failed (`error`).
 */
export interface PotPanel {
  basis: string;
  mixed: boolean;
  /** The sentence beside each of the three numbers, keyed by field name. */
  definitions: Record<string, string>;
  total: PotTotal | null;
  sleeves: SleevePot[];
  as_of_utc: string;
  error?: string;
}

export interface NavCard {
  sleeve: string;
  run_id: string | null;
  mode: string | null;
  ts_utc: string | null;
  nav_usdt: number | null;
  cash_usdt: number | null;
  reserved_usdt: number | null;
  open_trades: number | null;
  /** Base-unit AMOUNTS keyed by base asset (`{"BTC": 0.04}`) — see `docs/contracts.md`. */
  positions: Record<string, { amount?: number } | number>;
  day_pct: number | null;
  week_pct: number | null;
  run_pct: number | null;
  run_started_utc: string | null;
  points?: number;
}

export interface ExposureAsset {
  asset: string;
  /** Base units held — `positions_json` is coins, not money. */
  amount: number | null;
  /** The close the amount was valued at; `null` when no closed candle could be found. */
  mark_usdt: number | null;
  value_usdt: number | null;
  /** `null` when the amount could not be marked — the page shows "unknown", never 0%. */
  weight: number | null;
  cap: number;
  util: number | null;
}

export interface ExposureSleeve {
  sleeve: string;
  /** `nav - cash` over NAV; `null` when neither the ledger nor the marks could give it. */
  gross: number | null;
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

/** Where one bot's starting pot came from. The screen prints `source_label` verbatim. */
export interface SeedSleeve {
  sleeve: string;
  state: string | null;
  /** `simulated` | `demo` | `live` — which kind of money this pot is. */
  basis: string;
  seed_usdt: number | null;
  /** `run` | `mode_file` | `config` | `account` | `account_live` | `none`. */
  source: string;
  /** "recorded at run start" / "configured for the next run" / "live demo account balance". */
  source_label: string;
  run_id: string | null;
  as_of_utc: string | null;
}

/**
 * "What did I put in", resolved per mode.
 *
 * `total_usdt` is `null` when the two bots are on different kinds of money (`mixed`): a
 * simulated pot and a demo pot are never added together, so there is no single total to
 * print and the card says which two things it is refusing to add.
 */
export interface SeedPanel {
  basis: string;
  /** "Simulated starting pot" / "Demo account" / "Real account". */
  label: string;
  total_usdt: number | null;
  source: string;
  source_label: string;
  as_of_utc: string | null;
  mixed: boolean;
  per_sleeve: boolean;
  sleeves: SeedSleeve[];
  note: string | null;
}

export interface DemoHolding {
  asset: string;
  amount: number;
  mark_usdt: number | null;
  value_usdt: number | null;
  /** Stablecoins are the cash half of the account, not a holding. */
  stable: boolean;
}

/**
 * The Binance Spot Demo account, read-only.
 *
 * `state` is `ok` | `not_configured` | `unreachable` | `refused`. Anything other than `ok`
 * means the numbers are missing or old (`stale`), never that they are zero.
 */
export interface DemoPanel {
  configured: boolean;
  /** True when a bot is actually running on demo — then this drives the money cards. */
  active: boolean;
  state: string;
  host?: string;
  key_env?: string;
  balance_reset?: string;
  account_type?: string | null;
  permissions?: string[];
  can_trade?: boolean;
  can_withdraw?: boolean;
  balances?: Record<string, number>;
  holdings?: DemoHolding[];
  /** Stablecoins at par. */
  cash_usdt?: number | null;
  /** The whole account; `null` the moment one holding could not be marked. */
  value_usdt?: number | null;
  unpriced?: string[];
  open_order_count?: number;
  open_orders?: Array<Record<string, unknown>>;
  /** Anything ever placed there: a resting order, or a journalled demo order or fill. */
  traded_here?: boolean;
  fills_recorded?: number;
  orders_recorded?: number;
  as_of_utc?: string;
  stale?: boolean;
  cached?: boolean;
  age_s?: number;
  error?: string | null;
  /** Only on `/overview/demo`: the venue's own order-sizing rules per pair. */
  filters?: {
    state: string;
    host?: string;
    error?: string | null;
    pairs?: Record<string, Record<string, unknown>>;
    not_tradable?: string[];
  };
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
  /** What was put in, and where that number came from. */
  seed?: SeedPanel;
  /** What it is worth, counted across every bot restart. Preferred over `nav` on Home. */
  pot?: PotPanel;
  /** The demo account: one quiet line when it is idle, the money source when it is live. */
  demo?: DemoPanel;
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
  /**
   * One bot's book and its trades.
   *
   * Home needs what the ledger cannot give it: the average price paid for each holding,
   * and every fill.  Those live on `/portfolio/<bot>`, so Home reads them directly rather
   * than sending the operator to another screen for the two tables they asked for.  It is
   * a separate query per bot on purpose — a bot that is down must not blank the table for
   * the one that is up.
   */
  portfolio: (sleeve: string) => api.get<PortfolioPayload>(`/portfolio/${sleeve}`),
  /**
   * The demo account in full, including the venue's own order-sizing filters.
   *
   * Home does not call this — the bundle already carries the one line it shows. It is the
   * screen behind that line, and the place the filters are read before the first order.
   */
  demo: (refresh = false) => api.get<DemoPanel>('/overview/demo', { refresh }),
  /** The whole story behind one decision, for the reasoning pane. */
  run: (runId: string) => api.get<RunDetail>(`/runs/${encodeURIComponent(runId)}`),
  /** What the safety checks did, narrowed to the coin the open row is about. */
  gateDecisions: (params: { sleeve?: string; pair?: string; limit?: number }) =>
    api.get<GateDecisionsPayload>('/risk/gate-decisions', params),
};

export const overviewKeys = {
  bundle: ['overview'] as const,
  nav: (days: number) => ['overview', 'nav', days] as const,
  portfolio: (sleeve: string) => ['overview', 'portfolio', sleeve] as const,
  run: (runId: string) => ['overview', 'run', runId] as const,
  demo: ['overview', 'demo'] as const,
  gate: (sleeve: string, pair: string) => ['overview', 'gate', sleeve, pair] as const,
};

/** The bots Home adds up. `benchmark` is a yardstick, not a bot, so it is not one. */
export const HOME_SLEEVES = ['a', 'b'] as const;
