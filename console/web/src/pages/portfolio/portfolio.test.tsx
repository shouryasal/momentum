import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { ApiClient } from '@/api';
import { ApiProvider } from '@/app/ApiContext';
import { fakeFetch, renderWithProviders } from '@/test/utils';

import type { FillRow, PortfolioPayload, Position, SleevePot, Wallet } from './api';
import { FillsTable, slippageBps } from './components/ActivityTables';
import { PositionsTable } from './components/PositionsTable';
import { PotCard } from './components/PotCard';
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
    expect(screen.getByText('books agree with the exchange')).toBeInTheDocument();
  });

  it('says so plainly instead of throwing when no money has been recorded yet', () => {
    renderWithProviders(<WalletPanel wallet={null} />);
    expect(screen.getByText(/Nothing has been recorded for this bot yet/)).toBeInTheDocument();
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
  it('renders the fee, and the price difference as a percentage with what it means', () => {
    renderWithProviders(<FillsTable rows={[FILL]} />);
    expect(screen.getByText('0.5 USDT')).toBeInTheDocument();
    // 10 bps, said the way an operator reads it rather than as a bare "10.0".
    expect(screen.getByText('+0.100%')).toBeInTheDocument();
    expect(screen.getByText('worse than planned')).toBeInTheDocument();
  });
});

/** Sleeve a on 2026-09-29: the first evening in the old database, the fast profile in the new. */
const POT: SleevePot = {
  sleeve: 'a',
  seed_usdt: 10_000,
  seed_source: 'config',
  cumulative_net_usdt: 9_963.1,
  gain_usdt: -36.9,
  realised_all_runs_usdt: -36.9,
  realised_current_run_usdt: 5.31,
  realised_earlier_runs_usdt: -42.21,
  open_mark_usdt: -1.05,
  open_value_usdt: 498.9,
  fees_usdt: 10.42,
  gross_usdt: -26.98,
  fully_priced: true,
  unpriced: [],
  runs: [
    { run: 'tradesv3', db: 'ft_userdata/a/tradesv3.sqlite', strategy: 'SleeveA',
      started_utc: '2026-09-23T13:37:37Z', ended_utc: '2026-09-23T21:58:53Z', current: false,
      closed_trades: 2, open_trades: 0, realised_usdt: -42.21, fees_usdt: 4.91, gross_usdt: -37.3,
      error: null },
    { run: 'test-a-000', db: 'ft_userdata/a/runs/test-a-000.sqlite', strategy: 'SleeveFast',
      started_utc: '2026-09-24T01:05:07Z', ended_utc: null, current: true,
      closed_trades: 5, open_trades: 1, realised_usdt: 5.31, fees_usdt: 5.51, gross_usdt: 10.32,
      error: null },
  ],
  restarts: 1,
  current_run: 'test-a-000',
  open: [
    { trade_id: 6, pair: 'DOGE/USDT', amount: 2_500, open_rate: 0.2, stake_usdt: 500,
      opened_utc: '2026-09-29T05:00:07Z', mark: 0.1996, mark_source: 'bot', unrealised_usdt: -1.05 },
  ],
  ledger_nav_usdt: 10_004.17,
  ledger_as_of_utc: '2026-09-29T06:00:00Z',
  ledger_gap_usdt: -42.12,
};

describe('PotCard', () => {
  it('shows three numbers with their definitions and the run-by-run table', () => {
    renderWithProviders(<PotCard pot={POT} />);
    const figures = screen.getByTestId('pot-figures');
    expect(figures).toHaveTextContent('worth now, all runs');
    expect(figures).toHaveTextContent('$9,963.10');
    expect(figures).toHaveTextContent('-$36.90 against the seed');
    expect(figures).toHaveTextContent('since the current run started');
    expect(figures).toHaveTextContent('+$5.31');
    expect(figures).toHaveTextContent('Earlier runs: -$42.21');
    expect(figures).toHaveTextContent('open positions, marked to market');
    expect(figures).toHaveTextContent('-$1.05');
    expect(figures).toHaveTextContent('Counted across every restart');

    expect(screen.getByText(/2 run databases, 1 restart/)).toBeInTheDocument();
    const old = screen.getByTestId('pot-run-tradesv3');
    expect(old).toHaveTextContent('run 1 of 2 · SleeveA');
    expect(old).toHaveTextContent('-$42.21');
    expect(old).toHaveTextContent('$4.91');
    expect(old).toHaveTextContent('-$37.30');
    const current = screen.getByTestId('pot-run-test-a-000');
    expect(current).toHaveTextContent('current');
    expect(current).toHaveTextContent('still running');
    expect(current).toHaveTextContent('5 (+1 open)');
    expect(screen.getByTestId('pot-ledger-gap')).toHaveTextContent(
      'The 15-minute ledger says $10,004.17 — -$42.12 away',
    );
  });

  it('says so when a position could not be priced, and renders nothing without a pot', () => {
    renderWithProviders(<PotCard pot={{ ...POT, fully_priced: false, unpriced: ['PEPE/USDT'] }} />);
    expect(screen.getByText('partly unpriced')).toBeInTheDocument();
    const { container } = renderWithProviders(<PotCard pot={null} />);
    expect(container.querySelector('[data-testid="pot-card"]')).toBeNull();
  });
});

