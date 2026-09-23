import { MantineProvider } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, type RenderResult } from '@testing-library/react';
import type { ReactElement, ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiClient } from '@/api';
import { ApiProvider } from '@/app/ApiContext';
import { fakeFetch } from '@/test/utils';
import { theme } from '@/theme';

/**
 * lightweight-charts wants a real 2D canvas context, which jsdom does not have. Rather
 * than assert on pixels, the library is replaced by a faithful stand-in that records what
 * the page asked for — which series, on which pane, with which data and markers. That is
 * the contract this page has with the renderer, and it is what can actually break.
 */
const lw = vi.hoisted(() => {
  interface SeriesRecord {
    type: string;
    options: Record<string, unknown>;
    paneIndex: number;
    data: unknown[];
    markers: unknown[];
    priceLines: unknown[];
    /** Which `createChart` call this series belongs to. */
    build: number;
  }
  const state = {
    charts: 0,
    removed: 0,
    fitted: 0,
    failNext: false,
    options: [] as Record<string, unknown>[],
    series: [] as SeriesRecord[],
    priceScales: [] as Array<{ id: string; options: unknown }>,
    paneHeights: [] as Array<{ pane: number; height: number }>,
    reset() {
      state.charts = 0;
      state.removed = 0;
      state.fitted = 0;
      state.failNext = false;
      state.options = [];
      state.series = [];
      state.priceScales = [];
      state.paneHeights = [];
    },
    of(type: string) {
      return state.series.filter((s) => s.type === type);
    },
    /**
     * The series of a given type from the *most recent* chart. A page that loads its
     * candles and then its markers legitimately rebuilds the chart, so counting across
     * every build would only be measuring how many round trips the fixtures took.
     */
    latest(type: string) {
      return state.series.filter((s) => s.build === state.charts && s.type === type);
    },
  };
  return state;
});

vi.mock('lightweight-charts', () => {
  const definition = (type: string) => ({ type, isBuiltIn: true, defaultOptions: {} });
  return {
    ColorType: { Solid: 'solid', VerticalGradient: 'gradient' },
    CrosshairMode: { Normal: 0, Magnet: 1, Hidden: 2 },
    LineStyle: { Solid: 0, Dotted: 1, Dashed: 2 },
    LineType: { Simple: 0, WithSteps: 1, Curved: 2 },
    CandlestickSeries: definition('Candlestick'),
    LineSeries: definition('Line'),
    HistogramSeries: definition('Histogram'),
    createChart: (_el: unknown, options: Record<string, unknown>) => {
      if (lw.failNext) throw new Error('no 2d context');
      lw.charts += 1;
      lw.options.push(options);
      const build = lw.charts;
      return {
        addSeries: (
          def: { type: string },
          seriesOptions: Record<string, unknown> = {},
          paneIndex = 0,
        ) => {
          const record = {
            type: def.type,
            options: seriesOptions,
            paneIndex,
            data: [] as unknown[],
            markers: [] as unknown[],
            priceLines: [] as unknown[],
            build,
          };
          lw.series.push(record);
          return {
            __record: record,
            setData: (data: unknown[]) => {
              record.data = data;
            },
            createPriceLine: (line: unknown) => {
              record.priceLines.push(line);
              return { remove: () => {}, applyOptions: () => {} };
            },
            applyOptions: () => {},
          };
        },
        priceScale: (id: string) => ({
          applyOptions: (options2: unknown) => lw.priceScales.push({ id, options: options2 }),
        }),
        panes: () =>
          [0, 1, 2, 3].map((pane) => ({
            setHeight: (height: number) => lw.paneHeights.push({ pane, height }),
            getHeight: () => 0,
          })),
        timeScale: () => ({ fitContent: () => { lw.fitted += 1; } }),
        remove: () => { lw.removed += 1; },
      };
    },
    createSeriesMarkers: (series: { __record?: { markers: unknown[] } }, markers: unknown[]) => {
      if (series.__record) series.__record.markers = markers;
      return { setMarkers: () => {}, detach: () => {} };
    },
  };
});

