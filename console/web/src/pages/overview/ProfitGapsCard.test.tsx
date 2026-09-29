/**
 * The profit & gap card on Home.
 *
 * The rules this file holds:
 *
 *   - A: the declared expectation is on the card with the sentence that days of results
 *     cannot confirm or refute it — a number never appears without that sentence;
 *   - B: three numbers, each with its definition, read as money;
 *   - D: the three sentences the server ranked, verbatim;
 *   - C: the ten gaps are behind one button and are NOT in the DOM until it is pressed,
 *     so the front of Home stays free of every builder word the Home guard bans;
 *   - a payload that is not a ledger (another page's, an empty body) reads as "not
 *     computed yet", never as zeros, and a server-side error reads as the error.
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ProfitGapsLedger, ProfitGapsResponse } from '@/api';
import { renderWithProviders } from '@/test/utils';

import {
  NOTHING_LOST,
  NOT_COMPUTED,
  ProfitGapsCard,
  expectationLine,
  isProfitGapsResponse,
  realisedTiles,
  sinceStartLine,
  topThree,
} from './ProfitGapsCard';

/* -------------------------------------------------------------------------- fixtures */

const NOTE =
  'Days of realised profit or loss cannot confirm or refute this expectation: 30 days can only tell whether the mechanism behaves, not what it earns (dip-strategy.md §10).';

const DEFINITIONS = {
  realised_net_usdt: 'Closed trades in this window, net of fees, across every database a bot has run on.',
  fees_usdt: 'What the venue took on those trades, and the share of gross profit it took.',
  btc_hold_usdt: 'What the same money would have done simply held as BTC over the same window.',
};

function line(key: string, label: string, value: number | null, unit: string) {
  return { key, label, value, unit, query: `SELECT ${key} FROM somewhere`, note: null };
}

/** The last 24 hours as the ledger read them on 2026-09-29. */
const DAY: ProfitGapsLedger = {
  window: { key: 'last_24h', since_utc: '2026-09-28T14:00:00Z', until_utc: '2026-09-29T14:00:00Z', hours: 24 },
  expected: {
    profile: 'fast-test',
    source: 'config/profiles/fast-test.yaml evidence block',
    expected_per_30d_pct: -1.29,
    expected_this_window_pct: -0.043,
    planned_max_drawdown_pct: -26.8,
    note: NOTE,
    lines: [line('expected_per_30d_pct', 'Expected return per 30 days', -1.29, '%')],
  },
  realised: {
    seed_total_usdt: 20_000,
    cumulative_net_usdt: 19_987.95,
    realised_net_usdt: 48.04,
    gross_usdt: 49.07,
    fees_usdt: 1.03,
    fee_gross_ratio: 0.021,
    trades: 2,
    wins: 2,
    win_rate: 1,
    exit_reasons: { roi: { trades: 2, net_usdt: 48.04, wins: 2 } },
    open_mark_usdt: -0.6,
    benchmark: {
      btc_hold_usdt: 438.55,
      btc_hold_pct: 2.19,
      basket_pairs: 31,
      basket_hold_usdt: 120.5,
      basket_hold_pct: 0.6,
      cost_per_side: 0.0015,
      query: 'SELECT close FROM candles',
    },
    per_sleeve: {},
    definitions: DEFINITIONS,
    lines: [line('realised_net_usdt', 'Realised in the window, net of fees', 48.04, 'USDT')],
  },
  gaps: [
    {
      key: 'uptime',
      title: 'Time the bots were not running',
      size: 6.9,
      unit: 'hours',
      severity: 29,
      weight: 1,
      score: 29,
      cause: 'the host slept or the bots were down',
      sentence: 'You were not trading 6.9 of the last 24 hours: the host was suspended (1 incident), 09-29 06:14Z-13:09Z.',
      lines: [line('dark_hours', 'Hours with no bot heartbeat', 6.9, 'hours')],
      detail: {},
      error: null,
    },
    {
      key: 'funnel',
      title: 'Signals lost between the detector and a fill',
      size: 4,
      unit: 'count',
      severity: 100,
      weight: 1,
      score: 100,
      cause: 'each stage that drops a signal for a reason that is not the market is a decision never taken',
      sentence: "4 of 4 signals that passed the screen never got a verdict: the validator errored on unknown feature_key: ['market_state.data_fresh'].",
      lines: [
        line('candidates', 'Detector candidates', 12, 'count'),
        line('screened', 'Passed the screen', 4, 'count'),
        line('validated', 'Reached the validator', 4, 'count'),
      ],
      detail: {},
      error: null,
    },
    {
      key: 'decision_inputs',
      title: 'Decisions made on empty inputs',
      size: 2,
      unit: 'count',
      severity: 100,
      weight: 1,
      score: 100,
      cause: 'the decision stage receives numbers computed by code',
      sentence:
        "The AI bot's decision stage abstained 2 of 2 times because it was handed no indicators (the market state has an empty assets block).",
      lines: [line('abstained_on_empty_inputs', 'Abstained on empty inputs', 2, 'count')],
      detail: {},
      error: null,
    },
    {
      key: 'fee_drag',
      title: 'Fee drag against the monthly budget',
      size: 1.03,
      unit: 'USDT',
      severity: 2,
      weight: 1,
      score: 2,
      cause: 'every sub-day trade pays the full round trip',
      sentence: 'Fees took 2% of gross profit (1.03 USDT on 49.07 USDT): the active profile books sub-1% rungs against a 0.30% round trip.',
      lines: [line('fees_usdt', 'Fees paid', 1.03, 'USDT')],
      detail: {},
      error: null,
    },
  ],
  top_three: [
    { key: 'funnel', title: 'Signals lost', sentence: "4 of 4 signals that passed the screen never got a verdict: the validator errored on unknown feature_key: ['market_state.data_fresh'].", size: 4, unit: 'count', severity: 100, weight: 1, score: 100 },
    { key: 'decision_inputs', title: 'Decisions made on empty inputs', sentence: "The AI bot's decision stage abstained 2 of 2 times because it was handed no indicators (the market state has an empty assets block).", size: 2, unit: 'count', severity: 100, weight: 1, score: 100 },
    { key: 'uptime', title: 'Time the bots were not running', sentence: 'You were not trading 6.9 of the last 24 hours: the host was suspended (1 incident), 09-29 06:14Z-13:09Z.', size: 6.9, unit: 'hours', severity: 29, weight: 1, score: 29 },
  ],
  errors: [],
};

