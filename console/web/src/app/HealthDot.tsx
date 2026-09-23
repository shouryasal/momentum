import { Indicator, Popover, Stack, Text, UnstyledButton } from '@mantine/core';
import { useState } from 'react';

import type { CheckStatus, HealthSummary } from '../api/contracts';
import { formatRelative } from '../lib/format';
import { STATUS_COLORS } from '../theme';

/** Worst status wins: one red check turns the header dot red. */
export function worstStatus(summary: HealthSummary | null | undefined): CheckStatus {
  if (!summary) return 'unknown';
  const order: CheckStatus[] = ['fail', 'warn', 'unknown', 'ok'];
  for (const status of order) {
    if (summary.status === status) return status;
    if (summary.checks.some((check) => check.status === status)) return status;
  }
  return 'ok';
}

export function HealthDot({ summary }: { summary: HealthSummary | null | undefined }) {
  const [opened, setOpened] = useState(false);
  const status = worstStatus(summary);
  return (
    <Popover opened={opened} onChange={setOpened} width={300} withArrow position="bottom-end">
      <Popover.Target>
        <UnstyledButton
          onClick={() => setOpened((value) => !value)}
          aria-label={`health ${status}`}
          data-testid="health-dot"
          data-status={status}
        >
          <Indicator color={STATUS_COLORS[status] ?? 'gray'} size={12} processing={status === 'warn'}>
            <Text size="xs" c="dimmed" px={4}>
              health
            </Text>
          </Indicator>
        </UnstyledButton>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap={4}>
          {!summary ? (
            <Text size="sm" c="dimmed">
              No health report yet.
            </Text>
          ) : (
            <>
              {summary.checks.map((check) => (
                <Text key={check.key} size="sm" c={STATUS_COLORS[check.status] ?? 'gray'}>
                  {check.label}: {check.status}
                  {check.detail ? ` — ${check.detail}` : ''}
                </Text>
              ))}
              <Text size="xs" c="dimmed">
                updated {formatRelative(summary.ts)}
              </Text>
            </>
          )}
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}
