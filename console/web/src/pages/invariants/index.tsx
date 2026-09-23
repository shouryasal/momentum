import {
  Accordion,
  Alert,
  Badge,
  Card,
  Code,
  Group,
  Loader,
  SegmentedControl,
  Stack,
  Text,
  Title,
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconCircleCheck,
  IconCircleX,
  IconHelpCircle,
} from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';

import { api, errorMessage } from '@/api';
import { usePageCommands } from '@/app/commandRegistry';
import { DataTable, EmptyState, JsonViewer } from '@/components';

type Status = 'ok' | 'warn' | 'fail' | 'unknown';

export interface InvariantRow {
  id: string;
  title: string;
  statement: string;
  enforced_by: string;
  category: string;
  strip: string | null;
  status: Status;
  detail: string;
  evidence: Record<string, unknown>;
}

interface InvariantsResponse {
  invariants: InvariantRow[];
  counts: Partial<Record<Status, number>>;
  ok: boolean;
  strip: Record<string, Status>;
  last_denials: Array<Record<string, unknown>>;
  checked_root: string;
}

const COLOUR: Record<Status, string> = {
  ok: 'teal',
  warn: 'yellow',
  fail: 'red',
  unknown: 'gray',
};

function StatusIcon({ status }: { status: Status }) {
  if (status === 'ok') return <IconCircleCheck size={16} color="var(--mantine-color-teal-6)" />;
  if (status === 'fail') return <IconCircleX size={16} color="var(--mantine-color-red-6)" />;
  if (status === 'warn') {
    return <IconAlertTriangle size={16} color="var(--mantine-color-yellow-6)" />;
  }
  return <IconHelpCircle size={16} color="var(--mantine-color-gray-5)" />;
}

/**
 * Invariants (spec 12 page 21).
 *
 * Each row is a promise, the `file:function` that keeps it, and a status computed from the
 * real artefact right now — the signed mode file, the bless digest, the installed hook, the
 * committed bot configs. `unknown` is shown honestly: it means the enforcing file has not
 * landed yet, which is not the same as "fine".
 */
export default function InvariantsPage() {
  const [filter, setFilter] = useState<string>('all');

  const query = useQuery({
    queryKey: ['invariants'],
    queryFn: () => api.get<InvariantsResponse>('/invariants'),
    refetchInterval: 60_000,
  });

  const rows = useMemo(() => {
    const all = query.data?.invariants ?? [];
    if (filter === 'all') return all;
    if (filter === 'problems') return all.filter((row) => row.status !== 'ok');
    return all.filter((row) => row.category === filter);
  }, [query.data, filter]);

  const categories = useMemo(
    () => [...new Set((query.data?.invariants ?? []).map((row) => row.category))].sort(),
    [query.data],
  );

  usePageCommands('invariants', [
    {
      id: 'problems',
      title: 'Invariants: show only what is not ok',
      keywords: ['fail', 'warn', 'broken'],
      run: (ctx) => {
        setFilter('problems');
        ctx.close();
      },
    },
    {
      id: 'all',
      title: 'Invariants: show all',
      run: (ctx) => {
        setFilter('all');
        ctx.close();
      },
    },
    {
      id: 'recheck',
      title: 'Re-check the invariants now',
      run: (ctx) => {
        void query.refetch();
        ctx.close();
      },
    },
  ]);

  if (query.isLoading) {
    return (
      <Group justify="center" p="xl">
        <Loader />
      </Group>
    );
  }

  if (query.isError || !query.data) {
    return <EmptyState title="Invariants unavailable" description={errorMessage(query.error)} />;
  }

  const data = query.data;

  return (
    <Stack gap="md" data-testid="invariants-page">
      <Group justify="space-between" align="flex-end">
        <Stack gap={2}>
          <Title order={3}>Safety invariants</Title>
          <Text size="sm" c="dimmed">
            Promises the code keeps, and the evidence that it still does.
          </Text>
        </Stack>
        <Group gap="xs">
          {(['ok', 'warn', 'fail', 'unknown'] as Status[]).map((status) =>
            data.counts[status] ? (
              <Badge key={status} color={COLOUR[status]}>
                {data.counts[status]} {status}
              </Badge>
            ) : null,
          )}
        </Group>
      </Group>

      {!data.ok ? (
        <Alert color="red" icon={<IconAlertTriangle size={18} />} title="An invariant is broken">
          A red row means the guarantee named in it is no longer enforced. Fix it before going
          live; preflight will refuse anyway.
        </Alert>
      ) : null}

      <Card withBorder padding="sm">
        <Stack gap="xs">
          <Text size="sm" fw={600}>
            Safety strip
          </Text>
          <Group gap="xs">
            {Object.entries(data.strip).map(([pill, status]) => (
              <Badge key={pill} color={COLOUR[status]} leftSection={<StatusIcon status={status} />}>
                {pill}
              </Badge>
            ))}
          </Group>
        </Stack>
      </Card>

      <SegmentedControl
        size="xs"
        value={filter}
        onChange={setFilter}
        data={[
          { value: 'all', label: 'All' },
          { value: 'problems', label: 'Needs attention' },
          ...categories.map((category) => ({ value: category, label: category })),
        ]}
      />

      <Accordion variant="separated" multiple data-testid="invariant-list">
        {rows.map((row) => (
          <Accordion.Item key={row.id} value={row.id}>
            <Accordion.Control icon={<StatusIcon status={row.status} />}>
              <Group justify="space-between" wrap="nowrap">
                <Stack gap={0}>
                  <Text fw={600}>{row.title}</Text>
                  <Text size="xs" c="dimmed">
                    {row.detail}
                  </Text>
                </Stack>
                <Group gap={6} wrap="nowrap">
                  <Badge size="xs" variant="light">
                    {row.category}
                  </Badge>
                  <Badge size="xs" color={COLOUR[row.status]}>
                    {row.status}
                  </Badge>
                </Group>
              </Group>
            </Accordion.Control>
            <Accordion.Panel>
              <Stack gap="xs">
                <Text size="sm">{row.statement}</Text>
                <Text size="sm">
                  Enforced by <Code>{row.enforced_by}</Code>
                </Text>
                <Text size="xs" c="dimmed">
                  Evidence
                </Text>
                <JsonViewer value={row.evidence} />
              </Stack>
            </Accordion.Panel>
          </Accordion.Item>
        ))}
      </Accordion>

      <Card withBorder padding="sm">
        <Stack gap="xs">
          <Text size="sm" fw={600}>
            Last hook denials
          </Text>
          <DataTable
            dense
            rows={data.last_denials}
            rowKey={(_row, index) => String(index)}
            emptyTitle="No denials recorded"
            emptyDescription="logs/hook-denials.jsonl is empty — no automated run has tried a tier-2 write."
            columns={[
              { key: 'ts', header: 'When', render: (row) => String(row['ts'] ?? row['ts_utc'] ?? '') },
              { key: 'tool', header: 'Tool', render: (row) => String(row['tool'] ?? '') },
              { key: 'reason', header: 'Reason', render: (row) => String(row['reason'] ?? '') },
              { key: 'path', header: 'Path', render: (row) => String(row['path'] ?? '') },
            ]}
          />
          <Text size="xs" c="dimmed">
            Checked against {data.checked_root}
          </Text>
        </Stack>
      </Card>
    </Stack>
  );
}
