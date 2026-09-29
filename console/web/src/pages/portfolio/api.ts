/**
 * Typed wrappers over `/api/portfolio/*` (spec 5.2), mirroring
 * `console/services/portfolio_service.py`.
 *
 * The ledger figures and the exchange figures are separate fields on purpose: the whole
 * point of the Portfolio page is that you can see them disagree.
 */
import type { ApiClient } from '@/api';

export type Sleeve = 'a' | 'b';

export interface TpRung {
  index: number;
  at_profit_pct: number;
  sell_fraction: number;
}

export interface Position {
  pair: string;
  amount: number;
  avg_entry: number;
  mark: number;
  value_usdt: number;
  upnl_usdt: number;
  upnl_pct: number;
  weight: number;
  weight_cap: number;
  entries_used: number;
  entries_max: number;
  stop_from_open: number;
  stop_price: number;
  trailing_active: boolean;
  tp_rungs_fired: number[];
  next_tp_rung: TpRung | null;
  mode: string;
  sim: boolean;
}

export interface OrderRow {
  id: number;
  ts_utc: string;
  sleeve: string;
  pair: string;
  side: string;
  order_type: string;
  ft_trade_id: number | null;
  ft_order_id: string | null;
  amount: number | null;
  price: number | null;
  status: string;
  mode: string | null;
  run_id: string | null;
}

export interface FillRow {
  id: number;
  ts_utc: string;
  sleeve: string;
  pair: string;
  side: string;
  fill_amount: number;
  fill_price: number;
  fee_amount: number | null;
  fee_currency: string | null;
  quote_bid: number | null;
  quote_ask: number | null;
  mode: string | null;
  run_id: string | null;
  /** The bot's own order id — the exact link to its exit reason and run database. */
  ft_order_id?: string | null;
  /**
   * What the bot's database says this fill was: the enter tag on a buy (`dca`,
   * `proposal`, `fast_breakout`) or the exit reason on a sell (`force_exit`,
   * `target_zero`, `trailing_stop_loss`, `partial_exit`, `exit_signal`, `roi`).
   * `null` when no bot database knows the order.
   */
  reason?: string | null;
  /** `human:console` for a hand flatten, `bot` for the strategy, `unknown` when a
   *  `force_exit` has no audit row behind it, `null` when the fill is unmatched. */
  actor?: string | null;
  /** `flatten` for a hand flatten; the `targets:*` refusal behind a `target_zero`. */
  cause?: string | null;
  /** The run database the order lives in (`tradesv3`, `test-a-000`). */
  run?: string | null;
}

/** One run database: a boundary the pot counts across. */
export interface PotRun {
  run: string;
  db: string;
  strategy: string | null;
  started_utc: string | null;
  ended_utc: string | null;
  /** The database the bot is running on now. */
  current: boolean;
  closed_trades: number;
  open_trades: number;
  /** Closed trades, net of fees. */
  realised_usdt: number;
  fees_usdt: number;
  /** `realised + fees` — before fees. */
  gross_usdt: number;
  error: string | null;
}

export interface PotOpenTrade {
  trade_id: number;
  pair: string;
  amount: number;
  open_rate: number;
  stake_usdt: number;
  opened_utc: string | null;
  mark: number | null;
  /** `bot` | `candle` | `daily:<source>` | `unpriced`. */
  mark_source: string;
  unrealised_usdt: number | null;
}

/**
 * One bot's cumulative pot (`console/services/pot_service.py`).
 *
 * Three numbers, three definitions: `cumulative_net_usdt` is what the seed is worth now
 * counted across every restart; `realised_current_run_usdt` is closed trades since the bot
 * started on its current database (the number that resets); `open_mark_usdt` is the open
 * book marked to market. `ledger_nav_usdt` is the old 15-minute ledger beside it, and
 * `ledger_gap_usdt` the distance between the two.
 */
export interface SleevePot {
  sleeve: string;
  seed_usdt: number | null;
  seed_source: string;
  cumulative_net_usdt: number | null;
  gain_usdt: number | null;
  realised_all_runs_usdt: number;
  realised_current_run_usdt: number;
  realised_earlier_runs_usdt: number;
  open_mark_usdt: number;
  open_value_usdt: number;
  fees_usdt: number;
  gross_usdt: number;
  fully_priced: boolean;
  unpriced: string[];
  runs: PotRun[];
  restarts: number;
  current_run: string | null;
  open: PotOpenTrade[];
  ledger_nav_usdt: number | null;
  ledger_as_of_utc: string | null;
  ledger_gap_usdt: number | null;
}

export interface NavPoint {
  ts_utc: string;
  nav_usdt: number;
  cash_usdt: number | null;
  reserved_usdt: number | null;
  btc_price: number | null;
  mode: string | null;
  run_id: string | null;
}

export interface Wallet {
  sleeve: string;
  ledger: { nav: number; cash: number; reserved: number; positions: number; free_usdt: number };
  exchange: { total: number; currencies: Array<Record<string, unknown>> };
  reconcile: {
    delta_usdt: number;
    tolerance_usdt: number;
    dust_usdt: number;
    mismatch: boolean;
    block_on_mismatch: boolean;
  };
}

export interface PortfolioPayload {
  sleeve: string;
  bot_up: boolean;
  positions: Position[];
  orders: OrderRow[];
  fills: FillRow[];
  wallet: Wallet;
  /** The cumulative pot; absent from a server that has not been restarted yet. */
  pot?: SleevePot | null;
}

export function portfolioApi(client: ApiClient) {
  return {
    load: (sleeve: Sleeve) => client.get<PortfolioPayload>(`/portfolio/${sleeve}`),
    orders: (sleeve: Sleeve, params: { pair?: string; since?: string; limit?: number } = {}) =>
      client.get<{ rows: OrderRow[] }>(`/portfolio/${sleeve}/orders`, params),
    fills: (sleeve: Sleeve, params: { pair?: string; since?: string; limit?: number } = {}) =>
      client.get<{ rows: FillRow[] }>(`/portfolio/${sleeve}/fills`, params),
    nav: (sleeve: Sleeve, params: { run_id?: string; since?: string } = {}) =>
      client.get<{ points: NavPoint[] }>(`/portfolio/${sleeve}/nav`, params),
    /** Step-up protected: it changes what the exchange is holding. */
    cancelOrder: (sleeve: Sleeve, tradeId: number) =>
      client.post<{ ok: boolean }>(`/portfolio/${sleeve}/orders/${tradeId}/cancel`),
  };
}

export type PortfolioApi = ReturnType<typeof portfolioApi>;
