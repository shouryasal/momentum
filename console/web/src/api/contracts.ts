/**
 * Hand-written mirror of the pydantic DTOs in `console/contracts.py` (spec 5.2).
 *
 * There is no code generation step: `openapi-typescript` needs a running backend, and the
 * shell must typecheck on its own.  Instead every mirrored model is also described at
 * runtime in {@link PY_CONTRACT_MODELS}, and `src/test/contracts.drift.test.ts` parses
 * `console/contracts.py` and fails when a mirrored model's field names drift.
 *
 * Rules for the mirror:
 *   - A model in {@link PY_CONTRACT_MODELS} must exist in `console/contracts.py` with
 *     exactly these fields.
 *   - `console/contracts.py` may contain models that are NOT mirrored here.
 *   - A type in the last section still has no python DTO (the endpoint belongs to another
 *     package, or the shape is derived in the browser).  Those are deliberately absent from
 *     the drift registry; add them to {@link PY_CONTRACT_MODELS} once the DTO exists —
 *     a name in the registry with no python class fails the drift test, and a python class
 *     with no registry entry fails it too.
 *   - Field names are snake_case on both sides — the API speaks python field names.
 */

/* ------------------------------------------------------------------ primitives */

/** Sleeve identifiers are lowercase everywhere (CLAUDE.md conventions). */
export type SleeveId = 'a' | 'b';

/** UTC ISO-8601 with a trailing `Z`. */
export type IsoUtc = string;

/** `run_id` format: `2026-09-22T08:30+04:00` (Gulf offset). */
export type RunId = string;

/** `TEST | ARMING | LIVE_PROPOSE | LIVE_EXECUTE | DISARMING` (mode state machine, spec 8). */
export type ModeStateName = 'TEST' | 'ARMING' | 'LIVE_PROPOSE' | 'LIVE_EXECUTE' | 'DISARMING';
export type CheckStatus = 'ok' | 'warn' | 'fail' | 'unknown';
export type AlertSeverity = 'info' | 'warning' | 'critical';

export const ERROR_CODES = [
  'unauthorized',
  'forbidden',
  'step_up_required',
  'csrf_failed',
  'bad_origin',
  'bad_host',
  'rate_limited',
  'automated_run',
  'not_found',
  'conflict',
  'locked',
  'unavailable',
  'invalid',
  'failed',
] as const;

export type ErrorCode = (typeof ERROR_CODES)[number];

/* ------------------------------------------------------------------ error shape */

export interface ErrorDetail {
  code: string;
  message: string;
  detail: Record<string, unknown> | null;
}

export interface ErrorResponse {
  error: ErrorDetail;
}

export interface Ok {
  ok: boolean;
}

/* ------------------------------------------------------------------ auth */

export interface LoginRequest {
  token: string;
}

export interface LoginResponse {
  csrf: string;
  expires: IsoUtc;
  actor: string;
}

export interface AuthState {
  authenticated: boolean;
  actor: string | null;
  csrf: string | null;
  expires: IsoUtc | null;
  step_up_until: IsoUtc | null;
}

export interface StepUpRequest {
  token: string;
}

export interface StepUpResponse {
  step_up_until: IsoUtc;
}

export interface RotateTokenResponse {
  url: string;
  token_path: string;
  rotated_at: IsoUtc;
}

/* ------------------------------------------------------------------ health / meta */

export interface HealthResponse {
  ok: boolean;
  version: string;
  ts: IsoUtc;
}

export interface GitInfo {
  available: boolean;
  branch: string | null;
  commit: string | null;
  short: string | null;
  dirty: boolean;
  error: string | null;
}

export interface SleeveMode {
  sleeve: string;
  state: ModeStateName | string;
  submode: string | null;
  run_id: RunId | null;
  seed_usdt: number | null;
  is_live: boolean;
  /** Start of the active run (`sleeve_runs.started_utc`); optional until a route fills it. */
  since?: IsoUtc | null;
  /** 1-based day of the run, for the `A: TEST · seed 10,000 · day 12` header badge. */
  day?: number | null;
}

export interface KillState {
  engaged: boolean;
  reason: string | null;
  since: IsoUtc | null;
  path: string;
}

export interface BlessState {
  ok: boolean;
  reason: string;
  changed: string[];
  missing: string[];
  blessed_at: IsoUtc | null;
  blessed_by: string | null;
}

export interface Invariant {
  key: string;
  title: string;
  enforced_by: string;
  status: CheckStatus;
  detail: string | null;
}

export interface MetaResponse {
  version: string;
  schema_version: number;
  config_version: number;
  started_at: IsoUtc;
  now: IsoUtc;
  port: number;
  host: string;
  automated_run: boolean;
  git: GitInfo;
  modes: SleeveMode[];
  mode_verified: boolean;
  mode_reason: string;
  kill: KillState;
  bless: BlessState;
  invariants: Invariant[];
}