const SINCE_START: ProfitGapsLedger = {
  ...DAY,
  window: { key: 'since_start', since_utc: '2026-09-23T13:37:37Z', until_utc: '2026-09-29T14:00:00Z', hours: 144.4 },
  realised: {
    ...DAY.realised,
    realised_net_usdt: -12.05,
    fees_usdt: 30.96,
    trades: 13,
    benchmark: { ...DAY.realised.benchmark, btc_hold_usdt: -401.2 },
  },
};

const LEDGER: ProfitGapsResponse = {
  generated_utc: '2026-09-29T14:00:00Z',
  profile: 'fast-test',
  windows: { last_24h: DAY, since_start: SINCE_START },
  cached: false,
  error: null,
};

/** The words the Home guard bans from the front of the page (`overview.test.tsx`). */
const BANNED: Array<[string, RegExp]> = [
  ['the funnel counts', /\bdetected\b|\bscreened\b|\bvalidated\b/],
  ['gate tallies', /let through|turned down|hit a hard limit/],
  ['a builder word for the bots', /\bsleeve\b/i],
  ['a health widget', /Everything is running|Something needs attention/],
  ['the model bill', /claude-sonnet|anthropic|qwen/i],
];

function render(payload: unknown, status = 200) {
  vi.stubGlobal('fetch', (async () =>
    new Response(JSON.stringify(payload), {
      status,
      headers: { 'content-type': 'application/json' },
    })) as unknown as typeof fetch);
  return renderWithProviders(<ProfitGapsCard />);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/* ---------------------------------------------------------------- the words alone */

describe('the words on the card', () => {
  it('states the expectation with the sentence that days cannot test it', () => {
    const text = expectationLine(DAY);
    expect(text).toContain('Expected: -1.29% per 30 days on the fast-test profile');
    expect(text).toContain('planned worst drawdown -26.80%');
    expect(text).toContain('cannot confirm or refute');
  });

  it('reads the three numbers as money with their definitions', () => {
    const tiles = realisedTiles(DAY);
    expect(tiles.map((t) => t.value)).toEqual(['+$48.04', '$1.03', '+$438.55']);
    expect(tiles[0]?.label).toBe('Made in the last 24 hours');
    expect(tiles[0]?.hint).toContain('2 trades, 100% won.');
    expect(tiles[0]?.hint).toContain(DEFINITIONS.realised_net_usdt);
    expect(tiles[1]?.hint).toContain('2% of gross profit');
    expect(tiles[2]?.hint).toContain('+2.19% on the money put in');
  });

  it('says so when there is no BTC price and when gross was not positive', () => {
    const noPrice = { ...DAY, realised: { ...DAY.realised, fee_gross_ratio: null, benchmark: { ...DAY.realised.benchmark, btc_hold_usdt: null, btc_hold_pct: null } } };
    const tiles = realisedTiles(noPrice);
    expect(tiles[2]?.value).toBe('no price on record');
    expect(tiles[1]?.hint).toContain('No gross profit to take a share of');
  });

  it('sums up the test since it began in one line', () => {
    expect(sinceStartLine(SINCE_START)).toBe(
      'Since the test began (6.0 days): made -$12.05 net on 13 trades, paid $30.96 in fees, holding BTC would have made -$401.20.',
    );
    expect(sinceStartLine(undefined)).toBeNull();
  });

  it('hands over the three sentences verbatim, or the one that says nothing was lost', () => {
    expect(topThree(DAY)).toEqual(DAY.top_three.map((t) => t.sentence));
    expect(topThree({ ...DAY, top_three: [] })).toEqual([NOTHING_LOST]);
  });

  it('recognises a ledger and nothing else', () => {
    expect(isProfitGapsResponse(LEDGER)).toBe(true);
    expect(isProfitGapsResponse({ count: 0, pending: [] })).toBe(false);
    expect(isProfitGapsResponse(null)).toBe(false);
  });
});

/* --------------------------------------------------------------------- the screen */

describe('ProfitGapsCard', () => {
  it('shows A, B and D, and keeps C behind the button', async () => {
    render(LEDGER);
    const card = await screen.findByTestId('profit-gaps-card');
    await waitFor(() => expect(screen.getByTestId('profit-gaps-expected')).toBeInTheDocument());

    // A
    expect(screen.getByTestId('profit-gaps-expected')).toHaveTextContent('-1.29% per 30 days');
    expect(screen.getByTestId('profit-gaps-expected')).toHaveTextContent('cannot confirm or refute');

    // B
    const tiles = within(screen.getByTestId('profit-gaps-realised'));
    expect(tiles.getAllByTestId('stat-card')).toHaveLength(3);
    expect(screen.getByTestId('profit-gaps-realised')).toHaveTextContent('+$48.04');
    expect(screen.getByTestId('profit-gaps-realised')).toHaveTextContent('$1.03');
    expect(screen.getByTestId('profit-gaps-realised')).toHaveTextContent('+$438.55');
    expect(screen.getByTestId('profit-gaps-since-start')).toHaveTextContent('Since the test began (6.0 days)');

    // D
    const top = screen.getByTestId('profit-gaps-top-three');
    expect(top).toHaveTextContent('1. 4 of 4 signals that passed the screen never got a verdict');
    expect(top).toHaveTextContent("2. The AI bot's decision stage abstained 2 of 2 times");
    expect(top).toHaveTextContent('3. You were not trading 6.9 of the last 24 hours');

    // C is not in the DOM, and the front carries none of the banned words.
    expect(screen.queryByTestId('profit-gaps-details')).toBeNull();
    const front = card.textContent ?? '';
    for (const [what, pattern] of BANNED) {
      expect(pattern.test(front), `the front of the card shows ${what}: ${front}`).toBe(false);
    }

    await userEvent.click(screen.getByTestId('profit-gaps-toggle'));
    const details = await screen.findByTestId('profit-gaps-details');
    expect(details).toHaveTextContent('Time the bots were not running');
    expect(details).toHaveTextContent('Hours with no bot heartbeat: 6.9 hours');
    expect(details).toHaveTextContent('Detector candidates: 12 count');
    expect(screen.getByTestId('profit-gap-line-uptime-dark_hours')).toHaveAttribute(
      'title',
      'SELECT dark_hours FROM somewhere',
    );

    await userEvent.click(screen.getByText('Since the test began'));
    expect(screen.getByTestId('profit-gaps-details')).toHaveTextContent('2026-09-23T13:37:37Z to 2026-09-29T14:00:00Z');

    await userEvent.click(screen.getByTestId('profit-gaps-toggle'));
    expect(screen.queryByTestId('profit-gaps-details')).toBeNull();
  });

  it('says "not computed yet" for a payload that is not a ledger, never zeros', async () => {
    render({ count: 0, pending: [] });
    expect(await screen.findByTestId('profit-gaps-empty')).toHaveTextContent(NOT_COMPUTED);
    expect(screen.getByTestId('profit-gaps-card').textContent ?? '').not.toMatch(/\$0\.00/);
  });

  it('shows the server error when the ledger could not be computed', async () => {
    render({ ...LEDGER, windows: {}, error: 'OperationalError: database is locked' });
    expect(await screen.findByTestId('profit-gaps-unavailable')).toHaveTextContent(
      'could not be computed: OperationalError: database is locked',
    );
  });

  it('shows the transport error when the request fails', async () => {
    render({ error: { code: 'internal', message: 'boom' } }, 500);
    expect(await screen.findByTestId('profit-gaps-unavailable')).toHaveTextContent('could not be read');
  });
});
