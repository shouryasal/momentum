/**
 * Home is four things, and the day it is five this file fails.
 *
 * The owner wrote the brief themselves: *"on main page i only want to see seed money,
 * ongoing holdings, current value of total money, maybe a table of each holding showing
 * value profit loss and a table of transactions done, if i double click i should see
 * reasoning"*, and *"i will not do things from ui, hide other things for now"*.
 *
 * So the guard here is mechanical. Home is rendered against a bundle that is *full* of
 * technical material — open incidents, gate tallies, a funnel, a cron schedule, a settings
 * diff, a model bill, run ids — and none of it may reach the screen. The regression test
 * names what came back, so the failure tells you which block to take out again.
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '@/test/utils';

import type { PortfolioPayload } from '../portfolio/api';
import OverviewPage, { reasoningTarget } from './index';
import type { OverviewPayload } from './api';
import {
  benchmarkLine,
  gainPhrase,
  holdingRows,
  money,
  seedMoney,
  signedMoney,
  signedPct,
  totalValue,
  totals,
  transactionRows,
} from './money';

/* ------------------------------------------------------------------------- the fixtures */

const BUSY: OverviewPayload = {
  ok: true,
  generated_utc: '2026-09-23T08:45:00Z',
  mode: {
    verified: true,
    reason: 'ok',
    phase: 'test',
    sleeves: {
      a: { state: 'TEST', submode: null, run_id: 'test-a-20260901-01', seed_usdt: 5_000 },
      b: { state: 'TEST', submode: null, run_id: 'test-b-20260901-01', seed_usdt: 5_000 },
    },
    kill: { engaged: false },
  },
  nav: {
    cards: [
      {
        sleeve: 'a', run_id: 'test-a-20260901-01', mode: 'TEST', ts_utc: '2026-09-23T08:00:00Z',
        nav_usdt: 5_240, cash_usdt: 1_800, reserved_usdt: 0, open_trades: 1,
        positions: { BTC: { amount: 0.04 } }, day_pct: 0.4, week_pct: 1.2, run_pct: 4.8,
        run_started_utc: '2026-09-01T00:00:00Z',
      },
      {
        sleeve: 'b', run_id: 'test-b-20260901-01', mode: 'TEST', ts_utc: '2026-09-23T08:00:00Z',
        nav_usdt: 5_000, cash_usdt: 5_000, reserved_usdt: 0, open_trades: 0,
        positions: {}, day_pct: 0, week_pct: 0, run_pct: 0,
        run_started_utc: '2026-09-01T00:00:00Z',
      },
      {
        sleeve: 'benchmark', run_id: 'bench', mode: null, ts_utc: '2026-09-23T08:00:00Z',
        nav_usdt: 10_100, cash_usdt: 0, reserved_usdt: 0, open_trades: 0,
        positions: {}, day_pct: 0.1, week_pct: 0.5, run_pct: 1.0, run_started_utc: null,
      },
    ],
  },
  nav_series: { since_utc: '2026-09-16T00:00:00Z', series: {} },
  exposure: {
    sleeves: [
      {
        sleeve: 'a', gross: 0.33, gross_cap: 0.6, gross_util: 0.55,
        assets: [
          { asset: 'BTC', amount: 0.04, mark_usdt: 86_000, value_usdt: 3_440, weight: 0.33, cap: 0.4, util: 0.82 },
        ],
      },
    ],
  },
  gate: {
    since_utc: '2026-09-23T00:00:00Z',
    allow: 7,
    reject: 1,
    breach: 0,
    recent: [
      {
        id: 31, ts_utc: '2026-09-23T08:31:00Z', sleeve: 'a', pair: 'BTC/USDT',
        reason: 'daily trade count reached', severity: 'reject',
        callback: 'freqtrade.callbacks.confirm_trade_entry',
      },
    ],
  },
  funnel: {
    since_utc: '2026-09-22T08:00:00Z',
    by_status: { detected: 12 },
    stages: [
      { stage: 'detected', count: 12 },
      { stage: 'screened', count: 5 },
      { stage: 'validated', count: 2 },
    ],
  },
  research: {
    last: {
      run_id: '2026-09-23T08:30+04:00', stage: 'decide', started_utc: '2026-09-23T08:30:00Z',
      finished_utc: '2026-09-23T08:32:00Z', status: 'success',
      requested_model: 'claude-sonnet-4-5', served_model: 'claude-sonnet-4-5',
      provider: 'anthropic', escalated: false, escalation_reasons: [], cost_usd: 0.12,
      trigger_reason: 'schedule', signal_id: null,
    },
  },
  schedule: {
    timezone: 'Asia/Dubai',
    jobs: [
      {
        job: 'decide', cron: '30 8 * * *', next_fire_local: '2026-09-24 08:30',
        next_fire_utc: '2026-09-24T04:30:00Z', deadline_s: 900, artifact: null,
      },
    ],
  },
  provider: {
    since_utc: '2026-09-22T08:00:00Z',
    usage: [{ provider: 'anthropic', calls: 4, cost: 0.31, ok: 4 }],
    rate_limit: {},
    recent_switches: [],
  },
  changed_today: {
    since_utc: '2026-09-23T00:00:00Z',
    config: [
      {
        id: 9, ts_utc: '2026-09-23T07:10:00Z', actor: 'human:console', file: 'config/earn.yaml',
        reason: 'raised the cash floor', changed_paths: ['risk.usdt_floor'],
        protected_changed: 1, applied: 1,
      },
    ],
    changes: [],
  },
  incidents: [
    {
      id: 4, opened_utc: '2026-09-23T06:00:00Z', kind: 'reconcile_drift', subkind: null,
      severity: 'warning', detail: 'ledger and exchange disagree by 0.0002 BTC',
    },
  ],
  approvals: { proposals: [], changes: [], count: 2 },
  flags: { active: [] },
  bless: { ok: true, reason: 'ok', changed: [], },
  limits: {
    daily_loss_stop: 0.03, monthly_loss_stop: 0.08, max_gross_exposure: 0.6,
    usdt_floor: 0.2, max_trades_per_day: 3,
  },
};