// Imported after the mock is declared; vitest hoists `vi.mock` above every import anyway.
import type { Marker, ProposalMarker, SignalRow } from './api';
import { PriceChart } from './components/PriceChart';
import ChartsPage from './index';
import { DEFAULT_OVERLAYS, buildChartModel, emptyModel } from './model';

// --------------------------------------------------------------------------- fixtures

const HOUR = 3600;
const T0 = Date.UTC(2026, 8, 1, 0, 0, 0) / 1000;

function iso(seconds: number): string {
  return new Date(seconds * 1000).toISOString().replace('.000Z', 'Z');
}

function candles(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    date: iso(T0 + i * HOUR),
    open: 100 + i * 0.1,
    high: 103 + i * 0.1,
    low: 97 + i * 0.1,
    close: 101 + i * 0.1,
    volume: 5 + i,
  }));
}

const MARKERS: Marker[] = [
  { kind: 'fill', ts: iso(T0 + 2 * HOUR), label: 'buy 0.01', side: 'buy', price: 101,
    mode: 'live', sim: false },
  { kind: 'fill', ts: iso(T0 + 4 * HOUR), label: 'sell 0.01', side: 'sell', price: 104,
    mode: 'test', sim: true },
  { kind: 'stop', ts: iso(T0 + 6 * HOUR), label: '-10%' },
  { kind: 'gate_reject', ts: iso(T0 + 8 * HOUR), label: 'blackout:macro_cpi',
    severity: 'breach' },
];

const PROPOSALS: ProposalMarker[] = [
  { kind: 'proposal', ts: iso(T0 + 5 * HOUR), run_id: '2026-09-01T08:30+04:00',
    module: 'trend', targets: { BTC: 0.45, ETH: 0.25, USDT: 0.3 }, abstain: false },
];

const SIGNALS: SignalRow[] = [
  { signal_id: 's1', ts_utc: iso(T0 + 3 * HOUR), detector: 'breakout', pair: 'BTC/USDT',
    direction: 'up', status: 'valid' },
];

const FLAGS = {
  ok: true,
  flags: {
    macro_cpi: { active: true, severity: 'block_entries', set_at: iso(T0 + 7 * HOUR),
      expires_at: iso(T0 + 9 * HOUR) },
  },
};

const STATE = { latest: {}, snapshots: [{ id: 1, ts_utc: iso(T0), regime: 'up' }] };

type Route = { status?: number; body?: unknown };

function routes(overrides: Record<string, Route> = {}, bars = 24): Record<string, Route> {
  return {
    '/api/market/pairs': {
      body: { pairs: ['BTC/USDT', 'ETH/USDT'], quote: 'USDT', timeframes: ['1h', '4h', '1d'] },
    },
    '/api/market/candles': { body: { pair: 'BTC/USDT', timeframe: '4h', candles: candles(bars) } },
    '/api/market/markers': {
      body: { pair: 'BTC/USDT', markers: MARKERS, proposals: PROPOSALS,
        kinds: ['fill', 'gate_reject'] },
    },
    '/api/signals': { body: { signals: SIGNALS } },
    '/api/risk/flags': { body: FLAGS },
    '/api/knowledge/state': { body: STATE },
    ...overrides,
  };
}

function renderThemed(
  ui: ReactElement,
  colorScheme: 'light' | 'dark' = 'dark',
): RenderResult {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <MantineProvider theme={theme} forceColorScheme={colorScheme} env="test">
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>{children}</MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>
  );
  return render(ui, { wrapper: Wrapper });
}

function renderPage(
  overrides: Record<string, Route> = {},
  { scheme = 'dark', bars = 24 }: { scheme?: 'light' | 'dark'; bars?: number } = {},
): RenderResult {
  const client = new ApiClient({ fetchImpl: fakeFetch(routes(overrides, bars)) });
  return renderThemed(
    <ApiProvider client={client}>
      <ChartsPage />
    </ApiProvider>,
    scheme,
  );
}

beforeEach(() => {
  lw.reset();
});

// --------------------------------------------------------------------------- the page

