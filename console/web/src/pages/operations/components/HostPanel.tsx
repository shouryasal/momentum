import { Alert, Badge, Card, Code, Group, Stack, Text } from '@mantine/core';
import { IconAlertTriangle, IconCheck } from '@tabler/icons-react';

import type { CheckStatus, HostSummary } from '../api';

const COLOUR: Record<CheckStatus, string> = { ok: 'teal', warn: 'yellow', fail: 'red' };

/**
 * Host readiness: ext4, sleep policy, the WSL keep-alive task, timezone, docker, systemd,
 * NTP and disk.
 *
 * This panel exists because the failure it catches is invisible from inside the app: if
 * Windows sleeps on AC, or the WSL VM idles out, or the checkout sits on a OneDrive path,
 * the bots stop (or the database corrupts) while the exchange keeps trading. A `blocking`
 * failure here is the same failure the live preflight refuses on.
 */
export function HostPanel({ host, loading }: { host: HostSummary | null; loading: boolean }) {
  if (loading || !host) return <Text c="dimmed">Checking the host…</Text>;

  return (
    <Stack gap="sm">
      {host.ok ? (
        <Alert color="teal" icon={<IconCheck size={16} />} title="Host is fit to run unattended">
          No blocking problem. Warnings below are worth fixing but do not stop a live run.
        </Alert>
      ) : (
        <Alert
          color="red"
          icon={<IconAlertTriangle size={16} />}
          title={`${host.blocking_failures.length} blocking problem(s): ${host.blocking_failures.join(', ')}`}
        >
          The live preflight refuses while any of these fail.
        </Alert>
      )}

      <Stack gap="xs">
        {host.checks.map((check) => (
          <Card key={check.name} withBorder padding="sm" radius="md">
            <Group justify="space-between" align="flex-start" wrap="nowrap">
              <Stack gap={2} style={{ minWidth: 0 }}>
                <Group gap="xs">
                  <Text fw={600}>{check.name}</Text>
                  {check.blocking ? (
                    <Badge size="xs" variant="outline" color="gray">
                      blocks live
                    </Badge>
                  ) : null}
                </Group>
                <Text size="sm" c="dimmed">
                  {check.detail}
                </Text>
                {check.fix && check.status !== 'ok' ? (
                  <Code block mt={4}>
                    {check.fix}
                  </Code>
                ) : null}
              </Stack>
              <Badge color={COLOUR[check.status]}>{check.status}</Badge>
            </Group>
          </Card>
        ))}
      </Stack>
    </Stack>
  );
}

export default HostPanel;
