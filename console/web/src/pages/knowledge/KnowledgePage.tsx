/**
 * Knowledge — everything the system believes and why (spec 12 page 14).
 *
 * Briefs by date, a news explorer that shows the corroboration rule and the model-vs-rule
 * label side by side, source reliability, the computed market state, asset dossiers,
 * incidents and the report files.
 *
 * The one editorial decision here: an uncorroborated item is drawn dimmed with an
 * "unconfirmed" badge rather than hidden. The two-source rule is what the brief applies;
 * the explorer's job is to show what was rejected as well as what was kept.
 */
import {
  Anchor,
  Badge,
  Card,
  Group,
  Loader,
  ScrollArea,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Table,
  Tabs,
  Text,
  Title,
  Tooltip,
} from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';

import { useApi } from '@/app/ApiContext';
import { usePageCommands, type CommandContext } from '@/app/commandRegistry';
import { DataTable, EmptyState, ErrorAlert, JsonViewer, type DataTableColumn } from '@/components';
import { formatRelative, formatUtcStamp } from '@/lib/format';

import { knowledgeApi, knowledgeKeys, type NewsItem, type SourceReliability } from './api';
import { MarkdownPane } from './components/MarkdownPane';

const WINDOWS = [
  { value: '24', label: 'last 24 h' },
  { value: '48', label: 'last 48 h' },
  { value: '168', label: 'last 7 d' },
];

const TABS = [
  ['briefs', 'Briefs'],
  ['news', 'News'],
  ['sources', 'Sources'],
  ['state', 'Market state'],
  ['dossiers', 'Dossiers'],
  ['incidents', 'Incidents'],
  ['reports', 'Reports'],
] as const;

