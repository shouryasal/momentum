import { describe, expect, it } from 'vitest';

import type { Candle, Marker, ProposalMarker, RiskFlag, SignalRow } from './api';
import {
  BLACKOUT_SHADE,
  DEFAULT_OVERLAYS,
  FILL_COLOR,
  GATE_REJECT_COLOR,
  MAX_MARKERS,
  REGIME_SHADE,
  VERDICT_COLOR,
  aggregate,
  barIndexAt,
  blackoutWindows,
  bucketEnds,
  buildChartModel,
  downsampleFactor,
  medianSpacing,
  mergeWindows,
  parseUtcSeconds,
  proposalLines,
  regimeBands,
  regimeBucket,
  sma,
  toBars,
  wilderAtr,
  wilderRsi,
} from './model';

const HOUR = 3600;
const T0 = Date.UTC(2026, 8, 1, 0, 0, 0) / 1000; // 2026-09-01T00:00:00Z

function iso(seconds: number): string {
  return new Date(seconds * 1000).toISOString().replace('.000Z', 'Z');
}

/** `n` deterministic 1h candles walking up and down so highs and lows differ per bar. */
function candleSeries(n: number, step = HOUR): Candle[] {
  return Array.from({ length: n }, (_, i) => {
    const base = 100 + Math.sin(i / 3) * 10 + i * 0.05;
    return {
      date: iso(T0 + i * step),
      open: base,
      high: base + 2 + (i % 5),
      low: base - 2 - (i % 3),
      close: base + (i % 2 === 0 ? 1 : -1),
      volume: 10 + i,
    };
  });
}

describe('parseUtcSeconds', () => {
  it('reads every timestamp shape the API actually produces', () => {
    expect(parseUtcSeconds('2026-09-01T00:00:00Z')).toBe(T0);
    // pandas stringifies a tz-aware Timestamp with a space and an explicit offset
    expect(parseUtcSeconds('2026-09-01 00:00:00+00:00')).toBe(T0);
    // ...and a naive one with no zone at all, which the repo treats as UTC
    expect(parseUtcSeconds('2026-09-01 00:00:00')).toBe(T0);
    expect(parseUtcSeconds('2026-09-01')).toBe(T0);
    expect(parseUtcSeconds('2026-09-01T04:00:00+04:00')).toBe(T0);
  });

  it('accepts epoch seconds and milliseconds, and rejects everything else', () => {
    expect(parseUtcSeconds(T0)).toBe(T0);
    expect(parseUtcSeconds(T0 * 1000)).toBe(T0);
    expect(parseUtcSeconds('not a date')).toBeNull();
    expect(parseUtcSeconds('')).toBeNull();
    expect(parseUtcSeconds(null)).toBeNull();
    expect(parseUtcSeconds(undefined)).toBeNull();
    expect(parseUtcSeconds(Number.NaN)).toBeNull();
  });
});

describe('toBars', () => {
  it('sorts by time, keeps the last row for a repeated timestamp, drops unusable rows', () => {
    const bars = toBars([
      { date: iso(T0 + HOUR), open: 2, high: 3, low: 1, close: 2, volume: 1 },
      { date: iso(T0), open: 1, high: 2, low: 0, close: 1, volume: 1 },
      { date: iso(T0), open: 9, high: 9, low: 9, close: 9, volume: 5 },
      { date: 'junk', open: 1, high: 1, low: 1, close: 1, volume: 1 },
      { date: iso(T0 + 2 * HOUR), open: Number.NaN, high: 1, low: 1, close: 1, volume: 1 },
    ] as Candle[]);
    expect(bars.map((b) => b.time)).toEqual([T0, T0 + HOUR]);
    expect(bars[0].close).toBe(9);
  });

  it('is empty for empty, null and undefined input', () => {
    expect(toBars([])).toEqual([]);
    expect(toBars(null)).toEqual([]);
    expect(toBars(undefined)).toEqual([]);
  });
});

