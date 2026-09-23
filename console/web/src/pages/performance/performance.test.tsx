import { MantineProvider } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within, type RenderResult } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement, ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';

import { ApiClient } from '@/api';
import { ApiProvider } from '@/app/ApiContext';
import { fakeFetch } from '@/test/utils';
import { theme } from '@/theme';

import PerformancePage from './index';

/**
 * Performance is the page that answers "is Earn earning its place?", so the states that
 * matter most are the unhappy ones: a failed read must never render as a flat line or an
 * empty table, because an empty chart and a 503 look identical and mean opposite things.
 */

function navPoints(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    ts_utc: `2026-09-0${(i % 9) + 1}T00:00:00Z`,
    days: i,
    nav: 10000 + i * 25,
    index: 100 + i * 0.25,
  }));
}

const SUMMARY = {
  sleeves: [
    {
      sleeve: 'a',
      run_id: 'test-a-20260901-01',
      mode: 'test',
      label: 'baseline',
      metrics: { return_pct: 4.2, excess_return_pct: 1.1, max_drawdown_pct: -6.5,
        sharpe: 0.9, fees_usdt: 12.5 },
    },
    {
      sleeve: 'b',
      run_id: 'test-b-20260901-01',
      mode: 'test',
      label: 'claude',
      metrics: { return_pct: 7.8, excess_return_pct: 4.7, max_drawdown_pct: -4.1,
        sharpe: 1.4, fees_usdt: 18.0 },
    },
  ],
  caveats: {
    b: { simulated: true, text: 'TEST run: these fills never paid real slippage.' },
  },
};

const ATTRIBUTION = [
  { tag: 'trend', trades: 4, pnl_usdt: 120.5, fees_usdt: 3.2 },
  { tag: 'stop', trades: 2, pnl_usdt: -55.25, fees_usdt: 1.1 },
];

const WHATIF = [
  { date_utc: '2026-09-01', nav_usdt: 10000, turnover: 0.1, cost_usdt: 1.2,
    last_proposal_run_id: 'r1' },
  { date_utc: '2026-09-02', nav_usdt: 10250, turnover: 0.2, cost_usdt: 2.3,
    last_proposal_run_id: 'r2' },
];

type Route = { status?: number; body?: unknown };

function routes(overrides: Record<string, Route> = {}): Record<string, Route> {
  return {
    '/api/perf/summary': { body: SUMMARY },
    '/api/perf/nav': {
      body: {
        sleeve: 'b',
        run_id: 'test-b-20260901-01',
        resolution: 'hour',
        points: navPoints(9),
        benchmark: navPoints(9).map((p) => ({ ...p, index: 100 })),
      },
    },
    '/api/perf/attribution': { body: ATTRIBUTION },
    '/api/perf/whatif': { body: WHATIF },
    ...overrides,
  };
}

function renderPage(
  overrides: Record<string, Route> = {},
  colorScheme: 'light' | 'dark' = 'dark',
): RenderResult {
  const client = new ApiClient({ fetchImpl: fakeFetch(routes(overrides)) });
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  const ui: ReactElement = (
    <ApiProvider client={client}>
      <PerformancePage />
    </ApiProvider>
  );
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <MantineProvider theme={theme} forceColorScheme={colorScheme} env="test">
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>
  );
  return render(ui, { wrapper: Wrapper });
}

/** The headline stat cards, scoped so the metrics table's numbers cannot answer for them. */
async function statCards(): Promise<HTMLElement> {
  const stats = screen.getByTestId('perf-stats');
  await waitFor(() => expect(within(stats).queryByTestId('stat-skeleton')).toBeNull());
  return stats;
}

