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