describe('downsampling', () => {
  it('only kicks in above the budget', () => {
    expect(downsampleFactor(400, 1200)).toBe(1);
    expect(downsampleFactor(1200, 1200)).toBe(1);
    expect(downsampleFactor(5000, 1200)).toBe(5);
    expect(downsampleFactor(5000, 0)).toBe(1);
  });

  it('aggregates OHLC honestly rather than dropping bars', () => {
    const bars = toBars(candleSeries(12));
    const buckets = aggregate(bars, 4);
    expect(buckets).toHaveLength(3);
    for (let b = 0; b < 3; b += 1) {
      const slice = bars.slice(b * 4, b * 4 + 4);
      expect(buckets[b].time).toBe(slice[0].time);
      expect(buckets[b].open).toBe(slice[0].open);
      expect(buckets[b].close).toBe(slice[3].close);
      expect(buckets[b].high).toBe(Math.max(...slice.map((x) => x.high)));
      expect(buckets[b].low).toBe(Math.min(...slice.map((x) => x.low)));
      expect(buckets[b].volume).toBeCloseTo(slice.reduce((s, x) => s + x.volume, 0), 9);
    }
  });

  it('keeps a short trailing bucket instead of discarding it', () => {
    const bars = toBars(candleSeries(10));
    expect(aggregate(bars, 4)).toHaveLength(3);
    expect(bucketEnds(10, 4)).toEqual([3, 7, 9]);
    expect(aggregate(bars, 1)).toBe(bars);
  });
});

describe('indicators', () => {
  it('sma is null until the window is full, then the mean of the window', () => {
    expect(sma([1, 2, 3, 4], 3)).toEqual([null, null, 2, 3]);
  });

  it("wilder's rsi saturates on a monotone series and sits at 50 on a flat one", () => {
    const rising = Array.from({ length: 30 }, (_, i) => 100 + i);
    const rsi = wilderRsi(rising, 14);
    expect(rsi.slice(0, 14).every((v) => v === null)).toBe(true);
    expect(rsi[29]).toBe(100);

    const flat = new Array(30).fill(100);
    expect(wilderRsi(flat, 14)[29]).toBe(50);
    // Too short to seed: every value stays null rather than being invented.
    expect(wilderRsi([1, 2, 3], 14).every((v) => v === null)).toBe(true);
  });

  it("wilder's atr is the smoothed true range and is null until seeded", () => {
    const bars = toBars(
      Array.from({ length: 5 }, (_, i) => ({
        date: iso(T0 + i * HOUR),
        open: 100,
        high: 110,
        low: 100,
        close: 105,
        volume: 1,
      })),
    );
    const atr = wilderAtr(bars, 3);
    expect(atr[0]).toBeNull();
    expect(atr[1]).toBeNull();
    // Bar 0 has no predecessor, so its TR is high-low = 10; later bars see 110-105 = 10 too.
    expect(atr[2]).toBeCloseTo(10, 9);
    expect(atr[4]).toBeCloseTo(10, 9);
  });
});

describe('barIndexAt', () => {
  const times = [T0, T0 + HOUR, T0 + 2 * HOUR];

  it('snaps a timestamp back to the bar that covers it', () => {
    expect(barIndexAt(times, T0, HOUR)).toBe(0);
    expect(barIndexAt(times, T0 + HOUR + 60, HOUR)).toBe(1);
    expect(barIndexAt(times, T0 + 2 * HOUR, HOUR)).toBe(2);
  });

  it('refuses to place anything outside the loaded window', () => {
    expect(barIndexAt(times, T0 - 1, HOUR)).toBeNull();
    expect(barIndexAt(times, T0 + 3 * HOUR + 1, HOUR)).toBeNull();
    expect(barIndexAt([], T0, HOUR)).toBeNull();
  });

  it('medianSpacing is the typical gap, and zero for a single bar', () => {
    expect(medianSpacing(times)).toBe(HOUR);
    expect(medianSpacing([T0])).toBe(0);
  });
});

