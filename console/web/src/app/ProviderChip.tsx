import { Badge, Popover, Stack, Text } from '@mantine/core';
import { useState } from 'react';

import { providerStatus, type ProviderCard, type ProvidersResponse } from '../api/contracts';
import { formatPct } from '../lib/format';
import { STATUS_COLORS } from '../theme';

/** `claude:subscription` -> `Claude subscription`; `ollama` -> `Ollama`. */
export function providerLabel(card: ProviderCard): string {
  const words = card.key.split(/[:_-]/).filter(Boolean);
  const first = words[0] ?? card.key;
  return [first.charAt(0).toUpperCase() + first.slice(1), ...words.slice(1)].join(' ');
}

function short(card: ProviderCard): string {
  const status = providerStatus(card);
  const mark = status === 'ok' ? '✓' : status === 'fail' ? '✗' : status === 'warn' ? '!' : '–';
  return `${providerLabel(card)} ${mark}`;
}

/** `Claude subscription ✓ · Ollama ✓ · RL 42%` (spec 12). */
export function ProviderChip({ data }: { data: ProvidersResponse | null | undefined }) {
  const [opened, setOpened] = useState(false);
  const providers = data?.providers ?? [];
  if (providers.length === 0) {
    return (
      <Badge variant="light" color="gray" data-testid="provider-chip">
        providers unknown
      </Badge>
    );
  }

  const statuses = providers.map(providerStatus);
  // A provider that is switched off is not a failure, so only enabled ones set the colour.
  const worst = statuses.includes('fail') ? 'fail' : statuses.includes('warn') ? 'warn' : 'ok';
  // `utilization` is a fraction of the rate-limit window (SDK RateLimitEvent), not a percent.
  const utilisation = data?.rate_limit?.utilization ?? null;
  const rate = utilisation === null ? '' : ` · RL ${formatPct(utilisation * 100, 0)}`;

  return (
    <Popover opened={opened} onChange={setOpened} width={340} withArrow position="bottom-end">
      <Popover.Target>
        <Badge
          variant="light"
          color={STATUS_COLORS[worst]}
          style={{ cursor: 'pointer' }}
          onClick={() => setOpened((value) => !value)}
          data-testid="provider-chip"
          data-status={worst}
        >
          {providers.filter((card) => card.enabled).map(short).join(' · ') || 'no provider enabled'}
          {rate}
        </Badge>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap={4}>
          {data?.auth_mode ? (
            <Text size="xs" c="dimmed">
              auth mode: {data.auth_mode}
            </Text>
          ) : null}
          {providers.map((card) => {
            const status = providerStatus(card);
            return (
              <Text key={card.key} size="sm" c={STATUS_COLORS[status] ?? 'gray'}>
                {providerLabel(card)}: {card.enabled ? status : 'disabled'}
                {card.circuit === 'open' ? ' · breaker open' : ''}
                {card.degraded_until ? ` · degraded until ${card.degraded_until}` : ''}
                {card.detail ? ` — ${card.detail}` : ''}
              </Text>
            );
          })}
          {data?.rate_limit?.resets_at ? (
            <Text size="xs" c="dimmed">
              rate limit {data.rate_limit.status ?? 'unknown'}, resets {data.rate_limit.resets_at}
            </Text>
          ) : null}
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}