/* ------------------------------------------------------------------ kill */

export interface KillRequest {
  reason: string;
  flatten: boolean;
}

export interface BotActionResult {
  sleeve: string;
  ok: boolean;
  detail: string;
}

export interface KillResponse {
  engaged: boolean;
  reason: string;
  ts: IsoUtc;
  bots: BotActionResult[];
}

export interface ResumeRequest {
  confirm_phrase: string;
}

/* ------------------------------------------------------------------ schema meta */

export interface FieldMeta {
  path: string;
  title: string | null;
  type: string | null;
  kind: 'leaf' | 'object' | 'array' | 'map';
  description: string | null;
  tier: string | null;
  group: string | null;
  unit: string | null;
  widget: string | null;
  effects: string[];
  protected: boolean;
  deprecated: boolean;
  help_md: string | null;
  enum: unknown[] | null;
  default: unknown;
  nullable: boolean;
  required: boolean;
  constraints: Record<string, unknown>;
}

export interface SchemaMetaResponse {
  config_id: string;
  fields: FieldMeta[];
  groups: Record<string, string[]>;
}

/* ------------------------------------------------------------------ jobs / SSE */

export interface JobState {
  id: string;
  job: string;
  status: 'queued' | 'running' | 'ok' | 'failed' | 'cancelled';
  started_at: IsoUtc | null;
  finished_at: IsoUtc | null;
  progress: number;
  message: string | null;
  error: string | null;
  actor: string | null;
}

export interface JobList {
  jobs: JobState[];
}

export interface StreamEvent<T = Record<string, unknown>> {
  topic: string;
  id: string;
  ts: IsoUtc;
  payload: T;
}

/** Spec 5.3 topic list.  `log:<name>` is dynamic and therefore not enumerated here. */
export const SSE_TOPICS = [
  'alert',
  'health',
  'kill',
  'mode',
  'transition',
  'bot',
  'nav',
  'order',
  'fill',
  'gate',
  'signal',
  'validation',
  'run',
  'proposal',
  'approval',
  'provider_switch',
  'config',
  'change',
  'job',
  'reconcile',
  'backtest',
  'claude_auth',
] as const;

export type SseTopic = (typeof SSE_TOPICS)[number] | `log:${string}`;

/* --------------------------------- later endpoints, and the views derived client-side */

/** `alert` topic payload; the backend publishes it, there is no DTO for it yet. */
export interface AlertPayload {
  severity: AlertSeverity;
  title: string;
  message: string;
  source?: string | null;
  key?: string | null;
}

/** `GET /api/ops/health` (P1 owns `console/routers/ops.py`). */
export interface HealthSnapshot {
  as_of: IsoUtc;
  freshness_minutes: Record<string, number | null>;
  data_age_minutes: number | null;
  staleness_limit_min: number;
  open_incidents: number;
  undelivered_alerts: number | null;
}

/** Header health dot view, derived client-side from {@link HealthSnapshot}. */
export interface HealthCheck {
  key: string;
  label: string;
  status: CheckStatus;
  detail: string | null;
}

export interface HealthSummary {
  status: CheckStatus;
  checks: HealthCheck[];
  ts: IsoUtc;
}

/**
 * `GET /api/approvals/pending` — `console.contracts.ApprovalsPending`.
 *
 * There is no `count`: the header counter is `items.length`, and an item is only in the
 * list while it is still undecided and unexpired (the router filters server-side).
 */
export interface ApprovalItem {
  run_id: RunId;
  ts_utc: IsoUtc;
  valid: boolean;
  abstain: boolean;
  status: string;
  decision?: string | null;
  decided_utc?: IsoUtc | null;
  actor?: string | null;
  channel?: string | null;
  note?: string | null;
  applied?: boolean;
  expires_utc: IsoUtc;
  seconds_left: number;
}

export interface ApprovalsPending {
  /** False when neither sleeve requires approval — the counter then stays at zero. */
  requires_approval: boolean;
  ttl_hours: number;
  items: ApprovalItem[];
}

/**
 * `GET /api/search?q=` — the Ctrl-K index, `console.contracts.SearchHit`
 * (`console/services/search_service.Hit` is what fills it).
 * The target path is `route`, not `path`; `results`, not `hits`.
 */
export interface SearchHit {
  kind: string;
  id: string;
  title: string;
  subtitle: string;
  route: string;
  group: string;
  score: number;
}

export interface SearchResponse {
  query: string;
  count: number;
  results: SearchHit[];
  /** Matches before `limit` was applied; absent for an empty query. */
  total_matches?: number | null;
  by_kind?: Record<string, number> | null;
  total_indexed?: number | null;
}

