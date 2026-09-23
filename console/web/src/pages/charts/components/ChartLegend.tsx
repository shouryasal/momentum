import { Box, Group, Text, Tooltip } from '@mantine/core';

import {
  BLACKOUT_SHADE,
  EXIT_COLOR,
  FILL_COLOR,
  GATE_REJECT_COLOR,
  REGIME_SHADE,
  UNKNOWN_COLOR,
  VERDICT_COLOR,
  type ChartModel,
} from '../model';

/**
 * The chart's vocabulary, spelled out.
 *
 * A candlestick with six kinds of mark on it is unreadable without a key, and the one
 * distinction that must never be guessed at is SIM versus LIVE — a pale circle is money
 * that was never at risk, a solid arrow is money that was.
 */

export interface LegendItem {
  key: string;
  label: string;
  color: string;
  /** Rendered hollow, the way simulated fills are drawn. */
  hollow?: boolean;
  shape?: 'dot' | 'square' | 'band';
  help?: string;
  count?: number;
}

export function legendItems(model?: ChartModel): LegendItem[] {
  const counts = model?.counts;
  const items: LegendItem[] = [
    {
      key: 'buy-live',
      label: 'Buy (LIVE)',
      color: FILL_COLOR.buyLive,
      help: 'A real order filled on the exchange.',
    },
    {
      key: 'sell-live',
      label: 'Sell (LIVE)',
      color: FILL_COLOR.sellLive,
      help: 'A real order filled on the exchange.',
    },
    {
      key: 'buy-sim',
      label: 'Buy (SIM)',
      color: FILL_COLOR.buySim,
      hollow: true,
      help: 'Dry-run fill on live market data — no money moved.',
    },
    {
      key: 'sell-sim',
      label: 'Sell (SIM)',
      color: FILL_COLOR.sellSim,
      hollow: true,
      help: 'Dry-run fill on live market data — no money moved.',
    },
    {
      key: 'stop',
      label: 'Stop exit',
      color: EXIT_COLOR.stop,
      shape: 'square',
      help: 'Stop exits are market orders — an invariant, not a setting.',
    },
    {
      key: 'take_profit',
      label: 'Take-profit exit',
      color: EXIT_COLOR.take_profit,
      shape: 'square',
    },
    {
      key: 'gate',
      label: 'Gate reject',
      color: GATE_REJECT_COLOR,
      shape: 'square',
      help: 'The risk gate refused the order; the label is the failing check.',
      count: counts?.gate,
    },
    {
      key: 'valid',
      label: 'Signal: valid',
      color: VERDICT_COLOR.valid,
      help: 'Signals are coloured by the validator verdict, not the detector score.',
    },
    { key: 'uncertain', label: 'Signal: uncertain', color: VERDICT_COLOR.uncertain },
    { key: 'invalid', label: 'Signal: invalid', color: VERDICT_COLOR.invalid },
    { key: 'screened_out', label: 'Signal: screened out', color: VERDICT_COLOR.screened_out },
    {
      key: 'regime-up',
      label: 'Regime up',
      color: REGIME_SHADE.up,
      shape: 'band',
      help: 'Shading behind the candles is the computed market regime at that bar.',
    },
    { key: 'regime-down', label: 'Regime down', color: REGIME_SHADE.down, shape: 'band' },
    {
      key: 'blackout',
      label: 'Blackout',
      color: BLACKOUT_SHADE,
      shape: 'band',
      help: 'A block_entries flag was in force — the gate refused every entry.',
    },
  ];
  return items;
}

function Swatch({ item }: { item: LegendItem }) {
  const shape = item.shape ?? 'dot';
  const size = shape === 'band' ? 14 : 10;
  return (
    <Box
      w={shape === 'band' ? 18 : size}
      h={size}
      style={{
        borderRadius: shape === 'dot' ? size : 2,
        background: item.hollow ? 'transparent' : (item.color || UNKNOWN_COLOR),
        border: `2px solid ${item.color || UNKNOWN_COLOR}`,
        flexShrink: 0,
      }}
    />
  );
}

export function ChartLegend({ model }: { model?: ChartModel }) {
  const items = legendItems(model);
  return (
    <Group gap="sm" wrap="wrap" data-testid="chart-legend">
      {items.map((item) => {
        const label = (
          <Group gap={5} wrap="nowrap">
            <Swatch item={item} />
            <Text size="xs" c="dimmed">
              {item.label}
              {item.count ? ` (${item.count})` : ''}
            </Text>
          </Group>
        );
        return item.help ? (
          <Tooltip key={item.key} label={item.help} multiline w={240}>
            {label}
          </Tooltip>
        ) : (
          <Box key={item.key}>{label}</Box>
        );
      })}
    </Group>
  );
}

export default ChartLegend;
