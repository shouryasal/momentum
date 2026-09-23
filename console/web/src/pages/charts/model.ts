/**
 * The Charts page's model layer: every number the chart draws is computed here, in pure
 * functions, so the rendering wrapper stays a thin adapter over lightweight-charts and the
 * arithmetic is testable without a canvas.
 *
 * Two things drive the shape of this file.
 *
 * **Volume.** `/market/candles` will hand back thousands of bars, and the page must stay
 * responsive. Rather than pushing everything at the renderer, the bars are aggregated into
 * at most `maxBars` buckets (open = first, high = max, low = min, close = last, volume =
 * sum) — real OHLC aggregation, not point-dropping, so a downsampled candle still tells the
 * truth about its range. Indicators are computed at **full** resolution and then sampled at
 * each bucket's last bar, which is the bar whose close the aggregated candle shows, so an
 * overlay never disagrees with the candle underneath it.
 *
 * **Honesty about placement.** A marker whose timestamp falls outside the loaded window is
 * dropped rather than clamped onto the first or last bar: a fill drawn on the wrong candle
 * is worse than a fill not drawn at all.
 */
import type {
  Candle,
  Marker,
  ProposalMarker,
  RegimeSnapshot,
  RiskFlag,
  SignalRow,
} from './api';

// --------------------------------------------------------------------------- types