const QUIET: OverviewPayload = {
  ok: true,
  generated_utc: '2026-09-23T08:45:00Z',
  mode: {
    verified: true, reason: 'ok', phase: 'test',
    sleeves: {
      a: { state: 'TEST', submode: null, run_id: null, seed_usdt: 5_000 },
      b: { state: 'TEST', submode: null, run_id: null, seed_usdt: 5_000 },
    },
    kill: { engaged: false },
  },
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
  fills: [
    {
      id: 1, ts_utc: '2026-09-02T09:00:00Z', sleeve: 'a', pair: 'BTC/USDT', side: 'buy',
      fill_amount: 0.06, fill_price: 80_000, fee_amount: 4.8, fee_currency: 'USDT',
      quote_bid: 79_990, quote_ask: 80_010, mode: 'test', run_id: '2026-09-02T08:30+04:00',
    },
    {
      id: 2, ts_utc: '2026-09-18T09:00:00Z', sleeve: 'a', pair: 'BTC/USDT', side: 'sell',
      fill_amount: 0.02, fill_price: 90_000, fee_amount: 1.8, fee_currency: 'USDT',
      quote_bid: 89_990, quote_ask: 90_010, mode: 'test', run_id: '2026-09-18T08:30+04:00',
    },
  ],
};

const RUN_DETAIL = {
  run_id: '2026-09-18T08:30+04:00',
  stages: [
    {
      stage: 'screen', started_utc: '2026-09-18T08:30:00Z', finished_utc: '2026-09-18T08:30:20Z',
      requested_model: 'qwen2.5:7b', served_model: 'qwen2.5:7b', provider: 'ollama',
      chain_index: 0, switched_from: null, auth_source: null, effort: null, escalated: false,
      escalation_reasons: [], input_tokens: 800, output_tokens: 120, cost_usd: 0,
      num_turns: 1, prompt_version: 'v3', status: 'success', error: null,
    },
    {
      stage: 'decide', started_utc: '2026-09-18T08:30:30Z', finished_utc: '2026-09-18T08:31:10Z',
      requested_model: 'claude-sonnet-4-5', served_model: 'claude-sonnet-4-5',
      provider: 'anthropic', chain_index: 0, switched_from: null, auth_source: 'oauth',
      effort: 'high', escalated: false, escalation_reasons: [], input_tokens: 9_000,
      output_tokens: 700, cost_usd: 0.14, num_turns: 2, prompt_version: 'v7',
      status: 'success', error: null,
    },
  ],
  proposal: {
    run_id: '2026-09-18T08:30+04:00', shadow: false, ts_utc: '2026-09-18T08:31:10Z',
    path: 'proposals/2026-09-18.json', prompt_version: 'v7', model: 'claude-sonnet-4-5',
    module: 'trend following', targets: { 'BTC/USDT': 0.25 }, exposure_scale: 0.8,
    confidence: 0.62, abstain: false, horizon_days: 14,
    rationale: ['Price is above its long average and pulling away from it.'],
    invalidation: 'A daily close back under the long average.', hard_case_flags: [],
    valid: true, invalid_reason: null, consumed_status: 'consumed', signal_id: 'sig-9',
    approval_status: null,
  },
  provider_switches: [],
  signal: {
    signal_id: 'sig-9', pair: 'BTC/USDT', detector: 'breakout', direction: 'up',
    detector_score: 0.71, screen_score: 0.66, screen_provider: 'ollama',
    screen_model: 'qwen2.5:7b',
    screen_rationale: 'Volume backed the move and the range had been tight for weeks.',
  },
};

