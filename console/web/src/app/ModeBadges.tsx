import { Badge, Group, Tooltip } from '@mantine/core';

import type { SleeveMode } from '../api/contracts';
import { formatNumber, sleeveLabel } from '../lib/format';
import { MODE_COLORS } from '../theme';

/** A mid-transition state pulses (spec 12). */
export function isTransitioning(mode: SleeveMode): boolean {
  return mode.state === 'ARMING' || mode.state === 'DISARMING';
}

export function modeLabel(mode: SleeveMode): string {
  switch (mode.state) {
    case 'TEST':
      return 'TEST';
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

/** `A: TEST · seed 10,000 · day 12` (spec 12). */
export function modeSummary(mode: SleeveMode, now?: Date): string {
  const parts = [`${sleeveLabel(mode.sleeve)}: ${modeLabel(mode)}`];
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
        return (
          <Tooltip
            key={mode.sleeve}
            label={`${modeSummary(mode)}${mode.run_id ? ` · run ${mode.run_id}` : ''}${
              mode.submode ? ` · ${mode.submode}` : ''
            }`}
          >
            <Badge
              color={color}
              variant={mode.is_live ? 'filled' : 'light'}
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
