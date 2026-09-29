/**
 * Transport for the control surface and the preview.
 *
 * The owner asked "is there a button on the UI that starts the autonomous running?". There
 * was not. `console/routers/autonomy.py` (mounted at `/api/control`) is the answer's engine
 * and this module is the typed view of it; `ControlCard.tsx` and `Preview.tsx` are the seat.
 *
 * Two independent ideas travel in one payload and must never be collapsed into one control:
 *
 *  - **whose money** — `modes[bot].state`, the signed mode machine: simulated, the exchange
 *    practice account, or real;
 *  - **how much it does by itself** — `bots[bot].level`: off, watching, proposing, trading.
 *
 * And a third thing that is neither: `verdict`, which says whether anything is actually
 * running. A bot can be switched on with nothing installed to run it, which was this host's
 * real state, so the verdict is the field the screen leads with.
 */

import { api } from '@/api';

/** off → watching → proposing → trading. The server sends the list; this is the order. */
export type ControlLevel = 'off' | 'watching' | 'proposing' | 'trading';

export const LEVEL_ORDER: ControlLevel[] = ['off', 'watching', 'proposing', 'trading'];

/**
 * One bot's level after every clamp that can only ever lower it.
 *
 * `requested` is what the signed state says; `level` is what actually applies once the
 * settings ceiling, the master switch and the kill switch have had their say. The screen
 * shows `level` and explains any gap with `clamped_by`, because a control that silently
 * displays a level the machine is not honouring is worse than no control.
 */
export interface BotView {
  bot: string;
  requested: ControlLevel;
  level: ControlLevel;
  ceiling: ControlLevel;
  clamped_by: string[];
  since: string | null;
  set_by: string | null;
  reason: string | null;
  /** Where a pause found it, so Start can put it back exactly. */
  resume_level: ControlLevel | null;
  decides: boolean;
  executes: boolean;
}

export interface BotSpend {
  bot: string;
  day_usd: number;
  month_usd: number;
  day_cap: number | null;
  month_cap: number | null;
  day_pct: number | null;
  month_pct: number | null;
  over_day: boolean;
  over_month: boolean;
  at_cap: string;
  /** `ok` | `degrade` | `hold` — what a model call must do at the ceiling. */
  action: string;
}

export interface ScheduleStatus {
  installed: boolean;
  matches: boolean;
  lines: number;
  diff: string;
  error: string | null;
}

export interface JobLiveness {
  job: string;
  required: ControlLevel;
  permitted: boolean;
  /** ok | late | never | skipping | failing | idle. */
  verdict: string;
  last_ok: string | null;
  last_fail: string | null;
  last_skip: string | null;
  last_skip_reason: string | null;
  last_status: string | null;
  next_fire: string | null;
  prev_fire: string | null;
  lock_held: boolean;
  minutes_late: number | null;
  note: string | null;
}

export interface ModeView {
  state: string;
  verified: boolean;
  is_live: boolean;
  is_demo: boolean;
}

/**
 * `off` | `not_scheduled` | `schedule_drifted` | `never_ran` | `blocked` | `late` |
 * `failing` | `alive`.
 *
 * `not_scheduled` is switched on with nothing installed to run it. `blocked` is the one
 * added after 2026-09-24: installed, on schedule, every job green, and the risk gate
 * refusing every entry for fourteen hours behind a flag that could never expire. `alive`
 * was technically true the whole time. Neither of them may ever render as a tick.
 */
export type Verdict =
  | 'off'
  | 'not_scheduled'
  | 'schedule_drifted'
  | 'never_ran'
  | 'blocked'
  | 'late'
  | 'failing'
  | 'alive';

/* ---------------------------------------------------------------------------- outcomes */

/** One active flag that is blocking entries, with its age and its way out. */
export interface ActingFlag {
  name: string;
  severity: string;
  reason: string | null;
  set_by: string | null;
  set_at: string | null;
  expires_at: string | null;
  scope: string;
  active_minutes: number | null;
  /** False is the defect: a flag with no expiry can only be lifted by whatever set it. */
  can_expire: boolean;
  clears_when: string;
}

/** One ingest source, from the same freshness sidecar the risk gate reads. */
export interface ActingSource {
  source: string;
  age_minutes: number | null;
  age_minutes_allowed: number | null;
  stale: boolean;
}

