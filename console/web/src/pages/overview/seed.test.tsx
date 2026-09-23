/**
 * What was put in, and the demo account that has never been traded on.
 *
 * The owner's question was: *"money put in shouldnt it be from demo api if we have put in
 * demo api, even if we are not executing trades from it we can atleast use it for every
 * other info until we start testing with it"*. Behind it were two facts, both measured:
 * Home read "MONEY PUT IN — Not set yet" while `modes.test.seed_usdt` held
 * `{a: 10000, b: 10000}`, and a working Binance demo key sat in `.env` that nothing read.
 *
 * Two rules this file exists to hold:
 *
 *   - **A number always names its source.** "recorded at run start", "configured for the
 *     next run", "live demo account balance" — one of those three, always, so a figure on
 *     this screen is never mysterious.
 *   - **Simulated money and demo money are never added.** One mode, one set of numbers,
 *     labelled. When the two bots disagree there is no total at all, and the card says so.
 */
import { screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '@/test/utils';

import type { PortfolioPayload } from '../portfolio/api';
import OverviewPage from './index';
import type { DemoPanel, OverviewPayload, SeedPanel } from './api';
import { demoLine, holdingRows, seedHint, seedMoney, totalValue } from './money';

/* -------------------------------------------------------------------------- fixtures */

const CONFIG_SEED: SeedPanel = {
  basis: 'simulated',
  label: 'Simulated starting pot',
  total_usdt: 20_000,
  source: 'config',
  source_label: 'configured for the next run',
  as_of_utc: null,
  mixed: false,
  per_sleeve: true,
  note: null,
  sleeves: [
    {
      sleeve: 'a', state: 'TEST', basis: 'simulated', seed_usdt: 10_000,
      source: 'config', source_label: 'configured for the next run',
      run_id: null, as_of_utc: null,
    },
    {
      sleeve: 'b', state: 'TEST', basis: 'simulated', seed_usdt: 10_000,
      source: 'config', source_label: 'configured for the next run',
      run_id: null, as_of_utc: null,
    },
  ],
};

/** The real shape `/api/overview` returns on a fresh checkout: no runs, nothing recorded. */
const FRESH: OverviewPayload = {
  ok: true,
  generated_utc: '2026-09-23T18:37:40Z',
  mode: {
    verified: false,
    reason: 'missing',
    phase: 'paper',
    sleeves: {
      a: { state: 'TEST', submode: null, run_id: null, seed_usdt: null },
      b: { state: 'TEST', submode: null, run_id: null, seed_usdt: null },
    },
    kill: { engaged: false },
  },
  seed: CONFIG_SEED,
  nav: { cards: [] },
  nav_series: { since_utc: '2026-09-16T00:00:00Z', series: {} },
  exposure: { sleeves: [] },
  gate: { since_utc: '2026-09-23T00:00:00Z', allow: 0, reject: 0, breach: 0, recent: [] },
  funnel: { since_utc: '2026-09-23T00:00:00Z', by_status: {}, stages: [] },
  research: { last: null },
  schedule: { timezone: 'Asia/Dubai', jobs: [] },
  changed_today: { since_utc: '2026-09-23T00:00:00Z', config: [], changes: [] },
  incidents: [],
  approvals: { proposals: [], changes: [], count: 0 },
};

/** Measured against `demo-api.binance.com` on 2026-09-23, before a single order. */
const DEMO_IDLE: DemoPanel = {
  configured: true,
  active: false,
  state: 'ok',
  host: 'demo-api.binance.com',
  can_trade: true,
  account_type: 'SPOT',
  balances: { USDC: 5_000, USDT: 5_000 },
  holdings: [
    { asset: 'USDT', amount: 5_000, mark_usdt: 1, value_usdt: 5_000, stable: true },
    { asset: 'USDC', amount: 5_000, mark_usdt: 1, value_usdt: 5_000, stable: true },
  ],
  cash_usdt: 10_000,
  value_usdt: 10_000,
  unpriced: [],
  open_order_count: 0,
  traded_here: false,
  as_of_utc: '2026-09-23T18:37:40Z',
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
    reconcile: {
      delta_usdt: 0, tolerance_usdt: 0, dust_usdt: 0, mismatch: false, block_on_mismatch: false,
    },
  },
});

