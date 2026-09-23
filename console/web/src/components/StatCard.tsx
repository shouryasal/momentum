import { Group, Paper, Skeleton, Stack, Text, Tooltip } from '@mantine/core';
import type { ReactNode } from 'react';

export interface StatCardProps {
  label: string;
  value: ReactNode;
  /** Signed change rendered under the value, e.g. `+1.24%`. */
  delta?: { value: ReactNode; positive: boolean } | null;
  /**
   * What the number means, printed under it.
   *
   * This used to live in a tooltip, which meant every headline figure on the console was
   * a bare number until you hovered — and a hover is not available on a touch screen and
   * is not there at all when someone reads a screenshot. The owner's rule is that a
   * number never appears without the sentence that says what it is, so the sentence is on
   * the card. Anything longer belongs in {@link StatCardProps.tooltip}.
   */
  hint?: string;
  /** A longer explanation, on hover. Never the only place a number is explained. */
  tooltip?: string;
  icon?: ReactNode;
  loading?: boolean;
  onClick?: () => void;
}

export function StatCard({
  label,
  value,
  delta,
  hint,
  tooltip,
  icon,
  loading,
  onClick,
}: StatCardProps) {
  const body = (
    <Paper
      p="md"
      radius="md"
      data-testid="stat-card"
      style={onClick ? { cursor: 'pointer' } : undefined}
      onClick={onClick}
    >
      <Group justify="space-between" align="flex-start" wrap="nowrap">
        <Stack gap={2}>
          <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
            {label}
          </Text>
          {loading ? (
            <Skeleton height={26} width={90} />
          ) : (
            <Text size="xl" fw={700} lh={1.2}>
              {value}
            </Text>
          )}
          {delta ? (
            <Text size="sm" c={delta.positive ? 'teal' : 'red'}>
              {delta.value}
            </Text>
          ) : null}
          {hint ? (
            <Text size="xs" c="dimmed" data-testid="stat-card-hint">
              {hint}
            </Text>
          ) : null}
        </Stack>
        {icon ? <div>{icon}</div> : null}
      </Group>
    </Paper>
  );
  return tooltip ? (
    <Tooltip label={tooltip} multiline w={280}>
      {body}
    </Tooltip>
  ) : (
    body
  );
}