const GATE_DECISIONS = {
  rows: [
    {
      id: 88, ts_utc: '2026-09-18T08:31:20Z', sleeve: 'a', pair: 'BTC/USDT', side: 'sell',
      intent: 'exit', callback: 'freqtrade.callbacks.confirm_trade_exit', allowed: true,
      reason: 'within every limit', severity: 'allow', checks: { weight_cap: true },
      proposed_stake: 1_800, nav: 5_240, gross_exposure: 0.33,
      run_id: '2026-09-18T08:30+04:00', action: 'sell', trade_id: 4,
    },
  ],
  counts: { allow: 1 },
  checks: ['weight_cap'],
};

/* ------------------------------------------------------------------------ the transport */

function stubFetch(payload: OverviewPayload, books: Record<string, PortfolioPayload> = {}) {
  vi.stubGlobal('fetch', (async (input: RequestInfo | URL) => {
    const url = String(input);
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    if (url.includes('/api/overview')) return json(payload);
    if (url.includes('/api/portfolio/a')) return json(books.a ?? EMPTY_BOOK('a'));
    if (url.includes('/api/portfolio/b')) return json(books.b ?? EMPTY_BOOK('b'));
    if (url.includes('/api/runs/')) return json(RUN_DETAIL);
    if (url.includes('/api/risk/gate-decisions')) return json(GATE_DECISIONS);
    return json({ count: 0, pending: [] });
  }) as unknown as typeof fetch);
}

