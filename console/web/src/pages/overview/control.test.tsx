/**
 * The control the owner asked for, and the two things it must never let anyone confuse.
 *
 * 1. "The bots are up" is not "the system is deciding". A host with nothing installed on
 *    its timer must say so in those words, never as a tick and never as silence.
 * 2. "Whose money" and "how much it does by itself" are two switches, not one. Play money
 *    on its own is not safe if it is also buying unattended, and real money that only
 *    proposes is not dangerous — so the two are read separately, always.
 *
 * And the preview: it has to show a real plan and what the safety checks would do with it,
 * and it has to be unmistakable as a preview.
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '@/test/utils';

import { ControlCard, actorText } from './ControlCard';
import { refusalText } from './Preview';
import type { ControlPayload, PreviewResult, PreviewState } from './control.api';
import {
  botSentence,
  clampSentence,
  levelMeaning,
  moneyKind,
  moveWarning,
  needsLivePhrase,
  nextRunSentence,
  spendSentence,
  spendTone,
  troubleSummary,
} from './controlWords';
import { LEVEL_ORDER } from './control.api';

/* ------------------------------------------------------------------------- the fixtures */

function bot(overrides: Partial<ControlPayload['bots'][string]> = {}) {
  return {
    bot: 'b',
    requested: 'off' as const,
    level: 'off' as const,
    ceiling: 'proposing' as const,
    clamped_by: [],
    since: null,
    set_by: null,
    reason: null,
    resume_level: null,
    decides: false,
    executes: false,
    ...overrides,
  };
}

function spend(overrides: Partial<ControlPayload['spend'][string]> = {}) {
  return {
    bot: 'b',
    day_usd: 0.4,
    month_usd: 12,
    day_cap: 4,
    month_cap: 150,
    day_pct: 10,
    month_pct: 8,
    over_day: false,
    over_month: false,
    at_cap: 'degrade',
    action: 'ok',
    ...overrides,
  };
}

/** A host exactly as this one was found: switched on, with nothing installed to run it. */
const NOT_SCHEDULED: ControlPayload = {
  verdict: 'not_scheduled',
  headline:
    'NOTHING IS SCHEDULED. Bots are switched on but no crontab is installed on this host, so no run will ever start by itself. Press Start.',
  as_of: '2026-09-24T06:00:00Z',
  timezone: 'Asia/Dubai',
  schedule: { installed: false, matches: false, lines: 0, diff: '', error: null },
  bots: {
    a: bot({ bot: 'a', requested: 'watching', level: 'watching' }),
    b: bot({ bot: 'b', requested: 'proposing', level: 'proposing', decides: true }),
  },
  spend: { a: spend({ bot: 'a' }), b: spend({ bot: 'b' }) },
  state: {
    verified: true,
    corroborated: true,
    trusted: true,
    reason: 'ok',
    set_at: '2026-09-24T05:00:00Z',
    set_by: 'human:console:abc',
    mirror_ahead: false,
  },
  kill_engaged: false,
  jobs: [
    {
      job: 'research_run',
      required: 'proposing',
      permitted: true,
      verdict: 'never',
      last_ok: null,
      last_fail: null,
      last_skip: null,
      last_skip_reason: null,
      last_status: null,
      next_fire: '2026-09-24T04:30:00Z',
      prev_fire: null,
      lock_held: false,
      minutes_late: null,
      note: 'has never completed',
    },
  ],
  modes: {
    a: { state: 'TEST', verified: true, is_live: false, is_demo: false },
    b: { state: 'TEST', verified: true, is_live: false, is_demo: false },
  },
  levels: [...LEVEL_ORDER],
  level_meaning: {
    off: 'nothing scheduled runs for this bot',
    watching: 'data, signals and the local holdings watcher run; no proposals, no orders',
    proposing: 'the decision loop runs and writes proposals; every order waits for approval',
    trading: 'the loop runs and the gate executes without per-order approval',
  },
  phrases: { flatten: 'FLATTEN ALL', arm_live_trading: 'TRADE LIVE UNATTENDED' },
  job_requirements: { research_run: 'proposing' },
};

