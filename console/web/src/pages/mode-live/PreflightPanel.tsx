import {
  Accordion,
  Alert,
  Badge,
  Button,
  Group,
  Loader,
  Stack,
  Text,
  Textarea,
  Title,
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconCheck,
  IconMinus,
  IconRefresh,
  IconX,
} from '@tabler/icons-react';
import { useState } from 'react';

import { JsonViewer } from '@/components';

import { statusColour, type PreflightItem, type PreflightResponse } from './api';

function StatusIcon({ status }: { status: string }) {
  if (status === 'pass') return <IconCheck size={16} color="var(--mantine-color-teal-6)" />;
  if (status === 'warn') return <IconAlertTriangle size={16} color="var(--mantine-color-yellow-6)" />;
  if (status === 'fail') return <IconX size={16} color="var(--mantine-color-red-6)" />;
  return <IconMinus size={16} color="var(--mantine-color-gray-5)" />;
}

export interface PreflightPanelProps {
  result: PreflightResponse | null;
  running: boolean;
  error?: string | null;
  overrideReason: string;
  onOverrideReason: (value: string) => void;
  onRun: () => void;
  expiresIn: number | null;
}

/**
 * The preflight checklist.
 *
 * Every item shows its own evidence, because the operator is being asked to accept
 * responsibility for going live: "all green" is not an argument, the twelve facts behind
 * it are. A failing item that can be overridden (`track_record`) reveals the reason box
 * rather than an "ignore" button — an override with no written reason is not an override,
 * it is an accident waiting to be repeated.
 */
export function PreflightPanel({
  result,
  running,
  error,
  overrideReason,
  onOverrideReason,
  onRun,
  expiresIn,
}: PreflightPanelProps) {
  const [open, setOpen] = useState<string[]>([]);
  const items = result?.items ?? [];
  const blocking = items.filter((item) => item.blocking && item.status === 'fail');
  const warnings = items.filter((item) => item.status === 'warn');
  const overridable = blocking.some((item) => item.id === 'track_record');

  return (
    <Stack gap="sm">
      <Group justify="space-between">
        <Title order={5}>Preflight</Title>
        <Group gap="xs">
          {expiresIn !== null && result ? (
            <Badge color={expiresIn > 60 ? 'gray' : 'orange'} variant="light">
              {expiresIn > 0 ? `valid ${Math.floor(expiresIn / 60)}m ${expiresIn % 60}s` : 'expired'}
            </Badge>
          ) : null}
          <Button
            size="xs"
            variant="light"
            leftSection={running ? <Loader size={12} /> : <IconRefresh size={14} />}
            onClick={onRun}
            disabled={running}
          >
            {result ? 'Re-run' : 'Run preflight'}
          </Button>
        </Group>
      </Group>

      {error ? (
        <Alert color="red" variant="light" title="Preflight could not run">
          {error}
        </Alert>
      ) : null}

      {!result && !running ? (
        <Text size="sm" c="dimmed">
          Nothing has been probed yet. The preflight is read-only: it reads the bots, the
          exchange, the host and the journal, and changes nothing.
        </Text>
      ) : null}

      {result ? (
        <>
          <Alert
            color={result.ok ? 'teal' : 'red'}
            variant="light"
            title={
              result.ok
                ? 'Every blocking item passes'
                : `${blocking.length} blocking item${blocking.length === 1 ? '' : 's'} failed`
            }
          >
            {result.ok
              ? `${warnings.length} warning${warnings.length === 1 ? '' : 's'}. The transition re-runs these checks before it touches anything.`
              : blocking.map((item) => item.title).join('; ')}
          </Alert>

          <Accordion multiple value={open} onChange={setOpen} variant="contained">
            {items.map((item) => (
              <PreflightRow key={item.id} item={item} />
            ))}
          </Accordion>

          {overridable ? (
            <Textarea
              label="Override reason (track record only)"
              description="Typed, audited, and shown on the run forever. Nothing else is overridable."
              placeholder="Why is it safe to go live with a shorter track record?"
              value={overrideReason}
              onChange={(event) => onOverrideReason(event.currentTarget.value)}
              minRows={2}
              autosize
            />
          ) : null}
        </>
      ) : null}
    </Stack>
  );
}

function PreflightRow({ item }: { item: PreflightItem }) {
  return (
    <Accordion.Item value={item.id}>
      <Accordion.Control icon={<StatusIcon status={item.status} />}>
        <Group justify="space-between" wrap="nowrap">
          <Text size="sm">{item.title}</Text>
          <Group gap={6} wrap="nowrap">
            {item.overridden ? (
              <Badge size="xs" color="orange" variant="filled">
                overridden
              </Badge>
            ) : null}
            <Badge size="xs" color={statusColour(item.status)} variant="light">
              {item.status}
            </Badge>
            {item.blocking ? (
              <Badge size="xs" variant="outline" color="gray">
                blocking
              </Badge>
            ) : null}
          </Group>
        </Group>
      </Accordion.Control>
      <Accordion.Panel>
        <Stack gap="xs">
          <Text size="sm">{item.detail}</Text>
          {Object.keys(item.evidence ?? {}).length > 0 ? (
            <JsonViewer value={item.evidence} />
          ) : null}
        </Stack>
      </Accordion.Panel>
    </Accordion.Item>
  );
}
