import {
  Accordion, Alert, Badge, Button, Card, Code, CopyButton, Group, Stack, Table, Text,
  TextInput, Title,
} from '@mantine/core';
import { IconCheck, IconCopy, IconDownload, IconNetwork } from '@tabler/icons-react';
import { useState } from 'react';

import { EmptyState } from '@/components';

import type { OllamaDetection, OllamaModel } from '../api';

export interface OllamaPanelProps {
  detection: OllamaDetection | undefined;
  models: OllamaModel[];
  loading: boolean;
  pulling: boolean;
  onDetect: () => void;
  onPull: (model: string) => void;
}

/**
 * The local-model panel — and, when nothing answers, the fix.
 *
 * Ollama runs on the Windows host; inside WSL2's NAT network `127.0.0.1` is not the host
 * and Ollama binds loopback by default, so "not detected" is the normal first state on a
 * fresh machine. The guidance block is therefore not an error message but the two
 * PowerShell recipes that make it reachable, rendered so they can be copied verbatim.
 */
export function OllamaPanel({
  detection,
  models,
  loading,
  pulling,
  onDetect,
  onPull,
}: OllamaPanelProps) {
  const [name, setName] = useState('llama3.1:8b');
  const guidance = detection?.guidance;

  return (
    <Stack gap="md">
      <Card withBorder padding="md" radius="md">
        <Group justify="space-between" mb="sm">
          <Group gap="xs">
            <IconNetwork size={18} />
            <Title order={5}>Local endpoint</Title>
            {detection?.ok ? (
              <Badge color="teal" variant="light">
                {detection.base_url} · v{detection.version ?? '?'}
              </Badge>
            ) : (
              <Badge color="red" variant="light">not detected</Badge>
            )}
          </Group>
          <Button size="xs" variant="light" loading={loading} onClick={onDetect}>
            Re-probe
          </Button>
        </Group>

        <Table withRowBorders={false} data-testid="ollama-probes">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>candidate</Table.Th>
              <Table.Th>result</Table.Th>
              <Table.Th ta="right">ms</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {(detection?.results ?? []).map((probe) => (
              <Table.Tr key={probe.url}>
                <Table.Td><Code>{probe.url}</Code></Table.Td>
                <Table.Td>
                  {probe.ok ? (
                    <Badge color="teal" variant="light">v{probe.version ?? 'ok'}</Badge>
                  ) : (
                    <Text size="xs" c="dimmed">{probe.error}</Text>
                  )}
                </Table.Td>
                <Table.Td ta="right">{probe.latency_ms}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
        {guidance?.wsl_gateway ? (
          <Text size="xs" c="dimmed" mt="xs">
            WSL default gateway (the Windows host on this NAT network):{' '}
            <Code>{guidance.wsl_gateway}</Code>
          </Text>
        ) : null}
      </Card>

      {guidance && !guidance.reachable ? (
        <Alert color="yellow" title="Ollama is not reachable from WSL" variant="light">
          <Text size="sm" mb="sm">{guidance.problem}</Text>
          <Accordion variant="contained">
            {guidance.options.map((option) => (
              <Accordion.Item key={option.title} value={option.title}>
                <Accordion.Control>{option.title}</Accordion.Control>
                <Accordion.Panel>
                  <Text size="xs" c="dimmed" mb={4}>{option.shell}</Text>
                  <Stack gap={4}>
                    {option.commands.map((command) => (
                      <Group key={command} gap="xs" wrap="nowrap" align="flex-start">
                        <Code block style={{ flex: 1, whiteSpace: 'pre-wrap' }}>
                          {command}
                        </Code>
                        <CopyButton value={command}>
                          {({ copied, copy }) => (
                            <Button
                              size="compact-xs"
                              variant="subtle"
                              onClick={copy}
                              leftSection={
                                copied ? <IconCheck size={12} /> : <IconCopy size={12} />
                              }
                            >
                              {copied ? 'copied' : 'copy'}
                            </Button>
                          )}
                        </CopyButton>
                      </Group>
                    ))}
                  </Stack>
                  <Text size="xs" mt="xs">{option.then}</Text>
                </Accordion.Panel>
              </Accordion.Item>
            ))}
          </Accordion>
          <Text size="xs" c="dimmed" mt="sm">{guidance.note}</Text>
        </Alert>
      ) : null}

      <Card withBorder padding="md" radius="md">
        <Group justify="space-between" mb="sm">
          <Title order={5}>Models on this endpoint</Title>
          <Group gap="xs">
            <TextInput
              size="xs"
              value={name}
              onChange={(event) => setName(event.currentTarget.value)}
              placeholder="llama3.1:8b"
              aria-label="model to pull"
            />
            <Button
              size="xs"
              leftSection={<IconDownload size={14} />}
              loading={pulling}
              disabled={!detection?.ok || !name.trim()}
              onClick={() => onPull(name.trim())}
            >
              Pull
            </Button>
          </Group>
        </Group>
        {models.length === 0 ? (
          <EmptyState
            title="No models pulled"
            description="Pull one above; progress streams on the job topic."
          />
        ) : (
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>model</Table.Th>
                <Table.Th>params</Table.Th>
                <Table.Th>quant</Table.Th>
                <Table.Th>size</Table.Th>
                <Table.Th>declared</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {models.map((model) => (
                <Table.Tr key={model.name}>
                  <Table.Td><Code>{model.name}</Code></Table.Td>
                  <Table.Td>{model.parameter_size ?? '—'}</Table.Td>
                  <Table.Td>{model.quantization ?? '—'}</Table.Td>
                  <Table.Td>
                    {model.size ? `${(model.size / 1e9).toFixed(1)} GB` : '—'}
                  </Table.Td>
                  <Table.Td>
                    {model.declared_in_models_yaml ? (
                      <Badge color="teal" variant="light">in models.yaml</Badge>
                    ) : (
                      <Badge color="gray" variant="outline">not routed</Badge>
                    )}
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Card>
    </Stack>
  );
}

export default OllamaPanel;
