import { describe, expect, it } from 'vitest';

import {
  CommandRegistry,
  scoreCommand,
  searchCommands,
  type Command,
} from '../app/commandRegistry';
import { providerStatus } from '../api/contracts';
import { pendingApprovalCount } from '../app/approvals';
import { healthSummaryFrom } from '../app/health';
import { isTransitioning, modeLabel, modeSummary, runDay } from '../app/ModeBadges';
import { providerLabel } from '../app/ProviderChip';
import { SAFETY_PILLS, stripFrom } from '../app/SafetyStrip';
import { collapseContext, diffLines, diffStats } from '../lib/diff';
import { CronParseError, isValidCron, nextCronFires, parseCron } from '../lib/cron';
import { formatDuration, formatFractionPct, formatRelative, formatUsd, formatUtcStamp } from '../lib/format';
import { deleteAt, getAt, pathToString, setAt } from '../lib/objectPath';

describe('cron', () => {
  it('parses lists, ranges and steps', () => {
    const parts = parseCron('0,30 4-6 * * 1-5');
    expect(parts.minute).toEqual([0, 30]);
    expect(parts.hour).toEqual([4, 5, 6]);
    expect(parts.dayOfWeek).toEqual([1, 2, 3, 4, 5]);
    expect(parseCron('*/15 * * * *').minute).toEqual([0, 15, 30, 45]);
  });

  it('rejects malformed expressions', () => {
    expect(() => parseCron('* * *')).toThrow(CronParseError);
    expect(() => parseCron('61 * * * *')).toThrow(CronParseError);
    expect(() => parseCron('* * * * 9')).toThrow(CronParseError);
    expect(isValidCron('@reboot')).toBe(false);
  });

  it('computes the next fires in UTC', () => {
    const from = new Date('2026-09-22T04:00:00Z');
    const fires = nextCronFires('30 4 * * *', 3, from);
    expect(fires.map((fire) => fire.toISOString())).toEqual([
      '2026-09-22T04:30:00.000Z',
      '2026-09-23T04:30:00.000Z',
      '2026-09-24T04:30:00.000Z',
    ]);
  });

  it('ORs day-of-month with day-of-week when both are restricted', () => {
    const fires = nextCronFires('0 0 1 * 0', 2, new Date('2026-09-22T00:00:00Z'));
    expect(fires[0]?.toISOString()).toBe('2026-09-27T00:00:00.000Z'); // Sunday
    expect(fires[1]?.toISOString()).toBe('2026-10-01T00:00:00.000Z'); // 1st
  });
});

describe('diff', () => {
  it('produces add/del/context lines', () => {
    const lines = diffLines('a\nb\nc', 'a\nB\nc');
    expect(diffStats(lines)).toEqual({ added: 1, removed: 1, changed: true });
    expect(lines.map((line) => line.kind)).toEqual(['context', 'del', 'add', 'context']);
  });

  it('reports no change for identical text', () => {
    expect(diffStats(diffLines('same\n', 'same')).changed).toBe(false);
  });

  it('collapses long unchanged runs', () => {
    const before = Array.from({ length: 30 }, (_, i) => `line ${i}`).join('\n');
    const after = before.replace('line 15', 'line fifteen');
    const collapsed = collapseContext(diffLines(before, after), 2);
    expect(collapsed.filter((entry) => entry === 'gap').length).toBe(2);
    expect(collapsed.length).toBeLessThan(31);
  });
});

describe('objectPath', () => {
  it('renders dotted and indexed paths', () => {
    expect(pathToString(['risk', 'max_dd'])).toBe('risk.max_dd');
    expect(pathToString(['pairs', 0, 'symbol'])).toBe('pairs[0].symbol');
  });

  it('gets, sets and deletes immutably', () => {
    const source = { risk: { max_dd: 0.1 }, pairs: ['BTC/USDT'] };
    expect(getAt(source, ['risk', 'max_dd'])).toBe(0.1);
    const updated = setAt(source, ['risk', 'max_dd'], 0.2);
    expect(updated.risk.max_dd).toBe(0.2);
    expect(source.risk.max_dd).toBe(0.1);
    const appended = setAt(source, ['pairs', 1], 'ETH/USDT');
    expect(appended.pairs).toEqual(['BTC/USDT', 'ETH/USDT']);
    expect(deleteAt(source, ['risk', 'max_dd']).risk).toEqual({});
    expect(deleteAt(source, ['pairs', 0]).pairs).toEqual([]);
  });

  it('creates missing containers on the way down', () => {
    expect(setAt({}, ['a', 'b', 'c'], 1)).toEqual({ a: { b: { c: 1 } } });
  });
});

