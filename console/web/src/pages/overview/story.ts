/**
 * Home, as sentences.
 *
 * Home used to be eight panels of counts: gate tallies, a funnel, job schedules, open
 * incidents, DB freshness, provider usage, "what changed today". The owner's complaint was
 * exact — *"why are errors and tech on home screen"* — so the numbers did not move to a
 * smaller font, they moved behind the detail pane, and what is left on the screen is what
 * a person would say out loud.
 *
 * Everything here is a pure function of the bundle the page already loaded, so the screen
 * has no second opinion about what happened, and `overview.test.tsx` can hold the wording
 * without rendering anything.
 *
 * The rule every function in this file keeps: no raw id, no hash, no file path, no model
 * id, no invariant key and no bare number. A number always arrives inside the phrase that
 * says what it is.
 */

import { sleeveFullName } from '@/lib/plain';

import type { OverviewPayload } from './api';

/* --------------------------------------------------------------------------- the clock */

const clockFormat = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'Asia/Dubai',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

/** `2026-09-23T08:30:00Z` -> `08:30`, in the operator's own time. */
export function clock(ts: string | null | undefined): string | null {
  if (!ts) return null;
  const date = new Date(ts);
  return Number.isNaN(date.getTime()) ? null : clockFormat.format(date);
}

function money(value: number | null | undefined): string | null {
  if (value === null || value === undefined || !Number.isFinite(value)) return null;
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function amount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  const digits = Math.abs(value) >= 1 ? 4 : 6;
  return String(Number(value.toFixed(digits)));
}

/* ------------------------------------------------------------------- is it running, how */

/**
 * What kind of money each bot is playing with, said the way the owner says it.
 *
 * The state word (`TEST`, `LIVE·EXECUTE`) stays on the badges in the header, where it is a
 * safety affordance. Down here it is a sentence, because "am I risking anything" is the
 * first question and it should not need a legend.
 */
export const MODE_PHRASE: Record<string, string> = {
  TEST: 'play money',
  LIVE_PROPOSE: 'real money, and it asks you before every trade',
  LIVE_EXECUTE: 'real money',
  ARMING: 'switching over to real money right now',
  DISARMING: 'switching back to play money right now',
};

export function modePhrase(state: string | null | undefined, submode?: string | null): string {
  const phrase = MODE_PHRASE[String(state ?? '').toUpperCase()];
  if (phrase) {
    // A demo submode is still play money, but it is play money on the real exchange's
    // test network — worth saying, because the orders are real order flow.
    if (phrase === 'play money' && String(submode ?? '').toLowerCase().includes('demo')) {
      return "play money on the exchange's test network";
    }
    return phrase;
  }
  return 'an unrecognised mode — treat it as not checked';
}

/** One line: is it running, and with what kind of money. */
export function runningLine(data: OverviewPayload): string {
  const sleeves = Object.entries(data.mode?.sleeves ?? {});
  if (sleeves.length === 0) return 'No bot is running yet.';
  if (data.mode?.kill?.engaged) {
    return 'Trading is stopped. You pressed the kill switch, and only you can start it again.';
  }
  const phrases = new Set(sleeves.map(([, mode]) => modePhrase(mode.state, mode.submode)));
  if (phrases.size === 1) {
    const only = [...phrases][0] as string;
    return sleeves.length === 1
      ? `One bot is running with ${only}.`
      : `Both bots are running with ${only}.`;
  }
  return sleeves
    .map(([sleeve, mode]) => `${sleeveFullName(sleeve)} is on ${modePhrase(mode.state, mode.submode)}`)
    .join('; ') + '.';
}

export interface SystemStatus {
  ok: boolean;
  /** The whole technical health block, reduced to one line a person reads. */
  headline: string;
  /** Why it is not fine, in sentences. Shown in the detail pane, never on the screen. */
  reasons: string[];
}

/**
 * The one calm status line that replaced the health block.
 *
 * Green is not "every check returned ok" — it is "there is nothing here for you to do".
 * That is the only reading the owner can act on, and it is why an open incident counts
 * while a stale freshness number does not.
 */
export function systemStatus(data: OverviewPayload): SystemStatus {
  const reasons: string[] = [];

  if (data.mode?.kill?.engaged) {
    reasons.push('Trading is stopped because the kill switch is on. Only you can turn it off.');
  }
  if (data.mode && data.mode.verified === false) {
    reasons.push(
      'The signed file that says which bot is on real money could not be checked, so both ' +
        'bots are being treated as play money. That is the safe direction.',
    );
  }
  if (data.bless && !data.bless.ok) {
    reasons.push(
      'The protected settings have changed since you last approved them. They need ' +
        'approving again before any bot can use real money.',
    );
  }
  const incidents = data.incidents?.length ?? 0;
  if (incidents > 0) {
    reasons.push(
      incidents === 1
        ? 'One thing went wrong and has not been closed off yet.'
        : `${incidents} things went wrong and have not been closed off yet.`,
    );
  }
  const breach = data.gate?.breach ?? 0;
  if (breach > 0) {
    reasons.push(
      breach === 1
        ? 'An order hit a hard limit today and was stopped.'
        : `${breach} orders hit a hard limit today and were stopped.`,
    );
  }

  return {
    ok: reasons.length === 0,
    headline: reasons.length === 0 ? 'Everything is running' : 'Something needs attention',
    reasons,
  };
}

