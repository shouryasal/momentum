/**
 * AI & Models (spec 12, page 10).
 *
 * Six tabs over one question: **who answered, and why that one?** Provider cards say
 * which credentials exist and whether a breaker is open; Ollama says whether the local
 * model is reachable and, when it is not, exactly which PowerShell fixes it; the routing
 * matrix shows the chain per task with the code-enforced tier floors marked as locked;
 * usage and switches say what that routing actually cost and every time it fell back;
 * the playground lets a prompt be tried against the real chain without writing anything.
 */
import { Alert, Badge, Group, Loader, SegmentedControl, Stack, Tabs, Text, Title } from '@mantine/core';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';

import { errorMessage } from '@/api';
import { useApi } from '@/app/ApiContext';
import { usePageCommands } from '@/app/commandRegistry';
import { DataTable, StatCard } from '@/components';

import {
  llmApi,
  llmKeys,
  type PlaygroundResult,
  type ProviderKey,
  type SwitchRow,
  type TestVerdict,
  type UsageGroup,
  type UsageRow,
} from './api';
import OllamaPanel from './components/OllamaPanel';
import Playground from './components/Playground';
import ProviderCards from './components/ProviderCards';
import RoutingMatrix from './components/RoutingMatrix';

const USAGE_GROUPS: UsageGroup[] = ['task', 'model', 'provider', 'auth', 'day'];

const SWITCH_COLOR: Record<string, string> = {
  rate_limited: 'yellow',
  auth_error: 'red',
  budget_exhausted: 'red',
  quota_exhausted: 'red',
  provider_down: 'orange',
  schema_invalid: 'grape',
  timeout: 'orange',
  error: 'gray',
  empty_output: 'gray',
  skipped_capability: 'blue',
};

