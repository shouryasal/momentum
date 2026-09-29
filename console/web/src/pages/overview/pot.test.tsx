/**
 * The cumulative pot on Home, and the ledger that reset.
 *
 * Measured on 2026-09-29: Home said "What it is worth now: $20,009.68" while the seed plus
 * every closed trade in every run database said $19,939.91. The 69.77 between them was
 * the first evening — a hand flatten of the rules bot (−42.21) and the AI bot's
 * double-buy-and-flatten (−27.56) — which the 15-minute ledger forgot when the bots were
 * restarted onto fresh databases at 23:45Z. The owner said "the 20000 put in is worth
 * less" and was right; the screen said otherwise.
 *
 * What this file holds:
 *
 *   - "What it is worth now" is the cumulative pot when the server sends one, and the
 *     old ledger only when it does not;
 *   - three numbers, not one, each with its definition on the card;
 *   - a restart is a sentence on the front and a row in the table behind it;
 *   - the two day-one events read as what they were, and by whom.
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '@/test/utils';

import type { PortfolioPayload, SleevePot } from '../portfolio/api';
import { whyOf } from '../portfolio/why';
import OverviewPage from './index';
import type { OverviewPayload, PotPanel } from './api';
import { potSummary, potTotal, restartLine, runRows, totalValue, totals, transactionRows } from './money';

/* -------------------------------------------------------------------------- fixtures */

/** Sleeve a as the runtime recorded it on 2026-09-29 05:45Z, no open positions. */
const POT_A: SleevePot = {
  sleeve: 'a',
  seed_usdt: 10_000,
  seed_source: 'config',
  cumulative_net_usdt: 9_963.1,
  gain_usdt: -36.9,
  realised_all_runs_usdt: -36.9,
  realised_current_run_usdt: 5.31,
  realised_earlier_runs_usdt: -42.21,
  open_mark_usdt: 0,
  open_value_usdt: 0,
  fees_usdt: 9.92,
  gross_usdt: -26.98,
  fully_priced: true,
  unpriced: [],
  runs: [
    {
      run: 'tradesv3', db: 'ft_userdata/a/tradesv3.sqlite', strategy: 'SleeveA',
      started_utc: '2026-09-23T13:37:37Z', ended_utc: '2026-09-23T21:58:53Z', current: false,
      closed_trades: 2, open_trades: 0, realised_usdt: -42.21, fees_usdt: 4.91, gross_usdt: -37.3,
      error: null,
    },
    {
      run: 'test-a-000', db: 'ft_userdata/a/runs/test-a-000.sqlite', strategy: 'SleeveFast',
      started_utc: '2026-09-24T01:05:07Z', ended_utc: null, current: true,
      closed_trades: 5, open_trades: 0, realised_usdt: 5.31, fees_usdt: 5.01, gross_usdt: 10.32,
      error: null,
    },
  ],
  restarts: 1,
  current_run: 'test-a-000',
  open: [],
  ledger_nav_usdt: 10_005.31,
  ledger_as_of_utc: '2026-09-29T05:45:00Z',
  ledger_gap_usdt: -42.21,
};

const POT_B: SleevePot = {
  ...POT_A,
  sleeve: 'b',
  cumulative_net_usdt: 9_976.81,
  gain_usdt: -23.19,
  realised_all_runs_usdt: -23.19,
  realised_current_run_usdt: 4.37,
  realised_earlier_runs_usdt: -27.56,
  fees_usdt: 20.01,
  runs: [
    {
      run: 'tradesv3', db: 'ft_userdata/b/tradesv3.sqlite', strategy: 'SleeveB',
      started_utc: '2026-09-23T13:37:36Z', ended_utc: '2026-09-23T13:52:36Z', current: false,
      closed_trades: 2, open_trades: 0, realised_usdt: -27.56, fees_usdt: 15.01, gross_usdt: -12.55,
      error: null,
    },
    {
      run: 'test-b-000', db: 'ft_userdata/b/runs/test-b-000.sqlite', strategy: 'SleeveFast',
      started_utc: '2026-09-24T01:05:05Z', ended_utc: null, current: true,
      closed_trades: 5, open_trades: 0, realised_usdt: 4.37, fees_usdt: 5.0, gross_usdt: 9.37,
      error: null,
    },
  ],
  current_run: 'test-b-000',
  ledger_nav_usdt: 10_004.37,
  ledger_gap_usdt: -27.56,
};