describe('blackout windows', () => {
  const flags: Record<string, RiskFlag> = {
    macro_fomc: {
      active: true,
      severity: 'block_entries',
      set_at: iso(T0),
      expires_at: iso(T0 + 4 * HOUR),
    },
    // reconcile_mismatch has its own gate check; it is not a blackout.
    reconcile_mismatch: { active: true, severity: 'block_entries', set_at: iso(T0) },
    chatter: { active: true, severity: 'info', set_at: iso(T0) },
    cleared: { active: false, severity: 'block_entries', set_at: iso(T0) },
  };

  it('takes only the block_entries flags the gate actually blackouts on', () => {
    const windows = blackoutWindows(flags);
    expect(windows).toHaveLength(1);
    expect(windows[0]).toMatchObject({ from: T0, to: T0 + 4 * HOUR, label: 'macro_fomc' });
  });

  it('runs an unexpired flag to the end of time rather than guessing a close', () => {
    const open = blackoutWindows({ halt: { active: true, severity: 'block_entries', set_at: iso(T0) } });
    expect(open[0].to).toBe(Number.POSITIVE_INFINITY);
  });

  it('recovers historical windows from the gate rejects that prove them', () => {
    const rejects: Marker[] = [
      { kind: 'gate_reject', ts: iso(T0 + 20 * HOUR), label: 'blackout:macro_cpi' },
      { kind: 'gate_reject', ts: iso(T0 + 21 * HOUR), label: 'blackout:macro_cpi' },
      { kind: 'gate_reject', ts: iso(T0 + 40 * HOUR), label: 'usdt_floor' },
    ];
    const windows = blackoutWindows({}, rejects, HOUR);
    expect(windows).toHaveLength(1);
    expect(windows[0].from).toBe(T0 + 20 * HOUR);
    expect(windows[0].to).toBe(T0 + 22 * HOUR);
  });

  it('merges touching windows and keeps both labels', () => {
    const merged = mergeWindows([
      { from: 0, to: 10, label: 'a' },
      { from: 5, to: 20, label: 'b' },
      { from: 100, to: 110, label: 'c' },
    ]);
    expect(merged).toEqual([
      { from: 0, to: 20, label: 'a, b' },
      { from: 100, to: 110, label: 'c' },
    ]);
  });

  it('survives a missing or unreadable flags file', () => {
    expect(blackoutWindows(null)).toEqual([]);
    expect(blackoutWindows(undefined)).toEqual([]);
  });
});

describe('regime shading', () => {
  it('reads the free-text regime column defensively', () => {
    expect(regimeBucket('up')).toBe('up');
    expect(regimeBucket('trend_up')).toBe('up');
    expect(regimeBucket('risk_on')).toBe('up');
    expect(regimeBucket('down')).toBe('down');
    expect(regimeBucket('bearish')).toBe('down');
    expect(regimeBucket('chop')).toBe('neutral');
    expect(regimeBucket(1)).toBe('up');
    expect(regimeBucket(-1)).toBe('down');
    expect(regimeBucket(null)).toBe('neutral');
  });

  it('holds each regime until the next snapshot and leaves earlier bars unshaded', () => {
    const bars = toBars(candleSeries(6));
    const bands = regimeBands(bars, [
      { ts_utc: iso(T0 + 2 * HOUR), regime: 'up' },
      { ts_utc: iso(T0 + 4 * HOUR), regime: 'down' },
    ]);
    // Bars 0 and 1 predate every snapshot: nothing is known, so nothing is claimed.
    expect(bands.map((b) => b.time)).toEqual([
      T0 + 2 * HOUR,
      T0 + 3 * HOUR,
      T0 + 4 * HOUR,
      T0 + 5 * HOUR,
    ]);
    expect(bands.map((b) => b.color)).toEqual([
      REGIME_SHADE.up,
      REGIME_SHADE.up,
      REGIME_SHADE.down,
      REGIME_SHADE.down,
    ]);
  });
});

describe('proposal step lines', () => {
  const bars = toBars(candleSeries(6));
  const proposals: ProposalMarker[] = [
    {
      kind: 'proposal',
      ts: iso(T0 + HOUR),
      run_id: 'r1',
      module: 'trend',
      targets: { BTC: 0.5, USDT: 0.5 },
      abstain: false,
    },
    {
      kind: 'proposal',
      ts: iso(T0 + 4 * HOUR),
      run_id: 'r2',
      module: 'trend',
      targets: { BTC: 0.2, USDT: 0.8 },
      abstain: true,
    },
  ];

  it('gives one line per target asset, in percent, at the bar it landed on', () => {
    const { lines, placed } = proposalLines(proposals, bars, HOUR);
    expect(placed).toBe(2);
    expect(lines.map((l) => l.asset)).toEqual(['BTC', 'USDT']);
    expect(lines[0].points).toEqual([
      { time: T0 + HOUR, value: 50 },
      { time: T0 + 4 * HOUR, value: 20 },
    ]);
    expect(lines[1].points[1].value).toBeCloseTo(80, 9);
  });

  it('marks abstains, and only abstains', () => {
    const { markers } = proposalLines(proposals, bars, HOUR);
    expect(markers).toHaveLength(1);
    expect(markers[0].text).toContain('abstain');
    expect(markers[0].group).toBe('proposal');
  });

  it('drops a proposal older than the window instead of pinning it to bar 0', () => {
    const stale: ProposalMarker[] = [{ ...proposals[0], ts: iso(T0 - 100 * HOUR) }];
    expect(proposalLines(stale, bars, HOUR).placed).toBe(0);
  });
});

