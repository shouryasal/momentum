import {
  Alert, Badge, Button, Card, Code, Group, Select, Stack, Table, Text, Textarea, Title,
} from '@mantine/core';
import { IconPlayerPlay } from '@tabler/icons-react';
import { useState } from 'react';

import type { PlaygroundResult, RoutingResponse } from '../api';

export interface PlaygroundProps {
  routing: RoutingResponse | undefined;
  result: PlaygroundResult | undefined;
  running: boolean;
  error: string | null;
  onRun: (body: { task: string; prompt: string; model_ref?: string }) => void;
}

/**
 * One routed call, on demand.
 *
 * It uses the task's real chain and real budgets so what you see here is what 08:30 will
 * do — but with tools forced off and no artefacts written, so it can never produce a
 * proposal, a signal or a file. The attempts table is the point: it shows which chain
 * entry served and what the ones before it failed with.
 */
export function Playground({ routing, result, running, error, onRun }: PlaygroundProps) {
  const [task, setTask] = useState<string | null>('scan');
  const [modelRef, setModelRef] = useState<string | null>(null);
  const [prompt, setPrompt] = useState('');

  const tasks = (routing?.tasks ?? []).map((row) => row.task);
  const models = Object.entries(routing?.models ?? {})
    .filter(([, entry]) => entry != null)
    .map(([alias, entry]) => ({
      value: alias,
      label: `${alias} — ${entry?.id ?? ''} (tier ${entry?.tier ?? '?'})`,
    }));
  const selected = routing?.tasks.find((row) => row.task === task);

  return (
    <Stack gap="md">
      <Card withBorder padding="md" radius="md">
        <Title order={5} mb="sm">Playground</Title>
        <Group grow align="flex-start" mb="sm">
          <Select
            label="task"
            description="routing, budgets and the failure policy of this task"
            data={tasks}
            value={task}
            onChange={setTask}
            searchable
          />
          <Select
            label="pin a model (optional)"
            description="skips the chain and calls exactly this alias"
            data={models}
            value={modelRef}
            onChange={setModelRef}
            clearable
            searchable
          />
        </Group>
        <Textarea
          label="prompt"
          placeholder="Ask exactly what the stage prompt would ask."
          minRows={6}
          autosize
          value={prompt}
          onChange={(event) => setPrompt(event.currentTarget.value)}
        />
        <Group justify="space-between" mt="sm">
          <Text size="xs" c="dimmed">
            {selected
              ? `tools forced to none · cap $${selected.max_usd_per_run ?? '?'} per run · ` +
                `no artefacts are written`
              : 'tools forced to none · no artefacts are written'}
          </Text>
          <Button
            leftSection={<IconPlayerPlay size={16} />}
            loading={running}
            disabled={!task || !prompt.trim()}
            onClick={() =>
              onRun({
                task: task as string,
                prompt,
                ...(modelRef ? { model_ref: modelRef } : {}),
              })
            }
          >
            Run
          </Button>
        </Group>
      </Card>

      {error ? <Alert color="red" title="The call failed">{error}</Alert> : null}

      {result ? (
        <Card withBorder padding="md" radius="md">
          <Group justify="space-between" mb="sm">
            <Group gap="xs">
              <Title order={6}>Result</Title>
              {result.served ? (
                <Badge color={result.switched ? 'orange' : 'teal'} variant="light">
                  served by {result.served}
                  {result.switched ? ' (after a switch)' : ''}
                </Badge>
              ) : (
                <Badge color="red" variant="light">
                  {result.failure ?? 'failed'} → {result.fallback_action ?? 'abstain'}
                </Badge>
              )}
            </Group>
          </Group>

          {result.text ? (
            <Code block style={{ whiteSpace: 'pre-wrap', maxHeight: 320, overflow: 'auto' }}>
              {result.text}
            </Code>
          ) : null}

          <Table mt="md" withRowBorders={false} data-testid="playground-attempts">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>#</Table.Th>
                <Table.Th>model</Table.Th>
                <Table.Th>status</Table.Th>
                <Table.Th ta="right">ms</Table.Th>
                <Table.Th ta="right">cost</Table.Th>
                <Table.Th>error</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {result.attempts.map((attempt, index) => (
                <Table.Tr key={`${attempt.model}-${index}`}>
                  <Table.Td>{attempt.idx}</Table.Td>
                  <Table.Td><Code>{attempt.model}</Code></Table.Td>
                  <Table.Td>
                    <Badge
                      size="sm"
                      variant="light"
                      color={attempt.status === 'ok' ? 'teal' : 'red'}
                    >
                      {attempt.status}
                    </Badge>
                  </Table.Td>
                  <Table.Td ta="right">{attempt.latency_ms}</Table.Td>
                  <Table.Td ta="right">
                    {attempt.cost_usd != null ? `$${attempt.cost_usd.toFixed(4)}` : '—'}
                  </Table.Td>
                  <Table.Td>
                    <Text size="xs" c="dimmed">{attempt.error ?? ''}</Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Card>
      ) : null}
    </Stack>
  );
}

export default Playground;
