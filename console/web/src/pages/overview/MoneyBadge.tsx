/**
 * "Simulated" / "Demo money" / "Real money" on every row of Home.
 *
 * The brief asked for each row to be *"marked clearly as simulated, demo or real money"*,
 * which the console's own `SIM` / `DEMO` / `LIVE` badge does not do — those are three
 * words a person has to learn. The classification is still the shared one in
 * `lib/plain.executionKind`, so this cannot drift from what the rest of the console means
 * by the same row; only the wording is the owner's.
 *
 * It lives in its own file so the reasoning pane and the two tables show the same words:
 * a row that reads "Simulated" in the table must not read "SIM" when it is opened.
 */
import { Badge, Tooltip } from '@mantine/core';

import { EXECUTION_HINT, executionKind, type ExecutionKind } from '@/lib/plain';

export const MONEY_WORDS: Record<ExecutionKind, string> = {
  sim: 'Simulated',
  demo: 'Demo money',
  live: 'Real money',
};

const MONEY_COLOUR: Record<ExecutionKind, string> = { sim: 'blue', demo: 'violet', live: 'red' };

export function MoneyBadge({ mode }: { mode: string | null | undefined }) {
  const kind = executionKind(mode);
  return (
    <Tooltip label={EXECUTION_HINT[kind]} multiline w={300} withArrow>
      <Badge
        size="xs"
        color={MONEY_COLOUR[kind]}
        variant={kind === 'live' ? 'filled' : 'light'}
        data-testid={`money-${kind}`}
        // Never abbreviate the one word that says whether this was real. A squeezed
        // column may wrap the cell; it may not turn "Demo money" into "DE…".
        styles={{ root: { flex: '0 0 auto' }, label: { overflow: 'visible' } }}
      >
        {MONEY_WORDS[kind]}
      </Badge>
    </Tooltip>
  );
}
