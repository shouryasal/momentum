import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { ApiClient } from '@/api';
import { ApiProvider } from '@/app/ApiContext';
import { fakeFetch, renderWithProviders } from '@/test/utils';

import AiModelsPage from './AiModelsPage';
import OllamaPanel from './components/OllamaPanel';
import RoutingMatrix from './components/RoutingMatrix';
import type { OllamaDetection, RoutingResponse } from './api';

const PROVIDERS = {
  auth_mode: 'subscription',
  providers: [
    {
      key: 'claude:subscription', kind: 'claude_sdk', enabled: true,
      credential_present: true, auth_source: 'token',
      detail: 'CLAUDE_CODE_OAUTH_TOKEN (claude setup-token)',
      circuit: 'closed', consecutive_failures: 0, open_until: null,
      last_ok_utc: '2026-09-22T08:00:00Z', last_error: null, degraded_until: null,
    },
    {
      key: 'claude:api_key', kind: 'claude_sdk', enabled: false,
      credential_present: false, auth_source: 'api_key',
      detail: 'metered; monthly cap $30',
      circuit: 'open', consecutive_failures: 3, open_until: '2026-09-22T08:15:00Z',
      last_ok_utc: null, last_error: 'HTTP 401 invalid x-api-key', degraded_until: null,
    },
    {
      key: 'ollama', kind: 'ollama', enabled: true, credential_present: false,
      auth_source: 'local', detail: 'not detected', base_url: null,
      circuit: 'closed', consecutive_failures: 0, open_until: null,
      last_ok_utc: null, last_error: null, degraded_until: null,
    },
  ],
  rate_limit: { status: null, utilization: null, resets_at: null },
  month: { month: '2026-09', total_usd: 12.5, by_provider: { 'claude:subscription': 12.5 } },
};

const ROUTING: RoutingResponse = {
  tasks: [
    {
      task: 'decide', chain: [{ alias: 'opus', provider: 'claude', id: 'claude-opus-5', tier: 4, declared: true, local: false }],
      escalation: { alias: 'fable', provider: 'claude', id: 'claude-fable-5-1', tier: 5, declared: true, local: false },
      tools: 'read_only', min_tier: 4, code_min_tier: 4, effective_min_tier: 4,
      local_forbidden: true, allow_local: false, local_mode: null, retry: 1, effort: 'max',
      max_turns: 12, max_usd_per_run: 4, monthly_budget_usd: 60, deadline_s: 900,
      on_all_failed: 'hold_last', overlay_head: false,
    },
    {
      task: 'scan',
      chain: [
        { alias: 'local_small', provider: 'ollama', id: 'llama3.1:8b', tier: 1, declared: true, local: true },
        { alias: 'haiku', provider: 'claude', id: 'claude-haiku-4-5-20251001', tier: 2, declared: true, local: false },
      ],
      escalation: null, tools: 'none', min_tier: 1, code_min_tier: 1,
      effective_min_tier: 1, local_forbidden: false, allow_local: true, local_mode: null,
      retry: 0, effort: 'high', max_turns: 1, max_usd_per_run: 0.05,
      monthly_budget_usd: 3, deadline_s: 90, on_all_failed: 'skip_screen',
      overlay_head: true,
    },
  ],
  switching: { max_attempts_per_call: 4 },
  budget: { mode: 'telemetry', monthly_total_usd: 150, throttle_at_pct: 80 },
  shadow: {},
  models: { opus: { provider: 'claude', id: 'claude-opus-5', tier: 4 } },
};

const UNREACHABLE: OllamaDetection = {
  base_url: null, version: null, ok: false, cached: false,
  results: [
    { url: 'http://127.0.0.1:11434', ok: false, version: null, error: 'ConnectError', latency_ms: 1 },
    { url: 'http://172.29.64.1:11434', ok: false, version: null, error: 'ConnectError', latency_ms: 2 },
  ],
  guidance: {
    reachable: false,
    base_url: null,
    tried: ['http://127.0.0.1:11434', 'http://172.29.64.1:11434'],
    wsl_gateway: '172.29.64.1',
    problem: 'Ollama runs on the Windows host; inside WSL2 127.0.0.1 is not the host.',
    options: [
      {
        title: 'Bind Ollama to all interfaces (works on any WSL mode)',
        shell: 'PowerShell (Windows, as Administrator)',
        commands: ['[Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "User")'],
        then: 'Earn will then find it at http://172.29.64.1:11434',
      },
    ],
    note: 'An explicit base_url must stay loopback or private.',
  },
};