describe('buildChartModel', () => {
  const candles = candleSeries(40);
  const markers: Marker[] = [
    { kind: 'fill', ts: iso(T0 + 2 * HOUR), label: 'buy 0.01', side: 'buy', price: 101,
      mode: 'live', sim: false },
    { kind: 'fill', ts: iso(T0 + 3 * HOUR), label: 'sell 0.01', side: 'sell', price: 104,
      mode: 'test', sim: true },
    { kind: 'stop', ts: iso(T0 + 5 * HOUR), label: '-10%' },
    { kind: 'take_profit', ts: iso(T0 + 6 * HOUR), label: 'rung 0' },
    { kind: 'gate_reject', ts: iso(T0 + 7 * HOUR), label: 'blackout:macro_cpi',
      severity: 'breach' },
    { kind: 'fill', ts: iso(T0 - 500 * HOUR), label: 'ancient', side: 'buy' },
  ];
  const signals: SignalRow[] = [
    { signal_id: 's1', ts_utc: iso(T0 + 8 * HOUR), detector: 'breakout', pair: 'BTC/USDT',
      direction: 'up', status: 'valid' },
    { signal_id: 's2', ts_utc: iso(T0 + 9 * HOUR), detector: 'rsi_extreme', pair: 'BTC/USDT',
      direction: 'down', status: 'invalid' },
    { signal_id: 's3', ts_utc: iso(T0 + 10 * HOUR), detector: 'funding', pair: 'BTC/USDT',
      direction: 'neutral', status: 'uncertain' },
  ];

  const model = buildChartModel({
    candles,
    markers,
    signals,
    proposals: [
      { kind: 'proposal', ts: iso(T0 + 12 * HOUR), run_id: 'r1', module: 'trend',
        targets: { BTC: 0.4, USDT: 0.6 }, abstain: false },
    ],
    flags: {
      macro_cpi: { active: true, severity: 'block_entries', set_at: iso(T0 + 7 * HOUR),
        expires_at: iso(T0 + 9 * HOUR) },
    },
    regimes: [{ ts_utc: iso(T0), regime: 'up' }],
    maPeriod: 5,
    atrPeriod: 5,
    atrMult: 2,
    rsiPeriod: 5,
  });

  it('separates SIM from LIVE in colour, shape and label', () => {
    const fills = model.markers.filter((m) => m.group === 'fill');
    expect(fills).toHaveLength(2);
    const [buy, sell] = fills;
    expect(buy).toMatchObject({ color: FILL_COLOR.buyLive, shape: 'arrowUp', sim: false });
    expect(buy.text).toBe('LIVE buy 0.01');
    expect(sell).toMatchObject({ color: FILL_COLOR.sellSim, shape: 'circle', sim: true });
    expect(sell.text).toBe('SIM sell 0.01');
  });

  it('draws stop and take-profit exits distinctly from ordinary fills', () => {
    const exits = model.markers.filter((m) => m.group === 'exit');
    expect(exits.map((e) => e.text)).toEqual(['STOP -10%', 'TP rung 0']);
  });

  it('colours signals by the validator verdict, not the detector', () => {
    const byId = new Map(model.markers.map((m) => [m.id, m]));
    expect(byId.get('signal-s1')?.color).toBe(VERDICT_COLOR.valid);
    expect(byId.get('signal-s2')?.color).toBe(VERDICT_COLOR.invalid);
    expect(byId.get('signal-s3')?.color).toBe(VERDICT_COLOR.uncertain);
    // A "down" signal hangs below the bar so it does not collide with the buy arrows.
    expect(byId.get('signal-s2')?.position).toBe('belowBar');
  });

  it('marks gate rejects and shades the blackout window they belong to', () => {
    const gate = model.markers.find((m) => m.group === 'gate');
    expect(gate?.color).toBe(GATE_REJECT_COLOR);
    expect(gate?.text).toContain('blackout:macro_cpi');
    expect(model.blackoutWindows).toHaveLength(1);
    expect(model.blackout.map((b) => b.time)).toEqual([
      T0 + 7 * HOUR,
      T0 + 8 * HOUR,
      T0 + 9 * HOUR,
    ]);
    expect(model.blackout[0].color).toBe(BLACKOUT_SHADE);
  });

  it('counts, rather than misplaces, anything outside the window', () => {
    expect(model.droppedMarkers).toBe(1);
    expect(model.markers.every((m) => m.time >= model.range!.from)).toBe(true);
  });

  it('shades every bar with the regime in force and keeps markers sorted', () => {
    expect(model.regime).toHaveLength(model.bars.length);
    expect(model.regime[0].color).toBe(REGIME_SHADE.up);
    const times = model.markers.map((m) => m.time);
    expect([...times].sort((a, b) => a - b)).toEqual(times);
  });

  it('computes the overlays at full resolution', () => {
    expect(model.ma.length).toBe(candles.length - 4);
    expect(model.rsi.length).toBe(candles.length - 5);
    expect(model.atrUpper).toHaveLength(model.atrLower.length);
    for (let i = 0; i < model.atrUpper.length; i += 1) {
      expect(model.atrUpper[i].value).toBeGreaterThan(model.atrLower[i].value);
    }
  });

  it('reports the marker tally the legend shows', () => {
    expect(model.counts).toMatchObject({ fill: 2, exit: 2, gate: 1, signal: 3, proposal: 1 });
  });
});

