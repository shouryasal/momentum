import { Group, Paper, Skeleton, Stack, Text, Tooltip } from '@mantine/core';
import type { ReactNode } from 'react';

export interface StatCardProps {
  label: string;
  value: ReactNode;
  /** Signed change rendered under the value, e.g. `+1.24%`. */
  delta?: { value: ReactNode; positive: boolean } | null;
  hint?: string;
  icon?: ReactNode;
  loading?: boolean;
  onClick?: () => void;
}

export function StatCard({ label, value, delta, hint, icon, loading, onClick }: StatCardProps) {
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
        </Stack>
        {icon ? <div>{icon}</div> : null}
      </Group>
    </Paper>
  );
  return hint ? (
    <Tooltip label={hint} multiline w={260}>
      {body}
    </Tooltip>
  ) : (
    body
  );
}