export interface Bar {
  /** UTC seconds — what lightweight-charts calls a `UTCTimestamp`. */
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface LinePoint {
  time: number;
  value: number;
}

export interface BandPoint {
  time: number;
  value: number;
  color: string;
}

export type MarkerGroup = 'fill' | 'exit' | 'signal' | 'gate' | 'proposal';

export interface ChartMarker {
  id: string;
  time: number;
  position: 'aboveBar' | 'belowBar' | 'inBar';
  shape: 'circle' | 'square' | 'arrowUp' | 'arrowDown';
  color: string;
  text: string;
  group: MarkerGroup;
  /** Present on fills: true outside LIVE, which is what the SIM styling keys off. */
  sim?: boolean;
}

export interface TimeWindow {
  from: number;
  to: number;
  label: string;
}

export interface ProposalLine {
  asset: string;
  color: string;
  points: LinePoint[];
}

export interface Overlays {
  ma: boolean;
  atr: boolean;
  regime: boolean;
  blackout: boolean;
  rsi: boolean;
  proposals: boolean;
  fills: boolean;
  exits: boolean;
  signals: boolean;
  gateRejects: boolean;
}

export const DEFAULT_OVERLAYS: Overlays = {
  ma: true,
  atr: true,
  regime: true,
  blackout: true,
  rsi: true,
  proposals: true,
  fills: true,
  exits: true,
  signals: true,
  gateRejects: true,
};

export interface BuildInput {
  candles: Candle[];
  markers?: Marker[];
  proposals?: ProposalMarker[];
  signals?: SignalRow[];
  flags?: Record<string, RiskFlag> | null;
  regimes?: RegimeSnapshot[];
  maPeriod?: number;
  atrPeriod?: number;
  atrMult?: number;
  rsiPeriod?: number;
  maxBars?: number;
  overlays?: Overlays;
}

export interface ChartModel {
  bars: Bar[];
  /** How many bars the API returned before aggregation. */
  sourceBars: number;
  /** Bars per drawn candle. 1 = nothing was aggregated. */
  factor: number;
  ma: LinePoint[];
  atrUpper: LinePoint[];
  atrLower: LinePoint[];
  rsi: LinePoint[];
  regime: BandPoint[];
  blackout: BandPoint[];
  markers: ChartMarker[];
  proposalLines: ProposalLine[];
  /** Abstain flags, drawn on the proposal pane rather than over the candles. */
  proposalMarkers: ChartMarker[];
  blackoutWindows: TimeWindow[];
  counts: Record<MarkerGroup, number>;
  /** Markers dropped because no loaded bar covers their timestamp. */
  droppedMarkers: number;
  range: { from: number; to: number } | null;
}

// --------------------------------------------------------------------------- colours

/** Fills. LIVE is saturated and solid; SIM is the pale twin, and says so in its label. */
export const FILL_COLOR = {
  buyLive: '#2b8a3e',
  buySim: '#8ce99a',
  sellLive: '#c92a2a',
  sellSim: '#ffa8a8',
} as const;

export const EXIT_COLOR: Record<string, string> = {
  stop: '#e03131',
  take_profit: '#0ca678',
};

export const GATE_REJECT_COLOR = '#f08c00';

/** Signal colours are the validator's verdict, not the detector's enthusiasm. */
export const VERDICT_COLOR: Record<string, string> = {
  valid: '#0ca678',
  invalid: '#e03131',
  uncertain: '#f59f00',
  blocked: '#e8590c',
  planned: '#7048e8',
  acted: '#9c36b5',
  validating: '#4c6ef5',
  screened: '#1c7ed6',
  screened_out: '#868e96',
  candidate: '#4dabf7',
  expired: '#adb5bd',
  error: '#c92a2a',
};

export const UNKNOWN_COLOR = '#868e96';

export const REGIME_SHADE: Record<string, string> = {
  up: 'rgba(47, 158, 68, 0.13)',
  down: 'rgba(224, 49, 49, 0.13)',
  neutral: 'rgba(134, 142, 150, 0.10)',
};

export const BLACKOUT_SHADE = 'rgba(240, 140, 0, 0.20)';

export const PROPOSAL_COLORS = [
  '#1c7ed6',
  '#e8590c',
  '#0ca678',
  '#9c36b5',
  '#f59f00',
  '#495057',
];

/** Drawn markers are capped so a year of fills cannot stall the page. */
export const MAX_MARKERS = 600;

/** Default render budget: more candles than this get aggregated into buckets. */
export const DEFAULT_MAX_BARS = 1200;

// --------------------------------------------------------------------------- time

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;
const HAS_ZONE = /(?:Z|z|[+-]\d{2}:?\d{2})$/;

/**
 * Seconds since the epoch from whatever the API hands over.
 *
 * `/market/candles` stringifies a pandas timestamp, so `"2026-09-22 04:00:00+00:00"` and
 * `"2026-09-22T04:00:00Z"` both appear in practice; journal rows are always ISO-8601 with
 * `Z`. A naive timestamp is read as UTC, which is the repo-wide convention.
 */
export function parseUtcSeconds(value: unknown): number | null {
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) return null;
    return Math.floor(value > 1e11 ? value / 1000 : value);
  }
  if (typeof value !== 'string') return null;
  const raw = value.trim();
  if (raw === '') return null;
  let text = raw.includes('T') ? raw : raw.replace(' ', 'T');
  if (DATE_ONLY.test(text)) text = `${text}T00:00:00Z`;
  else if (!HAS_ZONE.test(text)) text = `${text}Z`;
  const ms = Date.parse(text);
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
}

// --------------------------------------------------------------------------- bars

