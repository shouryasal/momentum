import { Badge, Code, Group, Text } from '@mantine/core';

import { DataTable, type DataTableColumn } from '@/components';

import type { LimitRow } from '../api';
import { fmt } from './MeterBar';

function renderValue(row: LimitRow): string {
  if (typeof row.value === 'number') return fmt(row.value, row.unit);
  return Object.entries(row.value)
    .map(([key, value]) => `${key} ${fmt(value, row.unit)}`)
    .join(' · ');
}

export interface LimitsTableProps {
  limits: LimitRow[];
  /** Deep-link into Settings; every one of these is tier 2 (human-only). */
  onOpenSetting?: (path: string) => void;
}

export function LimitsTable({ limits, onOpenSetting }: LimitsTableProps) {
  const columns: Array<DataTableColumn<LimitRow>> = [
    {
      key: 'name',
      header: 'Limit',
      sortValue: (row) => row.name,
      render: (row) => (
        <Text size="sm" fw={500}>
          {row.name}
        </Text>
      ),
    },
    {
      key: 'value',
      header: 'Value',
      render: (row) => (
        <Text size="sm" ff="monospace">
          {renderValue(row)}
        </Text>
      ),
    },
    {
      key: 'path',
      header: 'Source',
      sortValue: (row) => row.path,
      render: (row) => (
        <Group gap="xs">
          <Code
            style={onOpenSetting ? { cursor: 'pointer' } : undefined}
            onClick={onOpenSetting ? () => onOpenSetting(row.path) : undefined}
          >
            {row.path}
          </Code>
          <Badge size="xs" color="gray" variant="light">
            human only
          </Badge>
        </Group>
      ),
    },
  ];
  return (
    <DataTable
      columns={columns}
      rows={limits}
      rowKey={(row) => row.name}
      emptyTitle="No limits loaded"
      emptyDescription="config/riskgate.json has not been generated yet."
      dense
    />
  );
}
