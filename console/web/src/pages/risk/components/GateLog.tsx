import { Badge, Group, SegmentedControl, Stack, Text, Tooltip } from '@mantine/core';

import { DataTable, type DataTableColumn } from '@/components';

import type { GateDecision } from '../api';

const SEVERITY_COLOR: Record<string, string> = {
  allow: 'teal',
  reject: 'yellow',
  breach: 'red',
};

/** Every check the gate evaluated for this decision; failures first, failures filled. */
function ChecksMatrix({ checks }: { checks: Record<string, boolean> }) {
  const entries = Object.entries(checks).sort(([, a], [, b]) => Number(a) - Number(b));
  if (!entries.length) {
    return (
      <Text size="xs" c="dimmed">
        —
      </Text>
    );
  }
  return (
    <Group gap={4} wrap="wrap">
      {entries.map(([name, passed]) => (
        <Tooltip key={name} label={`${name}: ${passed ? 'pass' : 'FAIL'}`} withArrow>
          <Badge size="xs" variant={passed ? 'light' : 'filled'} color={passed ? 'gray' : 'red'}>
            {name}
          </Badge>
        </Tooltip>
      ))}
    </Group>
  );
}

export interface GateLogProps {
  rows: GateDecision[];
  counts: Record<string, number>;
  severity: string;
  onSeverityChange: (value: string) => void;
  loading?: boolean;
}

export function GateLog({ rows, counts, severity, onSeverityChange, loading }: GateLogProps) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  const columns: Array<DataTableColumn<GateDecision>> = [
    {
      key: 'ts',
      header: 'Time (UTC)',
      sortValue: (row) => row.ts_utc,
      render: (row) => (
        <Text size="xs" ff="monospace">
          {row.ts_utc}
        </Text>
      ),
    },
    {
      key: 'sleeve',
      header: 'Sleeve',
      sortValue: (row) => row.sleeve,
      render: (row) => (
        <Badge size="xs" variant="light">
          {row.sleeve.toUpperCase()}
        </Badge>
      ),
    },
    { key: 'pair', header: 'Pair', sortValue: (row) => row.pair,
      render: (row) => <Text size="xs">{row.pair}</Text> },
    {
      key: 'callback',
      header: 'Callback',
      sortValue: (row) => row.callback,
      render: (row) => (
        <Stack gap={0}>
          <Text size="xs">{row.callback}</Text>
          {row.action ? (
            <Text size="xs" c="dimmed">
              {row.action}
            </Text>
          ) : null}
        </Stack>
      ),
    },
    {
      key: 'reason',
      header: 'Reason',
      sortValue: (row) => row.reason,
      render: (row) => (
        <Badge size="sm" color={SEVERITY_COLOR[row.severity] ?? 'gray'}>
          {row.reason}
        </Badge>
      ),
    },
    { key: 'checks', header: 'Checks', render: (row) => <ChecksMatrix checks={row.checks} /> },
  ];

  return (
    <Stack gap="sm">
      <Group justify="space-between">
        <SegmentedControl
          size="xs"
          value={severity}
          onChange={onSeverityChange}
          data={[
            { label: `all (${total})`, value: 'all' },
            { label: `breach (${counts.breach ?? 0})`, value: 'breach' },
            { label: `reject (${counts.reject ?? 0})`, value: 'reject' },
            { label: `allow (${counts.allow ?? 0})`, value: 'allow' },
          ]}
        />
      </Group>
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(row) => String(row.id)}
        loading={loading}
        maxHeight={480}
        dense
        emptyTitle="No gate decisions"
        emptyDescription="Nothing matched this filter. The gate only writes a row when a callback fires."
      />
    </Stack>
  );
}
