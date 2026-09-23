import { Badge, Group, Stack, Text } from '@mantine/core';

import { DataTable } from '@/components';

import type { CheckRow, EvidenceRow } from '../api';

function render(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(4);
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  return String(value);
}

/**
 * Claimed vs verified, one row per number.
 *
 * A red row means the model's own figure is more than 10% away from what the code
 * recomputed. It is already written to `root_cause_events` as `evidence_mismatch`; this is
 * where a human sees it.
 */
export function EvidenceTable({ rows }: { rows: EvidenceRow[] }) {
  return (
    <DataTable
      rows={rows}
      rowKey={(row) => row.field}
      dense
      emptyTitle="No evidence recorded"
      emptyDescription="This change was rejected before anything was recomputed."
      columns={[
        {
          key: 'field',
          header: 'Field',
          render: (row) => (
            <Text size="sm" ff="monospace">
              {row.field}
            </Text>
          ),
          sortValue: (row) => row.field,
        },
        {
          key: 'claimed',
          header: 'Claimed by the model',
          align: 'right',
          render: (row) => <Text size="sm">{render(row.claimed)}</Text>,
        },
        {
          key: 'verified',
          header: 'Recomputed',
          align: 'right',
          render: (row) => (
            <Text size="sm" fw={600}>
              {render(row.verified)}
            </Text>
          ),
        },
        {
          key: 'delta',
          header: '',
          render: (row) =>
            row.mismatch ? (
              <Badge color="red" variant="filled">
                off by {row.delta_pct ?? '?'}%
              </Badge>
            ) : null,
          sortValue: (row) => (row.mismatch ? 1 : 0),
        },
      ]}
    />
  );
}

const VERDICT_COLOUR: Record<string, string> = {
  pass: 'teal',
  fail: 'red',
  hold: 'yellow',
  skip: 'gray',
};

/** The ordered list of checks the gate ran, with the first offender's reason. */
export function ChecksList({ checks }: { checks: CheckRow[] }) {
  if (checks.length === 0) {
    return <Text c="dimmed" size="sm">No checks recorded for this change.</Text>;
  }
  return (
    <Stack gap="xs">
      {checks.map((check) => (
        <Group key={check.name} gap="sm" wrap="nowrap" align="flex-start">
          <Badge color={VERDICT_COLOUR[check.verdict] ?? 'gray'} variant="light" w={72}>
            {check.verdict}
          </Badge>
          <div>
            <Text size="sm" fw={600}>
              {check.name}
            </Text>
            {check.detail ? (
              <Text size="xs" c="dimmed">
                {check.detail}
              </Text>
            ) : null}
          </div>
        </Group>
      ))}
    </Stack>
  );
}