export default function KnowledgePage() {
  const client = useApi();
  const api = useMemo(() => knowledgeApi(client), [client]);
  const [tab, setTab] = useState<string | null>('briefs');

  usePageCommands(
    'knowledge',
    TABS.map(([value, label]) => ({
      id: `tab-${value}`,
      title: `Knowledge: ${label}`,
      run: (ctx: CommandContext) => {
        setTab(value);
        ctx.close();
      },
    })),
  );

  return (
    <Stack gap="md">
      <div>
        <Title order={2}>Knowledge</Title>
        <Text c="dimmed" size="sm">
          The archive the models read from: corroborated news, the computed market state, asset
          dossiers and the reports. Every number here was produced by code, not recalled.
        </Text>
      </div>

      <Tabs value={tab} onChange={setTab} keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="briefs">Briefs</Tabs.Tab>
          <Tabs.Tab value="news">News</Tabs.Tab>
          <Tabs.Tab value="sources">Sources</Tabs.Tab>
          <Tabs.Tab value="state">Market state</Tabs.Tab>
          <Tabs.Tab value="dossiers">Dossiers</Tabs.Tab>
          <Tabs.Tab value="incidents">Incidents</Tabs.Tab>
          <Tabs.Tab value="reports">Reports</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="briefs" pt="sm">
          <BriefsTab api={api} />
        </Tabs.Panel>
        <Tabs.Panel value="news" pt="sm">
          <NewsTab api={api} />
        </Tabs.Panel>
        <Tabs.Panel value="sources" pt="sm">
          <SourcesTab api={api} />
        </Tabs.Panel>
        <Tabs.Panel value="state" pt="sm">
          <StateTab api={api} />
        </Tabs.Panel>
        <Tabs.Panel value="dossiers" pt="sm">
          <DossiersTab api={api} />
        </Tabs.Panel>
        <Tabs.Panel value="incidents" pt="sm">
          <IncidentsTab api={api} />
        </Tabs.Panel>
        <Tabs.Panel value="reports" pt="sm">
          <ReportsTab api={api} />
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}

type Api = ReturnType<typeof knowledgeApi>;

function BriefsTab({ api }: { api: Api }) {
  const [selected, setSelected] = useState<string | null>(null);
  const list = useQuery({ queryKey: knowledgeKeys.briefs, queryFn: () => api.briefs() });
  const briefs = list.data?.briefs ?? [];
  const active = selected ?? briefs[0]?.date ?? null;
  const body = useQuery({
    queryKey: knowledgeKeys.brief(active ?? ''),
    queryFn: () => api.brief(active as string),
    enabled: Boolean(active),
  });

  if (list.isLoading) return <Loader />;
  if (list.isError) return <ErrorAlert error={list.error} title="Could not list the briefs" />;
  if (briefs.length === 0) {
    return <EmptyState title="No briefs yet" description="The brief stage writes one per research run." />;
  }

  return (
    <SimpleGrid cols={{ base: 1, md: 3 }} spacing="sm">
      <Card withBorder radius="md" padding="xs">
        <ScrollArea.Autosize mah={560}>
          <Stack gap={2}>
            {briefs.map((b) => (
              <Anchor
                key={b.date}
                component="button"
                onClick={() => setSelected(b.date)}
                fw={b.date === active ? 700 : 400}
                ta="left"
              >
                {b.date}
              </Anchor>
            ))}
          </Stack>
        </ScrollArea.Autosize>
      </Card>
      <Card withBorder radius="md" padding="md" style={{ gridColumn: 'span 2' }}>
        <MarkdownPane text={body.data} loading={body.isLoading} />
      </Card>
    </SimpleGrid>
  );
}

function NewsTab({ api }: { api: Api }) {
  const [hours, setHours] = useState('48');
  const [eventClass, setEventClass] = useState<string | null>(null);
  const [corroboratedOnly, setCorroboratedOnly] = useState(false);
  const corroborated = corroboratedOnly ? true : null;

  const query = useQuery({
    queryKey: knowledgeKeys.news(Number(hours), eventClass, corroborated),
    queryFn: () => api.news({ hours: Number(hours), event_class: eventClass, corroborated }),
  });
  const rows = query.data?.news ?? [];
  const eventClasses = useMemo(
    () =>
      Array.from(new Set(rows.map((r) => r.event_class).filter(Boolean) as string[]))
        .sort()
        .map((v) => ({ value: v, label: v })),
    [rows],
  );

  const columns: Array<DataTableColumn<NewsItem>> = [
    {
      key: 'ts',
      header: 'When',
      width: 120,
      sortValue: (r) => r.published_at ?? r.fetched_at,
      render: (r) => (
        <Tooltip label={r.published_at ?? r.fetched_at} withArrow>
          <Text size="sm">{formatRelative(r.published_at ?? r.fetched_at)}</Text>
        </Tooltip>
      ),
    },
    {
      key: 'title',
      header: 'Headline',
      render: (r) => (
        <Anchor href={r.url} target="_blank" rel="noreferrer" size="sm" c={r.corroborated ? undefined : 'dimmed'}>
          {r.title}
        </Anchor>
      ),
    },
    {
      key: 'source',
      header: 'Source',
      width: 150,
      sortValue: (r) => r.source,
      render: (r) => (
        <Group gap={4} wrap="nowrap">
          <Text size="sm">{r.source}</Text>
          <Badge size="xs" variant="outline" color={r.source_class === 'primary' ? 'teal' : 'gray'}>
            {r.source_class}
          </Badge>
        </Group>
      ),
    },
    {
      key: 'class',
      header: 'Event',
      width: 160,
      render: (r) => (
        <Group gap={4} wrap="nowrap">
          {r.event_class ? <Badge size="xs" variant="light">{r.event_class}</Badge> : <Text size="xs" c="dimmed">—</Text>}
          <Tooltip label={`labelled by ${r.label_source}`} withArrow>
            <Badge size="xs" variant="outline" color={r.label_source === 'rule' ? 'gray' : 'blue'}>
              {r.label_source === 'rule' ? 'rule' : 'model'}
            </Badge>
          </Tooltip>
        </Group>
      ),
    },
    {
      key: 'corroborated',
      header: 'Corroboration',
      width: 140,
      render: (r) =>
        r.corroborated ? (
          <Badge size="sm" color="teal" variant="light">
            {r.corroborating_sources} sources
          </Badge>
        ) : (
          <Badge size="sm" color="gray" variant="light">
            unconfirmed
          </Badge>
        ),
    },
    {
      key: 'claim',
      header: 'Claim check',
      width: 110,
      render: (r) =>
        r.claim_verified === null ? (
          <Text size="xs" c="dimmed">
            n/a
          </Text>
        ) : (
          <Badge size="sm" color={r.claim_verified ? 'teal' : 'red'} variant="light">
            {r.claim_verified ? 'verified' : 'falsified'}
          </Badge>
        ),
    },
  ];

  return (
    <Stack gap="sm">
      <Group gap="sm">
        <Select data={WINDOWS} value={hours} onChange={(v) => setHours(v ?? '48')} w={150} label="Window" />
        <Select
          data={eventClasses}
          value={eventClass}
          onChange={setEventClass}
          clearable
          w={180}
          label="Event class"
          placeholder="any"
        />
        <Switch
          label="Corroborated only"
          checked={corroboratedOnly}
          onChange={(e) => setCorroboratedOnly(e.currentTarget.checked)}
          mt={24}
        />
      </Group>
      <ErrorAlert error={query.error} title="Could not load the news archive" />
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.url_hash}
        loading={query.isLoading}
        emptyTitle="No news in this window"
        maxHeight={560}
        dense
      />
    </Stack>
  );
}

