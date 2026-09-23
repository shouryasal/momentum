/**
 * The conversion funnel as six proportional bars with the drop-off between them.
 *
 * Deliberately not a chart library: the shape is six numbers and what matters is the
 * CONVERSION between stages, which a bar plus "→ 42%" says more honestly than an area.
 */
import { Box, Group, Skeleton, Stack, Text, Tooltip } from '@mantine/core';

import type { FunnelCounts } from '../api';

const STAGES: Array<{ key: keyof FunnelCounts; label: string; color: string; hint: string }> = [
  { key: 'detected', label: 'Detected', color: 'var(--mantine-color-gray-5)', hint: 'Deterministic detector hits after dedupe' },
  { key: 'screened', label: 'Screened', color: 'var(--mantine-color-blue-5)', hint: 'Kept by the cheap screener (or scored on the detector alone)' },
  { key: 'validated', label: 'Validated', color: 'var(--mantine-color-indigo-5)', hint: 'Reached the strong validator and got a verdict' },
  { key: 'valid', label: 'Valid', color: 'var(--mantine-color-teal-5)', hint: 'Verdict valid at or above min_confidence' },
  { key: 'planned', label: 'Planned', color: 'var(--mantine-color-violet-5)', hint: 'Passed the guards and fired a research run' },
  { key: 'acted', label: 'Acted', color: 'var(--mantine-color-green-6)', hint: 'A proposal was written carrying this signal' },
];

function pct(value: number, of: number): string {
  if (!of) return '—';
  return `${Math.round((value / of) * 100)}%`;
}

export function FunnelBar({ counts, loading }: { counts?: FunnelCounts; loading?: boolean }) {
  if (loading) return <Skeleton height={140} radius="sm" />;
  if (!counts) return null;
  const top = Math.max(counts.detected, 1);

  return (
    <Stack gap={6} data-testid="signal-funnel">
      {STAGES.map((stage, index) => {
        const value = counts[stage.key];
        const previous = index === 0 ? null : counts[STAGES[index - 1]!.key];
        const width = Math.max((value / top) * 100, value > 0 ? 2 : 0);
        return (
          <Group key={stage.key} gap="sm" wrap="nowrap" align="center">
            <Text size="sm" w={90} ta="right" c="dimmed">
              {stage.label}
            </Text>
            <Tooltip label={stage.hint} withArrow>
              <Box style={{ flex: 1, minWidth: 0 }}>
                <Box
                  style={{
                    width: `${width}%`,
                    height: 16,
                    borderRadius: 4,
                    background: stage.color,
                    transition: 'width 200ms ease',
                  }}
                />
              </Box>
            </Tooltip>
            <Text size="sm" w={50} ff="monospace" ta="right">
              {value}
            </Text>
            <Text size="xs" w={60} c="dimmed" ta="right">
              {previous === null ? '' : `→ ${pct(value, previous)}`}
            </Text>
          </Group>
        );
      })}
      <Group gap="lg" pl={98} pt={4}>
        <Text size="xs" c="dimmed">
          screened out {counts.screened_out}
        </Text>
        <Text size="xs" c="dimmed">
          expired {counts.expired}
        </Text>
        <Text size="xs" c={counts.errors ? 'red' : 'dimmed'}>
          errors {counts.errors}
        </Text>
      </Group>
    </Stack>
  );
}
