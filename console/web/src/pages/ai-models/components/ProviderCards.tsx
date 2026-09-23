import { Alert, Badge, Button, Card, Code, Group, SimpleGrid, Stack, Text } from '@mantine/core';
import { IconAlertTriangle, IconPlugConnected, IconRefresh } from '@tabler/icons-react';

import type { MonthTotals, ProviderCard, RateLimit, TestVerdict } from '../api';

const CIRCUIT_COLOR: Record<string, string> = {
  closed: 'teal',
  half_open: 'yellow',
  open: 'red',
};

function relative(ts: string | null): string {
  if (!ts) return 'never';
  return ts.replace('T', ' ').replace('Z', ' UTC');
}

export interface ProviderCardsProps {
  providers: ProviderCard[];
  rateLimit: RateLimit;
  month: MonthTotals;
  verdicts: Record<string, TestVerdict | undefined>;
  busy: string | null;
  onTest: (key: ProviderCard['key']) => void;
  onReset: (key: ProviderCard['key']) => void;
}

/**
 * One card per provider key. The two Claude cards differ only in the credential they
 * carry, which is why the auth mode is shown on each rather than once at the top: in
 * `auto` both are enabled and either may serve the next call.
 */
export function ProviderCards({
  providers,
  rateLimit,
  month,
  verdicts,
  busy,
  onTest,
  onReset,
}: ProviderCardsProps) {
  return (
    <Stack gap="md">
      {rateLimit.status ? (
        <Alert
          color={rateLimit.status === 'rejected' ? 'red' : 'yellow'}
          icon={<IconAlertTriangle size={18} />}
          title={`Subscription rate limit: ${rateLimit.status}`}
        >
          {rateLimit.utilization != null
            ? `${(rateLimit.utilization * 100).toFixed(0)}% of the window used`
            : 'utilisation unknown'}
          {rateLimit.resets_at ? ` · resets ${relative(rateLimit.resets_at)}` : ''}
        </Alert>
      ) : null}

      <SimpleGrid cols={{ base: 1, md: 3 }} spacing="md">
        {providers.map((provider) => {
          const verdict = verdicts[provider.key];
          return (
            <Card key={provider.key} withBorder padding="md" radius="md">
              <Group justify="space-between" mb={4} wrap="nowrap">
                <Text fw={600}>{provider.key}</Text>
                <Badge color={CIRCUIT_COLOR[provider.circuit] ?? 'gray'} variant="light">
                  {provider.circuit}
                </Badge>
              </Group>

              <Group gap="xs" mb="xs">
                <Badge variant={provider.enabled ? 'filled' : 'outline'} color="blue">
                  {provider.enabled ? 'in the chain' : 'not in this auth mode'}
                </Badge>
                <Badge
                  variant="light"
                  color={provider.credential_present ? 'teal' : 'red'}
                >
                  {provider.credential_present ? 'credential present' : 'no credential'}
                </Badge>
              </Group>

              <Text size="sm" c="dimmed">
                {provider.detail}
              </Text>

              <Stack gap={2} mt="sm">
                <Text size="xs" c="dimmed">
                  last success: {relative(provider.last_ok_utc)}
                </Text>
                {provider.consecutive_failures > 0 ? (
                  <Text size="xs" c="red">
                    {provider.consecutive_failures} consecutive failures
                    {provider.open_until ? ` · open until ${relative(provider.open_until)}` : ''}
                  </Text>
                ) : null}
                {provider.last_error ? (
                  <Code block style={{ fontSize: 11, whiteSpace: 'pre-wrap' }}>
                    {provider.last_error}
                  </Code>
                ) : null}
                {provider.degraded_until ? (
                  <Text size="xs" c="yellow">
                    preferred credential degraded until {relative(provider.degraded_until)}
                  </Text>
                ) : null}
                {month.by_provider[provider.key] != null ? (
                  <Text size="xs" c="dimmed">
                    spent this month: ${month.by_provider[provider.key]?.toFixed(2)}
                  </Text>
                ) : null}
              </Stack>

              <Group gap="xs" mt="md">
                <Button
                  size="xs"
                  variant="light"
                  leftSection={<IconPlugConnected size={14} />}
                  loading={busy === `test:${provider.key}`}
                  onClick={() => onTest(provider.key)}
                >
                  Test
                </Button>
                <Button
                  size="xs"
                  variant="subtle"
                  color="gray"
                  leftSection={<IconRefresh size={14} />}
                  disabled={provider.circuit === 'closed'}
                  loading={busy === `reset:${provider.key}`}
                  onClick={() => onReset(provider.key)}
                >
                  Reset circuit
                </Button>
              </Group>

              {verdict ? (
                <Alert
                  mt="sm"
                  color={verdict.ok ? 'teal' : 'red'}
                  variant="light"
                  p="xs"
                >
                  <Text size="xs">
                    {verdict.detail} ({verdict.latency_ms} ms)
                  </Text>
                </Alert>
              ) : null}
            </Card>
          );
        })}
      </SimpleGrid>
    </Stack>
  );
}

export default ProviderCards;