/** The same host after Pause: both bots watching, and the loop alive but making no plans. */
const PAUSED: ControlPayload = {
  ...NOT_SCHEDULED,
  verdict: 'alive',
  headline: 'The loop is alive and on schedule.',
  schedule: { installed: true, matches: true, lines: 14, diff: '', error: null },
  bots: {
    a: bot({ bot: 'a', requested: 'watching', level: 'watching' }),
    b: bot({
      bot: 'b',
      requested: 'watching',
      level: 'watching',
      resume_level: 'proposing',
      since: '2026-09-24T06:00:00Z',
    }),
  },
  jobs: [
    {
      ...NOT_SCHEDULED.jobs[0]!,
      job: 'ingest',
      required: 'watching',
      verdict: 'ok',
      last_ok: '2026-09-24T05:45:00Z',
      next_fire: '2026-09-24T06:15:00Z',
      note: null,
    },
  ],
};

const IDLE_PREVIEW: PreviewState = {
  budget: {
    max_usd: 2,
    min_interval_s: 600,
    wait_s: 0,
    ready: true,
    last_started_utc: null,
    last_cost_usd: null,
  },
  last: null,
  note: 'A preview runs the real decision and the real safety checks, and places nothing.',
};

const REAL_PLAN: PreviewResult = {
  preview: true,
  run_id: '2026-09-24T10:00+04:00',
  preview_id: 'preview-20260924T060000Z',
  started_utc: '2026-09-24T06:00:00Z',
  finished_utc: '2026-09-24T06:03:00Z',
  requested_model: 'claude-opus-5',
  served_model: 'claude-opus-5',
  effort: 'high',
  escalation_reasons: [],
  hard_case_flags: [],
  cost_usd: 0.42,
  prompt_version: 'research.v4',
  wrote_nothing: true,
  ok: true,
  error: null,
  plan: {
    module: 'trend',
    abstain: false,
    targets: { BTC: 0.6, ETH: 0.2, CASH: 0.2 },
    exposure_scale: 1,
    confidence: 0.6,
    horizon_days: 7,
    rationale: ['Trend is intact on the daily.', 'Volatility is inside its normal band.'],
    invalidation: 'a daily close below the 50-day average',
  },
  gate: {
    available: true,
    sleeve: 'b',
    nav_usdt: 10000,
    nav_valid: true,
    nav_reason: '',
    kill_engaged: false,
    verdicts: [
      { pair: 'BTC/USDT', stake_usdt: 6000, allowed: false, reason: 'weight_cap:BTC/USDT', failed: ['weight_cap'] },
      { pair: 'ETH/USDT', stake_usdt: 2000, allowed: true, reason: 'ok', failed: [] },
    ],
    allowed: 1,
    refused: 1,
    writes_suppressed: [],
  },
};

const ABSTAINED: PreviewResult = {
  ...REAL_PLAN,
  plan: {
    ...REAL_PLAN.plan!,
    abstain: true,
    targets: { CASH: 1 },
    rationale: ['The newest candle is a day old.'],
  },
  gate: { ...REAL_PLAN.gate!, verdicts: [], allowed: 0, refused: 0 },
};

