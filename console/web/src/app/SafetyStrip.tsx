import { Anchor, Badge, Group, Popover, Stack, Text, Tooltip, UnstyledButton } from '@mantine/core';
import { IconCircleCheck, IconCircleX, IconHelpCircle, IconShieldCheck } from '@tabler/icons-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';

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
 * What each pill promises, in one sentence.
 *
 * The pill text itself is not translated: these names match what
 * `console/services/invariants_service.py` reports and what the Invariants page lists, and
 * a safety label that reads differently in two places is worse than a jargon one.  The
 * sentence rides in the tooltip instead, so the strip is readable without a glossary.
 */
export const PILL_MEANING: Record<string, string> = {
  'gate-in-order-path':
    'Every order passes fixed safety checks written in code before it is sent. Claude cannot go around them.',
  'automated-runs-cannot-change-limits':
    'An unattended Claude run is blocked from editing the risk limits or reaching this console at all.',
  'live-entry-human-only':
    'Only a person can move a bot from play money to real money, and only after the checks pass.',
  'console-127.0.0.1':
    'This console listens on your machine only. It is not reachable from the network.',
  'config-blessed':
    'The protected settings still match the signature taken when you last approved them.',
};

/**
 * What each pill is called on screen, in words nobody has to ask about.
 *
 * The key stays the name `console/services/invariants_service.py` reports and the
 * Invariants page lists — the strip's `data-testid` and its tooltip both still carry it,
 * so the two halves cannot drift — but the operator reads the promise, not the key.
 */
export const PILL_LABEL: Record<string, string> = {
  'gate-in-order-path': 'Safety checks run on every order',
  'automated-runs-cannot-change-limits': 'Robots cannot change the limits',
  'live-entry-human-only': 'Only you can switch to real money',
  'console-127.0.0.1': 'This console is on your machine only',
  'config-blessed': 'Protected settings still signed',
};

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

/** The worst status across the strip; `ok` only when every pill is `ok`. */
export function worstStatus(items: StripItem[]): CheckStatus {
  let worst: CheckStatus = 'ok';
  for (const item of items) {
    if (WORST[item.status] > WORST[worst]) worst = item.status;
  }
  return worst;
}

/**
 * One small badge, and the five promises behind it.
 *
 * This used to be a permanent strip of five labelled pills across the whole width of the
 * header — a wall of text the owner read once and then had to read past every day. The
 * guarantees still matter, so nothing about them changed: the same five invariants, the
 * same statuses, the same sentences, the same deep link into the Invariants page. What
 * changed is that they are now one badge that says whether they all hold, and the detail
 * opens when the operator asks for it.
 *
 * A badge that is not green is not quiet: it takes the status colour and says how many
 * promises need looking at, because "we did not check" and "it holds" must never look the
 * same.
 */
export function SafetyStrip({ strip, invariants = [] }: SafetyStripProps) {
  const [opened, setOpened] = useState(false);
  const items = stripFrom(strip, invariants);
  const worst = worstStatus(items);
  const unhappy = items.filter((item) => item.status !== 'ok');
  const label =
    worst === 'ok'
      ? 'Safety checks on'
      : unhappy.length === 1
        ? 'One safety promise needs a look'
        : `${unhappy.length} safety promises need a look`;

  return (
    <Group px="md" py={4} gap="xs" data-testid="safety-strip" className="earn-safety-strip">
      <Popover
        opened={opened}
        onChange={setOpened}
        width={380}
        position="bottom-start"
        withArrow
        shadow="md"
      >
        <Popover.Target>
          <UnstyledButton
            onClick={() => setOpened((open) => !open)}
            data-testid="safety-badge"
            data-status={worst}
            aria-label="What keeps this safe"
          >
            <Badge
              size="sm"
              variant="light"
              color={worst === 'ok' ? 'teal' : (STATUS_COLORS[worst] ?? 'red')}
              leftSection={<IconShieldCheck size={12} />}
              style={{ cursor: 'pointer' }}
            >
              {label}
            </Badge>
          </UnstyledButton>
        </Popover.Target>
        <Popover.Dropdown>
          <Stack gap="sm" data-testid="safety-detail">
            <Text size="xs" c="dimmed">
              Five promises this system keeps, whatever anyone — including Claude — asks it to
              do.
            </Text>
            {items.map((item) => (
              <Tooltip
                key={item.pill}
                multiline
                w={320}
                label={`${PILL_MEANING[item.pill] ?? item.pill} Known to the system as ${item.pill} (${item.detail}).`}
              >
                <Group gap={6} wrap="nowrap" data-testid={`safety-${item.pill}`} data-status={item.status}>
                  <StatusIcon status={item.status} />
                  <Text
                    size="xs"
                    c={item.status === 'ok' ? undefined : (STATUS_COLORS[item.status] ?? 'red')}
                  >
                    {PILL_LABEL[item.pill] ?? item.pill}
                  </Text>
                </Group>
              </Tooltip>
            ))}
            <Anchor component={Link} to="/invariants" size="xs" onClick={() => setOpened(false)}>
              See the rule and the code that enforces it
            </Anchor>
          </Stack>
        </Popover.Dropdown>
      </Popover>
    </Group>
  );
}
