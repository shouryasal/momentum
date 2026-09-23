import { Alert, Box, useComputedColorScheme } from '@mantine/core';
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  LineType,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts';
import { useEffect, useMemo, useRef, useState } from 'react';

import type { ChartMarker, ChartModel, Overlays } from '../model';

/**
 * The Charts renderer: lightweight-charts 5.2, three panes.
 *
 *   pane 0  candles, the MA overlay, the ATR channel, regime and blackout shading, and
 *           every event marker (fills, stop/TP exits, signals, gate rejects)
 *   pane 1  the proposal target step lines — a weight holds until a later proposal moves it
 *   pane 2  RSI with its 30/70 guides
 *
 * The shading is drawn as overlay histograms on their own invisible price scales, pinned
 * to the full pane height and added *before* the candles so they sit behind them. That
 * keeps the whole page on the documented core API — no custom canvas primitives to drift
 * against a library upgrade.
 *
 * Construction and data are deliberately separate effects. The chart is rebuilt only when
 * the *set* of series changes (an overlay toggled, the theme flipped, a new proposal
 * asset); new candles or new markers are pushed into the series that already exist. A
 * refetch therefore costs one `setData` instead of a teardown, and the operator keeps the
 * zoom and pan they had.
 *
 * `createChart` needs a real 2D canvas context, which jsdom does not provide. Rather than
 * let that take the page down, construction is guarded and the failure becomes a visible
 * note: the controls, the legend and the proposal table all still work.
 */

export interface PriceChartProps {
  model: ChartModel;
  overlays: Overlays;
  maPeriod: number;
  atrPeriod: number;
  atrMult: number;
  rsiPeriod: number;
  height?: number;
}

interface Palette {
  text: string;
  grid: string;
  border: string;
  separator: string;
  separatorHover: string;
  up: string;
  down: string;
  ma: string;
  atr: string;
  rsi: string;
  guide: string;
}

const DARK: Palette = {
  text: '#c1c2c5',
  grid: 'rgba(255, 255, 255, 0.06)',
  border: '#373a40',
  separator: '#373a40',
  separatorHover: 'rgba(178, 181, 189, 0.2)',
  up: '#40c057',
  down: '#fa5252',
  ma: '#74c0fc',
  atr: 'rgba(116, 143, 252, 0.75)',
  rsi: '#e599f7',
  guide: 'rgba(255, 255, 255, 0.25)',
};

const LIGHT: Palette = {
  text: '#495057',
  grid: 'rgba(0, 0, 0, 0.06)',
  border: '#ced4da',
  separator: '#dee2e6',
  separatorHover: 'rgba(73, 80, 87, 0.2)',
  up: '#2f9e44',
  down: '#e03131',
  ma: '#1c7ed6',
  atr: 'rgba(76, 110, 245, 0.6)',
  rsi: '#9c36b5',
  guide: 'rgba(0, 0, 0, 0.2)',
};

const HIDDEN_SCALE = { visible: false, scaleMargins: { top: 0, bottom: 0 } } as const;

interface Handles {
  chart: IChartApi;
  candles: ISeriesApi<'Candlestick'>;
  ma: ISeriesApi<'Line'> | null;
  atrUpper: ISeriesApi<'Line'> | null;
  atrLower: ISeriesApi<'Line'> | null;
  rsi: ISeriesApi<'Line'> | null;
  regime: ISeriesApi<'Histogram'> | null;
  blackout: ISeriesApi<'Histogram'> | null;
  proposals: Array<{ asset: string; series: ISeriesApi<'Line'> }>;
}

function asTime(seconds: number): UTCTimestamp {
  return seconds as UTCTimestamp;
}

function toSeriesMarkers(markers: ChartMarker[]): SeriesMarker<Time>[] {
  return markers.map((marker) => ({
    time: asTime(marker.time),
    position: marker.position,
    shape: marker.shape,
    color: marker.color,
    text: marker.text,
    id: marker.id,
    // Real money is drawn a size larger than simulated money.
    size: marker.sim === false ? 1.4 : 1,
  }));
}