/* ------------------------------------------------------------- what do I hold, worth what */

export interface Holding {
  sleeve: string;
  /** "Rules bot (no AI) holds 0.04 BTC, worth about $3,412." */
  text: string;
}

export function holdings(data: OverviewPayload): Holding[] {
  const sleeves = (data.exposure?.sleeves ?? []).filter((sleeve) => sleeve.sleeve !== 'benchmark');
  if (sleeves.length === 0) return [];
  return sleeves.map((sleeve) => {
    const held = sleeve.assets.filter((asset) => (asset.amount ?? 0) > 0);
    const name = sleeveFullName(sleeve.sleeve);
    if (held.length === 0) {
      return { sleeve: sleeve.sleeve, text: `${name} holds nothing right now — it is all cash.` };
    }
    const parts = held.map((asset) => {
      const worth = money(asset.value_usdt);
      return `${amount(asset.amount)} ${asset.asset}${worth ? `, worth about ${worth}` : ''}`;
    });
    return { sleeve: sleeve.sleeve, text: `${name} holds ${parts.join(' and ')}.` };
  });
}

/* -------------------------------------------------------- did anything happen, in sentences */

export interface Happening {
  /** Stable key for the list. */
  key: string;
  /** The detail-pane kind this sentence opens, or null when there is nothing behind it. */
  kind: 'check' | 'change' | 'decision' | null;
  /** The detail-pane id. */
  id: string;
  /** `08:30`, or null when the thing carries no time. */
  time: string | null;
  /** One sentence, in plain words. */
  text: string;
  /** Sort key: the raw timestamp, newest first. */
  ts: string;
}

/** "the safety checks were happy with it" / "the safety checks turned it down". */
function decisionSentence(last: NonNullable<OverviewPayload['research']>['last']): string {
  if (!last) return '';
  if (last.status === 'success') {
    return 'The system looked at the market and decided what to hold.';
  }
  if (last.status === 'abstained' || last.status === 'abstain') {
    return 'The system looked at the market and chose not to trade — the safe default.';
  }
  return 'The system tried to look at the market but could not finish.';
}

/**
 * What happened today, as sentences a person reads.
 *
 * Deliberately not here: open incidents (the status line owns those), the funnel counts,
 * the job schedule, the provider bill and the model that answered. They are all in the
 * detail pane behind "Everything is running".
 */
export function happenings(data: OverviewPayload): Happening[] {
  const out: Happening[] = [];

  const last = data.research?.last;
  if (last) {
    out.push({
      key: 'decision',
      kind: 'decision',
      id: 'last',
      time: clock(last.finished_utc ?? last.started_utc),
      ts: last.finished_utc ?? last.started_utc,
      text: decisionSentence(last),
    });
  }

  for (const row of data.gate?.recent ?? []) {
    out.push({
      key: `check-${row.id}`,
      kind: 'check',
      id: String(row.id),
      time: clock(row.ts_utc),
      ts: row.ts_utc,
      text:
        `An order for ${row.pair} from the ${sleeveFullName(row.sleeve)} was turned down — ` +
        `${row.reason}.`,
    });
  }

  for (const row of data.changed_today?.config ?? []) {
    const count = row.changed_paths.length;
    out.push({
      key: `change-${row.id}`,
      kind: 'change',
      id: String(row.id),
      time: clock(row.ts_utc),
      ts: row.ts_utc,
      text:
        `A setting was changed${count > 1 ? ` (${count} of them)` : ''}. ` +
        'Click to see what it was.',
    });
  }

  return out.sort((a, b) => String(b.ts).localeCompare(String(a.ts))).slice(0, 6);
}

/** The one line Home shows when the system has genuinely done nothing yet. */
export const NOTHING_HAPPENED =
  'Nothing has happened yet. When the system trades, decides or is stopped from trading, it will say so here.';

/** "Nothing is waiting on you." / "2 things are waiting for your yes or no." */
export function waitingLine(data: OverviewPayload): string {
  const count = data.approvals?.count ?? 0;
  if (count === 0) return 'Nothing is waiting on you.';
  return count === 1
    ? 'One thing is waiting for your yes or no.'
    : `${count} things are waiting for your yes or no.`;
}
