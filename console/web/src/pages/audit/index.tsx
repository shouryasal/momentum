import {
  Badge,
  Card,
  Code,
  Drawer,
  Grid,
  Group,
  Loader,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import { IconSearch } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import { api, errorMessage } from '@/api';
import { usePageCommands } from '@/app/commandRegistry';
import { CodeEditor, DataTable, EmptyState, JsonViewer } from '@/components';

export interface AuditEntry {
  source: string;
  ts_utc: string;
  actor: string;
  action: string;
  target: string | null;
  result: string;
  ref: string | null;
  detail: unknown;
  has_diff: boolean;
  diff: string | null;
}

interface AuditResponse {
  entries: AuditEntry[];
  count: number;
  by_source: Record<string, number>;
  sources: string[];
}

const RESULTS = [
  { value: '', label: 'Any result' },
  { value: 'ok', label: 'ok' },
  { value: 'denied', label: 'denied' },
  { value: 'failed', label: 'failed' },
];

const COLOUR: Record<string, string> = {
  ok: 'teal',
  denied: 'orange',
  failed: 'red',
};

/**
 * Audit (spec 12 page 20).
 *
 * One timeline over `audit_log`, `config_audit`, `mode_transitions`, approvals, change
 * events and console jobs. A refusal is a row here too — that is the point: "nothing
 * happened" and "something was denied" must never look the same.
 */
export default function AuditPage() {
  const [actor, setActor] = useState('');
  const [action, setAction] = useState('');
  const [source, setSource] = useState<string | null>('');
  const [result, setResult] = useState<string | null>('');
  const [q, setQ] = useState('');
  const [from, setFrom] = useState('');
  const [includeDiff, setIncludeDiff] = useState(false);
  const [selected, setSelected] = useState<AuditEntry | null>(null);

  const query = useQuery({
    queryKey: ['audit', { actor, action, source, result, q, from, includeDiff }],
    queryFn: () =>
      api.get<AuditResponse>('/audit', {
        ...(actor ? { actor } : {}),
        ...(action ? { action } : {}),
        ...(source ? { source } : {}),
        ...(result ? { result } : {}),
        ...(q ? { q } : {}),
        ...(from ? { from } : {}),
        include_diff: includeDiff,
        limit: 300,
      }),
    refetchInterval: 60_000,
  });

  const sources = query.data?.sources ?? [];

  usePageCommands('audit', [
    {
      id: 'clear-filters',
      title: 'Clear the audit filters',
      run: (ctx) => {
        setActor('');
        setAction('');
        setSource('');
        setResult('');
        setQ('');
        setFrom('');
        ctx.close();
      },
    },
    {
      id: 'denials',
      title: 'Audit: show refusals only',
      subtitle: 'Everything privileged that was denied',
      keywords: ['denied', 'refused', 'hook'],
      run: (ctx) => {
        setResult('denied');
        ctx.close();
      },
    },
    {
      id: 'diffs',
      title: 'Audit: include the diffs',
      run: (ctx) => {
        setIncludeDiff(true);
        ctx.close();
      },
    },
  ]);

  return (
    <Stack gap="md" data-testid="audit-page">
      <Group justify="space-between" align="flex-end">
        <Stack gap={2}>
          <Title order={3}>Audit</Title>
          <Text size="sm" c="dimmed">
            Everything privileged that happened, allowed or refused.
          </Text>
        </Stack>
        <Group gap="xs">
          {Object.entries(query.data?.by_source ?? {}).map(([name, count]) => (
            <Badge key={name} variant="light">
              {name}: {count}
            </Badge>
          ))}
        </Group>
      </Group>

      <Card withBorder padding="sm">
        <Grid>
          <Grid.Col span={{ base: 12, sm: 6, md: 3 }}>
            <TextInput
              size="xs"
              label="Actor"
              placeholder="human:console"
              value={actor}
              onChange={(event) => setActor(event.currentTarget.value)}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 12, sm: 6, md: 3 }}>
            <TextInput
              size="xs"
              label="Action"
              placeholder="config.save"
              value={action}
              onChange={(event) => setAction(event.currentTarget.value)}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 12, sm: 6, md: 2 }}>
            <Select
              size="xs"
              label="Source"
              data={[{ value: '', label: 'All sources' },
                ...sources.map((name) => ({ value: name, label: name }))]}
              value={source}
              onChange={setSource}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 12, sm: 6, md: 2 }}>
            <Select size="xs" label="Result" data={RESULTS} value={result} onChange={setResult} />
          </Grid.Col>
          <Grid.Col span={{ base: 12, sm: 6, md: 2 }}>
            <TextInput
              size="xs"
              label="From (UTC)"
              placeholder="2026-09-01T00:00:00Z"
              value={from}
              onChange={(event) => setFrom(event.currentTarget.value)}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 12, md: 8 }}>
            <TextInput
              size="xs"
              label="Search"
              leftSection={<IconSearch size={14} />}
              placeholder="target, detail, changed path"
              value={q}
              onChange={(event) => setQ(event.currentTarget.value)}
            />
          </Grid.Col>
          <Grid.Col span={{ base: 12, md: 4 }}>
            <Switch
              mt={22}
              size="xs"
              label="Load diffs with the timeline"
              checked={includeDiff}
              onChange={(event) => setIncludeDiff(event.currentTarget.checked)}
            />
          </Grid.Col>
        </Grid>
      </Card>

      {query.isLoading ? (
        <Group justify="center" p="xl">
          <Loader />
        </Group>
      ) : query.isError ? (
        <EmptyState title="Audit unavailable" description={errorMessage(query.error)} />
      ) : (
        <DataTable<AuditEntry>
          rows={query.data?.entries ?? []}
          rowKey={(row, index) => `${row.source}:${row.ref}:${index}`}
          onRowClick={setSelected}
          emptyTitle="Nothing recorded yet"
          emptyDescription="The journal is empty, or no row matches these filters."
          maxHeight={640}
          columns={[
            {
              key: 'ts',
              header: 'When (UTC)',
              render: (row) => row.ts_utc,
              sortValue: (row) => row.ts_utc,
              width: 190,
            },
            {
              key: 'source',
              header: 'Source',
              render: (row) => (
                <Badge size="xs" variant="light">
                  {row.source}
                </Badge>
              ),
              sortValue: (row) => row.source,
            },
            {
              key: 'action',
              header: 'Action',
              render: (row) => <Code>{row.action}</Code>,
              sortValue: (row) => row.action,
            },
            { key: 'target', header: 'Target', render: (row) => row.target ?? '' },
            { key: 'actor', header: 'Actor', render: (row) => row.actor, sortValue: (r) => r.actor },
            {
              key: 'result',
              header: 'Result',
              render: (row) => (
                <Badge size="xs" color={COLOUR[row.result] ?? 'gray'}>
                  {row.result}
                </Badge>
              ),
              sortValue: (row) => row.result,
            },
            {
              key: 'diff',
              header: '',
              render: (row) =>
                row.has_diff ? (
                  <Badge size="xs" variant="outline">
                    diff
                  </Badge>
                ) : null,
            },
          ]}
        />
      )}

      <Drawer
        opened={selected !== null}
        onClose={() => setSelected(null)}
        position="right"
        size="lg"
        title={selected ? `${selected.source} · ${selected.action}` : ''}
      >
        {selected ? <EntryDetail entry={selected} /> : null}
      </Drawer>
    </Stack>
  );
}