function finite(value: unknown): number | null {
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

/** Candles → sorted, de-duplicated bars. Unparseable or non-numeric rows are dropped. */
export function toBars(candles: Candle[] | undefined | null): Bar[] {
  const parsed: Bar[] = [];
  for (const candle of candles ?? []) {
    if (!candle) continue;
    const time = parseUtcSeconds(candle.date);
    const open = finite(candle.open);
    const high = finite(candle.high);
    const low = finite(candle.low);
    const close = finite(candle.close);
    if (time === null || open === null || high === null || low === null || close === null) {
      continue;
    }
    parsed.push({ time, open, high, low, close, volume: finite(candle.volume) ?? 0 });
  }
  parsed.sort((a, b) => a.time - b.time);
  const out: Bar[] = [];
  for (const bar of parsed) {
    const prev = out[out.length - 1];
    if (prev && prev.time === bar.time) out[out.length - 1] = bar;
    else out.push(bar);
  }
  return out;
}

/** Bars per bucket needed to fit `count` bars into `maxBars`. Never below 1. */
export function downsampleFactor(count: number, maxBars: number): number {
  if (!Number.isFinite(maxBars) || maxBars <= 0) return 1;
  if (count <= maxBars) return 1;
  return Math.ceil(count / maxBars);
}

/** The index of the last full-resolution bar in each bucket. */
export function bucketEnds(count: number, factor: number): number[] {
  const step = Math.max(1, Math.floor(factor));
  const out: number[] = [];
  for (let i = 0; i < count; i += step) out.push(Math.min(i + step, count) - 1);
  return out;
}

/** OHLC aggregation: open = first, high = max, low = min, close = last, volume = sum. */
export function aggregate(bars: Bar[], factor: number): Bar[] {
  const step = Math.max(1, Math.floor(factor));
  if (step === 1) return bars;
  const out: Bar[] = [];
  for (let i = 0; i < bars.length; i += step) {
    const first = bars[i];
    let high = first.high;
    let low = first.low;
    let volume = 0;
    let close = first.close;
    for (let j = i; j < Math.min(i + step, bars.length); j += 1) {
      const bar = bars[j];
      if (bar.high > high) high = bar.high;
      if (bar.low < low) low = bar.low;
      volume += bar.volume;
      close = bar.close;
    }
    out.push({ time: first.time, open: first.open, high, low, close, volume });
  }
  return out;
}

// --------------------------------------------------------------------------- indicators

/** Simple moving average; `null` until `period` values exist. */
export function sma(values: number[], period: number): Array<number | null> {
  const p = Math.max(1, Math.floor(period));
  const out: Array<number | null> = [];
  let sum = 0;
  for (let i = 0; i < values.length; i += 1) {
    sum += values[i];
    if (i >= p) sum -= values[i - p];
    out.push(i >= p - 1 ? sum / p : null);
  }
  return out;
}

/** Wilder's RSI — the same smoothing `runs/signals/features.py` uses for `rsi_extreme`. */
export function wilderRsi(values: number[], period: number): Array<number | null> {
  const p = Math.max(1, Math.floor(period));
  const out: Array<number | null> = new Array(values.length).fill(null);
  if (values.length <= p) return out;
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= p; i += 1) {
    const delta = values[i] - values[i - 1];
    if (delta >= 0) gain += delta;
    else loss -= delta;
  }
  gain /= p;
  loss /= p;
  out[p] = rsiFrom(gain, loss);
  for (let i = p + 1; i < values.length; i += 1) {
    const delta = values[i] - values[i - 1];
    gain = (gain * (p - 1) + Math.max(delta, 0)) / p;
    loss = (loss * (p - 1) + Math.max(-delta, 0)) / p;
    out[i] = rsiFrom(gain, loss);
  }
  return out;
}

function rsiFrom(gain: number, loss: number): number {
  if (loss === 0) return gain === 0 ? 50 : 100;
  const rs = gain / loss;
  return 100 - 100 / (1 + rs);
}

/** Wilder's ATR over the true range. `null` until `period` bars exist. */
export function wilderAtr(bars: Bar[], period: number): Array<number | null> {
  const p = Math.max(1, Math.floor(period));
  const out: Array<number | null> = new Array(bars.length).fill(null);
  if (bars.length < p) return out;
  const tr: number[] = [];
  for (let i = 0; i < bars.length; i += 1) {
    const bar = bars[i];
    const prev = i > 0 ? bars[i - 1].close : bar.open;
    tr.push(Math.max(bar.high - bar.low, Math.abs(bar.high - prev), Math.abs(bar.low - prev)));
  }
  let atr = 0;
  for (let i = 0; i < p; i += 1) atr += tr[i];
  atr /= p;
  out[p - 1] = atr;
  for (let i = p; i < bars.length; i += 1) {
    atr = (atr * (p - 1) + tr[i]) / p;
    out[i] = atr;
  }
  return out;
}

