import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { ApiClient } from '@/api';
import { ApiProvider } from '@/app/ApiContext';
import { FakeEventSource, fakeFetch, renderWithProviders } from '@/test/utils';

import SignalsPage from './SignalsPage';
import { FunnelBar } from './components/FunnelBar';
import type { FunnelCounts, SignalRow } from './api';

const SIGNAL: SignalRow = {
  signal_id: 'sig-20261027T0405Z-btc-breakout',
  ts_utc: '2026-10-27T04:05:00Z',
  scan_id: 'scan-1',
  source: 'detector',
  detector: 'breakout',
  pair: 'BTC/USDT',
  direction: 'up',
  detector_score: 0.8,
  screen_score: 0.9,
  strength: 0.84,
  fast_path: false,
  status: 'screened',
  status_reason: null,
  screen_provider: 'claude',
  screen_model: 'claude-haiku',
  screen_rationale: 'close above the 20d high',
  run_id: null,
  proposal_run_id: null,
  updated_utc: '2026-10-27T04:06:00Z',
};

const COUNTS: FunnelCounts = {
  detected: 10,
  screened: 4,
  validated: 2,
  valid: 1,
  planned: 1,
  acted: 1,
  screened_out: 6,
  expired: 0,
  errors: 0,
};

// The page subscribes to the `signal` and `validation` SSE topics; jsdom has no
// EventSource, so the shell's scriptable fake stands in for it.
beforeAll(() => {
  Object.defineProperty(globalThis, 'EventSource', {
    writable: true,
    value: FakeEventSource,
  });
});

afterEach(() => FakeEventSource.reset());

function client(routes: Parameters<typeof fakeFetch>[0]): ApiClient {
  return new ApiClient({ fetchImpl: fakeFetch(routes) });
}

function renderPage(extra: Parameters<typeof fakeFetch>[0] = {}) {
  const api = client({
    '/api/signals': { body: { signals: [SIGNAL] } },
    '/api/signals/funnel': {
      body: {
        window_hours: 24,
        counts: COUNTS,
        by_detector: [
          { group: 'breakout', n: 4, hits: 3, hit_rate: 0.75, avg_ret: 2.1, avg_confidence: 0.8 },
        ],
        by_model: [],
        screen: { by_model: [], screen_unavailable: 2 },
      },
    },
    ...extra,
  });
  return renderWithProviders(
    <ApiProvider client={api}>
      <SignalsPage />
    </ApiProvider>,
  );
}

describe('FunnelBar', () => {
  it('shows every stage with its conversion from the one above', () => {
    renderWithProviders(<FunnelBar counts={COUNTS} />);
    const funnel = screen.getByTestId('signal-funnel');
    expect(within(funnel).getByText('Detected')).toBeInTheDocument();
    expect(within(funnel).getByText('Acted')).toBeInTheDocument();
    // 4 screened out of 10 detected
    expect(within(funnel).getByText('→ 40%')).toBeInTheDocument();
    expect(within(funnel).getByText('screened out 6')).toBeInTheDocument();
  });

  it('renders nothing rather than guessing when the counts are missing', () => {
    const { container } = renderWithProviders(<FunnelBar />);
    expect(container.querySelector('[data-testid="signal-funnel"]')).toBeNull();
  });
});

describe('SignalsPage', () => {
  it('lists signals with their detector, score and status', async () => {
    renderPage();
    // 'breakout' appears in the list AND in the per-detector hit-rate table
    expect((await screen.findAllByText('breakout')).length).toBeGreaterThan(0);
    expect(screen.getByText('BTC/USDT')).toBeInTheDocument();
    expect(screen.getAllByText('screened').length).toBeGreaterThan(0);
  });

  it('warns when signals were scored without the screener', async () => {
    renderPage();
    expect(
      await screen.findByText('2 scored on the detector alone'),
    ).toBeInTheDocument();
  });

  it('shows the resolved hit rate per detector', async () => {
    renderPage();
    expect(await screen.findByText('Hit rate by detector')).toBeInTheDocument();
    // the table fills in after the funnel query resolves, so wait for the cell
    expect(await screen.findByText('75%')).toBeInTheDocument();
  });

  it('starts a scan through POST /api/signals/scan-now', async () => {
    const fetchImpl = vi.fn(
      fakeFetch({
        '/api/signals': { body: { signals: [SIGNAL] } },
        '/api/signals/funnel': {
          body: {
            window_hours: 24,
            counts: COUNTS,
            by_detector: [],
            by_model: [],
            screen: { by_model: [], screen_unavailable: 0 },
          },
        },
        '/api/signals/scan-now': { body: { spawned: true, pid: 4242 } },
      }),
    );
    renderWithProviders(
      <ApiProvider client={new ApiClient({ fetchImpl: fetchImpl as unknown as typeof fetch })}>
        <SignalsPage />
      </ApiProvider>,
    );
    await screen.findAllByText('breakout');
    await userEvent.click(screen.getByRole('button', { name: /scan now/i }));
    await waitFor(() => {
      const calls = fetchImpl.mock.calls.map((c) => String(c[0]));
      expect(calls).toContain('/api/signals/scan-now');
    });
  });

  it('says so when the scan could not be spawned', async () => {
    renderPage({ '/api/signals/scan-now': { body: { spawned: false, pid: null } } });
    await screen.findAllByText('breakout');
    await userEvent.click(screen.getByRole('button', { name: /scan now/i }));
    expect(await screen.findByText('Scan could not be spawned')).toBeInTheDocument();
  });

  it('explains that a manual signal does not skip the pipeline', async () => {
    renderPage();
    await screen.findAllByText('breakout');
    await userEvent.click(screen.getByRole('button', { name: /inject signal/i }));
    expect(await screen.findByText(/It is a request to look, not an instruction to act/i))
      .toBeInTheDocument();
  });

  it('opens the detail drawer with the validator counter-evidence', async () => {
    renderPage({
      [`/api/signals/${SIGNAL.signal_id}`]: {
        body: {
          ...SIGNAL,
          features: { 'BTC/USDT.close': 105 },
          detector_detail: {},
          news_refs: [],
          blocked: [],
          run: null,
          proposal: null,
          validations: [
            {
              id: 1,
              signal_id: SIGNAL.signal_id,
              ts_utc: '2026-10-27T04:10:00Z',
              provider: 'claude',
              model: 'claude-sonnet-5',
              verdict: 'valid',
              confidence: 0.82,
              horizon_hours: 24,
              thesis: 'The breakout is confirmed by volume.',
              invalidation: 'a daily close back inside the range',
              escalated: false,
              cost_usd: 0.4,
              latency_ms: 900,
              pack_path: 'journal/snapshots/signals/x/pack.json',
              error: null,
              outcome_ret: null,
              outcome_hit: null,
              outcome_resolved_at: null,
              reasons: ['close above the 20d high'],
              counter_evidence: [],
              suggested: { direction: 'up' },
            },
          ],
        },
      },
    });
    // click the LIST row (the pair only appears there, not in the hit-rate table)
    await userEvent.click(await screen.findByText('BTC/USDT'));
    expect(await screen.findByText('Counter-evidence')).toBeInTheDocument();
    // an empty counter-evidence list is called out, not silently rendered as blank
    expect(
      screen.getByText(/None recorded — treat this verdict with extra suspicion/i),
    ).toBeInTheDocument();
    expect(screen.getByText('BTC/USDT.close')).toBeInTheDocument();
  });
});
