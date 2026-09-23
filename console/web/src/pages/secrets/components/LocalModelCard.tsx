/**
 * The local model, as one line the operator reads and never edits.
 *
 * The owner's instruction was blunt: *for other local LLMs we should connect from the
 * backend*. So there is no base-URL box and no model box here, and there is no route
 * behind them either — `GET /api/llm/local-model` is read-only, and the server does the
 * three things a person used to do by hand:
 *
 *   * finds the endpoint (inside WSL2, `127.0.0.1` is **not** the Windows host, so the
 *     default gateway and `host.docker.internal` are probed too),
 *   * uses whichever model `models.yaml` already routes local tasks to,
 *   * downloads it if it is missing, and says so while it does.
 *
 * The one thing the operator may have to act on is a daemon that is not listening where
 * WSL can see it — and then this card shows exactly one command, for exactly one shell.
 */
import { Alert, Badge, Card, Group, Loader, Progress, Stack, Text, Title } from '@mantine/core';
import { IconCpu } from '@tabler/icons-react';

import type { LocalModelState, LocalModelStatus } from '../api';

import { CommandLine } from './ClaudeSignInModal';

export interface LocalModelCardProps {
  status: LocalModelStatus | undefined;
  loading?: boolean;
  /** The developer view also lists the addresses that were probed. */
  detailed?: boolean;
}

const COLOR: Record<LocalModelState, string> = {
  connected: 'teal',
  pulling: 'blue',
  missing: 'yellow',
  unreachable: 'orange',
  disabled: 'gray',
  unknown: 'gray',
};

export function LocalModelCard({ status, loading = false, detailed = false }: LocalModelCardProps) {
  const state = status?.state ?? 'unknown';
  const pull = status?.pull;

  return (
    <Card withBorder padding="md" radius="md" data-testid="local-model">
      <Group justify="space-between" align="flex-start" wrap="wrap">
        <Stack gap={4} style={{ flex: 1, minWidth: 260 }}>
          <Group gap="xs">
            <IconCpu size={16} />
            <Title order={5}>Local model</Title>
            <Badge variant="light" color={COLOR[state]} data-testid="local-model-state">
              {state}
            </Badge>
            {loading && !status ? <Loader size="xs" /> : null}
          </Group>
          <Text size="sm" data-testid="local-model-line">
            {status?.line ?? 'checking the local model…'}
          </Text>
          {status?.base_url ? (
            <Text size="xs" c="dimmed">
              {status.base_url}
              {status.version ? ` · ollama ${status.version}` : ''}
              {status.model ? ` · ${status.model}` : ''}
            </Text>
          ) : null}
        </Stack>
      </Group>

      {state === 'pulling' && pull ? (
        <Stack gap={4} mt="sm">
          <Progress value={Math.round((pull.progress ?? 0) * 100)} animated />
          <Text size="xs" c="dimmed">{pull.message ?? 'downloading'}</Text>
        </Stack>
      ) : null}

      {status?.reason && state !== 'connected' ? (
        <Alert color={COLOR[state]} variant="light" mt="sm" data-testid="local-model-reason">
          <Stack gap="xs">
            <Text size="sm">{status.reason}</Text>
            {status.fix_command ? (
              <CommandLine command={status.fix_command} shell={status.fix_shell ?? 'PowerShell'} />
            ) : null}
            {status.fix_shell ? (
              <Text size="xs" c="dimmed">
                Run it in <b>{status.fix_shell}</b>, then this line fixes itself — there is
                nothing to type into the console.
              </Text>
            ) : null}
          </Stack>
        </Alert>
      ) : null}

      <Text size="xs" c="dimmed" mt="sm">
        Read-only by design: the console detects the endpoint, uses the model the routing
        config already points at, and downloads it if it is missing. Nothing here is a
        setting.
      </Text>

      {detailed && status?.tried?.length ? (
        <Text size="xs" c="dimmed" mt={4} data-testid="local-model-tried">
          probed: {status.tried.join(', ')}
        </Text>
      ) : null}
    </Card>
  );
}

export default LocalModelCard;
