import { Badge, Group, Tooltip } from '@mantine/core';

import type { SleeveMode } from '../api/contracts';
import { formatNumber } from '../lib/format';
import { sleeveNaming } from '../lib/plain';
import { MODE_COLORS } from '../theme';

/**
 * What each state means, in one sentence.
 *
 * The state word itself never changes — `TEST` and `LIVE·EXECUTE` are safety affordances
 * and have to read identically everywhere, including in the logs — but nobody should have
 * to already know what they mean, so the badge carries the sentence in its tooltip.
 */
export const MODE_MEANING: Record<string, string> = {
  TEST: 'Paper money. Orders are simulated from live prices; nothing reaches an exchange.',
  DEMO_PROPOSE:
    'Binance Demo Mode: REAL orders on demo-api.binance.com with FAKE money, and every ' +
    'trade waits for you to approve it first. A rehearsal — never live performance.',
  DEMO_EXECUTE:
    'Binance Demo Mode: REAL orders on demo-api.binance.com with FAKE money, trading on ' +
    'its own inside the risk limits. A rehearsal — never live performance.',
  LIVE_PROPOSE: 'Real money, but every trade waits for you to approve it first.',
  LIVE_EXECUTE: 'Real money, trading on its own inside the risk limits.',
  ARMING: 'Moving to a real exchange right now; the checks are running.',
  DISARMING: 'Coming back from an exchange to paper money right now.',
};

export function modeMeaning(mode: SleeveMode): string {
  return MODE_MEANING[mode.state] ?? 'Unrecognised state; treat it as not verified.';
}

/** A mid-transition state pulses (spec 12). */
export function isTransitioning(mode: SleeveMode): boolean {
  return mode.state === 'ARMING' || mode.state === 'DISARMING';
}

export function modeLabel(mode: SleeveMode): string {
  switch (mode.state) {
    case 'TEST':
      return 'TEST';
    case 'DEMO_PROPOSE':
      return 'DEMO·PROPOSE';
    case 'DEMO_EXECUTE':
      return 'DEMO·EXECUTE';
    case 'LIVE_PROPOSE':
      return 'LIVE·PROPOSE';
    case 'LIVE_EXECUTE':
      return 'LIVE·EXECUTE';
    case 'ARMING':
      return 'TRANSITIONING';
    case 'DISARMING':
      return 'TRANSITIONING';
    default:
      return String(mode.state);
  }
}

/** Real orders on a real venue: demo or live. Not a synonym for "real money". */
export function isOnAnExchange(mode: SleeveMode): boolean {
  return mode.is_live || mode.state === 'DEMO_PROPOSE' || mode.state === 'DEMO_EXECUTE';
}

/**
 * Which day of the run this is, counted from the date inside the run id.
 *
 * `SleeveMode` carries no start date, but a run id is `<mode>-<sleeve>-YYYYMMDD-NN`
 * (`ops/lib/mode_state.py`), which is the day the run was opened.  Day 1 is the start
 * date itself.  Anything that does not parse returns `null` and the badge simply omits
 * the day rather than showing a wrong one.
 */
export function runDay(runId: string | null | undefined, now: Date = new Date()): number | null {
  if (!runId) return null;
  const match = /-(\d{4})(\d{2})(\d{2})(?:-|$)/.exec(runId);
  if (!match) return null;
  const [, y, m, d] = match;
  const start = Date.UTC(Number(y), Number(m) - 1, Number(d));
  if (!Number.isFinite(start)) return null;
  const today = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const days = Math.floor((today - start) / 86_400_000) + 1;
  return days >= 1 ? days : null;
}

/**
 * `Rules bot: TEST · seed 10,000 · day 12`.
 *
 * The sleeve is named, not lettered: the operator asked what "sleeve A" was, so the badge
 * leads with what the bot is and keeps `sleeve a` in the tooltip, where the identifier the
 * API and the logs use is still one hover away.
 */
export function modeSummary(mode: SleeveMode, now?: Date): string {
  const parts = [`${sleeveNaming(mode.sleeve).name}: ${modeLabel(mode)}`];
  if (mode.seed_usdt !== null && mode.seed_usdt !== undefined) {
    parts.push(`seed ${formatNumber(mode.seed_usdt, 0)}`);
  }
  const day = runDay(mode.run_id, now);
  if (day !== null) parts.push(`day ${day}`);
  return parts.join(' · ');
}

export function ModeBadges({ modes }: { modes: SleeveMode[] }) {
  if (modes.length === 0) {
    return (
      <Badge color="gray" variant="light" data-testid="mode-badge-unknown">
        mode unknown
      </Badge>
    );
  }
  return (
    <Group gap={6} wrap="nowrap">
      {modes.map((mode) => {
        const transitioning = isTransitioning(mode);
        const color = MODE_COLORS[mode.state] ?? 'gray';
        const naming = sleeveNaming(mode.sleeve);
        return (
          <Tooltip
            key={mode.sleeve}
            multiline
            w={320}
            label={
              `${naming.full} (${naming.raw}). ${naming.hint} ` +
              `${modeLabel(mode)}: ${modeMeaning(mode)}` +
              `${mode.run_id ? ` · run ${mode.run_id}` : ''}` +
              `${mode.submode ? ` · ${mode.submode}` : ''}`
            }
          >
            <Badge
              color={color}
              // Filled means "this sleeve is on a real exchange", which is true of demo as
              // well as live. `data-live` stays what it always was — real money only — so
              // nothing downstream can mistake a demo sleeve for a live one.
              variant={isOnAnExchange(mode) ? 'filled' : 'light'}
              data-testid={`mode-badge-${mode.sleeve}`}
              data-state={mode.state}
              data-live={mode.is_live ? 'true' : 'false'}
              className={transitioning ? 'earn-pulse' : undefined}
            >
              {modeSummary(mode)}
            </Badge>
          </Tooltip>
        );
      })}
    </Group>
  );
}