/**
 * `GET /api/invariants/strip` — the header safety strip, `console.contracts`.
 * Keys are the pill names of spec 12, values the worst status behind each pill.
 */
export interface SafetyStripResponse {
  strip: Record<string, CheckStatus>;
  ok: boolean;
  counts: Partial<Record<CheckStatus, number>>;
}

/** `claude_auth.CLAUDE_PROVIDER_KEYS` plus the local runner. */
export type ProviderKey = 'claude:subscription' | 'claude:api_key' | 'ollama';
export type CircuitState = 'closed' | 'open' | 'half_open';
export type AuthMode = 'subscription' | 'api_key' | 'auto';

/**
 * `GET /api/llm/providers` — `console.contracts.ProviderCard`.
 *
 * A card carries a credential *flag* and a breaker, not a status word; {@link providerStatus}
 * derives the traffic light the header chip shows.  `credential_present` is presence only:
 * no console response ever carries a key, a token or a password.
 */
export interface ProviderCard {
  key: ProviderKey;
  kind: 'claude_sdk' | 'ollama';
  enabled: boolean;
  credential_present: boolean;
  auth_source: string;
  detail: string;
  base_url?: string | null;
  circuit: CircuitState;
  consecutive_failures: number;
  open_until: IsoUtc | null;
  last_ok_utc: IsoUtc | null;
  last_error: string | null;
  /** Set while a subscription rate limit is being backed off from. */
  degraded_until: IsoUtc | null;
}

/** `ops_state` rate-limit signal; `utilization` is a fraction of the window. */
export interface RateLimitState {
  status: string | null;
  utilization: number | null;
  resets_at: IsoUtc | null;
}

export interface MonthTotals {
  month: string;
  total_usd: number;
  by_provider: Record<string, number>;
}

export interface ProvidersResponse {
  auth_mode: AuthMode;
  providers: ProviderCard[];
  rate_limit: RateLimitState;
  month: MonthTotals;
}

/** Traffic light for one provider card: a disabled provider is not a failure. */
export function providerStatus(card: ProviderCard): CheckStatus {
  if (!card.enabled) return 'unknown';
  if (card.circuit === 'open') return 'fail';
  if (!card.credential_present) return 'fail';
  if (card.circuit === 'half_open' || (card.consecutive_failures ?? 0) > 0) return 'warn';
  return 'ok';
}

/* ------------------------------------------------------------- profit & gap ledger */

/** One ledger line: a number, its unit, and the query that produced it. */
export interface ProfitGapsLine {
  key: string;
  label: string;
  value: number | null;
  unit: string;
  query: string;
  note: string | null;
}

/** One way the mechanism forwent or wasted money for a reason that is not the strategy. */
export interface ProfitGapsGap {
  key: string;
  title: string;
  /** USDT where realised, hours or a count where not — `unit` says which. */
  size: number;
  unit: string;
  /** 0-100, comparable across gaps: how much of the window or the money this took. */
  severity: number;
  cause: string;
  /** The sentence a non-engineer reads on Home; empty when the gap did not occur. */
  sentence: string;
  lines: ProfitGapsLine[];
  detail: Record<string, unknown>;
  error: string | null;
  /** How much of the system the gap touches, 0-1; the ranking is `severity × weight`. */
  weight: number;
  score: number;
}

export interface ProfitGapsTop {
  key: string;
  title: string;
  sentence: string;
  size: number;
  unit: string;
  severity: number;
  weight: number;
  score: number;
}

export interface ProfitGapsExpected {
  profile: string;
  source: string;
  expected_per_30d_pct: number | null;
  expected_this_window_pct: number | null;
  planned_max_drawdown_pct: number | null;
  /** The sentence that says days of results cannot confirm or refute the expectation. */
  note: string;
  lines: ProfitGapsLine[];
}

export interface ProfitGapsRealised {
  seed_total_usdt: number | null;
  cumulative_net_usdt: number | null;
  realised_net_usdt: number | null;
  gross_usdt: number | null;
  fees_usdt: number | null;
  fee_gross_ratio: number | null;
  trades: number;
  wins: number;
  win_rate: number | null;
  exit_reasons: Record<string, { trades: number; net_usdt: number; wins: number }>;
  open_mark_usdt: number | null;
  benchmark: {
    btc_hold_usdt: number | null;
    btc_hold_pct: number | null;
    basket_pairs: number;
    basket_hold_usdt: number | null;
    basket_hold_pct: number | null;
    cost_per_side: number;
    query: string;
  };
  per_sleeve: Record<string, Record<string, unknown>>;
  /** The sentence beside each of the three numbers, keyed by field name. */
  definitions: Record<string, string>;
  lines: ProfitGapsLine[];
}