export function PriceChart({
  model,
  overlays,
  maPeriod,
  atrPeriod,
  atrMult,
  rsiPeriod,
  height = 460,
}: PriceChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const handlesRef = useRef<Handles | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const scheme = useComputedColorScheme('dark');

  const hasBars = model.bars.length > 0;
  const showMa = overlays.ma && model.ma.length > 0;
  const showAtr = overlays.atr && model.atrUpper.length > 0;
  const showRsi = overlays.rsi && model.rsi.length > 0;
  const showRegime = model.regime.length > 0;
  const showBlackout = model.blackout.length > 0;
  const proposalAssets = overlays.proposals
    ? model.proposalLines.map((line) => line.asset).join(',')
    : '';

  /** Everything that decides *which* series exist. Data changes are not in here. */
  const structure = useMemo(
    () =>
      [
        scheme,
        hasBars ? 'bars' : 'none',
        showMa ? `ma${maPeriod}` : '',
        showAtr ? `atr${atrPeriod}x${atrMult}` : '',
        showRsi ? `rsi${rsiPeriod}` : '',
        showRegime ? 'regime' : '',
        showBlackout ? 'blackout' : '',
        proposalAssets,
        String(height),
      ].join('|'),
    [
      scheme,
      hasBars,
      showMa,
      showAtr,
      showRsi,
      showRegime,
      showBlackout,
      proposalAssets,
      maPeriod,
      atrPeriod,
      atrMult,
      rsiPeriod,
      height,
    ],
  );

  // --- build (only when the set of series changes) --------------------------
  useEffect(() => {
    setFailure(null);
    handlesRef.current = null;
    const element = containerRef.current;
    if (!element || !hasBars) return undefined;
    const palette = scheme === 'dark' ? DARK : LIGHT;
    let chart: IChartApi | null = null;

    try {
      chart = createChart(element, {
        autoSize: true,
        layout: {
          background: { type: ColorType.Solid, color: 'transparent' },
          textColor: palette.text,
          attributionLogo: false,
          panes: {
            enableResize: true,
            separatorColor: palette.separator,
            separatorHoverColor: palette.separatorHover,
          },
        },
        grid: {
          vertLines: { color: palette.grid },
          horzLines: { color: palette.grid },
        },
        rightPriceScale: { borderColor: palette.border },
        timeScale: { borderColor: palette.border, timeVisible: true, secondsVisible: false },
        crosshair: { mode: CrosshairMode.Normal },
      });
      const api = chart;

      // Shading first, so it renders behind the candles.
      let regime: ISeriesApi<'Histogram'> | null = null;
      if (showRegime) {
        regime = api.addSeries(HistogramSeries, {
          priceScaleId: 'earn-regime',
          base: 0,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        api.priceScale('earn-regime').applyOptions(HIDDEN_SCALE);
      }
      let blackout: ISeriesApi<'Histogram'> | null = null;
      if (showBlackout) {
        blackout = api.addSeries(HistogramSeries, {
          priceScaleId: 'earn-blackout',
          base: 0,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        api.priceScale('earn-blackout').applyOptions(HIDDEN_SCALE);
      }

      const candles = api.addSeries(CandlestickSeries, {
        upColor: palette.up,
        downColor: palette.down,
        borderUpColor: palette.up,
        borderDownColor: palette.down,
        wickUpColor: palette.up,
        wickDownColor: palette.down,
      });

      const ma = showMa
        ? api.addSeries(LineSeries, {
            color: palette.ma,
            lineWidth: 2,
            priceLineVisible: false,
            lastValueVisible: false,
            title: `MA${maPeriod}`,
          })
        : null;

      const atrLine = (title: string) =>
        api.addSeries(LineSeries, {
          color: palette.atr,
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          priceLineVisible: false,
          lastValueVisible: false,
          title,
        });
      const atrUpper = showAtr ? atrLine(`+${atrMult}×ATR${atrPeriod}`) : null;
      const atrLower = showAtr ? atrLine(`-${atrMult}×ATR${atrPeriod}`) : null;

      // --- pane 1: proposal target steps ----------------------------------
      let paneIndex = 1;
      const proposals: Handles['proposals'] = [];
      if (overlays.proposals && model.proposalLines.length > 0) {
        for (const line of model.proposalLines) {
          proposals.push({
            asset: line.asset,
            series: api.addSeries(
              LineSeries,
              {
                color: line.color,
                lineWidth: 2,
                lineType: LineType.WithSteps,
                priceLineVisible: false,
                title: line.asset,
                priceFormat: { type: 'custom', minMove: 0.1, formatter: percent },
              },
              paneIndex,
            ),
          });
        }
        api.panes()[paneIndex]?.setHeight(90);
        paneIndex += 1;
      }

      // --- pane 2: RSI ------------------------------------------------------
      let rsi: ISeriesApi<'Line'> | null = null;
      if (showRsi) {
        rsi = api.addSeries(
          LineSeries,
          {
            color: palette.rsi,
            lineWidth: 2,
            priceLineVisible: false,
            title: `RSI${rsiPeriod}`,
          },
          paneIndex,
        );
        for (const level of [70, 30]) {
          rsi.createPriceLine({
            price: level,
            color: palette.guide,
            lineWidth: 1,
            lineStyle: LineStyle.Dashed,
            axisLabelVisible: true,
            title: String(level),
          });
        }
        api.panes()[paneIndex]?.setHeight(100);
      }

      handlesRef.current = {
        chart,
        candles,
        ma,
        atrUpper,
        atrLower,
        rsi,
        regime,
        blackout,
        proposals,
      };
    } catch (error) {
      handlesRef.current = null;
      try {
        chart?.remove();
      } catch {
        /* it never finished constructing; nothing to tear down */
      }
      setFailure(error instanceof Error ? error.message : String(error));
      return undefined;
    }

    const built = chart;
    return () => {
      handlesRef.current = null;
      try {
        built.remove();
      } catch {
        /* already disposed */
      }
    };
    // `model` is read for the proposal line identities, which `structure` already tracks;
    // its data is pushed by the effect below so a refetch never rebuilds the chart.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [structure]);

  // --- data (every model change) --------------------------------------------
  useEffect(() => {
    const handles = handlesRef.current;
    if (!handles) return;
    try {
      handles.candles.setData(
        model.bars.map((bar) => ({
          time: asTime(bar.time),
          open: bar.open,
          high: bar.high,
          low: bar.low,
          close: bar.close,
        })),
      );
      handles.ma?.setData(model.ma.map((p) => ({ time: asTime(p.time), value: p.value })));
      handles.atrUpper?.setData(
        model.atrUpper.map((p) => ({ time: asTime(p.time), value: p.value })),
      );
      handles.atrLower?.setData(
        model.atrLower.map((p) => ({ time: asTime(p.time), value: p.value })),
      );
      handles.rsi?.setData(model.rsi.map((p) => ({ time: asTime(p.time), value: p.value })));
      handles.regime?.setData(
        model.regime.map((p) => ({ time: asTime(p.time), value: p.value, color: p.color })),
      );
      handles.blackout?.setData(
        model.blackout.map((p) => ({ time: asTime(p.time), value: p.value, color: p.color })),
      );
      for (const entry of handles.proposals) {
        const line = model.proposalLines.find((l) => l.asset === entry.asset);
        entry.series.setData(
          (line?.points ?? []).map((p) => ({ time: asTime(p.time), value: p.value })),
        );
      }
      createSeriesMarkers(handles.candles, toSeriesMarkers(model.markers));
      const first = handles.proposals[0];
      if (first) createSeriesMarkers(first.series, toSeriesMarkers(model.proposalMarkers));
      handles.chart.timeScale().fitContent();
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error));
    }
  }, [model, structure]);

  return (
    <Box>
      <div
        ref={containerRef}
        data-testid="price-chart"
        data-bars={model.bars.length}
        data-markers={model.markers.length}
        style={{ width: '100%', height: `${height}px` }}
      />
      {failure ? (
        <Alert color="yellow" mt="sm" title="The chart canvas could not start">
          {failure} — the numbers below are unaffected.
        </Alert>
      ) : null}
    </Box>
  );
}

function percent(value: number): string {
  return `${value.toFixed(0)}%`;
}

export default PriceChart;
