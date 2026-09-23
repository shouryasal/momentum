/**
 * Typed wrappers over `/api/risk/*` (spec 5.2), mirroring
 * `console/services/risk_service.py`.
 *
 * Every number here is a *read* of what the deterministic gate enforces — the same
 * limits, the same anchors, the same counters the callbacks use. The one write is the
 * monthly resume, which the backend refuses without a human actor and a typed phrase.
 */
import type { ApiClient } from '@/api';

export type Sleeve = 'a' | 'b';
export type Severity = 'allow' | 'reject' | 'breach';
export type LimitUnit = 'fraction' | 'pct' | 'usdt' | 'minutes' | 'count';

export interface LimitRow {
  name: string;
  path: string;
  value: number | Record<string, number>;
  unit: LimitUnit;
}

export interface LimitsPayload {
  sleeve: string;
  checks: string[];
  limits: LimitRow[];
}

export interface Meter {
  used: number;
  limit: number;
  headroom: number;
  pct: number;
  /**
   * False when the meter divides by a NAV the console could not read. The bar must then
   * say "unknown" — 0% and a red USDT-floor breach are both lies, and this is the
   * operator's only view of proximity to a stop.
   */
  valid?: boolean;
}

export interface UtilisationPayload {
  sleeve: string;
  nav: number;
  nav_valid: boolean;
  /** `bot` | `ledger` | `caller` | `unavailable` — where NAV and the book came from. */
  nav_source: string;
  free_usdt: number;
  positions: Record<string, number>;
  nav_derived: string[];
  meters: Record<string, Meter>;
}

export interface Anchors {
  sleeve: string;
  monthly_locked: boolean;
  monthly_locked_month: string;
  monthly_resumed_utc: string;
  month_anchor_nav: number | null;
  month_anchor_month: string;
  day_anchor_nav: number | null;
  day_anchor_date: string;
  locked_until: string;
  monthly_loss_stop: number;
  daily_loss_stop: number;
  confirm_phrase: string;
}

export interface Mechanics {
  sleeve: string;
  timeframe: string;
  startup_candles: number;
  mode: string;
  run_id: string;
  trading: Record<string, unknown>;
  plan_bounds: Record<string, unknown>;
  config_path: string;
}

export interface GateDecision {
  id: number;
  ts_utc: string;
  sleeve: string;
  pair: string;
  side: string | null;
  intent: string;
  callback: string;
  allowed: boolean;
  reason: string;
  severity: Severity;
  checks: Record<string, boolean>;
  proposed_stake: number | null;
  nav: number | null;
  gross_exposure: number | null;
  run_id: string | null;
  action: string | null;
  trade_id: number | null;
}

export interface FlagEntry {
  active?: boolean;
  severity?: string;
  scope?: string;
  reason?: string;
  set_by?: string;
  set_at?: string;
  expires_at?: string | null;
}

export interface FlagsPayload {
  path: string;
  ok: boolean;
  error?: string;
  updated_at?: string;
  flags: Record<string, FlagEntry>;
}

export interface RiskOverview {
  sleeves: Record<string, { limits: LimitsPayload; anchors: Anchors; mechanics: Mechanics }>;
  navs: Record<string, number>;
  flags: FlagsPayload;
  kill: boolean;
}

export interface ResumeResult {
  sleeve: string;
  resumed: boolean;
  anchor_nav: number;
  anchor_month: string;
  resumed_utc: string;
  locks_deleted: number[];
  lock_errors: string[];
  nav_source: string;
  audit_id: number | null;
}

export interface GateDecisionsPayload {
  rows: GateDecision[];
  counts: Record<string, number>;
  checks: string[];
}

export function riskApi(client: ApiClient) {
  return {
    overview: () => client.get<RiskOverview>('/risk'),
    limits: (sleeve: Sleeve) => client.get<LimitsPayload>(`/risk/${sleeve}/limits`),
    anchors: (sleeve: Sleeve) => client.get<Anchors>(`/risk/${sleeve}/anchors`),
    mechanics: (sleeve: Sleeve) => client.get<Mechanics>(`/risk/${sleeve}/mechanics`),
    /**
     * NAV, the book and free USDT are read **server-side** from the bot (falling back to
     * the journal's last `nav_points` row). The page used to send `nav=0, free_usdt=0`
     * because it had no way to know them, which made every NAV-derived meter read 0% and
     * the USDT floor read as a permanent breach. `nav` stays available as a what-if.
     */
    utilisation: (sleeve: Sleeve, nav?: number, freeUsdt?: number) =>
      client.get<UtilisationPayload>(`/risk/${sleeve}/utilisation`, {
        ...(nav === undefined ? {} : { nav }),
        ...(freeUsdt === undefined ? {} : { free_usdt: freeUsdt }),
      }),
    gateDecisions: (params: {
      sleeve?: Sleeve;
      severity?: Severity;
      pair?: string;
      since?: string;
      limit?: number;
    }) => client.get<GateDecisionsPayload>('/risk/gate-decisions', params),
    flags: () => client.get<FlagsPayload>('/risk/flags'),
    /** Step-up protected; the phrase comes from `anchors.confirm_phrase`. */
    resumeMonthly: (sleeve: Sleeve, confirmPhrase: string) =>
      client.post<ResumeResult>(`/risk/${sleeve}/resume-monthly`, {
        confirm_phrase: confirmPhrase,
      }),
  };
}

export type RiskApi = ReturnType<typeof riskApi>;