describe('buildChartModel under real data volumes', () => {
  it('aggregates thousands of bars down to the render budget', () => {
    const model = buildChartModel({ candles: candleSeries(5000), maxBars: 1000 });
    expect(model.sourceBars).toBe(5000);
    expect(model.factor).toBe(5);
    expect(model.bars).toHaveLength(1000);
    expect(model.ma.length).toBeLessThanOrEqual(1000);
    expect(model.rsi.length).toBeLessThanOrEqual(1000);
    // The aggregated extremes are the real extremes, not a sampled subset of them.
    const full = toBars(candleSeries(5000));
    expect(Math.max(...model.bars.map((b) => b.high))).toBe(Math.max(...full.map((b) => b.high)));
    expect(Math.min(...model.bars.map((b) => b.low))).toBe(Math.min(...full.map((b) => b.low)));
  });

  it('caps the drawn markers at the newest MAX_MARKERS', () => {
    const candles = candleSeries(2000);
    const markers: Marker[] = candles.map((candle, i) => ({
      kind: 'fill',
      ts: candle.date,
      label: `buy ${i}`,
      side: 'buy',
      mode: 'test',
    }));
    const model = buildChartModel({ candles, markers, maxBars: 2000 });
    expect(model.counts.fill).toBe(2000);
    expect(model.markers).toHaveLength(MAX_MARKERS);
    expect(model.markers[model.markers.length - 1].time).toBe(
      parseUtcSeconds(candles[candles.length - 1].date),
    );
  });

  it('is an empty model, never a throw, when there are no candles', () => {
    const model = buildChartModel({ candles: [], markers: [{ kind: 'fill', ts: iso(T0), label: 'x' }] });
    expect(model.bars).toEqual([]);
    expect(model.range).toBeNull();
    expect(model.markers).toEqual([]);
  });
});

describe('overlay toggles', () => {
  it('stop computing what the operator turned off', () => {
    const model = buildChartModel({
      candles: candleSeries(40),
      markers: [{ kind: 'fill', ts: iso(T0 + HOUR), label: 'buy', side: 'buy' }],
      signals: [{ signal_id: 's1', ts_utc: iso(T0 + HOUR), detector: 'd', pair: 'BTC/USDT',
        direction: 'up', status: 'valid' }],
      regimes: [{ ts_utc: iso(T0), regime: 'up' }],
      overlays: {
        ...DEFAULT_OVERLAYS,
        ma: false,
        atr: false,
        rsi: false,
        regime: false,
        signals: false,
        fills: false,
      },
    });
    expect(model.ma).toEqual([]);
    expect(model.atrUpper).toEqual([]);
    expect(model.rsi).toEqual([]);
    expect(model.regime).toEqual([]);
    expect(model.markers).toEqual([]);
  });
});