/** Pick indicator values at the bucket ends and label them with the bucket's own time. */
export function sampleAt(
  values: Array<number | null>,
  ends: number[],
  times: number[],
): LinePoint[] {
  const out: LinePoint[] = [];
  for (let i = 0; i < ends.length && i < times.length; i += 1) {
    const value = values[ends[i]];
    if (value === null || value === undefined || !Number.isFinite(value)) continue;
    out.push({ time: times[i], value });
  }
  return out;
}

// --------------------------------------------------------------------------- placement

/**
 * The index of the bar that covers `seconds`, or `null` when nothing does.
 *
 * "Covers" means the last bar starting at or before the timestamp, and — for the final bar,
 * which has no successor to bound it — within one bar's spacing of its start. Anything
 * earlier than the first bar or beyond that tail is outside the loaded window.
 */
export function barIndexAt(times: number[], seconds: number, spacing: number): number | null {
  if (times.length === 0) return null;
  if (seconds < times[0]) return null;
  let lo = 0;
  let hi = times.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] <= seconds) {
      found = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  if (found < 0) return null;
  if (found === times.length - 1) {
    const span = spacing > 0 ? spacing : 0;
    if (seconds > times[found] + span) return null;
  }
  return found;
}

/** Median gap between bars — the width one bar stands for. */
export function medianSpacing(times: number[]): number {
  if (times.length < 2) return 0;
  const gaps: number[] = [];
  for (let i = 1; i < times.length; i += 1) gaps.push(times[i] - times[i - 1]);
  gaps.sort((a, b) => a - b);
  return gaps[Math.floor(gaps.length / 2)] || 0;
}

// --------------------------------------------------------------------------- markers

function fillMarker(marker: Marker, time: number, index: number): ChartMarker {
  const sell = String(marker.side ?? '').toLowerCase() === 'sell';
  const sim = marker.sim !== false && String(marker.mode ?? 'test').toLowerCase() !== 'live';
  const color = sell
    ? sim
      ? FILL_COLOR.sellSim
      : FILL_COLOR.sellLive
    : sim
      ? FILL_COLOR.buySim
      : FILL_COLOR.buyLive;
  return {
    id: `fill-${index}`,
    time,
    position: sell ? 'aboveBar' : 'belowBar',
    // A hollow-looking circle for simulated money, a solid arrow for real money: the
    // difference between the two is the whole point of the TEST/LIVE split.
    shape: sim ? 'circle' : sell ? 'arrowDown' : 'arrowUp',
    color,
    text: `${sim ? 'SIM' : 'LIVE'} ${marker.label ?? (sell ? 'sell' : 'buy')}`,
    group: 'fill',
    sim,
  };
}

function exitMarker(marker: Marker, time: number, index: number): ChartMarker {
  const stop = marker.kind === 'stop';
  return {
    id: `exit-${index}`,
    time,
    position: 'aboveBar',
    shape: 'square',
    color: EXIT_COLOR[marker.kind] ?? UNKNOWN_COLOR,
    text: `${stop ? 'STOP' : 'TP'} ${marker.label ?? ''}`.trim(),
    group: 'exit',
  };
}

function gateMarker(marker: Marker, time: number, index: number): ChartMarker {
  return {
    id: `gate-${index}`,
    time,
    position: 'belowBar',
    shape: 'square',
    color: GATE_REJECT_COLOR,
    text: `gate ${marker.label ?? 'reject'}`,
    group: 'gate',
  };
}

export function signalMarker(signal: SignalRow, time: number): ChartMarker {
  const status = String(signal.status ?? '').toLowerCase();
  const down = String(signal.direction ?? '').toLowerCase() === 'down';
  return {
    id: `signal-${signal.signal_id}`,
    time,
    position: down ? 'belowBar' : 'aboveBar',
    shape: 'circle',
    color: VERDICT_COLOR[status] ?? UNKNOWN_COLOR,
    text: `${signal.detector} · ${status || 'unknown'}`,
    group: 'signal',
  };
}