describe('ChartsPage', () => {
  it('shows a skeleton before the universe and the candles arrive', () => {
    renderPage();
    expect(screen.getByTestId('chart-skeleton')).toBeInTheDocument();
    expect(screen.queryByTestId('price-chart')).not.toBeInTheDocument();
  });

  it('draws the chart, the legend and the proposals for the first pair', async () => {
    renderPage();
    expect(await screen.findByTestId('price-chart')).toBeInTheDocument();
    expect(screen.getByText('Charts')).toBeInTheDocument();
    expect(screen.getByTestId('chart-legend')).toBeInTheDocument();
    expect(await screen.findByText('2026-09-01T08:30+04:00')).toBeInTheDocument();
    expect(screen.getByText('BTC 45% · ETH 25% · USDT 30%')).toBeInTheDocument();
    expect(screen.queryByTestId('context-warning')).not.toBeInTheDocument();
    await waitFor(() => expect(lw.latest('Candlestick')).toHaveLength(1));
    expect(lw.latest('Candlestick')[0].data).toHaveLength(24);
  });

  it('tells the operator when it aggregated the bars to stay fast', async () => {
    renderPage({}, { bars: 2400 });
    const badge = await screen.findByTestId('downsample-badge');
    expect(badge).toHaveTextContent('2400 bars → 1200 (2× buckets)');
    await waitFor(() => expect(lw.latest('Candlestick')).toHaveLength(1));
    expect(lw.latest('Candlestick')[0].data).toHaveLength(1200);
  });

  it('explains an empty feather store instead of drawing an empty chart', async () => {
    renderPage({
      '/api/market/candles': { body: { pair: 'BTC/USDT', timeframe: '4h', candles: [] } },
    });
    expect(
      await screen.findByText(/No candles for this pair and timeframe/),
    ).toBeInTheDocument();
    expect(screen.queryByTestId('price-chart')).not.toBeInTheDocument();
    expect(lw.charts).toBe(0);
  });

  it('shows the candle failure rather than an empty-looking chart', async () => {
    renderPage({
      '/api/market/candles': {
        status: 503,
        body: { error: { code: 'unavailable', message: 'feather store missing' } },
      },
    });
    const alert = await screen.findByTestId('error-alert');
    expect(alert).toHaveTextContent('feather store missing');
    expect(screen.queryByTestId('price-chart')).not.toBeInTheDocument();
  });

  it('still draws the candles when only the context reads fail, and says what is missing',
    async () => {
      renderPage({
        '/api/signals': { status: 503, body: { error: { code: 'unavailable', message: 'no journal' } } },
        '/api/risk/flags': { status: 500, body: { error: { code: 'boom', message: 'bad flags' } } },
        '/api/knowledge/state': { status: 503, body: { error: { code: 'unavailable', message: 'no db' } } },
      });
      expect(await screen.findByTestId('price-chart')).toBeInTheDocument();
      const warning = await screen.findByTestId('context-warning');
      expect(warning).toHaveTextContent('signals');
      expect(warning).toHaveTextContent('blackout flags');
      expect(warning).toHaveTextContent('regime history');
      expect(screen.queryByTestId('error-alert')).not.toBeInTheDocument();
    });

  it('renders in the light theme and hands the renderer the light palette', async () => {
    renderPage({}, { scheme: 'light' });
    expect(await screen.findByTestId('price-chart')).toBeInTheDocument();
    await waitFor(() => expect(lw.charts).toBeGreaterThan(0));
    const layout = lw.options[lw.options.length - 1].layout as { textColor: string };
    expect(layout.textColor).toBe('#495057');
  });

  it('renders in the dark theme with the dark palette', async () => {
    renderPage({}, { scheme: 'dark' });
    expect(await screen.findByTestId('price-chart')).toBeInTheDocument();
    await waitFor(() => expect(lw.charts).toBeGreaterThan(0));
    const layout = lw.options[lw.options.length - 1].layout as { textColor: string };
    expect(layout.textColor).toBe('#c1c2c5');
  });
});

// --------------------------------------------------------------------------- renderer