describe('format', () => {
  it('formats UTC stamps with a trailing Z', () => {
    expect(formatUtcStamp('2026-09-22T04:30:00Z')).toBe('22/09/2026 04:30Z');
    expect(formatUtcStamp(null)).toBe('—');
  });

  it('formats fractions as percentages', () => {
    expect(formatFractionPct(0.0425)).toBe('4.25%');
    expect(formatUsd(-1234.5)).toBe('-$1,234.50');
    expect(formatDuration(3_725_000)).toBe('1h 2m');
  });

  it('formats relative times in both directions', () => {
    const now = new Date('2026-09-22T04:30:00Z');
    expect(formatRelative('2026-09-22T04:25:00Z', now)).toBe('5m ago');
    expect(formatRelative('2026-09-22T05:30:00Z', now)).toBe('in 1h');
  });
});

describe('health summary', () => {
  const base = {
    as_of: '2026-09-22T04:30:00Z',
    freshness_minutes: { 'BTC/USDT-1h': 10 },
    data_age_minutes: 10,
    staleness_limit_min: 120,
    open_incidents: 0,
    undelivered_alerts: 0,
  };

  it('is ok when data is fresh and nothing is open', () => {
    const summary = healthSummaryFrom(base);
    expect(summary?.status).toBe('ok');
    expect(summary?.ts).toBe('2026-09-22T04:30:00Z');
  });

  it('fails on stale data and warns on open incidents', () => {
    expect(healthSummaryFrom({ ...base, data_age_minutes: 300 })?.status).toBe('fail');
    expect(healthSummaryFrom({ ...base, open_incidents: 2 })?.status).toBe('warn');
    expect(healthSummaryFrom({ ...base, data_age_minutes: 100 })?.status).toBe('warn');
  });

  it('is unknown, never ok, when a number is missing', () => {
    expect(healthSummaryFrom({ ...base, data_age_minutes: null })?.status).toBe('unknown');
    expect(healthSummaryFrom(null)).toBeNull();
  });
});

describe('mode badges', () => {
  const mode = {
    sleeve: 'a',
    state: 'TEST',
    submode: null,
    run_id: null,
    seed_usdt: 10_000,
    is_live: false,
  };

  it('labels every mode state', () => {
    expect(modeSummary(mode)).toBe('Rules bot: TEST · seed 10,000');
    expect(modeLabel({ ...mode, state: 'LIVE_PROPOSE' })).toBe('LIVE·PROPOSE');
    expect(modeLabel({ ...mode, state: 'LIVE_EXECUTE' })).toBe('LIVE·EXECUTE');
    expect(modeLabel({ ...mode, state: 'ARMING' })).toBe('TRANSITIONING');
    expect(isTransitioning({ ...mode, state: 'DISARMING' })).toBe(true);
    expect(isTransitioning(mode)).toBe(false);
  });

  it('omits the seed when the mode state carries none', () => {
    expect(modeSummary({ ...mode, sleeve: 'b', seed_usdt: null })).toBe('AI bot: TEST');
  });

  it('counts the run day from the date inside the run id', () => {
    const now = new Date('2026-11-07T09:00:00Z');
    expect(runDay('test-a-20261027-01', now)).toBe(12);
    expect(runDay('test-a-20261107-01', now)).toBe(1);
    expect(modeSummary({ ...mode, run_id: 'test-a-20261027-01' }, now)).toBe(
      'Rules bot: TEST · seed 10,000 · day 12',
    );
  });

  it('shows no day rather than a wrong one when the run id carries no date', () => {
    const now = new Date('2026-11-07T09:00:00Z');
    expect(runDay(null, now)).toBeNull();
    // A research run_id, not a sleeve run id — no `-YYYYMMDD` group.
    expect(runDay('2026-09-22T08:30+04:00', now)).toBeNull();
    // A run opened in the future is not day zero or a negative day.
    expect(runDay('test-a-20261231-01', now)).toBeNull();
  });
});

describe('pending approvals', () => {
  const item = {
    run_id: 'r1',
    ts_utc: '2026-09-22T04:30:00Z',
    valid: true,
    abstain: false,
    status: 'pending',
    decision: null,
    expires_utc: '2026-09-22T16:30:00Z',
    seconds_left: 3600,
  };

  it('counts only undecided, unexpired rows', () => {
    expect(
      pendingApprovalCount({
        requires_approval: true,
        ttl_hours: 12,
        items: [
          item,
          { ...item, run_id: 'r2', decision: 'approve', status: 'approved' },
          { ...item, run_id: 'r3', seconds_left: 0 },
        ],
      }),
    ).toBe(1);
  });

  it('is zero when no sleeve requires approval, and when there is no answer yet', () => {
    expect(pendingApprovalCount({ requires_approval: false, ttl_hours: 12, items: [item] })).toBe(0);
    expect(pendingApprovalCount(undefined)).toBe(0);
  });
});