function render(
  payload: OverviewPayload = BUSY,
  books: Record<string, PortfolioPayload> = { a: BOOK_A },
  route = '/',
) {
  stubFetch(payload, books);
  return renderWithProviders(<OverviewPage />, { route });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/* ------------------------------------------------------------------------------ the screen */

describe('Home', () => {
  it('shows the four things asked for, in order, as money', async () => {
    render();
    await waitFor(() => expect(screen.getByTestId('overview-page')).toBeInTheDocument());

    // 1. What was put in.
    const cards = screen.getByTestId('money-cards');
    expect(cards).toHaveTextContent('Money put in');
    expect(cards).toHaveTextContent('$10,000.00');

    // 2. What it is worth, up or down, and one line against simply holding BTC.
    expect(cards).toHaveTextContent('What it is worth now');
    expect(cards).toHaveTextContent('$10,240.00');
    expect(cards).toHaveTextContent('+$240.00 (+2.40%)');
    expect(cards).toHaveTextContent('Simply holding BTC over the same time would be +1.00%');
    expect(cards).toHaveTextContent('1.40 points ahead');

    // 3. One row per holding, with cost, worth and profit.
    const holdings = await within(screen.getByTestId('holdings-card')).findByTestId('data-table');
    expect(holdings).toHaveTextContent('BTC');
    expect(holdings).toHaveTextContent('$3,200.00'); // 0.04 at 80,000
    expect(holdings).toHaveTextContent('$3,440.00');
    expect(holdings).toHaveTextContent('+$240.00 (+7.50%)');
    expect(holdings).toHaveTextContent('Simulated');

    // 4. Every buy and sell, newest first, with what the sell made.
    const trades = within(screen.getByTestId('transactions-card')).getByTestId('data-table');
    const rows = within(trades).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('Sold');
    expect(rows[0]).toHaveTextContent('+$200.00 (+12.50%)'); // 0.02 sold at 90k, bought at 80k
    expect(rows[1]).toHaveTextContent('Bought');
    expect(rows[1]).toHaveTextContent('$4,800.00');
  });

  /**
   * The regression guard the brief asked for by name.
   *
   * Every string below is in the fixture and every one of them used to be printed on Home.
   * If a technical block comes back, this fails and says which.
   */
  it('fails if Home regains a technical block', async () => {
    render();
    await waitFor(() => expect(screen.getByTestId('overview-page')).toBeInTheDocument());
    const front = within(screen.getByTestId('master-pane')).getByTestId('overview-page');
    const text = front.textContent ?? '';

    const banned: Array<[string, RegExp]> = [
      ['a health widget', /Everything is running|Something needs attention/],
      ['an open-incident list', /reconcile_drift|ledger and exchange disagree/],
      ['gate tallies', /let through|turned down|hit a hard limit/],
      ['the funnel counts', /\bdetected\b|\bscreened\b|\bvalidated\b/],
      ['the job schedule', /30 8 \* \* \*|due to run next/i],
      ['a settings diff', /risk\.usdt_floor|config\/earn\.yaml|what changed today/i],
      ['the model bill', /claude-sonnet|anthropic|qwen/i],
      ['a run id', /test-a-20260901-01|2026-09-23T08:30\+04:00/],
      ['a callback path', /freqtrade\.callbacks/],
      ['an invariant key or safety strip', /gate-in-order-path|config-blessed|safety promise/i],
      ['a freshness stamp', /2026-09-23T08:45:00Z/],
      ['an approvals count', /waiting for your yes or no|waiting on you/i],
      ['a builder word for the bots', /\bsleeve\b/i],
    ];
    for (const [what, pattern] of banned) {
      expect(pattern.test(text), `Home is showing ${what} again: ${text.slice(0, 600)}`).toBe(false);
    }

    // And the screen is exactly four blocks — the heading, the two totals, the two tables
    // — with no fifth panel smuggled in beside them.
    expect(screen.getByTestId('page-intro')).toBeInTheDocument();
    expect(screen.getByTestId('money-cards')).toBeInTheDocument();
    expect(screen.getByTestId('holdings-card')).toBeInTheDocument();
    expect(screen.getByTestId('transactions-card')).toBeInTheDocument();
    expect(
      front.children.length,
      `Home grew a block: ${[...front.children].map((el) => el.getAttribute('data-testid')).join(', ')}`,
    ).toBe(4);
  });

  it('says so in one plain sentence when nothing has traded, never an empty table', async () => {
    render(QUIET, {});
    await waitFor(() => expect(screen.getByTestId('overview-page')).toBeInTheDocument());

    expect(await screen.findByTestId('no-holdings')).toHaveTextContent('No holdings yet');
    expect(screen.getByTestId('no-transactions')).toHaveTextContent(
      'No transactions yet — the bots are in test mode',
    );
    expect(screen.queryAllByTestId('data-table')).toHaveLength(0);
    expect(screen.queryByTestId('data-table-loading')).toBeNull();

    // The money that IS known still reads as money; the rest says so rather than showing 0.
    const cards = screen.getByTestId('money-cards');
    expect(cards).toHaveTextContent('$10,000.00');
    expect(cards).toHaveTextContent('Nothing recorded yet');
    expect(cards.textContent ?? '').not.toMatch(/\$0\.00/);
  });

  it('selects on one click and opens the reasoning on two', async () => {
    render();
    const trades = await within(await screen.findByTestId('transactions-card')).findByTestId(
      'data-table',
    );
    const sell = within(trades).getByTestId('data-row-a:2');

    await userEvent.click(sell);
    expect(sell).toHaveAttribute('data-selected', 'true');
    expect(screen.queryByTestId('detail-pane')).toBeNull();

    await userEvent.dblClick(sell);
    const pane = await screen.findByTestId('detail-pane');
    expect(within(pane).getByTestId('detail-pane-title')).toHaveTextContent('Why it traded BTC');

    // The reasoning, step by step.
    await waitFor(() =>
      expect(within(pane).getByTestId('reason-noticed')).toHaveTextContent('breakout'),
    );
    expect(within(pane).getByTestId('reason-local-rationale')).toHaveTextContent('Volume backed');
    expect(within(pane).getByTestId('reason-conclusion')).toHaveTextContent('trend following');
    expect(within(pane).getByTestId('reason-plan')).toHaveTextContent('25.0% of the pot');
    await waitFor(() =>
      expect(within(pane).getByTestId('reason-safety')).toHaveTextContent('within every limit'),
    );
    expect(pane).toHaveTextContent('the local model on this machine');
    expect(pane).toHaveTextContent('Claude');
  });

  it('opens the same reasoning from a pasted link', async () => {
    render(BUSY, { a: BOOK_A }, '/?detail=holding:a:BTC');
    const pane = await screen.findByTestId('detail-pane');
    expect(within(pane).getByTestId('detail-pane-title')).toHaveTextContent('Why it holds BTC');
    await waitFor(() => expect(within(pane).getByTestId('reasoning')).toBeInTheDocument());
  });

  it('still opens a saved link into the old technical block, off the screen', async () => {
    render(BUSY, { a: BOOK_A }, '/?detail=status:now');
    const pane = await screen.findByTestId('detail-pane');
    expect(pane).toHaveTextContent('reconcile_drift');
    // ...and it is not on the screen behind it.
    const front = within(screen.getByTestId('master-pane')).getByTestId('overview-page');
    expect(front.textContent ?? '').not.toMatch(/reconcile_drift/);
  });
});

/* -------------------------------------------------------------------- the arithmetic alone */

describe('the money on Home', () => {
  it('adds up what went in and what it is worth, and never turns unknown into zero', () => {
    expect(seedMoney(BUSY)).toBe(10_000);
    expect(totalValue(BUSY)).toBe(10_240);
    expect(totals(BUSY)).toEqual({ seed: 10_000, value: 10_240, gain: 240, gainFraction: 0.024 });

    expect(totalValue(QUIET)).toBeNull();
    expect(totals(QUIET).gain).toBeNull();
    expect(seedMoney({ ...QUIET, mode: { ...QUIET.mode, sleeves: {} } })).toBeNull();
  });

  it('writes money as money', () => {
    expect(money(5_000)).toBe('$5,000.00');
    expect(signedMoney(123.45)).toBe('+$123.45');
    expect(signedMoney(-9)).toBe('-$9.00');
    expect(signedPct(0.025)).toBe('+2.50%');
    expect(gainPhrase(123.45, 0.025)).toBe('+$123.45 (+2.50%)');
    expect(gainPhrase(null, null)).toBe('not recorded');
  });

  it('compares against holding BTC in exactly one line', () => {
    const line = benchmarkLine(BUSY);
    expect(line).toBe(
      'Simply holding BTC over the same time would be +1.00% — you are 1.40 points ahead.',
    );
    expect(line.split('\n')).toHaveLength(1);
    expect(benchmarkLine(QUIET)).toMatch(/nothing to compare against yet/);
  });

  it('gives every holding what it cost beside what it is worth', () => {
    const rows = holdingRows([BOOK_A], BUSY);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      asset: 'BTC', amount: 0.04, costEach: 80_000, costTotal: 3_200,
      valueNow: 3_440, gain: 240, gainFraction: 0.075,
    });
  });

  it('marks a holding the bot could not price as not recorded, not as zero', () => {
    const rows = holdingRows([], BUSY);
    expect(rows[0]).toMatchObject({ asset: 'BTC', amount: 0.04, costTotal: null, valueNow: 3_440 });
    expect(rows[0]?.gain).toBeNull();
  });

  it('works out what a sell actually made, and admits when it cannot', () => {
    const rows = transactionRows([BOOK_A]);
    // Newest first.
    expect(rows.map((row) => row.side)).toEqual(['sell', 'buy']);
    expect(rows[0]?.realised).toBeCloseTo(200, 6);
    expect(rows[0]?.realisedFraction).toBeCloseTo(0.125, 6);
    expect(rows[1]?.realised).toBeNull();

    // A sell with no buy behind it on record is never given a confident number.
    const orphan = transactionRows([{ ...BOOK_A, fills: [BOOK_A.fills[1]] }]);
    expect(orphan[0]?.realised).toBeNull();
  });

  it('hands the reasoning pane the decision behind the row', () => {
    const holdings = holdingRows([BOOK_A], BUSY);
    const trades = transactionRows([BOOK_A]);
    const target = reasoningTarget('holding', 'a:BTC', holdings, trades);
    expect(target?.runId).toBe('2026-09-18T08:30+04:00');
    expect(target?.facts.map((fact) => fact.label)).toContain('Profit or loss');

    const trade = reasoningTarget('transaction', 'a:2', holdings, trades);
    expect(trade?.kind).toBe('transaction');
    expect(reasoningTarget('transaction', 'a:999', holdings, trades)).toBeNull();
  });
});
