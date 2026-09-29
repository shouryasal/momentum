/**
 * The red state on Home that did not exist on 2026-09-24.
 *
 * For fourteen hours this screen said "The loop is alive and on schedule". It was true. The
 * risk gate had refused 655 consecutive buys since 03:00 behind a `data_stale` flag written
 * with `expires_at: null`, the strategy went on finding signals every cycle, and nothing —
 * not the console, not an alert, not a log anybody read — said the system had stopped.
 *
 * So these tests assert the words, not the markup. An operator opening Home has to be able
 * to read what is stopped, why, since when and what will clear it without knowing that
 * `blackout:data_stale` is a thing. And the banner must be impossible to arrange into a
 * tick: the last test here fails the day "alive" can sit alone on a blocked host.
 */
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '@/test/utils';

import { ControlCard } from './ControlCard';
import { LEVEL_ORDER, type Acting, type ControlPayload, type PreviewState } from './control.api';
import { actingSentence, blockedBanner, humaniseMinutes, supervisorWarning } from './controlWords';

/* -------------------------------------------------------------------------- the fixtures */

function bot(overrides: Partial<ControlPayload['bots'][string]> = {}) {
  return {
    bot: 'b',
    requested: 'proposing' as const,
    level: 'proposing' as const,
    ceiling: 'proposing' as const,
    clamped_by: [],
    since: null,
    set_by: null,
    reason: null,
    resume_level: null,
    decides: true,
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

/** Exactly what the server computed that night: 655 refusals, 14 hours, no way out. */
const OVERNIGHT: Acting = {
  as_of: '2026-09-24T20:00:00Z',
  window_hours: 24,
  entries_allowed: 0,
  entries_refused: 655,
  exits_allowed: 2,
  last_allowed_entry: '2026-09-24T01:05:07Z',
  minutes_since_allowed_entry: 1134.9,
  streak: 655,
  streak_reason: 'blackout:data_stale',
  streak_since: '2026-09-24T06:00:03Z',
  refusals: [
    {
      reason: 'blackout:data_stale',
      count: 655,
      words: 'data has been stale',
      since: '2026-09-24T06:00:03Z',
    },
  ],
  current_reason: 'blackout:data_stale',
  current_words: 'data has been stale',
  refused_since_last_allowed: 655,
  blocking_flags: [
    {
      name: 'data_stale',
      severity: 'block_entries',
      reason: 'data age 30 min',
      set_by: 'healthcheck',
      set_at: '2026-09-24T06:00:03Z',
      expires_at: null,
      scope: 'ALL',
      active_minutes: 840,
      can_expire: false,
      clears_when:
        'nothing clears it on a clock: it has no expiry, so it lifts only when healthcheck decides to lift it',
    },
  ],
  sources: [
    { source: 'book_snapshots', age_minutes: 860, age_minutes_allowed: 860, stale: true },
    { source: 'candles_1h', age_minutes: 880, age_minutes_allowed: 820, stale: true },
  ],
  phases: [
    {
      phase: 'candles',
      last_ok: '2026-09-24T06:20:03Z',
      last_fail: null,
      last_error: null,
      minutes_since_ok: 820,
      failing: false,
    },
    {
      phase: 'funding',
      last_ok: '2026-09-24T05:20:03Z',
      last_fail: '2026-09-24T20:00:00Z',
      last_error:
        "Client error '400 Bad Request' for url 'https://fapi.binance.com/fapi/v1/premiumIndex?symbol=PEPEUSDT'",
      minutes_since_ok: 880,
      failing: true,
    },
  ],
  verdict: 'not_trading',
  headline: 'Not trading: data has been stale since 10:00, 655 entries refused. Stale data: book_snapshots, candles_1h.',
  blocked_since: '2026-09-24T06:00:03Z',
  blocked_minutes: 840,
  clears_when:
    'nothing clears it on a clock: it has no expiry, so it lifts only when healthcheck decides to lift it',
  blocked_what: 'every new entry, on both bots',
};

/**
 * The host as it actually was: every job green, the crontab installed, and the system
 * switched off from the inside. `verdict: 'blocked'` is what the server sends now; the
 * `alive` variant below is what it used to send, and the banner has to survive both.
 */
const BLOCKED: ControlPayload = {
  verdict: 'blocked',
  headline: 'TRADING IS BLOCKED. Not trading: data has been stale since 10:00, 655 entries refused.',
  as_of: '2026-09-24T20:00:00Z',
  timezone: 'Asia/Dubai',
  schedule: { installed: true, matches: true, lines: 15, diff: '', error: null },
  bots: { a: bot({ bot: 'a' }), b: bot({ bot: 'b' }) },
  spend: { a: spend({ bot: 'a' }), b: spend({ bot: 'b' }) },
  state: {
    verified: true,
    corroborated: true,
    trusted: true,
    reason: 'ok',
    set_at: '2026-09-23T05:00:00Z',
    set_by: 'human:console:abc',
    mirror_ahead: false,
  },
  kill_engaged: false,
  jobs: [
    {
      job: 'ingest',
      required: 'watching',
      permitted: true,
      verdict: 'ok',
      last_ok: '2026-09-24T19:55:00Z',
      last_fail: null,
      last_skip: null,
      last_skip_reason: null,
      last_status: 'ok',
      next_fire: '2026-09-24T20:15:00Z',
      prev_fire: '2026-09-24T19:45:00Z',
      lock_held: false,
      minutes_late: null,
      note: null,
    },
  ],
  acting: OVERNIGHT,
  acting_error: null,
  supervisor: {
    unit: 'earn-console.service',
    verdict: 'supervised',
    note: 'earn-console.service is enabled in the user manager and active.',
    scope: 'user',
    enabled: 'enabled',
    active: 'active',
    error: null,
    install_hint: 'python -m ops.gen_ops_files --install-user-units',
  },
  modes: {
    a: { state: 'TEST', verified: true, is_live: false, is_demo: false },
    b: { state: 'TEST', verified: true, is_live: false, is_demo: false },
  },
  levels: [...LEVEL_ORDER],
  level_meaning: {
    off: 'nothing scheduled runs for this bot',
    watching: 'data and signals run; no proposals, no orders',
    proposing: 'the decision loop runs; every order waits for approval',
    trading: 'the loop runs and the gate executes without per-order approval',
  },
  phrases: { flatten: 'SELL EVERYTHING', arm_live_trading: 'TRADE LIVE UNATTENDED' },
  job_requirements: { ingest: 'watching' },
};

/** The same host, working: entries getting through and nothing blocking them. */
const TRADING: ControlPayload = {
  ...BLOCKED,
  verdict: 'alive',
  headline: 'The loop is alive and on schedule.',
  acting: {
    ...OVERNIGHT,
    entries_allowed: 6,
    entries_refused: 3,
    minutes_since_allowed_entry: 12,
    current_reason: 'weight_cap:BTC/USDT',
    current_words: 'weight_cap:BTC/USDT',
    refused_since_last_allowed: 0,
    streak: 0,
    streak_reason: null,
    streak_since: null,
    blocking_flags: [],
    sources: OVERNIGHT.sources.map((source) => ({ ...source, stale: false })),
    phases: OVERNIGHT.phases.map((phase) => ({ ...phase, failing: false })),
    verdict: 'trading',
    headline: 'Trading: 6 entries allowed and 3 refused in the last 24h.',
    blocked_since: null,
    blocked_minutes: null,
    clears_when: null,
    blocked_what: null,
  },
};

/** An older server, or one whose measurement failed. It must admit that, not imply health. */
const UNMEASURED: ControlPayload = {
  ...TRADING,
  acting: null,
  acting_error: 'RuntimeError: journal is locked',
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

function stubFetch(control: ControlPayload, units?: unknown) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
    if (url.startsWith('/api/control/units')) return json(units ?? { verified: true, units: [] });
    if (url.startsWith('/api/auth/step-up')) return json({ ok: true });
    if (url.startsWith('/api/control')) return json(control);
    if (url.startsWith('/api/preview')) return json(IDLE_PREVIEW);
    return json({ error: { code: 'not_found', message: url } }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/* ---------------------------------------------------------------------------- the words */

describe('the words for a blocked system', () => {
  it('says what is stopped, why, since when, and what will clear it', () => {
    const banner = blockedBanner(BLOCKED)!;
    expect(banner.title).toContain('TRADING IS BLOCKED');
    const labels = banner.facts.map((fact) => fact.label);
    expect(labels).toContain('What is stopped');
    expect(labels).toContain('Why');
    expect(labels).toContain('Since');
    expect(labels).toContain('What will clear it');
  });

  it('never makes the operator read a slug', () => {
    const banner = blockedBanner(BLOCKED)!;
    const why = banner.facts.find((fact) => fact.label === 'Why')!.value;
    expect(why).toContain('data has been stale');
    expect(why).toContain('655 buys refused since the last one got through');
    expect(banner.title).not.toContain('blackout:data_stale');
  });

  it('names the flag that cannot expire as the thing it is', () => {
    const banner = blockedBanner(BLOCKED)!;
    const flag = banner.facts.find((fact) => fact.label === 'Flag "data_stale"')!.value;
    expect(flag).toContain('set by healthcheck');
    expect(flag).toContain('It has no expiry, so it cannot lift itself');
  });

  it('carries the real cause, not just the symptom', () => {
    const banner = blockedBanner(BLOCKED)!;
    const phase = banner.facts.find((fact) => fact.label.includes('funding'))!.value;
    expect(phase).toContain('PEPEUSDT');
  });

  it('says how long it has been, in hours a person thinks in', () => {
    expect(humaniseMinutes(840)).toBe('14h 00m');
    expect(humaniseMinutes(45)).toBe('45m');
    expect(humaniseMinutes(2000)).toBe('1d 9h');
    expect(humaniseMinutes(null)).toBe('an unknown time');
  });

  it('explains why nothing looked wrong when the loop still calls itself alive', () => {
    const banner = blockedBanner({ ...BLOCKED, verdict: 'alive' })!;
    expect(banner.contradiction).toContain('Every scheduled job is running normally');
    expect(banner.contradiction).toContain('told you nothing');
  });

  it('does not exist at all on a host that is trading', () => {
    expect(blockedBanner(TRADING)).toBeNull();
  });

  it('counts outcomes out loud on a working host', () => {
    expect(actingSentence(TRADING)).toContain('6 buys allowed and 3 refused');
    expect(actingSentence(TRADING)).toContain('12m ago');
  });

  it('stays silent about outcomes while the banner is shouting', () => {
    expect(actingSentence(BLOCKED)).toBeNull();
  });

  it('admits when it could not measure, rather than implying health', () => {
    expect(actingSentence(UNMEASURED)).toContain('Cannot tell');
    expect(actingSentence(UNMEASURED)).toContain('journal is locked');
  });
});

describe('the words for a console that may not come back', () => {
  it('warns, with the reason it matters, when nothing supervises it', () => {
    const warning = supervisorWarning({ ...BLOCKED.supervisor!, verdict: 'unsupervised' })!;
    expect(warning.text).toContain('Nothing will restart this console');
    expect(warning.text).toContain('stopped trading');
    expect(warning.canInstall).toBe(true);
  });

  it('is louder when the unit exists and is not running', () => {
    const warning = supervisorWarning({
      ...BLOCKED.supervisor!,
      verdict: 'failing',
      active: 'failed',
    })!;
    expect(warning.tone).toBe('bad');
    expect(warning.text).toContain('it stays stopped');
  });

  it('says nothing when the probe could not answer', () => {
    expect(supervisorWarning({ ...BLOCKED.supervisor!, verdict: 'unknown' })).toBeNull();
  });

  it('says nothing when it is supervised', () => {
    expect(supervisorWarning(BLOCKED.supervisor)).toBeNull();
  });
});

/* ----------------------------------------------------------------------------- the card */

describe('Home on a blocked host', () => {
  it('shows TRADING IS BLOCKED in plain words', async () => {
    stubFetch(BLOCKED);
    renderWithProviders(<ControlCard />);

    const banner = await screen.findByTestId('trading-blocked');
    expect(banner).toHaveTextContent('TRADING IS BLOCKED');
    expect(banner).toHaveTextContent('data has been stale');
    expect(banner).toHaveTextContent('655 buys refused');
    expect(banner).toHaveTextContent('It has no expiry, so it cannot lift itself');
    expect(banner).toHaveTextContent('PEPEUSDT');
  });

  it('puts the red state above everything else on the card', async () => {
    stubFetch(BLOCKED);
    renderWithProviders(<ControlCard />);

    const banner = await screen.findByTestId('trading-blocked');
    const liveness = screen.getByTestId('liveness');
    // A banner under the money or under the per-bot rows is a banner nobody reads first.
    expect(banner.compareDocumentPosition(liveness) & Node.DOCUMENT_POSITION_FOLLOWING)
      .toBeTruthy();
  });

  it('cannot be arranged into a tick: a blocked host never shows one', async () => {
    stubFetch({ ...BLOCKED, verdict: 'alive', headline: 'The loop is alive and on schedule.' });
    renderWithProviders(<ControlCard />);

    // The old, true, useless sentence may still be there — but not on its own.
    expect(await screen.findByTestId('trading-blocked')).toBeInTheDocument();
    expect(screen.getByTestId('blocked-contradiction')).toHaveTextContent('told you nothing');
  });

  it('shows no banner and counts the buys on a working host', async () => {
    stubFetch(TRADING);
    renderWithProviders(<ControlCard />);

    await waitFor(() => expect(screen.getByTestId('liveness-headline')).toBeInTheDocument());
    expect(screen.queryByTestId('trading-blocked')).toBeNull();
    expect(screen.getByTestId('liveness-outcomes')).toHaveTextContent(
      '6 buys allowed and 3 refused',
    );
  });

  it('offers to make the console restart itself when nothing does', async () => {
    stubFetch({
      ...TRADING,
      supervisor: { ...TRADING.supervisor!, verdict: 'unsupervised', scope: null },
    });
    const user = userEvent.setup();
    renderWithProviders(<ControlCard />);

    const warning = await screen.findByTestId('supervisor-warning');
    expect(warning).toHaveTextContent('Nothing will restart this console');
    await user.click(screen.getByTestId('install-supervisor'));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('starts this console again');
    expect(dialog).toHaveTextContent('needs no password');
  });

  it('says so, quietly, when the console will come back', async () => {
    stubFetch(TRADING);
    renderWithProviders(<ControlCard />);
    expect(await screen.findByTestId('supervisor-ok')).toHaveTextContent('starts it again');
  });
});