// --------------------------------------------------------------------------- windows

const RECONCILE_FLAG = 'reconcile_mismatch';

/**
 * Blackout windows, from the two places the console can see them.
 *
 * `knowledge/flags.json` gives the live ones: `strategies/riskgate.py` fails its `blackout`
 * check on any `block_entries` flag other than `reconcile_mismatch` (which has its own
 * check), so that is exactly the set drawn here. Historical ones are recovered from the
 * gate's own refusals — a `blackout:*` reject proves the window was closed at that bar.
 */
export function blackoutWindows(
  flags: Record<string, RiskFlag> | null | undefined,
  rejects: Marker[] = [],
  spacing = 0,
): TimeWindow[] {
  const out: TimeWindow[] = [];
  for (const [name, flag] of Object.entries(flags ?? {})) {
    if (!flag || flag.active === false) continue;
    if (flag.severity !== 'block_entries') continue;
    if (name === RECONCILE_FLAG) continue;
    const from = parseUtcSeconds(flag.set_at);
    if (from === null) continue;
    const to = parseUtcSeconds(flag.expires_at) ?? Number.POSITIVE_INFINITY;
    out.push({ from, to, label: name });
  }
  for (const reject of rejects) {
    if (!String(reject.label ?? '').startsWith('blackout')) continue;
    const at = parseUtcSeconds(reject.ts);
    if (at === null) continue;
    out.push({ from: at, to: at + spacing, label: reject.label });
  }
  out.sort((a, b) => a.from - b.from);
  return mergeWindows(out);
}

/** Overlapping or touching windows collapse into one, keeping every label. */
export function mergeWindows(windows: TimeWindow[]): TimeWindow[] {
  const out: TimeWindow[] = [];
  for (const window of windows) {
    const prev = out[out.length - 1];
    if (prev && window.from <= prev.to) {
      prev.to = Math.max(prev.to, window.to);
      if (!prev.label.split(', ').includes(window.label)) {
        prev.label = `${prev.label}, ${window.label}`;
      }
      continue;
    }
    out.push({ ...window });
  }
  return out;
}

const REGIME_UP = /^(up|bull|bullish|on|expansion|risk_on|1|true)$/;
const REGIME_DOWN = /^(down|bear|bearish|off|contraction|risk_off|0|false)$/;

/** `state_snapshots.regime` is a free-text column; read it defensively. */
export function regimeBucket(value: unknown): 'up' | 'down' | 'neutral' {
  if (typeof value === 'number') return value > 0 ? 'up' : value < 0 ? 'down' : 'neutral';
  const text = String(value ?? '').toLowerCase().trim();
  if (text === '') return 'neutral';
  const tokens = text.split(/[^a-z0-9]+/).filter(Boolean);
  if (tokens.some((token) => REGIME_UP.test(token))) return 'up';
  if (tokens.some((token) => REGIME_DOWN.test(token))) return 'down';
  return 'neutral';
}

/** The regime in force at each bar, as a full-height shaded column. */
export function regimeBands(bars: Bar[], snapshots: RegimeSnapshot[] = []): BandPoint[] {
  const points = snapshots
    .map((snap) => ({
      time: parseUtcSeconds(snap.asof_candle_utc ?? snap.ts_utc),
      bucket: regimeBucket(snap.regime),
    }))
    .filter((p): p is { time: number; bucket: 'up' | 'down' | 'neutral' } => p.time !== null)
    .sort((a, b) => a.time - b.time);
  if (points.length === 0) return [];
  const out: BandPoint[] = [];
  let cursor = 0;
  let current: 'up' | 'down' | 'neutral' | null = null;
  for (const bar of bars) {
    while (cursor < points.length && points[cursor].time <= bar.time) {
      current = points[cursor].bucket;
      cursor += 1;
    }
    if (current === null) continue;
    out.push({ time: bar.time, value: 1, color: REGIME_SHADE[current] });
  }
  return out;
}