function SourcesTab({ api }: { api: Api }) {
  const query = useQuery({ queryKey: knowledgeKeys.sources, queryFn: () => api.sources() });
  const rows = query.data?.sources ?? [];
  const columns: Array<DataTableColumn<SourceReliability>> = [
    { key: 'source', header: 'Source', render: (r) => <Text size="sm">{r.source}</Text> },
    { key: 'unconf', header: 'Unconfirmed', align: 'right', render: (r) => r.n_unconfirmed },
    {
      key: 'later',
      header: 'Later corroborated',
      align: 'right',
      render: (r) => r.n_corroborated_later,
    },
    { key: 'false', header: 'Falsified', align: 'right', render: (r) => r.n_falsified },
    {
      key: 'claims',
      header: 'Claims verified',
      align: 'right',
      render: (r) => `${r.n_claims_verified}/${r.n_claims_checked}`,
    },
  ];
  if (query.isError) {
    return <ErrorAlert error={query.error} title="Could not load source reliability" />;
  }
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(r) => r.source}
      loading={query.isLoading}
      emptyTitle="No reliability data yet"
      emptyDescription="The daily review updates this deterministically as unconfirmed items resolve."
    />
  );
}

function StateTab({ api }: { api: Api }) {
  const query = useQuery({ queryKey: knowledgeKeys.state, queryFn: () => api.state(20) });
  if (query.isLoading) return <Loader />;
  if (query.isError) {
    return <ErrorAlert error={query.error} title="Could not read the market state" />;
  }
  const data = query.data;
  return (
    <Stack gap="sm">
      <Card withBorder radius="md" padding="md">
        <Title order={5} mb="xs">
          Latest computed state
        </Title>
        {data?.latest ? (
          <JsonViewer value={data.latest} defaultExpandedDepth={1} />
        ) : (
          <EmptyState compact title="No state file" description={data?.path ?? ''} />
        )}
      </Card>
      {data?.snapshots?.length ? (
        <Card withBorder radius="md" padding="md">
          <Title order={5} mb="xs">
            Regime history
          </Title>
          <Table striped withTableBorder>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>When</Table.Th>
                <Table.Th>Regime</Table.Th>
                <Table.Th>Producer</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {data.snapshots.map((s) => (
                <Table.Tr key={s.id}>
                  <Table.Td>{formatUtcStamp(s.ts_utc)}</Table.Td>
                  <Table.Td>
                    <Badge variant="light">{s.regime ?? '—'}</Badge>
                  </Table.Td>
                  <Table.Td>{s.producer}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Card>
      ) : null}
    </Stack>
  );
}

function DossiersTab({ api }: { api: Api }) {
  const [selected, setSelected] = useState<string | null>(null);
  const list = useQuery({ queryKey: knowledgeKeys.dossiers, queryFn: () => api.dossiers() });
  const dossiers = list.data?.dossiers ?? [];
  const active = selected ?? dossiers[0]?.asset ?? null;
  const body = useQuery({
    queryKey: knowledgeKeys.dossier(active ?? ''),
    queryFn: () => api.dossier(active as string),
    enabled: Boolean(active),
  });

  if (list.isLoading) return <Loader />;
  if (list.isError) return <ErrorAlert error={list.error} title="Could not list the dossiers" />;
  if (dossiers.length === 0) {
    return (
      <EmptyState
        title="No dossiers yet"
        description="The asset-dossier skill writes one per asset: history, drawdowns, vol regimes, seasonality."
      />
    );
  }
  return (
    <SimpleGrid cols={{ base: 1, md: 4 }} spacing="sm">
      <Card withBorder radius="md" padding="xs">
        <Stack gap={2}>
          {dossiers.map((d) => (
            <Anchor
              key={d.asset}
              component="button"
              onClick={() => setSelected(d.asset)}
              fw={d.asset === active ? 700 : 400}
              ta="left"
            >
              {d.asset}
            </Anchor>
          ))}
        </Stack>
      </Card>
      <Card withBorder radius="md" padding="md" style={{ gridColumn: 'span 3' }}>
        <MarkdownPane text={body.data} loading={body.isLoading} />
      </Card>
    </SimpleGrid>
  );
}

function IncidentsTab({ api }: { api: Api }) {
  const query = useQuery({ queryKey: knowledgeKeys.incidents, queryFn: () => api.incidents(30) });
  const rows = query.data?.incidents ?? [];
  if (query.isError) return <ErrorAlert error={query.error} title="Could not load the incidents" />;
  return (
    <DataTable
      dense
      loading={query.isLoading}
      rows={rows}
      rowKey={(r, i) => String(r.id ?? i)}
      emptyTitle="No incidents in the last 30 days"
      columns={[
        { key: 'ts', header: 'When', render: (r) => <Text size="sm">{formatUtcStamp(String(r.opened_utc ?? ''))}</Text> },
        { key: 'kind', header: 'Kind', render: (r) => <Badge variant="light">{String(r.kind ?? '—')}</Badge> },
        { key: 'subkind', header: 'Subkind', render: (r) => <Text size="sm">{String(r.subkind ?? '—')}</Text> },
        { key: 'detail', header: 'Detail', render: (r) => <Text size="sm" lineClamp={2}>{String(r.detail ?? '')}</Text> },
      ]}
    />
  );
}

function ReportsTab({ api }: { api: Api }) {
  const query = useQuery({ queryKey: knowledgeKeys.reports, queryFn: () => api.reports() });
  const rows = query.data?.reports ?? [];
  if (query.isError) return <ErrorAlert error={query.error} title="Could not list the reports" />;
  return (
    <DataTable
      dense
      loading={query.isLoading}
      rows={rows}
      rowKey={(r) => r.path}
      emptyTitle="No reports yet"
      emptyDescription="Weekly reviews and the xlsx workbook land in reports/."
      columns={[
        {
          key: 'path',
          header: 'File',
          render: (r) => (
            <Anchor href={`/api/reports/${r.path}`} target="_blank" rel="noreferrer" size="sm">
              {r.path}
            </Anchor>
          ),
        },
        { key: 'modified', header: 'Modified', render: (r) => <Text size="sm">{formatUtcStamp(r.modified)}</Text> },
        {
          key: 'bytes',
          header: 'Size',
          align: 'right',
          render: (r) => <Text size="sm">{(r.bytes / 1024).toFixed(1)} KB</Text>,
        },
      ]}
    />
  );
}