/** One window of the ledger, as `runs.profit_gaps.Ledger` serialises it. */
export interface ProfitGapsLedger {
  window: { key: string; since_utc: string; until_utc: string; hours: number };
  expected: ProfitGapsExpected;
  realised: ProfitGapsRealised;
  gaps: ProfitGapsGap[];
  top_three: ProfitGapsTop[];
  errors: string[];
}

export type ProfitGapsWindowKey = 'last_24h' | 'since_start';

/** `GET /profit-gaps` — mirrors `console.contracts.ProfitGapsResponse`. */
export interface ProfitGapsResponse {
  generated_utc: string;
  profile: string | null;
  windows: Partial<Record<ProfitGapsWindowKey, ProfitGapsLedger>>;
  cached: boolean;
  error: string | null;
}

/* ------------------------------------------------------------------ drift registry */

/**
 * `python class name -> field names`, checked by `src/test/contracts.drift.test.ts`
 * against `console/contracts.py`.  Only models that exist there belong here.
 */
export const PY_CONTRACT_MODELS: Readonly<Record<string, readonly string[]>> = {
  ErrorDetail: ['code', 'message', 'detail'],
  ErrorResponse: ['error'],
  Ok: ['ok'],
  LoginRequest: ['token'],
  LoginResponse: ['csrf', 'expires', 'actor'],
  AuthState: ['authenticated', 'actor', 'csrf', 'expires', 'step_up_until'],
  StepUpRequest: ['token'],
  StepUpResponse: ['step_up_until'],
  RotateTokenResponse: ['url', 'token_path', 'rotated_at'],
  HealthResponse: ['ok', 'version', 'ts'],
  GitInfo: ['available', 'branch', 'commit', 'short', 'dirty', 'error'],
  SleeveMode: ['sleeve', 'state', 'submode', 'run_id', 'seed_usdt', 'is_live', 'since', 'day'],
  KillState: ['engaged', 'reason', 'since', 'path'],
  BlessState: ['ok', 'reason', 'changed', 'missing', 'blessed_at', 'blessed_by'],
  Invariant: ['key', 'title', 'enforced_by', 'status', 'detail'],
  MetaResponse: [
    'version',
    'schema_version',
    'config_version',
    'started_at',
    'now',
    'port',
    'host',
    'automated_run',
    'git',
    'modes',
    'mode_verified',
    'mode_reason',
    'kill',
    'bless',
    'invariants',
  ],
  KillRequest: ['reason', 'flatten'],
  BotActionResult: ['sleeve', 'ok', 'detail'],
  KillResponse: ['engaged', 'reason', 'ts', 'bots'],
  ResumeRequest: ['confirm_phrase'],
  FieldMeta: [
    'path',
    'title',
    'type',
    'kind',
    'description',
    'tier',
    'group',
    'unit',
    'widget',
    'effects',
    'protected',
    'deprecated',
    'help_md',
    'enum',
    'default',
    'nullable',
    'required',
    'constraints',
  ],
  SchemaMetaResponse: ['config_id', 'fields', 'groups'],
  JobState: [
    'id',
    'job',
    'status',
    'started_at',
    'finished_at',
    'progress',
    'message',
    'error',
    'actor',
  ],
  JobList: ['jobs'],
  StreamEvent: ['topic', 'id', 'ts', 'payload'],
  ApprovalItem: [
    'run_id',
    'ts_utc',
    'valid',
    'abstain',
    'status',
    'decision',
    'decided_utc',
    'actor',
    'channel',
    'note',
    'applied',
    'expires_utc',
    'seconds_left',
  ],
  ApprovalsPending: ['requires_approval', 'ttl_hours', 'items'],
  SearchHit: ['kind', 'id', 'title', 'subtitle', 'route', 'group', 'score'],
  SearchResponse: ['query', 'count', 'results', 'total_matches', 'by_kind', 'total_indexed'],
  SafetyStripResponse: ['strip', 'ok', 'counts'],
  ProviderCard: [
    'key',
    'kind',
    'enabled',
    'credential_present',
    'auth_source',
    'detail',
    'base_url',
    'circuit',
    'consecutive_failures',
    'open_until',
    'last_ok_utc',
    'last_error',
    'degraded_until',
  ],
  RateLimitState: ['status', 'utilization', 'resets_at'],
  MonthTotals: ['month', 'total_usd', 'by_provider'],
  ProvidersResponse: ['auth_mode', 'providers', 'rate_limit', 'month'],
  ProfitGapsResponse: ['generated_utc', 'profile', 'windows', 'cached', 'error'],
} as const;