/** Bars covered by a blackout window, as a full-height shaded column. */
export function blackoutBands(bars: Bar[], windows: TimeWindow[]): BandPoint[] {
  if (windows.length === 0) return [];
  const out: BandPoint[] = [];
  for (const bar of bars) {
    if (windows.some((w) => bar.time >= w.from && bar.time <= w.to)) {
      out.push({ time: bar.time, value: 1, color: BLACKOUT_SHADE });
    }
  }
  return out;
}

// --------------------------------------------------------------------------- proposals

/**
 * Proposals as a target step line per asset: a weight holds until the next proposal moves
 * it, which is exactly what SleeveB does with it, so a step line is the honest shape.
 */
export function proposalLines(
  proposals: ProposalMarker[],
  bars: Bar[],
  spacing: number,
): { lines: ProposalLine[]; markers: ChartMarker[]; placed: number } {
  if (bars.length === 0) return { lines: [], markers: [], placed: 0 };
  const times = bars.map((b) => b.time);
  const sorted = proposals
    .map((proposal) => ({ proposal, at: parseUtcSeconds(proposal.ts) }))
    .filter((row): row is { proposal: ProposalMarker; at: number } => row.at !== null)
    .sort((a, b) => a.at - b.at);

  const assets = new Set<string>();
  for (const { proposal } of sorted) {
    for (const asset of Object.keys(proposal.targets ?? {})) assets.add(asset);
  }
  const ordered = [...assets].sort().slice(0, PROPOSAL_COLORS.length);

  const byAsset = new Map<string, LinePoint[]>(ordered.map((asset) => [asset, []]));
  const markers: ChartMarker[] = [];
  let placed = 0;
  for (const { proposal, at } of sorted) {
    const index = barIndexAt(times, at, spacing);
    if (index === null) continue;
    placed += 1;
    const time = times[index];
    for (const asset of ordered) {
      const weight = Number(proposal.targets?.[asset]);
      if (!Number.isFinite(weight)) continue;
      const points = byAsset.get(asset);
      if (!points) continue;
      const prev = points[points.length - 1];
      if (prev && prev.time === time) prev.value = weight * 100;
      else points.push({ time, value: weight * 100 });
    }
    if (proposal.abstain) {
      markers.push({
        id: `proposal-${proposal.run_id}`,
        time,
        position: 'aboveBar',
        shape: 'square',
        color: UNKNOWN_COLOR,
        text: `abstain · ${proposal.module}`,
        group: 'proposal',
      });
    }
  }
  const lines: ProposalLine[] = ordered
    .map((asset, i) => ({
      asset,
      color: PROPOSAL_COLORS[i % PROPOSAL_COLORS.length],
      points: byAsset.get(asset) ?? [],
    }))
    .filter((line) => line.points.length > 0);
  return { lines, markers: dedupeSorted(markers), placed };
}

// --------------------------------------------------------------------------- build

function dedupeSorted(markers: ChartMarker[]): ChartMarker[] {
  return [...markers].sort((a, b) => a.time - b.time || a.id.localeCompare(b.id));
}

const EMPTY_COUNTS: Record<MarkerGroup, number> = {
  fill: 0,
  exit: 0,
  signal: 0,
  gate: 0,
  proposal: 0,
};

export function emptyModel(): ChartModel {
  return {
    bars: [],
    sourceBars: 0,
    factor: 1,
    ma: [],
    atrUpper: [],
    atrLower: [],
    rsi: [],
    regime: [],
    blackout: [],
    markers: [],
    proposalLines: [],
    proposalMarkers: [],
    blackoutWindows: [],
    counts: { ...EMPTY_COUNTS },
    droppedMarkers: 0,
    range: null,
  };
}