/** One ingest phase. `funding` failing while `candles` was fine is what started all this. */
export interface ActingPhase {
  phase: string;
  last_ok: string | null;
  last_fail: string | null;
  last_error: string | null;
  minutes_since_ok: number | null;
  failing: boolean;
}

/** One refusal reason, counted, with the words a person reads instead of the slug. */
export interface ActingRefusal {
  reason: string;
  count: number;
  words: string;
  since: string | null;
}

/**
 * Did the system actually *do* anything.
 *
 * Every number here is an outcome. "Jobs ran on schedule" was true all night on
 * 2026-09-24; not one of these fields would have been.
 */
export interface Acting {
  as_of: string;
  window_hours: number;
  entries_allowed: number;
  entries_refused: number;
  exits_allowed: number;
  last_allowed_entry: string | null;
  minutes_since_allowed_entry: number | null;
  /** Newest-first run of refused entries that all share one reason. */
  streak: number;
  streak_reason: string | null;
  streak_since: string | null;
  refusals: ActingRefusal[];
  /**
   * The dominant refusal reason *since the last allowed entry*, and how many there have
   * been. `refusals` covers the whole window, which on a host that was wedged yesterday and
   * is fine today is still dominated by yesterday — so the screen leads with these.
   */
  current_reason: string | null;
  current_words: string | null;
  refused_since_last_allowed: number;
  blocking_flags: ActingFlag[];
  sources: ActingSource[];
  phases: ActingPhase[];
  /** `trading` | `quiet` | `not_trading` | `idle` | `unknown`. */
  verdict: string;
  headline: string;
  blocked_since: string | null;
  blocked_minutes: number | null;
  clears_when: string | null;
  blocked_what: string | null;
}

/** Is anything going to restart the console when it dies? On 2026-09-24, nothing was. */
export interface SupervisorStatus {
  unit: string;
  /** `supervised` | `failing` | `unsupervised` | `unknown`. Unknown is never an alarm. */
  verdict: string;
  note: string;
  scope: string | null;
  enabled: string | null;
  active: string | null;
  error: string | null;
  install_hint: string;
}

/** What installing the supervisor actually verified — never what it merely attempted. */
export interface UnitInstall {
  unit: string;
  scope: string;
  path?: string;
  installed: boolean;
  enabled: boolean;
  active: string;
  verified: boolean;
  lingering?: boolean;
  error?: string | null;
  linger_error?: string;
  sudo?: string;
}

export interface UnitInstallResult {
  verified: boolean;
  units: UnitInstall[];
  supervisor: SupervisorStatus;
}

export interface ControlPayload {
  verdict: Verdict;
  headline: string;
  as_of: string;
  timezone: string;
  schedule: ScheduleStatus;
  bots: Record<string, BotView>;
  spend: Record<string, BotSpend>;
  state: {
    verified: boolean;
    corroborated: boolean;
    trusted: boolean;
    reason: string;
    set_at: string | null;
    set_by: string | null;
    mirror_ahead: boolean;
  };
  kill_engaged: boolean;
  jobs: JobLiveness[];
  /**
   * The outcome half of the picture. Optional on the wire because an older server does not
   * send it, and the screen has to say "cannot tell" rather than imply health — the same
   * rule the whole card follows. `null` means the measurement itself failed; see
   * `acting_error`.
   */
  acting?: Acting | null;
  acting_error?: string | null;
  supervisor?: SupervisorStatus | null;
  modes: Record<string, ModeView>;
  levels: ControlLevel[];
  level_meaning: Record<string, string>;
  phrases: { flatten: string; arm_live_trading: string };
  job_requirements: Record<string, ControlLevel>;
}

/** What one move did, and everything it touched. */
export interface Transition {
  bot: string;
  action: string;
  before: ControlLevel;
  after: ControlLevel;
  schedule: ScheduleStatus | null;
  units: Array<Record<string, unknown>>;
  bots_told: Array<Record<string, unknown>>;
  note: string | null;
}

/* ------------------------------------------------------------------------------ preview */

export interface PreviewGateVerdict {
  pair: string;
  stake_usdt: number;
  allowed: boolean;
  /** `ok`, or the first failing check — `weight_cap:BTC/USDT`, `staleness`, … */
  reason: string;
  failed: string[];
}