function EntryDetail({ entry }: { entry: AuditEntry }) {
  const detail = useQuery({
    queryKey: ['audit', 'entry', entry.source, entry.ref],
    queryFn: () =>
      api.get<{ rows: Array<Record<string, unknown>> }>(
        `/audit/entry/${entry.source}/${entry.ref ?? ''}`,
      ),
    enabled: Boolean(entry.ref),
  });

  const row = detail.data?.rows?.[0];
  const diff = entry.diff ?? (typeof row?.['diff'] === 'string' ? (row['diff'] as string) : null);

  return (
    <Stack gap="md">
      <Group gap="xs">
        <Badge variant="light">{entry.ts_utc}</Badge>
        <Badge color={COLOUR[entry.result] ?? 'gray'}>{entry.result}</Badge>
        <Badge variant="outline">{entry.actor}</Badge>
      </Group>
      {entry.target ? (
        <Text size="sm">
          Target: <Code>{entry.target}</Code>
        </Text>
      ) : null}
      <JsonViewer value={entry.detail} />
      {diff ? (
        <Stack gap={4}>
          <Text size="sm" fw={600}>
            Diff
          </Text>
          <CodeEditor value={diff} language="text" readOnly height="320px" />
        </Stack>
      ) : null}
      {detail.isLoading ? <Loader size="xs" /> : null}
    </Stack>
  );
}