describe('PriceChart', () => {
  const model = buildChartModel({
    candles: candles(40),
    markers: MARKERS,
    proposals: PROPOSALS,
    signals: SIGNALS,
    flags: FLAGS.flags,
    regimes: STATE.snapshots,
    maPeriod: 10,
    atrPeriod: 5,
    atrMult: 2,
    rsiPeriod: 5,
  });

  const props = {
    model,
    overlays: DEFAULT_OVERLAYS,
    maPeriod: 10,
    atrPeriod: 5,
    atrMult: 2,
    rsiPeriod: 5,
  };

  it('puts the candles, the overlays and the shading on the price pane', () => {
    renderThemed(<PriceChart {...props} />);
    const candlestick = lw.of('Candlestick');
    expect(candlestick).toHaveLength(1);
    expect(candlestick[0].paneIndex).toBe(0);
    expect(candlestick[0].data).toHaveLength(model.bars.length);

    // regime + blackout shading, on their own hidden scales, added before the candles so
    // they render behind them.
    const histograms = lw.of('Histogram');
    expect(histograms.map((h) => h.options.priceScaleId)).toEqual([
      'earn-regime',
      'earn-blackout',
    ]);
    expect(lw.series.indexOf(histograms[0])).toBeLessThan(lw.series.indexOf(candlestick[0]));
    expect(lw.priceScales.map((p) => p.id)).toEqual(['earn-regime', 'earn-blackout']);

    const priceLines = lw.of('Line').filter((s) => s.paneIndex === 0);
    expect(priceLines.map((s) => s.options.title)).toEqual([
      'MA10',
      '+2×ATR5',
      '-2×ATR5',
    ]);
  });

  it('attaches every event marker to the candle series', () => {
    renderThemed(<PriceChart {...props} />);
    const markers = lw.of('Candlestick')[0].markers as Array<{ text: string; size: number }>;
    expect(markers.map((m) => m.text)).toEqual([
      'LIVE buy 0.01',
      'breakout · valid',
      'SIM sell 0.01',
      'STOP -10%',
      'gate blackout:macro_cpi',
    ]);
    // Real money is drawn larger than simulated money.
    expect(markers[0].size).toBe(1.4);
    expect(markers[2].size).toBe(1);
  });

  it('gives proposals a sub-pane of step lines and RSI a pane of its own', () => {
    renderThemed(<PriceChart {...props} />);
    const proposals = lw.of('Line').filter((s) => s.paneIndex === 1);
    expect(proposals.map((s) => s.options.title)).toEqual(['BTC', 'ETH', 'USDT']);
    expect(proposals.every((s) => s.options.lineType === 1)).toBe(true);

    const rsi = lw.of('Line').filter((s) => s.paneIndex === 2);
    expect(rsi).toHaveLength(1);
    expect(rsi[0].options.title).toBe('RSI5');
    expect(rsi[0].priceLines.map((l) => (l as { price: number }).price)).toEqual([70, 30]);
    expect(lw.paneHeights).toEqual([
      { pane: 1, height: 90 },
      { pane: 2, height: 100 },
    ]);
    expect(lw.fitted).toBe(1);
  });

  it('draws only the candles when every overlay is switched off', () => {
    const bare = buildChartModel({
      candles: candles(40),
      overlays: { ...DEFAULT_OVERLAYS, ma: false, atr: false, rsi: false, regime: false,
        blackout: false, proposals: false },
    });
    renderThemed(
      <PriceChart
        {...props}
        model={bare}
        overlays={{ ...DEFAULT_OVERLAYS, ma: false, atr: false, rsi: false, regime: false,
          blackout: false, proposals: false }}
      />,
    );
    expect(lw.of('Candlestick')).toHaveLength(1);
    expect(lw.of('Line')).toHaveLength(0);
    expect(lw.of('Histogram')).toHaveLength(0);
  });

  it('degrades to a visible note when the canvas cannot start', async () => {
    lw.failNext = true;
    renderThemed(<PriceChart {...props} />);
    expect(await screen.findByText(/The chart canvas could not start/)).toBeInTheDocument();
    expect(screen.getByTestId('price-chart')).toBeInTheDocument();
  });

  it('builds nothing at all for an empty model', () => {
    renderThemed(<PriceChart {...props} model={emptyModel()} />);
    expect(lw.charts).toBe(0);
  });

  it('tears the chart down when it unmounts', () => {
    const view = renderThemed(<PriceChart {...props} />);
    expect(lw.charts).toBe(1);
    view.unmount();
    expect(lw.removed).toBe(1);
  });
});