describe('PerformancePage', () => {
  it('shows skeletons and a spinner before the first read lands', () => {
    renderPage();
    expect(screen.getAllByTestId('stat-skeleton')).toHaveLength(4);
    expect(screen.getByTestId('nav-loading')).toBeInTheDocument();
  });

  it('leads with the excess return, because the benchmark is the point', async () => {
    renderPage();
    const stats = await statCards();
    expect(within(stats).getByText('7.80%')).toBeInTheDocument();
    expect(within(stats).getByText('4.70% vs BTC')).toBeInTheDocument();
    expect(await screen.findByText('test-b-20260901-01')).toBeInTheDocument();
  });

  it('keeps the TCA caveat visible next to the P&L', async () => {
    renderPage();
    expect(
      await screen.findByText(/these fills never paid real slippage/),
    ).toBeInTheDocument();
  });

  it('says so when the journal cannot be read, rather than drawing a flat line', async () => {
    renderPage({
      '/api/perf/nav': {
        status: 503,
        body: { error: { code: 'unavailable', message: 'journal.db missing' } },
      },
      '/api/perf/summary': {
        status: 503,
        body: { error: { code: 'unavailable', message: 'journal.db missing' } },
      },
    });
    const alerts = await screen.findAllByTestId('error-alert');
    expect(alerts.length).toBeGreaterThanOrEqual(2);
    expect(alerts[0]).toHaveTextContent('journal.db missing');
    expect(await screen.findByText('The NAV series did not load')).toBeInTheDocument();
  });

  it('shows the empty state when a run exists but has no NAV points yet', async () => {
    renderPage({
      '/api/perf/nav': {
        body: { sleeve: 'b', run_id: null, resolution: 'hour', points: [], benchmark: [] },
      },
      '/api/perf/summary': { body: { sleeves: [], caveats: {} } },
    });
    expect(await screen.findByText('No NAV points yet')).toBeInTheDocument();
    expect(screen.queryByTestId('error-alert')).not.toBeInTheDocument();
  });

  it('renders the metrics table for every sleeve with an open run', async () => {
    renderPage();
    await userEvent.click(await screen.findByRole('tab', { name: 'Metrics' }));
    expect(await screen.findByText(/Sleeve A · baseline/)).toBeInTheDocument();
    expect(screen.getByText(/Sleeve B · claude/)).toBeInTheDocument();
  });

  it('shows attribution rows and surfaces an attribution failure on its own', async () => {
    renderPage();
    await userEvent.click(await screen.findByRole('tab', { name: 'Attribution' }));
    expect(await screen.findByText('trend')).toBeInTheDocument();
    expect(screen.getByText('120.50')).toBeInTheDocument();
    expect(screen.getByText('-55.25')).toBeInTheDocument();
  });

  it('keeps an attribution failure inside its own panel', async () => {
    renderPage({
      '/api/perf/attribution': {
        status: 500,
        body: { error: { code: 'server_error', message: 'bad tag join' } },
      },
    });
    // The headline numbers still loaded, so only the panel complains.
    const stats = await statCards();
    expect(within(stats).getByText('7.80%')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('tab', { name: 'Attribution' }));
    await waitFor(() =>
      expect(screen.getByTestId('error-alert')).toHaveTextContent('bad tag join'),
    );
  });

  it('draws the proposals-only curve with its modelled costs', async () => {
    renderPage();
    await userEvent.click(await screen.findByRole('tab', { name: 'Proposals alone' }));
    expect(await screen.findByText('3.50 USDT of modelled costs')).toBeInTheDocument();
  });

  it('explains an empty what-if track instead of drawing nothing', async () => {
    renderPage({ '/api/perf/whatif': { body: [] } });
    await userEvent.click(await screen.findByRole('tab', { name: 'Proposals alone' }));
    expect(await screen.findByText('No what-if track yet')).toBeInTheDocument();
  });

  it('renders in the light theme', async () => {
    renderPage({}, 'light');
    expect(within(await statCards()).getByText('7.80%')).toBeInTheDocument();
    expect(screen.getByText('Performance')).toBeInTheDocument();
  });

  it('renders in the dark theme', async () => {
    renderPage({}, 'dark');
    expect(within(await statCards()).getByText('7.80%')).toBeInTheDocument();
    expect(screen.getByText('Performance')).toBeInTheDocument();
  });

  it('switches sleeve without losing the page', async () => {
    renderPage();
    const stats = await statCards();
    expect(within(stats).getByText('7.80%')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('radio', { name: 'Sleeve A' }));
    await waitFor(() => expect(within(stats).getByText('4.20%')).toBeInTheDocument());
  });
});