describe('FillsTable: why, and who', () => {
  it('names the hand flatten and the mandate-loss flatten as events, not trades', () => {
    renderWithProviders(
      <FillsTable
        rows={[
          { ...FILL, id: 12, side: 'sell', reason: 'force_exit', actor: 'human:console', cause: 'flatten' },
          { ...FILL, id: 8, side: 'sell', reason: 'target_zero', actor: 'bot', cause: 'targets:no_proposal_ever' },
          { ...FILL, id: 20, side: 'sell', reason: 'partial_exit', actor: 'bot', cause: null },
          { ...FILL, id: 99, side: 'sell' },
        ]}
      />,
    );
    expect(screen.getByTestId('fill-why-12')).toHaveTextContent('SOLD BY HAND — SELL EVERYTHING on this console');
    expect(screen.getByTestId('fill-why-8')).toHaveTextContent('BOT SOLD EVERYTHING — no valid mandate');
    expect(screen.getByTestId('fill-why-20')).toHaveTextContent('profit rung');
    expect(screen.getByTestId('fill-why-99')).toHaveTextContent('not recorded');
  });
});

describe('PortfolioPage', () => {
  it('leads with the pot when the server sends one', async () => {
    const client = new ApiClient({
      fetchImpl: fakeFetch({ '/api/portfolio/a': { body: { ...PAYLOAD, pot: POT } } }),
    });
    renderWithProviders(
      <ApiProvider client={client}>
        <PortfolioPage />
      </ApiProvider>,
    );
    expect(await screen.findByTestId('pot-card')).toBeInTheDocument();
    expect(screen.getByText('What the money put in is worth')).toBeInTheDocument();
  });

  it('loads the selected bot', async () => {
    const client = new ApiClient({
      fetchImpl: fakeFetch({ '/api/portfolio/a': { body: PAYLOAD } }),
    });
    renderWithProviders(
      <ApiProvider client={client}>
        <PortfolioPage />
      </ApiProvider>,
    );
    expect(await screen.findByText('Trading')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('bot running')).toBeInTheDocument());
    expect(screen.getByText('ledger NAV')).toBeInTheDocument();
    // The bots are named, not lettered.
    expect(screen.getByRole('tab', { name: 'Rules bot (no AI)' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'AI bot (Claude)' })).toBeInTheDocument();
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
    await waitFor(() => expect(screen.getByText('bot not running')).toBeInTheDocument());
    expect(screen.getByText('No open positions')).toBeInTheDocument();
  });

  it('opens the story behind a position in the detail pane, and closes it again', async () => {
    const user = userEvent.setup();
    const client = new ApiClient({
      fetchImpl: fakeFetch({ '/api/portfolio/a': { body: PAYLOAD } }),
    });
    renderWithProviders(
      <ApiProvider client={client}>
        <PortfolioPage />
      </ApiProvider>,
    );
    await screen.findByText('Trading');
    expect(screen.getByTestId('master-detail')).toHaveAttribute('data-detail-open', 'false');

    // The positions table is the first card, so its cell is the first match.
    await user.click(screen.getAllByText('BTC/USDT')[0] as HTMLElement);
    const pane = await screen.findByTestId('detail-pane');
    expect(pane).toBeInTheDocument();
    expect(screen.getByTestId('detail-pane-subtitle')).toHaveTextContent('where it sells out');
    expect(screen.getByTestId('detail-pane-raw-id')).toHaveTextContent('position BTC/USDT');
    expect(screen.getByTestId('master-detail')).toHaveAttribute('data-detail-open', 'true');

    await user.click(screen.getByTestId('detail-pane-close'));
    await waitFor(() => expect(screen.queryByTestId('detail-pane')).not.toBeInTheDocument());
  });

  it('restores the open row from the URL', async () => {
    const client = new ApiClient({
      fetchImpl: fakeFetch({ '/api/portfolio/a': { body: PAYLOAD } }),
    });
    renderWithProviders(
      <ApiProvider client={client}>
        <PortfolioPage />
      </ApiProvider>,
      { route: '/portfolio?detail=position:BTC%2FUSDT' },
    );
    expect(await screen.findByTestId('detail-pane')).toBeInTheDocument();
    expect(screen.getByTestId('detail-pane-raw-id')).toHaveTextContent('position BTC/USDT');
  });
});