export function AiModelsPage() {
  const client = useApi();
  const api = useMemo(() => llmApi(client), [client]);
  const queryClient = useQueryClient();

  const [group, setGroup] = useState<UsageGroup>('task');
  const [verdicts, setVerdicts] = useState<Record<string, TestVerdict | undefined>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [playgroundResult, setPlaygroundResult] = useState<PlaygroundResult | undefined>();
  const [playgroundError, setPlaygroundError] = useState<string | null>(null);

  const providers = useQuery({ queryKey: llmKeys.providers, queryFn: api.providers });
  const routing = useQuery({ queryKey: llmKeys.routing, queryFn: api.routing });
  const detection = useQuery({ queryKey: llmKeys.detect, queryFn: api.detect });
  const models = useQuery({
    queryKey: llmKeys.models,
    queryFn: api.models,
    enabled: Boolean(detection.data?.ok),
    retry: false,
  });
  const usage = useQuery({ queryKey: llmKeys.usage(group), queryFn: () => api.usage(group) });
  const switches = useQuery({ queryKey: llmKeys.switches, queryFn: () => api.switches() });

  const test = useMutation({
    mutationFn: (key: ProviderKey) => api.testProvider(key),
    onMutate: (key) => setBusy(`test:${key}`),
    onSuccess: (verdict) => setVerdicts((current) => ({ ...current, [verdict.target]: verdict })),
    onSettled: () => setBusy(null),
  });

  const reset = useMutation({
    mutationFn: (key: ProviderKey) => api.resetCircuit(key),
    onMutate: (key) => setBusy(`reset:${key}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: llmKeys.providers }),
    onSettled: () => setBusy(null),
  });

  const pull = useMutation({
    mutationFn: (model: string) => api.pull(model),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: llmKeys.models }),
  });

  usePageCommands('ai-models', [
    {
      id: 'test-subscription',
      title: 'Test the Claude subscription credential',
      keywords: ['provider', 'probe'],
      run: (ctx) => {
        test.mutate('claude:subscription');
        ctx.close();
      },
    },
    {
      id: 'detect-ollama',
      title: 'Re-detect Ollama',
      keywords: ['local', 'llama'],
      run: (ctx) => {
        void detection.refetch();
        ctx.close();
      },
    },
    {
      id: 'usage-by-day',
      title: 'Show model usage by day',
      run: (ctx) => {
        setGroup('day');
        ctx.close();
      },
    },
  ]);

  const playground = useMutation({
    mutationFn: api.playground,
    onMutate: () => {
      setPlaygroundError(null);
      setPlaygroundResult(undefined);
    },
    onSuccess: (result) => setPlaygroundResult(result),
    onError: (error: unknown) => setPlaygroundError(errorMessage(error)),
  });

  /** The provider key of a test verdict is the target name, not the key; map it back. */
  const verdictsByKey = useMemo(() => {
    const byTarget: Record<string, TestVerdict | undefined> = {};
    for (const [target, verdict] of Object.entries(verdicts)) {
      const key =
        target === 'claude_subscription'
          ? 'claude:subscription'
          : target === 'claude_api_key'
            ? 'claude:api_key'
            : target;
      byTarget[key] = verdict;
    }
    return byTarget;
  }, [verdicts]);

  const usageColumns = [
    { key: 'bucket', header: group, render: (row: UsageRow) => row.bucket,
      sortValue: (row: UsageRow) => row.bucket },
    { key: 'calls', header: 'calls', align: 'right' as const,
      render: (row: UsageRow) => row.calls, sortValue: (row: UsageRow) => row.calls },
    { key: 'ok', header: 'success', align: 'right' as const,
      render: (row: UsageRow) => `${(row.success_rate * 100).toFixed(0)}%`,
      sortValue: (row: UsageRow) => row.success_rate },
    { key: 'cost', header: 'cost', align: 'right' as const,
      render: (row: UsageRow) => `$${row.cost_usd.toFixed(3)}`,
      sortValue: (row: UsageRow) => row.cost_usd },
    { key: 'tokens', header: 'tokens in/out', align: 'right' as const,
      render: (row: UsageRow) => `${row.input_tokens} / ${row.output_tokens}`,
      sortValue: (row: UsageRow) => row.input_tokens + row.output_tokens },
    { key: 'latency', header: 'avg ms', align: 'right' as const,
      render: (row: UsageRow) => Math.round(row.avg_latency_ms),
      sortValue: (row: UsageRow) => row.avg_latency_ms },
  ];

  const switchColumns = [
    { key: 'ts', header: 'when', render: (row: SwitchRow) => row.ts_utc,
      sortValue: (row: SwitchRow) => row.ts_utc },
    { key: 'task', header: 'task', render: (row: SwitchRow) => row.task },
    { key: 'from', header: 'from',
      render: (row: SwitchRow) => `${row.from_model ?? '—'} (${row.from_provider ?? '—'})` },
    { key: 'to', header: 'to',
      render: (row: SwitchRow) => `${row.to_model ?? '—'} (${row.to_provider ?? '—'})` },
    { key: 'reason', header: 'reason',
      render: (row: SwitchRow) => (
        <Badge variant="light" color={SWITCH_COLOR[row.reason] ?? 'gray'}>
          {row.reason}
        </Badge>
      ) },
    { key: 'detail', header: 'detail',
      render: (row: SwitchRow) => <Text size="xs" c="dimmed">{row.detail ?? ''}</Text> },
  ];

  if (providers.isLoading) {
    return <Group justify="center" p="xl"><Loader /></Group>;
  }

  return (
    <Stack gap="md">
      <Group justify="space-between" align="flex-end">
        <div>
          <Title order={3}>AI &amp; Models</Title>
          <Text size="sm" c="dimmed">
            Providers, chains, cost and every fallback this system has made.
          </Text>
        </div>
        <Badge size="lg" variant="light">
          auth mode: {providers.data?.auth_mode ?? '—'}
        </Badge>
      </Group>

      {providers.isError ? (
        <Alert color="red" title="Could not read the provider layer">
          {errorMessage(providers.error)}
        </Alert>
      ) : null}

      <Group grow>
        <StatCard
          label="spent this month"
          value={`$${(providers.data?.month?.total_usd ?? 0).toFixed(2)}`}
          hint="metered calls only; local inference is free"
        />
        <StatCard
          label="rate limit"
          value={
            providers.data?.rate_limit?.utilization != null
              ? `${(providers.data.rate_limit.utilization * 100).toFixed(0)}%`
              : (providers.data?.rate_limit?.status ?? 'clear')
          }
          hint="subscription window utilisation"
        />
        <StatCard
          label="open circuits"
          value={(providers.data?.providers ?? []).filter((p) => p.circuit === 'open').length}
          hint="providers the router is currently skipping"
        />
        <StatCard
          label="switches logged"
          value={switches.data?.switches.length ?? 0}
          hint="every downgrade is journaled"
        />
      </Group>

      <Tabs defaultValue="providers" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="providers">Providers</Tabs.Tab>
          <Tabs.Tab value="ollama">Ollama</Tabs.Tab>
          <Tabs.Tab value="routing">Routing</Tabs.Tab>
          <Tabs.Tab value="usage">Usage</Tabs.Tab>
          <Tabs.Tab value="switches">Switches</Tabs.Tab>
          <Tabs.Tab value="playground">Playground</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="providers" pt="md">
          <ProviderCards
            providers={providers.data?.providers ?? []}
            rateLimit={providers.data?.rate_limit ?? {
              status: null, utilization: null, resets_at: null,
            }}
            month={providers.data?.month ?? { month: '', total_usd: 0, by_provider: {} }}
            verdicts={verdictsByKey}
            busy={busy}
            onTest={(key) => test.mutate(key)}
            onReset={(key) => reset.mutate(key)}
          />
        </Tabs.Panel>

        <Tabs.Panel value="ollama" pt="md">
          <OllamaPanel
            detection={detection.data}
            models={models.data?.models ?? []}
            loading={detection.isFetching}
            pulling={pull.isPending}
            onDetect={() => {
              queryClient.invalidateQueries({ queryKey: llmKeys.detect });
              queryClient.invalidateQueries({ queryKey: llmKeys.models });
            }}
            onPull={(model) => pull.mutate(model)}
          />
        </Tabs.Panel>

        <Tabs.Panel value="routing" pt="md">
          {routing.data ? (
            <RoutingMatrix routing={routing.data} />
          ) : (
            <Group justify="center" p="xl"><Loader /></Group>
          )}
        </Tabs.Panel>

        <Tabs.Panel value="usage" pt="md">
          <Stack gap="sm">
            <SegmentedControl
              value={group}
              onChange={(value) => setGroup(value as UsageGroup)}
              data={USAGE_GROUPS.map((value) => ({ value, label: value }))}
            />
            <DataTable
              columns={usageColumns}
              rows={usage.data?.rows ?? []}
              rowKey={(row) => row.bucket}
              loading={usage.isLoading}
              emptyTitle="No calls yet"
              emptyDescription="Every attempt writes an llm_calls row; nothing has run in this window."
            />
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value="switches" pt="md">
          <DataTable
            columns={switchColumns}
            rows={switches.data?.switches ?? []}
            rowKey={(row) => String(row.id)}
            loading={switches.isLoading}
            emptyTitle="No switches"
            emptyDescription="The head of every chain has served so far."
          />
        </Tabs.Panel>

        <Tabs.Panel value="playground" pt="md">
          <Playground
            routing={routing.data}
            result={playgroundResult}
            running={playground.isPending}
            error={playgroundError}
            onRun={(body) => playground.mutate(body)}
          />
        </Tabs.Panel>
      </Tabs>
    </Stack>
  );
}

export default AiModelsPage;