const POT: PotPanel = {
  basis: 'simulated',
  mixed: false,
  definitions: {
    cumulative_net_usdt: 'What the money put in is worth now: the seed, plus every closed trade in every run database net of fees, plus open positions marked to market. Counted across every bot restart.',
    realised_current_run_usdt: 'Closed trades, net of fees, since the bot started on its current database. This is the number that resets to zero on a restart.',
    open_mark_usdt: 'Profit or loss on the positions still open, at the newest price the system has. Not yet realised; it moves with the market.',
  },
  total: {
    seed_usdt: 20_000,
    cumulative_net_usdt: 19_939.91,
    gain_usdt: -60.09,
    gain_pct: -0.30045,
    realised_all_runs_usdt: -60.09,
    realised_current_run_usdt: 9.68,
    realised_earlier_runs_usdt: -69.77,
    open_mark_usdt: 0,
    fees_usdt: 29.93,
    gross_usdt: -30.16,
    fully_priced: true,
    unpriced: [],
    restarts: 2,
    runs: 4,
    ledger_nav_usdt: 20_009.68,
    ledger_gap_usdt: -69.77,
  },
  sleeves: [POT_A, POT_B],
  as_of_utc: '2026-09-29T05:45:00Z',
};

/** The bundle as the server sent it that morning: the reset ledger AND the pot. */
const MORNING: OverviewPayload = {
  ok: true,
  generated_utc: '2026-09-29T05:50:00Z',
  mode: {
    verified: false, reason: 'missing', phase: 'paper',
    sleeves: {
      a: { state: 'TEST', submode: null, run_id: null, seed_usdt: null },
      b: { state: 'TEST', submode: null, run_id: null, seed_usdt: null },
    },
    kill: { engaged: false },
  },
  seed: {
    basis: 'simulated', label: 'Simulated starting pot', total_usdt: 20_000, source: 'config',
    source_label: 'configured for the next run', as_of_utc: null, mixed: false, per_sleeve: true,
    note: null,
    sleeves: [
      { sleeve: 'a', state: 'TEST', basis: 'simulated', seed_usdt: 10_000, source: 'config',
        source_label: 'configured for the next run', run_id: null, as_of_utc: null },
      { sleeve: 'b', state: 'TEST', basis: 'simulated', seed_usdt: 10_000, source: 'config',
        source_label: 'configured for the next run', run_id: null, as_of_utc: null },
    ],
  },
  pot: POT,
  nav: {
    cards: [
      { sleeve: 'a', run_id: null, mode: 'test', ts_utc: '2026-09-29T05:45:00Z',
        nav_usdt: 10_005.31, cash_usdt: 10_005.31, reserved_usdt: 0, open_trades: 0,
        positions: {}, day_pct: 0, week_pct: 0.05, run_pct: 0.46, run_started_utc: '2026-09-23T21:45:00Z' },
      { sleeve: 'b', run_id: null, mode: 'test', ts_utc: '2026-09-29T05:45:00Z',
        nav_usdt: 10_004.37, cash_usdt: 10_004.37, reserved_usdt: 0, open_trades: 0,
        positions: {}, day_pct: 0, week_pct: 0.04, run_pct: 0.32, run_started_utc: '2026-09-23T21:45:00Z' },
    ],
  },
  nav_series: { since_utc: '2026-09-22T00:00:00Z', series: {} },
  exposure: { sleeves: [] },
  gate: { since_utc: '2026-09-28T20:00:00Z', allow: 0, reject: 0, breach: 0, recent: [] },
  funnel: { since_utc: '2026-09-28T05:50:00Z', by_status: {}, stages: [] },
  research: { last: null },
  schedule: { timezone: 'Asia/Dubai', jobs: [] },
  changed_today: { since_utc: '2026-09-28T20:00:00Z', config: [], changes: [] },
  incidents: [],
  approvals: { proposals: [], changes: [], count: 0 },
};

const EMPTY_BOOK = (sleeve: string): PortfolioPayload => ({
  sleeve,
  bot_up: false,
  positions: [],
  orders: [],
  fills: [],
  wallet: {
    sleeve,
    ledger: { nav: 0, cash: 0, reserved: 0, positions: 0, free_usdt: 0 },
    exchange: { total: 0, currencies: [] },
    reconcile: { delta_usdt: 0, tolerance_usdt: 0, dust_usdt: 0, mismatch: false, block_on_mismatch: false },
  },
});