describe('provider status', () => {
  const card = {
    key: 'claude:subscription' as const,
    kind: 'claude_sdk' as const,
    enabled: true,
    credential_present: true,
    auth_source: 'token',
    detail: '',
    circuit: 'closed' as const,
    consecutive_failures: 0,
    open_until: null,
    last_ok_utc: null,
    last_error: null,
    degraded_until: null,
  };

  it('reads the traffic light off the credential and the breaker', () => {
    expect(providerStatus(card)).toBe('ok');
    expect(providerStatus({ ...card, circuit: 'open' })).toBe('fail');
    expect(providerStatus({ ...card, credential_present: false })).toBe('fail');
    expect(providerStatus({ ...card, consecutive_failures: 2 })).toBe('warn');
  });

  it('does not call a switched-off provider a failure', () => {
    expect(providerStatus({ ...card, enabled: false, credential_present: false })).toBe('unknown');
  });

  it('labels a provider from its key', () => {
    expect(providerLabel(card)).toBe('Claude subscription');
    expect(providerLabel({ ...card, key: 'ollama' })).toBe('Ollama');
  });
});

describe('safety strip', () => {
  it('reads the five spec pills, in order, from /api/invariants/strip', () => {
    const items = stripFrom({
      ok: false,
      counts: {},
      strip: {
        'config-blessed': 'fail',
        'gate-in-order-path': 'ok',
        'some-new-pill': 'warn',
      },
    });
    expect(items.slice(0, 5).map((item) => item.pill)).toEqual([...SAFETY_PILLS]);
    expect(items[0]?.status).toBe('ok');
    // Not reported by the server: unknown, never a tick.
    expect(items[1]?.status).toBe('unknown');
    expect(items[4]?.status).toBe('fail');
    // A pill the server grows later is appended rather than dropped.
    expect(items[5]).toEqual({ pill: 'some-new-pill', status: 'warn', detail: 'from /api/invariants' });
  });

  it('falls back to /api/meta keys, taking the worst status per pill', () => {
    const items = stripFrom(null, [
      { key: 'console_bind', title: '', enforced_by: '', status: 'ok', detail: null },
      { key: 'mode_signed', title: '', enforced_by: '', status: 'ok', detail: null },
      { key: 'committed_dry_run', title: '', enforced_by: '', status: 'fail', detail: 'live' },
      { key: 'unmapped', title: '', enforced_by: '', status: 'ok', detail: null },
    ]);
    const byPill = new Map(items.map((item) => [item.pill, item.status]));
    expect(byPill.get('console-127.0.0.1')).toBe('ok');
    // mode_signed ok + committed_dry_run fail both back live-entry-human-only.
    expect(byPill.get('live-entry-human-only')).toBe('fail');
    expect(byPill.get('gate-in-order-path')).toBe('unknown');
    expect(byPill.has('unmapped')).toBe(false);
  });
});

describe('commandRegistry', () => {
  const make = (id: string, title: string, group = 'Pages'): Command => ({
    id,
    title,
    group,
    run: () => undefined,
  });

  it('registers and unregisters commands', () => {
    const registry = new CommandRegistry();
    const remove = registry.register([make('a', 'Overview'), make('b', 'Risk')]);
    expect(registry.list()).toHaveLength(2);
    remove();
    expect(registry.list()).toHaveLength(0);
  });

  it('notifies subscribers', () => {
    const registry = new CommandRegistry();
    let calls = 0;
    const unsubscribe = registry.subscribe(() => {
      calls += 1;
    });
    registry.register([make('a', 'Overview')]);
    expect(calls).toBe(1);
    unsubscribe();
    registry.register([make('b', 'Risk')]);
    expect(calls).toBe(1);
  });

  it('ranks substring matches above subsequence matches', () => {
    const overview = make('a', 'Overview');
    const operations = make('b', 'Operations');
    expect(scoreCommand(overview, 'over')).toBeGreaterThan(scoreCommand(operations, 'over'));
    expect(scoreCommand(overview, 'zzz')).toBe(-1);
    const hits = searchCommands([overview, operations], 'ovw');
    expect(hits[0]?.id).toBe('a');
  });
});
