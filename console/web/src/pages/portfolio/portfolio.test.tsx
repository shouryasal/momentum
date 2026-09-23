import { screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ApiClient } from '@/api';
import { ApiProvider } from '@/app/ApiContext';
import { fakeFetch, renderWithProviders } from '@/test/utils';

import type { FillRow, PortfolioPayload, Position, Wallet } from './api';
import { FillsTable, slippageBps } from './components/ActivityTables';
import { PositionsTable } from './components/PositionsTable';
import { WalletPanel } from './components/WalletPanel';
import PortfolioPage from './index';

const POSITION: Position = {
  pair: 'BTC/USDT',
  amount: 0.05,
  avg_entry: 40000,
  mark: 50000,
  value_usdt: 2500,
  upnl_usdt: 500,
  upnl_pct: 0.25,
  weight: 0.25,
  weight_cap: 0.4,
  entries_used: 4,
  entries_max: 4,
  stop_from_open: -0.1,
  stop_price: 36000,
  trailing_active: false,
  tp_rungs_fired: [0],
  next_tp_rung: { index: 1, at_profit_pct: 0.2, sell_fraction: 0.25 },
  mode: 'test',
  sim: true,
};

const WALLET: Wallet = {
  sleeve: 'a',
  ledger: { nav: 10000, cash: 6500, reserved: 3500, positions: 0, free_usdt: 6500 },
  exchange: { total: 10000, currencies: [] },
  reconcile: { delta_usdt: 0, tolerance_usdt: 50, dust_usdt: 10, mismatch: false,
    block_on_mismatch: true },
};

const FILL: FillRow = {
  id: 1,
  ts_utc: '2026-09-22T08:00:00Z',
  sleeve: 'a',
  pair: 'BTC/USDT',
  side: 'buy',
  fill_amount: 0.01,
  fill_price: 50050,
  fee_amount: 0.5,
  fee_currency: 'USDT',
  quote_bid: 49900,
  quote_ask: 50000,
  mode: 'test',
  run_id: 'test-a-1',
};

const PAYLOAD: PortfolioPayload = {
  sleeve: 'a',
  bot_up: true,
  positions: [POSITION],
  orders: [],
  fills: [FILL],
  wallet: WALLET,
};

describe('PositionsTable', () => {
  it('flags a simulated position and a fully used entry budget', () => {
    renderWithProviders(<PositionsTable positions={[POSITION]} />);
    expect(screen.getByText('SIM')).toBeInTheDocument();
    expect(screen.getByText('4 / 4')).toBeInTheDocument();
    expect(screen.getByText('36000.00')).toBeInTheDocument();
    expect(screen.getByText(/tp2 @ 20.00%/)).toBeInTheDocument();
  });

  it('says so plainly when the sleeve is flat', () => {
    renderWithProviders(<PositionsTable positions={[]} />);
    expect(screen.getByText('No open positions')).toBeInTheDocument();
  });
});

describe('WalletPanel', () => {
  it('shows reserved USDT separately so ledger NAV is explainable', () => {
    renderWithProviders(<WalletPanel wallet={WALLET} />);
    expect(screen.getByText('3500.00 USDT')).toBeInTheDocument();
    expect(screen.getByText('reconciled')).toBeInTheDocument();
  });

  it('raises the alarm when the ledger and the exchange disagree', () => {
    renderWithProviders(
      <WalletPanel wallet={{ ...WALLET,
        reconcile: { ...WALLET.reconcile, delta_usdt: 2000, mismatch: true } }} />,
    );
    expect(screen.getByText('Ledger and exchange disagree')).toBeInTheDocument();
    expect(screen.getByText(/Entries are blocked/)).toBeInTheDocument();
  });
});

describe('slippage', () => {
  it('measures a buy against the ask and a sell against the bid', () => {
    expect(slippageBps(FILL)).toBeCloseTo(10, 0);
    expect(slippageBps({ ...FILL, side: 'sell', fill_price: 49850 })).toBeCloseTo(10, 0);
  });

  it('is undefined without a decision-time quote', () => {
    expect(slippageBps({ ...FILL, quote_ask: null })).toBeNull();
  });
});

describe('FillsTable', () => {
  it('renders the fee and the slippage', () => {
    renderWithProviders(<FillsTable rows={[FILL]} />);
    expect(screen.getByText('0.5 USDT')).toBeInTheDocument();
    expect(screen.getByText('10.0')).toBeInTheDocument();
  });
});

describe('PortfolioPage', () => {
  it('loads the selected sleeve', async () => {
    const client = new ApiClient({
      fetchImpl: fakeFetch({ '/api/portfolio/a': { body: PAYLOAD } }),
    });
    renderWithProviders(
      <ApiProvider client={client}>
        <PortfolioPage />
      </ApiProvider>,
    );
    expect(await screen.findByText('Portfolio')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('bot up')).toBeInTheDocument());
    expect(screen.getByText('ledger NAV')).toBeInTheDocument();
  });

  it('keeps rendering when the bot is down', async () => {
    const client = new ApiClient({
      fetchImpl: fakeFetch({
        '/api/portfolio/a': { body: { ...PAYLOAD, bot_up: false, positions: [] } },
      }),
    });
    renderWithProviders(
      <ApiProvider client={client}>
        <PortfolioPage />
      </ApiProvider>,
    );
    await waitFor(() => expect(screen.getByText('bot down')).toBeInTheDocument());
    expect(screen.getByText('No open positions')).toBeInTheDocument();
  });
});