/** The first evening's fills as the server now annotates them. */
const BOOK_A: PortfolioPayload = {
  ...EMPTY_BOOK('a'),
  bot_up: true,
  pot: POT_A,
  fills: [
    { id: 3, ts_utc: '2026-09-23T13:37:43Z', sleeve: 'a', pair: 'BTC/USDT', side: 'buy',
      fill_amount: 0.0173, fill_price: 85_761.87, fee_amount: 1.48, fee_currency: 'USDT',
      quote_bid: null, quote_ask: null, mode: 'test', run_id: null,
      ft_order_id: 'dry_run_buy_BTC/USDT_f87c9ef3', reason: 'dca', actor: 'bot', cause: null,
      run: 'tradesv3' },
    { id: 12, ts_utc: '2026-09-23T21:58:52Z', sleeve: 'a', pair: 'BTC/USDT', side: 'sell',
      fill_amount: 0.0173, fill_price: 84_441.33, fee_amount: 1.46, fee_currency: 'USDT',
      quote_bid: null, quote_ask: null, mode: 'test', run_id: 'test-a-000',
      ft_order_id: 'dry_run_sell_BTC/USDT_abc5e67c', reason: 'force_exit', actor: 'human:console',
      cause: 'flatten', run: 'tradesv3' },
  ],
};

const BOOK_B: PortfolioPayload = {
  ...EMPTY_BOOK('b'),
  bot_up: true,
  pot: POT_B,
  fills: [
    { id: 2, ts_utc: '2026-09-23T13:37:42Z', sleeve: 'b', pair: 'BTC/USDT', side: 'buy',
      fill_amount: 0.02309, fill_price: 85_748.84, fee_amount: 1.98, fee_currency: 'USDT',
      quote_bid: null, quote_ask: null, mode: 'test', run_id: null,
      ft_order_id: 'dry_run_buy_BTC/USDT_261cbe46', reason: 'proposal', actor: 'bot', cause: null,
      run: 'tradesv3' },
    { id: 7, ts_utc: '2026-09-23T13:37:46Z', sleeve: 'b', pair: 'BTC/USDT', side: 'buy',
      fill_amount: 0.02309, fill_price: 85_747.45, fee_amount: 1.98, fee_currency: 'USDT',
      quote_bid: null, quote_ask: null, mode: 'test', run_id: null,
      ft_order_id: 'dry_run_buy_BTC/USDT_88621d46', reason: 'rebalance', actor: 'bot', cause: null,
      run: 'tradesv3' },
    { id: 8, ts_utc: '2026-09-23T13:52:35Z', sleeve: 'b', pair: 'BTC/USDT', side: 'sell',
      fill_amount: 0.04618, fill_price: 85_627.17, fee_amount: 3.95, fee_currency: 'USDT',
      quote_bid: null, quote_ask: null, mode: 'test', run_id: 'test-b-000',
      ft_order_id: 'dry_run_sell_BTC/USDT_7ffe3a45', reason: 'target_zero', actor: 'bot',
      cause: 'targets:no_proposal_ever', run: 'tradesv3' },
    { id: 20, ts_utc: '2026-09-24T20:54:08Z', sleeve: 'b', pair: 'NEAR/USDT', side: 'sell',
      fill_amount: 45.3, fill_price: 4.675, fee_amount: 0.21, fee_currency: 'USDT',
      quote_bid: null, quote_ask: null, mode: 'test', run_id: 'test-b-000',
      ft_order_id: 'dry_run_sell_NEAR/USDT_27844f5a', reason: 'trailing_stop_loss', actor: 'bot',
      cause: null, run: 'test-b-000' },
  ],
};

