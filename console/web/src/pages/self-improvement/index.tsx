import {
  Alert, Badge, Card, Group, Loader, SegmentedControl, SimpleGrid, Stack, Table, Text, Title,
} from '@mantine/core';
import { useState } from 'react';

import { usePageCommands } from '@/app/commandRegistry';
import { DataTable, StatCard } from '@/components';

import { ChangeDetailDrawer } from './ChangeDetail';
import {
  STATUS_COLOURS,
  useAutonomy,
  useChanges,
  useSaveAutonomy,
  useTimeline,
  type ChangeRow,
} from './api';
import { AutonomyMatrixCard } from './components/AutonomyMatrixCard';

const FILTERS = [
  { label: 'Open', value: 'open' },
  { label: 'Merged', value: 'auto_merged' },
  { label: 'Approved', value: 'approved' },
  { label: 'Rejected', value: 'rejected' },
  { label: 'Reverted', value: 'reverted' },
  { label: 'All', value: 'all' },
];

/**
 * The Self-Improvement page.
 *
 * Everything the loop did to itself, and the two questions a human actually asks: is this
 * change's evidence real, and how much may the loop do on its own next week.
 */
export default function SelfImprovementPage() {
  const [filter, setFilter] = useState('open');
  const [selected, setSelected] = useState<string | null>(null);
  const changes = useChanges(filter === 'all' ? undefined : filter);
  const autonomy = useAutonomy();
  const timeline = useTimeline();
  const saveAutonomy = useSaveAutonomy();

  const counts = changes.data?.counts ?? {};

  usePageCommands('self-improvement', [
    {
      id: 'held',
      title: 'Changes held for you',
      subtitle: 'The queue the loop cannot merge on its own',
      keywords: ['queue', 'approve'],
      run: (ctx) => {
        setFilter('open');
        ctx.close();
      },
    },
    {
      id: 'reverted',
      title: 'Changes that were reverted',
      run: (ctx) => {
        setFilter('reverted');
        ctx.close();
      },
    },
    {
      id: 'autonomy',
      title: 'Autonomy matrix',
      subtitle: 'How much the loop may do on its own, per kind and per mode',
      run: (ctx) => {
        void autonomy.refetch();
        ctx.close();
      },
    },
  ]);

  return (
    <Stack gap="lg">
      <Title order={2}>Self-improvement</Title>

      <SimpleGrid cols={{ base: 2, md: 4 }}>
        <StatCard label="Held for you" value={String(counts.held ?? 0)} />
        <StatCard label="Merged automatically" value={String(counts.auto_merged ?? 0)} />
        <StatCard label="Rejected" value={String(counts.rejected ?? 0)} />
        <StatCard label="Reverted" value={String(counts.reverted ?? 0)} />
      </SimpleGrid>

      <Card withBorder padding="md">
        <Group justify="space-between" mb="sm">
          <Title order={4}>Changes</Title>
          <SegmentedControl data={FILTERS} value={filter} onChange={setFilter} size="xs" />
        </Group>
        {changes.error ? (
          <Alert color="red" title="Could not load the change queue">
            {String(changes.error)}
          </Alert>
        ) : null}
        <DataTable<ChangeRow>
          rows={changes.data?.items ?? []}
          rowKey={(row) => row.change_id}
          loading={changes.isLoading}
          onRowClick={(row) => setSelected(row.change_id)}
          emptyTitle="Nothing in the queue"
          emptyDescription="The review runs have not proposed anything under this filter."
          columns={[
            {
              key: 'id',
              header: 'Change',
              render: (row) => (
                <Stack gap={0}>
                  <Text size="sm" ff="monospace">
                    {row.change_id}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {row.target}
                  </Text>
                </Stack>
              ),
              sortValue: (row) => row.change_id,
            },
            {
              key: 'kind',
              header: 'Kind',
              render: (row) => (
                <Badge variant="light">
                  {row.kind}
                  {row.op && row.op !== 'edit' ? ` · ${row.op}` : ''}
                </Badge>
              ),
              sortValue: (row) => `${row.kind}/${row.op ?? ''}`,
            },
            {
              key: 'status',
              header: 'Status',
              render: (row) => (
                <Badge color={STATUS_COLOURS[row.status] ?? 'gray'}>{row.status}</Badge>
              ),
              sortValue: (row) => row.status,
            },
            {
              key: 'author',
              header: 'Authored by',
              render: (row) => <Text size="sm">{row.author_model ?? '—'}</Text>,
              sortValue: (row) => row.author_model ?? '',
            },
            {
              key: 'reason',
              header: 'Reason',
              render: (row) => (
                <Text size="xs" c="dimmed" lineClamp={2}>
                  {row.reason ?? ''}
                </Text>
              ),
            },
            {
              key: 'when',
              header: 'Proposed',
              render: (row) => <Text size="xs">{row.proposed_at}</Text>,
              sortValue: (row) => row.proposed_at,
            },
          ]}
        />
      </Card>

      {autonomy.isLoading ? <Loader /> : null}
      {autonomy.data ? (
        <AutonomyMatrixCard
          matrix={autonomy.data}
          saving={saveAutonomy.isPending}
          error={saveAutonomy.error ? String(saveAutonomy.error) : null}
          onSave={(body) => saveAutonomy.mutateAsync(body)}
        />
      ) : null}

      <SimpleGrid cols={{ base: 1, md: 2 }}>
        <Card withBorder padding="md">
          <Title order={4} mb="sm">
            Merges and reverts
          </Title>
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>When</Table.Th>
                <Table.Th>Change</Table.Th>
                <Table.Th>Event</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {(timeline.data?.items ?? []).map((point) => (
                <Table.Tr key={`${point.change_id}-${point.ts_utc}-${point.event}`}>
                  <Table.Td>{point.ts_utc}</Table.Td>
                  <Table.Td>
                    <Text
                      size="sm"
                      ff="monospace"
                      style={{ cursor: 'pointer' }}
                      onClick={() => setSelected(point.change_id)}
                    >
                      {point.change_id}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Badge color={point.event === 'reverted' ? 'orange' : 'teal'} variant="light">
                      {point.event}
                    </Badge>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Card>

        <Card withBorder padding="md">
          <Title order={4} mb="sm">
            Recurring root causes
          </Title>
          <Text size="xs" c="dimmed" mb="xs">
            A cause the loop keeps diagnosing without fixing is itself the fault.
          </Text>
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Key</Table.Th>
                <Table.Th>Weeks</Table.Th>
                <Table.Th>Events</Table.Th>
                <Table.Th>Escalated</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {(timeline.data?.recurring_causes ?? []).map((cause) => (
                <Table.Tr key={cause.recurrence_key}>
                  <Table.Td>
                    <Text size="sm" ff="monospace">
                      {cause.recurrence_key}
                    </Text>
                  </Table.Td>
                  <Table.Td>{cause.weeks}</Table.Td>
                  <Table.Td>{cause.events}</Table.Td>
                  <Table.Td>
                    {cause.escalated ? <Badge color="red">yes</Badge> : <Text size="xs">—</Text>}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Card>
      </SimpleGrid>

      <ChangeDetailDrawer changeId={selected} onClose={() => setSelected(null)} />
    </Stack>
  );
}
