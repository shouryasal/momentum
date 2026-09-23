/**
 * Typed wrappers over the endpoints the Charts page reads (spec 12 page 6).
 *
 * Nothing here invents a route: every call goes to an endpoint that already exists in
 * `docs/contracts.md` §9.6 —
 *
 *   `/market/pairs`     the universe and the ingested timeframes
 *   `/market/candles`   OHLCV from the same feather store the containers read, so a chart
 *                       and a backtest are looking at identical bars (it does not resample)
 *   `/market/markers`   journal rows: fills (with a SIM flag outside live), gate rejects,
 *                       stop/TP exits, and the proposal target steps
 *   `/signals`          the signal list, whose `status` IS the validator verdict once the
 *                       validator has run (valid / invalid / uncertain / …)
 *   `/risk/flags`       `knowledge/flags.json`; a `block_entries` flag is the blackout the
 *                       gate enforces, and its `set_at`/`expires_at` give the window
 *   `/knowledge/state`  `state_snapshots`, whose `regime` column drives the regime shading
 *
 * The last three are *context*: the page still draws candles and fills when they fail (a
 * fresh checkout has no journal), so they are fetched separately and degrade to a note.
 */
import type { ApiClient } from '@/api';

export interface Candle {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export type MarkerKind =
  | 'fill'
  | 'gate_reject'
  | 'signal'
  | 'proposal'
  | 'stop'
  | 'take_profit';

export interface Marker {
  kind: MarkerKind;
  ts: string;
  label: string;
  side?: string;
  price?: number;
  amount?: number;
  mode?: string;
  sim?: boolean;
  severity?: string;
}

export interface ProposalMarker {
  kind: 'proposal';
  ts: string;
  run_id: string;
  module: string;
  targets: Record<string, number>;
  abstain: boolean;
}

export interface PairsPayload {
  pairs: string[];
  quote: string;
  timeframes: string[];
}

export interface CandlesPayload {
  pair: string;
  timeframe: string;
  candles: Candle[];
}

export interface MarkersPayload {
  pair?: string;
  markers: Marker[];
  proposals: ProposalMarker[];
  kinds: MarkerKind[];
}

/** `signals.status` — the CHECK list in `ops/sql/journal.sql`. */
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

export interface SignalRow {
  signal_id: string;
  ts_utc: string;
  detector: string;
  pair: string | null;
  direction: string | null;
  status: SignalStatus | string;
  status_reason?: string | null;
  detector_score?: number | null;
  screen_score?: number | null;
  strength?: number | null;
  fast_path?: boolean;
}

export interface RiskFlag {
  active?: boolean;
  severity?: string;
  scope?: string;
  reason?: string;
  set_by?: string;
  set_at?: string;
  expires_at?: string | null;
}

export interface FlagsPayload {
  path?: string;
  ok?: boolean;
  error?: string;
  updated_at?: string;
  flags?: Record<string, RiskFlag>;
}

export interface RegimeSnapshot {
  id?: number;
  ts_utc: string;
  asof_candle_utc?: string | null;
  regime?: string | null;
  producer?: string | null;
}

export interface StatePayload {
  latest?: Record<string, unknown> | null;
  path?: string;
  snapshots?: RegimeSnapshot[];
}

export function marketApi(client: ApiClient) {
  return {
    pairs: () => client.get<PairsPayload>('/market/pairs'),

    candles: (pair: string, timeframe: string, limit = 500) =>
      client.get<CandlesPayload>('/market/candles', { pair, timeframe, limit }),

    markers: (pair: string, sleeve?: string, since?: string, limit = 2000) =>
      client.get<MarkersPayload>('/market/markers', {
        pair,
        limit,
        ...(sleeve ? { sleeve } : {}),
        ...(since ? { since } : {}),
      }),

    /** Signals for one pair. `status` carries the validator verdict once it has run. */
    signals: (pair: string, sinceHours?: number, limit = 300) =>
      client.get<{ signals: SignalRow[] }>('/signals', {
        pair,
        limit,
        ...(sinceHours ? { since_hours: sinceHours } : {}),
      }),

    /** `knowledge/flags.json` — blackout windows come from the `block_entries` entries. */
    flags: () => client.get<FlagsPayload>('/risk/flags'),

    /** `state_snapshots`, newest first — the regime history the shading is drawn from. */
    regime: (history = 200) => client.get<StatePayload>('/knowledge/state', { history }),
  };
}

export type MarketApi = ReturnType<typeof marketApi>;
