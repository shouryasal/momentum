import { Badge, Group, Text, Tooltip } from '@mantine/core';
import type { ReactNode } from 'react';

import {
  EXECUTION_COLOR,
  EXECUTION_HINT,
  EXECUTION_LABEL,
  executionKind,
  explain,
  sleeveNaming,
  type ExecutionKind,
  type SleeveKey,
} from '../lib/plain';

export interface ExplainProps {
  /** The glossary key; defaults to the rendered text. */
  term?: string;
  /** Override the sentence, for a word the glossary does not carry. */
  hint?: string;
  children: ReactNode;
}

/**
 * A builder word with its one-sentence explanation attached.
 *
 * The console cannot stop printing words like "gate" or "proposal" — they are what the
 * logs, the API and the config call these things — but it can stop printing them bare.
 * Anything wrapped here gets a dotted underline and a tooltip from `lib/plain.GLOSSARY`.
 * With no glossary entry and no `hint` the children render untouched, so a wrong key
 * degrades to plain text instead of an empty tooltip.
 */
export function Explain({ term, hint, children }: ExplainProps) {
  const key = term ?? (typeof children === 'string' ? children : '');
  const sentence = hint ?? explain(key);
  if (!sentence) return <>{children}</>;
  return (
    <Tooltip label={sentence} multiline w={320} withArrow>
      <Text
        span
        inherit
        data-testid={`explain-${key.toLowerCase().replace(/\s+/g, '-')}`}
        style={{ textDecoration: 'underline dotted', textUnderlineOffset: 3, cursor: 'help' }}
      >
        {children}
      </Text>
    </Tooltip>
  );
}

export interface SleeveNameProps {
  sleeve: SleeveKey | null | undefined;
  /** `full` shows "Rules bot (no AI)"; `short` shows "Rules bot". */
  variant?: 'full' | 'short';
  /** Print the raw id (`sleeve a`) next to the name as well as in the tooltip. */
  withRaw?: boolean;
  size?: string;
}

/** The human name for a sleeve, with the raw id and one sentence behind it. */
export function SleeveName({
  sleeve,
  variant = 'short',
  withRaw = false,
  size = 'sm',
}: SleeveNameProps) {
  const naming = sleeveNaming(sleeve);
  return (
    <Tooltip label={`${naming.hint} (${naming.raw})`} multiline w={320} withArrow>
      <Group gap={4} wrap="nowrap" component="span" data-testid={`sleeve-name-${sleeve ?? 'none'}`}>
        <Text span size={size} inherit>
          {variant === 'full' ? naming.full : naming.name}
        </Text>
        {withRaw ? (
          <Text span size="xs" c="dimmed">
            {naming.raw}
          </Text>
        ) : null}
      </Group>
    </Tooltip>
  );
}

export interface ExecutionBadgeProps {
  /** The row's `mode` as the journal recorded it. */
  mode: string | null | undefined;
  size?: string;
}

/**
 * SIM / DEMO / LIVE on a row, never left to inference.
 *
 * Every table that shows something the system did to the market carries one of these, so
 * "was this real?" is answered on the row rather than by remembering which page you are
 * on.  The classification lives in `lib/plain.executionKind` so the three words mean the
 * same thing on every screen.
 */
export function ExecutionBadge({ mode, size = 'xs' }: ExecutionBadgeProps) {
  const kind: ExecutionKind = executionKind(mode);
  return (
    <Tooltip label={EXECUTION_HINT[kind]} multiline w={300} withArrow>
      <Badge
        size={size}
        color={EXECUTION_COLOR[kind]}
        variant={kind === 'live' ? 'filled' : 'light'}
        data-testid={`execution-${kind}`}
        data-execution={kind}
      >
        {EXECUTION_LABEL[kind]}
      </Badge>
    </Tooltip>
  );
}