export interface PreviewGate {
  available: boolean;
  reason?: string;
  sleeve?: string;
  nav_usdt?: number;
  nav_valid?: boolean;
  nav_reason?: string;
  kill_engaged?: boolean;
  verdicts: PreviewGateVerdict[];
  allowed: number;
  refused: number;
  writes_suppressed?: string[];
}

export interface PreviewPlan {
  module: string;
  /** True when it declined to trade because the inputs were stale or contradictory. */
  abstain: boolean;
  targets: Record<string, number>;
  exposure_scale: number;
  confidence: number;
  horizon_days: number;
  rationale: string[];
  invalidation: string;
}

export interface PreviewResult {
  preview: true;
  /** A real Gulf-time run id, because the plan schema demands one. */
  run_id: string;
  /** What marks this as a preview everywhere it travels. */
  preview_id: string;
  started_utc: string;
  finished_utc: string;
  requested_model: string;
  served_model: string | null;
  effort: string | null;
  escalation_reasons: string[];
  hard_case_flags: string[];
  cost_usd: number;
  prompt_version: string;
  wrote_nothing: true;
  ok: boolean;
  error: string | null;
  plan: PreviewPlan | null;
  gate: PreviewGate | null;
}

export interface PreviewBudget {
  max_usd: number;
  min_interval_s: number;
  wait_s: number;
  ready: boolean;
  last_started_utc: string | null;
  last_cost_usd: number | null;
}

export interface PreviewState {
  budget: PreviewBudget;
  last: (Partial<PreviewResult> & { status?: string }) | null;
  note: string;
}

/* ------------------------------------------------------------------------------ calls */

export interface LevelBody {
  bot: string;
  level: ControlLevel;
  reason?: string;
  confirm_phrase?: string;
  preflight_id?: string;
}

export interface StartBody {
  bot: string;
  level?: ControlLevel;
  reason?: string;
  confirm_phrase?: string;
  preflight_id?: string;
  enable_systemd?: boolean;
}

/**
 * Is this actually the control payload?
 *
 * A control that renders half a screen from a response it did not understand is the exact
 * failure this whole card exists to prevent: it would look like an answer. An older server,
 * a proxy that swallowed the path, or a partial response must all end at "cannot tell",
 * so the shape is checked before a single field is read.
 */
export function isControlPayload(value: unknown): value is ControlPayload {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<ControlPayload>;
  return (
    typeof candidate.verdict === 'string' &&
    typeof candidate.headline === 'string' &&
    Array.isArray(candidate.jobs) &&
    Boolean(candidate.bots) &&
    Boolean(candidate.schedule) &&
    Boolean(candidate.phrases)
  );
}

export const controlApi = {
  get: () => api.get<ControlPayload>('/control'),
  schedule: () => api.get<ScheduleStatus>('/control/schedule'),
  installSchedule: () => api.post<Record<string, unknown>>('/control/schedule'),
  acting: () => api.get<Acting>('/control/acting'),
  supervisor: () => api.get<SupervisorStatus>('/control/supervisor'),
  installUnits: () => api.post<UnitInstallResult>('/control/units'),
  start: (body: StartBody) => api.post<Transition>('/control/start', body),
  pause: (bot: string, reason?: string) =>
    api.post<Transition>('/control/pause', { bot, ...(reason ? { reason } : {}) }),
  stop: (bot: string, reason?: string) =>
    api.post<Transition>('/control/stop', { bot, ...(reason ? { reason } : {}) }),
  setLevel: (body: LevelBody) => api.put<Transition>('/control/level', body),
  flatten: (bot: string, confirmPhrase: string, reason?: string) =>
    api.post<Record<string, unknown>>('/control/flatten', {
      bot,
      confirm_phrase: confirmPhrase,
      ...(reason ? { reason } : {}),
    }),

  preview: () => api.get<PreviewState>('/preview'),
  runPreview: (sleeve: string) => api.post<PreviewResult>(`/preview?sleeve=${sleeve}`),
};

export const controlKeys = {
  control: ['control'] as const,
  preview: ['control', 'preview'] as const,
};

/** The bots the control shows. `benchmark` is a yardstick, not a bot. */
export const CONTROL_BOTS = ['a', 'b'] as const;
