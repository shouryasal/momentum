/**
 * The proposals timeline as stacked target weights (spec 12 page 8).
 *
 * One row per proposal, each a 100%-wide stack of the target weights, newest first. A
 * stacked bar rather than a chart because the weights always sum to 1: what a reader wants
 * is "how did the mix change", and side-by-side stacks answer that without an axis. An
 * abstain is drawn dimmed — it kept the previous targets rather than choosing these.
 */
import { Badge, Box, Group, Loader, Paper, Stack, Text, Tooltip } from '@mantine/core';

import { EmptyState } from '@/components';
import { formatNumber, formatUtcStamp } from '@/lib/format';

import type { ProposalRow } from '../api';

const COLORS: Record<string, string> = {
  BTC: 'var(--mantine-color-orange-6)',
  ETH: 'var(--mantine-color-indigo-5)',
  USDT: 'var(--mantine-color-gray-5)',
};

function colorFor(asset: string, index: number): string {
  return (
    COLORS[asset] ??
    ['var(--mantine-color-teal-5)', 'var(--mantine-color-grape-5)', 'var(--mantine-color-cyan-5)'][
      index % 3
    ]!
  );
}

export interface TargetsTimelineProps {
  proposals: ProposalRow[];
  loading?: boolean;
  onSelect?: (runId: string) => void;
}

export function TargetsTimeline({ proposals, loading, onSelect }: TargetsTimelineProps) {
  if (loading) return <Loader />;
  if (proposals.length === 0) {
    return (
      <EmptyState
        title="No proposals in this window"
        description="A proposal is written only when the decide stage produces one that passes host validation."
      />
    );
  }

  const assets = Array.from(
    new Set(proposals.flatMap((p) => Object.keys(p.targets ?? {}))),
  ).sort();

  return (
    <Stack gap="xs" data-testid="targets-timeline">
      <Group gap="md">
        {assets.map((asset, index) => (
          <Group key={asset} gap={6}>
            <Box w={12} h={12} style={{ background: colorFor(asset, index), borderRadius: 2 }} />
            <Text size="xs" c="dimmed">
              {asset}
            </Text>
          </Group>
        ))}
      </Group>

      {proposals.map((p) => {
        const entries = assets.map((asset) => [asset, p.targets?.[asset] ?? 0] as const);
        const total = entries.reduce((sum, [, value]) => sum + value, 0) || 1;
        return (
          <Paper
            key={`${p.run_id}-${p.shadow ? 'shadow' : 'live'}`}
            withBorder
            p="xs"
            radius="sm"
            style={onSelect ? { cursor: 'pointer' } : undefined}
            onClick={() => onSelect?.(p.run_id)}
            opacity={p.abstain ? 0.55 : 1}
          >
            <Group gap="sm" wrap="nowrap" align="center">
              <Stack gap={0} w={190}>
                <Text size="sm" ff="monospace">
                  {p.run_id}
                </Text>
                <Text size="xs" c="dimmed">
                  {formatUtcStamp(p.ts_utc)}
                </Text>
              </Stack>

              <Group gap={4} w={200} wrap="nowrap">
                <Badge size="xs" variant="light">
                  {p.module ?? '—'}
                </Badge>
                {p.abstain ? (
                  <Badge size="xs" color="gray">
                    abstain
                  </Badge>
                ) : null}
                {p.shadow ? (
                  <Badge size="xs" color="grape" variant="light">
                    shadow
                  </Badge>
                ) : null}
                {p.signal_id ? (
                  <Badge size="xs" color="violet" variant="light">
                    signal
                  </Badge>
                ) : null}
                {!p.valid ? (
                  <Badge size="xs" color="red">
                    invalid
                  </Badge>
                ) : null}
              </Group>

              <Box style={{ flex: 1, minWidth: 120, display: 'flex', height: 18, borderRadius: 4, overflow: 'hidden' }}>
                {entries.map(([asset, value], index) => (
                  <Tooltip key={asset} label={`${asset} ${(value * 100).toFixed(1)}%`} withArrow>
                    <Box
                      style={{
                        width: `${(value / total) * 100}%`,
                        background: colorFor(asset, index),
                      }}
                    />
                  </Tooltip>
                ))}
              </Box>

              <Text size="xs" c="dimmed" w={110} ta="right">
                conf {formatNumber(p.confidence, 2)} · x{formatNumber(p.exposure_scale, 2)}
              </Text>
            </Group>
          </Paper>
        );
      })}
    </Stack>
  );
}