const BOOK_A: PortfolioPayload = {
  ...EMPTY_BOOK('a'),
  bot_up: true,
  positions: [
    {
      pair: 'BTC/USDT', amount: 0.04, avg_entry: 80_000, mark: 86_000, value_usdt: 3_440,
      upnl_usdt: 240, upnl_pct: 0.075, weight: 0.33, weight_cap: 0.4, entries_used: 1,
      entries_max: 3, stop_from_open: -0.08, stop_price: 73_600, trailing_active: false,
      tp_rungs_fired: [], next_tp_rung: null, mode: 'test', sim: true,
    },
  ],
};

function render(payload: OverviewPayload) {
  vi.stubGlobal('fetch', (async (input: RequestInfo | URL) => {
    const url = String(input);
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    if (url.includes('/api/overview')) return json(payload);
    if (url.includes('/api/portfolio/a')) return json(EMPTY_BOOK('a'));
    if (url.includes('/api/portfolio/b')) return json(EMPTY_BOOK('b'));
    return json({ count: 0, pending: [] });
  }) as unknown as typeof fetch);
  return renderWithProviders(<OverviewPage />, { route: '/' });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/* ------------------------------------------------------------------ what was put in */

describe('what was put in', () => {
  it('shows the configured seed rather than "Not set yet", and says whose it is', () => {
    expect(seedMoney(FRESH)).toBe(20_000);
    expect(seedHint(FRESH)).toBe('Simulated starting pot — configured for the next run.');
  });

  it('says "recorded at run start" once a run has recorded one', () => {
    const recorded: OverviewPayload = {
      ...FRESH,
      seed: {
        ...CONFIG_SEED, total_usdt: 9_400, source: 'run',
        source_label: 'recorded at run start', as_of_utc: '2026-09-01T00:00:00Z',
      },
    };
    expect(seedMoney(recorded)).toBe(9_400);
    expect(seedHint(recorded)).toBe('Simulated starting pot — recorded at run start.');
  });

  it('calls a demo pot the demo account, read live', () => {
    const onDemo: OverviewPayload = {
      ...FRESH,
      seed: {
        ...CONFIG_SEED, basis: 'demo', label: 'Demo account', total_usdt: 10_000,
        source: 'account', source_label: 'live demo account balance', per_sleeve: false,
      },
    };
    expect(seedMoney(onDemo)).toBe(10_000);
    expect(seedHint(onDemo)).toBe('Demo account — live demo account balance.');
  });

  it('refuses to add a simulated pot to a demo pot', () => {
    const mixed: OverviewPayload = {
      ...FRESH,
      seed: {
        ...CONFIG_SEED, basis: 'mixed', mixed: true, total_usdt: null,
        note: 'The two bots are on different kinds of money, so there is no single total '
          + 'to show — simulated and demo pots are never added together.',
      },
    };
    expect(seedMoney(mixed)).toBeNull();
    expect(seedHint(mixed)).toMatch(/never added together/);
  });

  it('still adds up the old way when the server sends no seed panel', () => {
    const legacy: OverviewPayload = {
      ...FRESH,
      seed: undefined,
      mode: {
        ...FRESH.mode,
        sleeves: {
          a: { state: 'TEST', submode: null, run_id: null, seed_usdt: 5_000 },
          b: { state: 'TEST', submode: null, run_id: null, seed_usdt: 5_000 },
        },
      },
    };
    expect(seedMoney(legacy)).toBe(10_000);
  });

  it('puts the figure and its source on the card', async () => {
    render(FRESH);
    await waitFor(() => expect(screen.getByTestId('overview-page')).toBeInTheDocument());
    const cards = screen.getByTestId('money-cards');
    expect(cards).toHaveTextContent('$20,000.00');
    expect(cards).toHaveTextContent('configured for the next run');
    expect(cards.textContent ?? '').not.toMatch(/Not set yet/);
  });
});

/* ------------------------------------------------------------ the demo account on Home */

describe('the demo account on Home', () => {
  it('is one quiet line while nothing is running on it', async () => {
    const line = demoLine({ ...FRESH, demo: DEMO_IDLE });
    expect(line).toBe(
      'A demo account is set up at demo-api.binance.com: $10,000.00 '
      + '(5,000 USDC, 5,000 USDT), trading is enabled. Nothing has been traded there yet.',
    );
    expect(line?.split('\n')).toHaveLength(1);

    render({ ...FRESH, demo: DEMO_IDLE });
    await waitFor(() => expect(screen.getByTestId('overview-page')).toBeInTheDocument());
    expect(screen.getByTestId('demo-line')).toHaveTextContent('Nothing has been traded there yet');
    // The demo balance is a footnote. It is never folded into the pot above it.
    expect(screen.getByTestId('money-cards')).toHaveTextContent('$20,000.00');
    expect(screen.getByTestId('money-cards').textContent ?? '').not.toMatch(/\$30,000/);
  });

  it('says it cannot be reached rather than blanking the screen', () => {
    const line = demoLine({
      ...FRESH,
      demo: {
        configured: true, active: false, state: 'unreachable', host: 'demo-api.binance.com',
        error: 'GET /api/v3/account on demo-api.binance.com failed (ConnectError)',
      },
    });
    expect(line).toMatch(/Cannot reach demo-api\.binance\.com/);
    expect(line).not.toMatch(/\$0\.00/);
  });

  it('says nothing at all with no demo key, and nothing once demo is the live mode', () => {
    expect(demoLine({
      ...FRESH, demo: { configured: false, active: false, state: 'not_configured' },
    })).toBeNull();
    expect(demoLine({ ...FRESH, demo: { ...DEMO_IDLE, active: true } })).toBeNull();
  });

  it('becomes the money and the holdings once a bot is on demo', () => {
    const live: OverviewPayload = {
      ...FRESH,
      nav: {
        cards: [{
          sleeve: 'a', run_id: 'demo-a-1', mode: 'demo', ts_utc: '2026-09-23T18:00:00Z',
          nav_usdt: 99_999, cash_usdt: 0, reserved_usdt: 0, open_trades: 1,
          positions: {}, day_pct: null, week_pct: null, run_pct: null, run_started_utc: null,
        }],
      },
      demo: {
        ...DEMO_IDLE,
        active: true,
        balances: { USDT: 4_000, BTC: 0.05 },
        holdings: [
          { asset: 'BTC', amount: 0.05, mark_usdt: 86_000, value_usdt: 4_300, stable: false },
          { asset: 'USDT', amount: 4_000, mark_usdt: 1, value_usdt: 4_000, stable: true },
        ],
        cash_usdt: 4_000,
        value_usdt: 8_300,
      },
    };
    // The exchange, not the ledger: the stale NAV card of 99,999 is not what is shown.
    expect(totalValue(live)).toBe(8_300);

    const rows = holdingRows([BOOK_A], live);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      asset: 'BTC', amount: 0.05, valueNow: 4_300, costEach: 80_000, mode: 'demo',
    });
    // Amount and value from the account; the cost basis from the bot's own book.
    expect(rows[0]?.costTotal).toBeCloseTo(4_000, 6);
    expect(rows[0]?.gain).toBeCloseTo(300, 6);
  });

  it('never marks an unpriced demo holding as worth nothing', () => {
    const live: OverviewPayload = {
      ...FRESH,
      demo: {
        ...DEMO_IDLE,
        active: true,
        holdings: [
          { asset: 'PEPE', amount: 1_000_000, mark_usdt: null, value_usdt: null, stable: false },
        ],
        value_usdt: null,
        unpriced: ['PEPE'],
      },
    };
    expect(totalValue(live)).toBeNull();
    expect(holdingRows([], live)[0]?.valueNow).toBeNull();
  });
});
