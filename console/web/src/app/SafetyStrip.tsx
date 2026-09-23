import { Group, Paper, Text, Tooltip, UnstyledButton } from '@mantine/core';
import { IconCircleCheck, IconCircleX, IconHelpCircle } from '@tabler/icons-react';
import { useNavigate } from 'react-router-dom';

import type { CheckStatus, Invariant, SafetyStripResponse } from '../api/contracts';
import { STATUS_COLORS } from '../theme';

/**
 * The five pills of spec 12, named exactly as `console/services/invariants_service.py`
 * names them — that service maps each invariant it checks onto one of these and reports
 * the worst status behind it, which is what `GET /api/invariants/strip` returns.
 */
export const SAFETY_PILLS = [
  'gate-in-order-path',
  'automated-runs-cannot-change-limits',
  'live-entry-human-only',
  'console-127.0.0.1',
  'config-blessed',
] as const;

export type SafetyPill = (typeof SAFETY_PILLS)[number];

/**
 * Fallback mapping from `GET /api/meta`'s invariant keys onto the strip.
 *
 * `/api/meta` reports a smaller set under different keys; it is the strip's second source,
 * used only when the invariants service is unreachable.  A pill with no source is
 * `unknown`, never ✓ — "we did not check" and "it holds" must not look the same.
 */
const META_KEY_TO_PILL: Record<string, SafetyPill> = {
  console_bind: 'console-127.0.0.1',
  config_blessed: 'config-blessed',
  automated_run_refused: 'automated-runs-cannot-change-limits',
  mode_signed: 'live-entry-human-only',
  committed_dry_run: 'live-entry-human-only',
};

const WORST: Record<CheckStatus, number> = { ok: 0, unknown: 1, warn: 2, fail: 3 };

export interface StripItem {
  pill: string;
  status: CheckStatus;
  /** What backs the pill, for the tooltip. */
  detail: string;
}

function isStatus(value: unknown): value is CheckStatus {
  return value === 'ok' || value === 'warn' || value === 'fail' || value === 'unknown';
}

/** The strip map the server sent, reduced to meta's keys when it did not answer. */
export function stripFrom(
  strip: SafetyStripResponse | null | undefined,
  metaInvariants: Invariant[] = [],
): StripItem[] {
  const source = new Map<string, CheckStatus>();
  let detail = 'from /api/invariants';

  if (strip?.strip) {
    for (const [pill, status] of Object.entries(strip.strip)) {
      if (isStatus(status)) source.set(pill, status);
    }
  } else {
    detail = 'derived from /api/meta — the invariants service did not answer';
    for (const invariant of metaInvariants) {
      const pill = META_KEY_TO_PILL[invariant.key];
      if (!pill) continue;
      const current = source.get(pill);
      if (current === undefined || WORST[invariant.status] > WORST[current]) {
        source.set(pill, invariant.status);
      }
    }
  }

  const head: StripItem[] = SAFETY_PILLS.map((pill) => ({
    pill,
    status: source.get(pill) ?? 'unknown',
    detail: source.has(pill) ? detail : 'not reported',
  }));
  const rest: StripItem[] = [...source.entries()]
    .filter(([pill]) => !SAFETY_PILLS.includes(pill as SafetyPill))
    .map(([pill, status]) => ({ pill, status, detail }));
  return [...head, ...rest];
}

function StatusIcon({ status }: { status: CheckStatus }) {
  if (status === 'ok') return <IconCircleCheck size={14} color="var(--mantine-color-teal-6)" />;
  if (status === 'unknown') return <IconHelpCircle size={14} color="var(--mantine-color-gray-6)" />;
  return <IconCircleX size={14} color="var(--mantine-color-red-6)" />;
}

export interface SafetyStripProps {
  /** `GET /api/invariants/strip`; null while it loads or when it failed. */
  strip: SafetyStripResponse | null | undefined;
  /** `GET /api/meta`'s invariants, used only as the fallback source. */
  invariants?: Invariant[];
}

/** Always-visible safety strip; each pill deep-links into the Invariants page (spec 12). */
export function SafetyStrip({ strip, invariants = [] }: SafetyStripProps) {
  const navigate = useNavigate();
  const items = stripFrom(strip, invariants);
  return (
    <Paper radius={0} px="md" py={4} data-testid="safety-strip">
      <Group gap="lg" wrap="wrap">
        {items.map((item) => (
          <Tooltip key={item.pill} label={`${item.pill}: ${item.status} — ${item.detail}`}>
            <UnstyledButton
              onClick={() => navigate('/invariants')}
              data-testid={`safety-${item.pill}`}
              data-status={item.status}
            >
              <Group gap={4} wrap="nowrap">
                <StatusIcon status={item.status} />
                <Text
                  size="xs"
                  c={item.status === 'ok' ? 'dimmed' : (STATUS_COLORS[item.status] ?? 'red')}
                >
                  {item.pill}
                </Text>
              </Group>
            </UnstyledButton>
          </Tooltip>
        ))}
      </Group>
    </Paper>
  );
}