function render(payload: OverviewPayload, books: Record<string, PortfolioPayload>, route = '/') {
  vi.stubGlobal('fetch', (async (input: RequestInfo | URL) => {
    const url = String(input);
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
    if (url.includes('/api/overview')) return json(payload);
    if (url.includes('/api/portfolio/a')) return json(books.a ?? EMPTY_BOOK('a'));
    if (url.includes('/api/portfolio/b')) return json(books.b ?? EMPTY_BOOK('b'));
    return json({ count: 0, pending: [] });
  }) as unknown as typeof fetch);
  return renderWithProviders(<OverviewPage />, { route });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/* ---------------------------------------------------------------- the arithmetic alone */

describe('the cumulative pot', () => {
  it('is what "worth now" means once the server sends it, and the ledger only until then', () => {
    // The morning of 2026-09-29: the ledger said 20,009.68; the truth was 19,939.91.
    expect(potTotal(MORNING)).toBe(19_939.91);
    expect(totalValue(MORNING)).toBe(19_939.91);
    expect(totals(MORNING)).toMatchObject({ seed: 20_000, value: 19_939.91 });
    expect(totals(MORNING).gain).toBeCloseTo(-60.09, 6);

    // A server that has not been restarted sends no pot: the old sum is still the answer.
    const legacy: OverviewPayload = { ...MORNING, pot: undefined };
    expect(potTotal(legacy)).toBeNull();
    expect(totalValue(legacy)).toBeCloseTo(20_009.68, 6);
  });

  it('refuses a partial total rather than printing an unpriced book as a whole one', () => {
    const partial: OverviewPayload = {
      ...MORNING,
      pot: { ...POT, total: { ...POT.total!, fully_priced: false, unpriced: ['PEPE/USDT'] } },
    };
    expect(potTotal(partial)).toBeNull();
    expect(potSummary(partial)?.unpriced).toEqual(['PEPE/USDT']);
    // ...and never adds two kinds of money together.
    expect(potTotal({ ...MORNING, pot: { ...POT, mixed: true, total: null } })).toBeNull();
  });

  it('carries three numbers with their definitions, not one', () => {
    const summary = potSummary(MORNING);
    expect(summary).toMatchObject({
      cumulative: 19_939.91, sinceRun: 9.68, beforeRun: -69.77, openMark: 0, restarts: 2, runs: 4,
      ledger: 20_009.68, ledgerGap: -69.77,
    });
    expect(Object.keys(summary?.definitions ?? {})).toEqual([
      'cumulative_net_usdt', 'realised_current_run_usdt', 'open_mark_usdt',
    ]);
  });

  it('says on the front what came before the current run', () => {
    expect(restartLine(potSummary(MORNING))).toBe(
      'Before this run: -$69.77 across 2 earlier runs. Click for the run-by-run table.',
    );
    const single = potSummary({ ...MORNING, pot: { ...POT, total: { ...POT.total!, restarts: 0 } } });
    expect(restartLine(single)).toMatch(/nothing has been counted twice or lost to a restart/);
  });

  it('lays the run databases out as rows with start, end, realised and fees', () => {
    const rows = runRows(MORNING);
    expect(rows.map((row) => row.key)).toEqual([
      'a:tradesv3', 'a:test-a-000', 'b:tradesv3', 'b:test-b-000',
    ]);
    expect(rows[0]).toMatchObject({
      label: 'run 1 of 2', strategy: 'SleeveA', started: '2026-09-23T13:37:37Z',
      ended: '2026-09-23T21:58:53Z', current: false, trades: 2, realised: -42.21, fees: 4.91,
    });
    expect(rows[1]).toMatchObject({ label: 'run 2 of 2', current: true, realised: 5.31 });
  });
});

/* ------------------------------------------------------------------ who did what */

describe('why a trade happened, and who did it', () => {
  it('names the hand flatten as a sale by hand from this console', () => {
    const why = whyOf({ side: 'sell', reason: 'force_exit', actor: 'human:console', cause: 'flatten' });
    expect(why).toMatchObject({ who: 'human', event: true });
    expect(why.short).toBe('SOLD BY HAND — SELL EVERYTHING on this console');
    expect(why.long).toMatch(/The strategy did not decide this/);
  });

  it("names the AI bot's phantom flatten as the bot selling everything for want of a mandate", () => {
    const why = whyOf({ side: 'sell', reason: 'target_zero', actor: 'bot', cause: 'targets:no_proposal_ever' });
    expect(why).toMatchObject({ who: 'bot', event: true });
    expect(why.short).toBe('BOT SOLD EVERYTHING — no valid mandate');
    expect(why.long).toMatch(/targets:no_proposal_ever/);
  });

  it('calls the ordinary exits and entries what they are, and admits an unmatched fill', () => {
    expect(whyOf({ side: 'sell', reason: 'trailing_stop_loss', actor: 'bot', cause: null }).short).toBe('trailing stop');
    expect(whyOf({ side: 'sell', reason: 'partial_exit', actor: 'bot', cause: null }).short).toBe('profit rung');
    expect(whyOf({ side: 'sell', reason: 'exit_signal', actor: 'bot', cause: null }).short).toBe('trend-loss signal');
    expect(whyOf({ side: 'buy', reason: 'rebalance', actor: 'bot', cause: null }).short).toBe('rebalance top-up');
    expect(whyOf({ side: 'buy', reason: 'proposal', actor: 'bot', cause: null }).short).toBe("Claude's plan");
    const unmatched = whyOf({ side: 'sell', reason: null, actor: null, cause: null });
    expect(unmatched).toMatchObject({ short: 'not recorded', who: 'unknown', event: false });
    const noAudit = whyOf({ side: 'sell', reason: 'force_exit', actor: 'unknown', cause: 'flatten' });
    expect(noAudit.short).toBe('forced sale (no record of who)');
  });

  it('puts the reason on every transaction row', () => {
    const rows = transactionRows([BOOK_A, BOOK_B]);
    const flatten = rows.find((row) => row.key === 'a:12');
    expect(flatten?.why.short).toMatch(/SOLD BY HAND/);
    const phantom = rows.find((row) => row.key === 'b:8');
    expect(phantom?.why.short).toMatch(/no valid mandate/);
    expect(rows.find((row) => row.key === 'b:20')?.why.short).toBe('trailing stop');
  });
});

/* ------------------------------------------------------------------------- the screen */

describe('Home with the pot', () => {
  it('shows the cumulative figure, the three numbers and the restart on the front', async () => {
    render(MORNING, { a: BOOK_A, b: BOOK_B });
    await waitFor(() => expect(screen.getByTestId('overview-page')).toBeInTheDocument());
    const cards = screen.getByTestId('money-cards');
    expect(cards).toHaveTextContent('$20,000.00');
    expect(cards).toHaveTextContent('$19,939.91');
    expect(cards).toHaveTextContent('-$60.09 (-0.30%)');
    expect(cards.textContent ?? '').not.toMatch(/20,009\.68/);
    expect(cards).toHaveTextContent('Since the current run started');
    expect(cards).toHaveTextContent('+$9.68');
    expect(cards).toHaveTextContent('Before this run: -$69.77 across 2 earlier runs');
    expect(cards).toHaveTextContent('Open positions, marked to market');
    expect(cards).toHaveTextContent('Counted across every restart');

    // Still five blocks: the extra numbers live inside the money cards.
    const front = within(screen.getByTestId('master-pane')).getByTestId('overview-page');
    expect(front.children.length).toBe(5);
    expect(front.textContent ?? '').not.toMatch(/\bsleeve\b/i);
  });

  it('opens the run-by-run table from the card, with every boundary and the ledger gap', async () => {
    render(MORNING, { a: BOOK_A, b: BOOK_B });
    await waitFor(() => expect(screen.getByTestId('overview-page')).toBeInTheDocument());
    await userEvent.click(screen.getByText('Since the current run started'));
    const pane = await screen.findByTestId('detail-pane');
    expect(within(pane).getByTestId('detail-pane-title')).toHaveTextContent('run by run');
    const table = within(pane).getByTestId('pot-detail-runs');
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(4);
    expect(rows[0]).toHaveTextContent('Rules bot');
    expect(rows[0]).toHaveTextContent('run 1 of 2');
    expect(rows[0]).toHaveTextContent('-$42.21');
    expect(rows[0]).toHaveTextContent('$4.91');
    expect(rows[1]).toHaveTextContent('current');
    expect(rows[1]).toHaveTextContent('still running');
    expect(rows[2]).toHaveTextContent('-$27.56');
    expect(rows[2]).toHaveTextContent('$15.01');
    expect(within(pane).getByTestId('pot-detail-ledger')).toHaveTextContent(
      'The 15-minute ledger says $20,009.68, -$69.77 away',
    );
  });

  it('shows the two day-one events as what they were, and by whom', async () => {
    render(MORNING, { a: BOOK_A, b: BOOK_B });
    const trades = await within(await screen.findByTestId('transactions-card')).findByTestId('data-table');
    expect(within(trades).getByTestId('why-a:12')).toHaveTextContent(
      'SOLD BY HAND — SELL EVERYTHING on this console',
    );
    expect(within(trades).getByTestId('why-b:8')).toHaveTextContent('BOT SOLD EVERYTHING — no valid mandate');
    expect(within(trades).getByTestId('why-b:20')).toHaveTextContent('trailing stop');
    expect(within(trades).getByTestId('why-b:7')).toHaveTextContent('rebalance top-up');
  });
});