function client(routes: Parameters<typeof fakeFetch>[0]): ApiClient {
  return new ApiClient({ fetchImpl: fakeFetch(routes) });
}

describe('RoutingMatrix', () => {
  it('marks the code-enforced tier floor as locked, not editable', () => {
    renderWithProviders(<RoutingMatrix routing={ROUTING} />);
    const matrix = screen.getByTestId('routing-matrix');
    expect(matrix).toHaveTextContent('decide');
    expect(matrix).toHaveTextContent('no local');
    // scan has no floor above 1 and may be served locally
    expect(matrix).toHaveTextContent('local_small');
  });

  it('flags a chain head that came from the tier-1 overlay', () => {
    renderWithProviders(<RoutingMatrix routing={ROUTING} />);
    expect(screen.getByTestId('routing-matrix')).toHaveTextContent('overlay');
  });

  it('shows the escalation model separately from the chain', () => {
    renderWithProviders(<RoutingMatrix routing={ROUTING} />);
    expect(screen.getByTestId('routing-matrix')).toHaveTextContent('↑ fable');
  });
});

describe('OllamaPanel', () => {
  it('shows every probed candidate with its failure', () => {
    renderWithProviders(
      <OllamaPanel
        detection={UNREACHABLE}
        models={[]}
        loading={false}
        pulling={false}
        onDetect={() => undefined}
        onPull={() => undefined}
      />,
    );
    const probes = screen.getByTestId('ollama-probes');
    expect(probes).toHaveTextContent('http://127.0.0.1:11434');
    expect(probes).toHaveTextContent('http://172.29.64.1:11434');
  });

  it('offers the exact PowerShell when nothing answers', async () => {
    renderWithProviders(
      <OllamaPanel
        detection={UNREACHABLE}
        models={[]}
        loading={false}
        pulling={false}
        onDetect={() => undefined}
        onPull={() => undefined}
      />,
    );
    expect(screen.getByText(/not reachable from WSL/)).toBeInTheDocument();
    await userEvent.click(screen.getByText(/Bind Ollama to all interfaces/));
    expect(await screen.findByText(/OLLAMA_HOST/)).toBeInTheDocument();
  });
});

describe('AiModelsPage', () => {
  it('renders a card per provider key with its breaker state', async () => {
    const api = client({
      '/api/llm/providers': { body: PROVIDERS },
      '/api/llm/routing': { body: ROUTING },
      '/api/llm/ollama/detect': { body: UNREACHABLE },
      '/api/llm/usage': { body: { group: 'task', rows: [], month: PROVIDERS.month } },
      '/api/llm/switches': { body: { switches: [] } },
    });
    renderWithProviders(
      <ApiProvider client={api}>
        <AiModelsPage />
      </ApiProvider>,
    );
    await waitFor(() => expect(screen.getByText('claude:subscription')).toBeInTheDocument());
    expect(screen.getByText('claude:api_key')).toBeInTheDocument();
    expect(screen.getByText('HTTP 401 invalid x-api-key')).toBeInTheDocument();
    expect(screen.getByText('auth mode: subscription')).toBeInTheDocument();
  });

  it('never renders a credential value, only presence', async () => {
    const api = client({
      '/api/llm/providers': { body: PROVIDERS },
      '/api/llm/routing': { body: ROUTING },
      '/api/llm/ollama/detect': { body: UNREACHABLE },
      '/api/llm/usage': { body: { group: 'task', rows: [], month: PROVIDERS.month } },
      '/api/llm/switches': { body: { switches: [] } },
    });
    const { container } = renderWithProviders(
      <ApiProvider client={api}>
        <AiModelsPage />
      </ApiProvider>,
    );
    await waitFor(() => expect(screen.getByText('claude:subscription')).toBeInTheDocument());
    expect(container.textContent).not.toMatch(/sk-ant-/);
    // presence is the only credential fact on the page — two providers lack one here
    expect(screen.getByText('credential present')).toBeInTheDocument();
    expect(screen.getAllByText('no credential')).toHaveLength(2);
  });
});