function stubFetch(control: ControlPayload | null, preview: PreviewState = IDLE_PREVIEW,
                   run?: PreviewResult) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = init?.method ?? 'GET';
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), {
        status,
        headers: { 'content-type': 'application/json' },
      });
    if (url.startsWith('/api/control')) {
      return control ? json(control) : json({ error: { code: 'unavailable', message: 'no' } }, 503);
    }
    if (url.startsWith('/api/preview')) {
      if (method === 'POST') {
        return run
          ? json(run)
          : json({ error: { code: 'rate_limited', message: 'wait 9 minutes' } }, 429);
      }
      return json(preview);
    }
    return json({ error: { code: 'not_found', message: url } }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/* ------------------------------------------------------------------------------ the card */

describe('the control on Home', () => {
  it('says nothing is scheduled rather than showing a tick', async () => {
    stubFetch(NOT_SCHEDULED);
    renderWithProviders(<ControlCard />);

    const headline = await screen.findByTestId('liveness-headline');
    expect(headline).toHaveTextContent('NOTHING IS SCHEDULED');
    expect(screen.getByTestId('liveness-next')).toHaveTextContent(
      'this system is not running by itself',
    );
  });

  it('shows whose money and how much it does by itself as two separate things', async () => {
    stubFetch(NOT_SCHEDULED);
    renderWithProviders(<ControlCard />);

    await waitFor(() => expect(screen.getByTestId('control-bot-b')).toBeInTheDocument());
    expect(screen.getByTestId('money-b')).toHaveTextContent('Play money');
    expect(screen.getByTestId('level-b')).toHaveTextContent('Planning, asks me first');
    // Two badges, not one combined word.
    expect(screen.getByTestId('money-b')).not.toBe(screen.getByTestId('level-b'));
    expect(screen.getByTestId('bot-sentence-b')).toHaveTextContent('waits for you');
  });

  it('offers Start on a bot that is off, and Pause on one that is not', async () => {
    stubFetch(NOT_SCHEDULED);
    renderWithProviders(<ControlCard />);

    await waitFor(() => expect(screen.getByTestId('control-bot-b')).toBeInTheDocument());
    // b is proposing, so it can be paused; a is watching, so it can be paused too.
    expect(screen.getByTestId('pause-b')).toBeInTheDocument();
    expect(screen.queryByTestId('start-b')).toBeNull();
  });

  it('asks before it moves a bot, and says what the move will do', async () => {
    stubFetch(NOT_SCHEDULED);
    const user = userEvent.setup();
    renderWithProviders(<ControlCard />);

    await waitFor(() => expect(screen.getByTestId('pause-b')).toBeInTheDocument());
    await user.click(screen.getByTestId('pause-b'));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('stops making plans');
    expect(dialog).toHaveTextContent('left exactly as it is');
  });

  it('makes selling everything type a phrase, and says it is not the emergency stop', async () => {
    stubFetch(NOT_SCHEDULED);
    const user = userEvent.setup();
    renderWithProviders(<ControlCard />);

    await waitFor(() => expect(screen.getByTestId('flatten-b')).toBeInTheDocument());
    await user.click(screen.getByTestId('flatten-b'));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('FLATTEN ALL');
    expect(dialog).toHaveTextContent('not the emergency stop');
  });

  it('shows the spend against the ceiling beside the switch that causes it', async () => {
    stubFetch(NOT_SCHEDULED);
    renderWithProviders(<ControlCard />);
    expect(await screen.findByTestId('spend-b')).toHaveTextContent('$12.00 of $150.00 this month');
  });

  it('says who changed it without printing a session id at you', () => {
    expect(actorText('human:console:i5LcbyqTzdGRuyJG')).toBe(' by you');
    expect(actorText('human:telegram:shourya')).toBe(' by telegram');
    expect(actorText(null)).toBe('');
  });

  it('admits it cannot tell when the control cannot be read', async () => {
    stubFetch(null);
    renderWithProviders(<ControlCard />);
    expect(await screen.findByTestId('control-unavailable')).toHaveTextContent('Cannot tell');
  });
});

/* ------------------------------------------------------------- the liveness line's words */

describe('what the liveness line says', () => {
  it('changes visibly when the bots are paused', () => {
    const before = nextRunSentence(NOT_SCHEDULED);
    const after = nextRunSentence(PAUSED);
    expect(before).not.toEqual(after);
    expect(before).toContain('not running by itself');
    expect(after).toContain('Next: fetch new prices and news');
  });

  /**
   * The point of Pause, said on the line that reports liveness.
   *
   * After a pause the schedule is still installed and the jobs still fire, so the headline
   * stays "alive" — which on its own would look exactly like a system that is still making
   * decisions. This line is what has to change, and it is the same distinction the owner
   * asked about: the bots being up is not the system deciding.
   */
  it('says nothing is deciding while every bot is only watching', () => {
    const deciding: ControlPayload = {
      ...PAUSED,
      bots: {
        a: bot({ bot: 'a', level: 'watching' }),
        b: bot({ bot: 'b', level: 'proposing', decides: true }),
      },
    };
    expect(nextRunSentence(deciding)).not.toContain('Nothing is deciding');
    expect(nextRunSentence(PAUSED)).toContain('Nothing is deciding');
    expect(nextRunSentence(PAUSED)).toContain('Next: fetch new prices and news');
  });

  it('never softens "nothing is scheduled" into a quiet day', () => {
    expect(nextRunSentence(NOT_SCHEDULED)).not.toMatch(/nothing due|all quiet|nothing to do/i);
  });

  it('says both bots are off plainly rather than reporting health', () => {
    const off: ControlPayload = { ...PAUSED, verdict: 'off' };
    expect(nextRunSentence(off)).toContain('Both bots are off');
  });

  it('surfaces only the jobs that are allowed to run and are not fine', () => {
    // One permitted job, and it has never run — so it is the whole permitted set, and the
    // summary says that once rather than listing it as if it were an isolated failure.
    expect(troubleSummary(NOT_SCHEDULED.jobs)?.lines).toEqual([
      'None of the 1 scheduled jobs has run yet, so nothing has happened by itself so far.',
    ]);
    expect(troubleSummary(PAUSED.jobs)).toBeNull();
  });

  it('names a job that is failing on its own rather than burying it in a list', () => {
    const jobs = [
      { ...PAUSED.jobs[0]!, job: 'ingest', verdict: 'ok' as const, note: null },
      { ...PAUSED.jobs[0]!, job: 'research_run', verdict: 'late', note: 'due 40 minutes ago' },
    ];
    const summary = troubleSummary(jobs);
    expect(summary?.lines).toEqual(['work out what to hold — due 40 minutes ago']);
    expect(summary?.more).toBe(0);
  });
});

/* --------------------------------------------------------------- the two switches, apart */

describe('whose money and how much it does by itself', () => {
  it('reads an unverified mode as play money, never as real', () => {
    expect(moneyKind({ state: 'LIVE_EXECUTE', verified: false, is_live: true, is_demo: false }))
      .toBe('simulated');
    expect(moneyKind({ state: 'LIVE_EXECUTE', verified: true, is_live: true, is_demo: false }))
      .toBe('real');
    expect(moneyKind(undefined)).toBe('simulated');
  });

  it('gives every level a sentence a person can read', () => {
    for (const level of LEVEL_ORDER) {
      expect(levelMeaning(level).length).toBeGreaterThan(30);
    }
  });

  it('describes a bot by both switches at once, never by one', () => {
    const real = { state: 'LIVE_EXECUTE', verified: true, is_live: true, is_demo: false };
    const trading = bot({ level: 'trading', decides: true, executes: true });
    const sentence = botSentence(trading, real);
    expect(sentence).toContain('real money');
    expect(sentence).toContain('safety checks');

    const watching = botSentence(bot({ level: 'watching' }), real);
    expect(watching).toContain('buys nothing');
    expect(watching).not.toContain('real money');
  });

  it('demands the typed phrase only where real money would trade unattended', () => {
    const real = { state: 'LIVE_EXECUTE', verified: true, is_live: true, is_demo: false };
    const test = { state: 'TEST', verified: true, is_live: false, is_demo: false };
    expect(needsLivePhrase('trading', real)).toBe(true);
    expect(needsLivePhrase('proposing', real)).toBe(false);
    expect(needsLivePhrase('trading', test)).toBe(false);
  });

  it('warns about unattended real money in those words', () => {
    const real = { state: 'LIVE_EXECUTE', verified: true, is_live: true, is_demo: false };
    expect(moveWarning('trading', real)).toContain('without asking you first');
    expect(moveWarning('off', real)).toContain('Nothing is sold');
  });

  it('explains a level that was lowered rather than showing the one asked for', () => {
    const clamped = bot({ requested: 'trading', level: 'watching', clamped_by: ['kill_engaged'] });
    expect(clampSentence(clamped)).toContain('emergency stop');
    expect(clampSentence(bot({ requested: 'off', level: 'off' }))).toBeNull();
  });

  it('flags spend that has reached its ceiling', () => {
    const over = spend({ over_month: true, month_usd: 150, month_pct: 100, action: 'degrade' });
    expect(spendTone(over)).toBe('over');
    expect(spendSentence(over)).toContain('cheaper models');
    expect(spendTone(spend({ month_pct: 90 }))).toBe('warn');
    expect(spendTone(spend())).toBe('ok');
  });
});

/* --------------------------------------------------------------------------- the preview */

describe('what would it do right now', () => {
  it('renders the plan it would make, and what the safety checks would do with it', async () => {
    stubFetch(NOT_SCHEDULED, IDLE_PREVIEW, REAL_PLAN);
    const user = userEvent.setup();
    renderWithProviders(<ControlCard />);

    await waitFor(() => expect(screen.getByTestId('preview-run')).toBeEnabled());
    await user.click(screen.getByTestId('preview-run'));

    const result = await screen.findByTestId('preview-result');
    expect(within(result).getByTestId('preview-badge')).toHaveTextContent(
      'nothing was placed or saved',
    );
    const targets = within(result).getByTestId('preview-targets');
    expect(targets).toHaveTextContent('BTC');
    expect(targets).toHaveTextContent('60.0%');
    expect(targets).toHaveTextContent('would be refused');
    expect(targets).toHaveTextContent('too much of one coin');
    expect(targets).toHaveTextContent('would be allowed');
    expect(within(result).getByTestId('preview-reasoning')).toHaveTextContent(
      'Trend is intact on the daily.',
    );
    expect(within(result).getByTestId('preview-footer')).toHaveTextContent('nothing was written');
  });

  it('says plainly when it would decline to trade', async () => {
    stubFetch(NOT_SCHEDULED, IDLE_PREVIEW, ABSTAINED);
    const user = userEvent.setup();
    renderWithProviders(<ControlCard />);

    await waitFor(() => expect(screen.getByTestId('preview-run')).toBeEnabled());
    await user.click(screen.getByTestId('preview-run'));

    expect(await screen.findByTestId('preview-abstain')).toHaveTextContent('chose not to trade');
    expect(screen.queryByTestId('preview-targets')).toBeNull();
  });

  it('refuses to run again too soon, and says how long to wait', async () => {
    stubFetch(NOT_SCHEDULED, {
      ...IDLE_PREVIEW,
      budget: { ...IDLE_PREVIEW.budget, ready: false, wait_s: 300 },
    });
    renderWithProviders(<ControlCard />);
    await waitFor(() => expect(screen.getByTestId('preview-run')).toBeDisabled());
  });

  it('shows the server’s refusal rather than a blank panel', async () => {
    stubFetch(NOT_SCHEDULED, IDLE_PREVIEW);
    const user = userEvent.setup();
    renderWithProviders(<ControlCard />);

    await waitFor(() => expect(screen.getByTestId('preview-run')).toBeEnabled());
    await user.click(screen.getByTestId('preview-run'));
    expect(await screen.findByTestId('preview-error')).toHaveTextContent('wait 9 minutes');
  });

  it('turns every safety-check name into words, and keeps the raw one', () => {
    expect(refusalText('staleness')).toContain('price data is too old');
    expect(refusalText('weight_cap:BTC/USDT')).toContain('(weight_cap:BTC/USDT)');
    expect(refusalText('something_new')).toBe('something_new');
  });
});