/** Everything the chart draws, from everything the page fetched. */
export function buildChartModel(input: BuildInput): ChartModel {
  const overlays = { ...DEFAULT_OVERLAYS, ...(input.overlays ?? {}) };
  const full = toBars(input.candles);
  if (full.length === 0) return emptyModel();

  const maxBars = input.maxBars ?? DEFAULT_MAX_BARS;
  const factor = downsampleFactor(full.length, maxBars);
  const bars = aggregate(full, factor);
  const ends = bucketEnds(full.length, factor);
  const times = bars.map((b) => b.time);
  const spacing = medianSpacing(times);

  const closes = full.map((b) => b.close);
  const ma = overlays.ma
    ? sampleAt(sma(closes, input.maPeriod ?? 200), ends, times)
    : [];

  let atrUpper: LinePoint[] = [];
  let atrLower: LinePoint[] = [];
  if (overlays.atr) {
    const atr = wilderAtr(full, input.atrPeriod ?? 14);
    const mult = input.atrMult ?? 3;
    const upper = atr.map((value, i) => (value === null ? null : full[i].close + mult * value));
    const lower = atr.map((value, i) => (value === null ? null : full[i].close - mult * value));
    atrUpper = sampleAt(upper, ends, times);
    atrLower = sampleAt(lower, ends, times);
  }

  const rsi = overlays.rsi
    ? sampleAt(wilderRsi(closes, input.rsiPeriod ?? 14), ends, times)
    : [];

  const rawMarkers = input.markers ?? [];
  const rejects = rawMarkers.filter((m) => m.kind === 'gate_reject');
  const windows = overlays.blackout ? blackoutWindows(input.flags, rejects, spacing) : [];

  const placed: ChartMarker[] = [];
  let dropped = 0;
  rawMarkers.forEach((marker, index) => {
    const at = parseUtcSeconds(marker.ts);
    const barIndex = at === null ? null : barIndexAt(times, at, spacing);
    if (barIndex === null) {
      dropped += 1;
      return;
    }
    const time = times[barIndex];
    if (marker.kind === 'fill') {
      if (overlays.fills) placed.push(fillMarker(marker, time, index));
    } else if (marker.kind === 'stop' || marker.kind === 'take_profit') {
      if (overlays.exits) placed.push(exitMarker(marker, time, index));
    } else if (marker.kind === 'gate_reject') {
      if (overlays.gateRejects) placed.push(gateMarker(marker, time, index));
    }
  });

  if (overlays.signals) {
    for (const signal of input.signals ?? []) {
      const at = parseUtcSeconds(signal.ts_utc);
      const barIndex = at === null ? null : barIndexAt(times, at, spacing);
      if (barIndex === null) {
        dropped += 1;
        continue;
      }
      placed.push(signalMarker(signal, times[barIndex]));
    }
  }

  const proposals = overlays.proposals
    ? proposalLines(input.proposals ?? [], bars, spacing)
    : { lines: [], markers: [], placed: 0 };

  const counts: Record<MarkerGroup, number> = { ...EMPTY_COUNTS };
  for (const marker of placed) counts[marker.group] += 1;
  counts.proposal = proposals.placed;

  // Newest markers win when there are more than the render budget: a chart of the last few
  // hundred events stays readable, and the tail is what an operator is looking at.
  const sorted = dedupeSorted(placed);
  const markers = sorted.length > MAX_MARKERS ? sorted.slice(sorted.length - MAX_MARKERS) : sorted;

  return {
    bars,
    sourceBars: full.length,
    factor,
    ma,
    atrUpper,
    atrLower,
    rsi,
    regime: overlays.regime ? regimeBands(bars, input.regimes ?? []) : [],
    blackout: blackoutBands(bars, windows),
    markers,
    proposalLines: proposals.lines,
    proposalMarkers: proposals.markers,
    blackoutWindows: windows,
    counts,
    droppedMarkers: dropped,
    range: { from: times[0], to: times[times.length - 1] },
  };
}
