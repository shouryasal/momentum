/**
 * The signal detail drawer (spec 12 page 7).
 *
 * Everything the pipeline recorded, in the order it happened: the detector's hit and the
 * exact feature values it cited, the screener's score with its provider and model, then
 * the validator's verdict — thesis, reasons, **counter-evidence**, invalidation, and the
 * evidence pack it was shown. Counter-evidence gets the same visual weight as the thesis
 * on purpose: a validation with none is usually a validation that did not look.
 */
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Code,
  Divider,
  Drawer,
  Group,
  List,
  Loader,
  Paper,
  ScrollArea,
  Stack,
  Table,
  Text,
  Tooltip,
} from '@mantine/core';
import { IconCheck, IconExternalLink, IconRefresh, IconX } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo } from 'react';

import { useApi } from '@/app/ApiContext';
import { EmptyState, JsonViewer } from '@/components';
import { formatNumber, formatUtcStamp } from '@/lib/format';

import { DIRECTION_COLOR, STATUS_COLOR, signalKeys, signalsApi, type Direction, type ValidationRow } from '../api';

export interface SignalDrawerProps {
  signalId: string | null;
  onClose: () => void;
  onChanged?: () => void;
}

export function SignalDrawer({ signalId, onClose, onChanged }: SignalDrawerProps) {
  const client = useApi();
  const queryClient = useQueryClient();
  const api = useMemo(() => signalsApi(client), [client]);

  const query = useQuery({
    queryKey: signalKeys.detail(signalId ?? ''),
    queryFn: () => api.detail(signalId as string),
    enabled: Boolean(signalId),
  });

  const revalidate = useMutation({
    mutationFn: () => api.revalidate(signalId as string),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: signalKeys.all });
      onChanged?.();
    },
  });
  const label = useMutation({
    mutationFn: (value: 'noise' | 'useful') => api.label(signalId as string, value),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: signalKeys.all });
      onChanged?.();
    },
  });

  const signal = query.data;
  const features = useMemo(() => Object.entries(signal?.features ?? {}), [signal]);

  return (
    <Drawer
      opened={Boolean(signalId)}
      onClose={onClose}
      position="right"
      size="xl"
      title={<Text fw={600}>{signalId ?? 'Signal'}</Text>}
    >
      {query.isLoading ? <Loader /> : null}
      {query.isError ? <Alert color="red">Could not load this signal.</Alert> : null}
      {signal ? (
        <Stack gap="md">
          <Group gap="xs" wrap="wrap">
            <Badge color={STATUS_COLOR[signal.status] ?? 'gray'}>{signal.status}</Badge>
            {signal.direction ? (
              <Badge variant="light" color={DIRECTION_COLOR[signal.direction as Direction]}>
                {signal.direction}
              </Badge>
            ) : null}
            <Badge variant="outline">{signal.detector}</Badge>
            {signal.pair ? <Badge variant="outline">{signal.pair}</Badge> : null}
            {signal.fast_path ? <Badge color="orange">fast path</Badge> : null}
            <Text size="xs" c="dimmed">
              detected {formatUtcStamp(signal.ts_utc)} · scan {signal.scan_id}
            </Text>
          </Group>

          {signal.status_reason ? (
            <Alert variant="light" color="gray" title="Status reason">
              {signal.status_reason}
            </Alert>
          ) : null}
          {signal.blocked.length > 0 ? (
            <Alert color="orange" title="Guards blocked this signal">
              <Group gap="xs">
                {signal.blocked.map((b) => (
                  <Badge key={b} color="orange" variant="light">
                    {b}
                  </Badge>
                ))}
              </Group>
              <Text size="sm" mt="xs">
                These are the same guards a scheduled trigger asks — kill switch, planner cooldown,
                daily cap and market-data staleness.
              </Text>
            </Alert>
          ) : null}

          <Group gap="sm">
            <Button
              size="xs"
              variant="default"
              leftSection={revalidate.isPending ? <Loader size={12} /> : <IconRefresh size={14} />}
              onClick={() => revalidate.mutate()}
              disabled={revalidate.isPending}
            >
              Revalidate
            </Button>
            <Button
              size="xs"
              variant="default"
              color="gray"
              leftSection={<IconX size={14} />}
              onClick={() => label.mutate('noise')}
            >
              Mark noise
            </Button>
            <Button
              size="xs"
              variant="default"
              color="teal"
              leftSection={<IconCheck size={14} />}
              onClick={() => label.mutate('useful')}
            >
              Mark useful
            </Button>
          </Group>

          <Divider label="Detector" labelPosition="left" />
          <Group gap="lg">
            <Metric label="Detector score" value={formatNumber(signal.detector_score, 3)} />
            <Metric
              label="Screen score"
              value={signal.screen_score === null ? 'n/a' : formatNumber(signal.screen_score, 3)}
            />
            <Metric label="Combined" value={formatNumber(signal.strength, 3)} />
          </Group>
          {Object.keys(signal.detector_detail ?? {}).length > 0 ? (
            <JsonViewer value={signal.detector_detail} label="detector detail" />
          ) : null}

          <Divider label="Features the detector cited" labelPosition="left" />
          {features.length === 0 ? (
            <EmptyState compact title="No cited features" description="This detector fires on news, not on a computed value." />
          ) : (
            <ScrollArea.Autosize mah={220}>
              <Table striped highlightOnHover withTableBorder>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Feature key</Table.Th>
                    <Table.Th style={{ textAlign: 'right' }}>Value</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {features.map(([key, value]) => (
                    <Table.Tr key={key}>
                      <Table.Td>
                        <Code>{key}</Code>
                      </Table.Td>
                      <Table.Td style={{ textAlign: 'right', fontFamily: 'monospace' }}>
                        {value === null ? '—' : String(value)}
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </ScrollArea.Autosize>
          )}

          <Divider label="Screener" labelPosition="left" />
          <Paper withBorder p="sm" radius="sm">
            <Group gap="xs" mb={4}>
              <Badge variant="light">{signal.screen_provider ?? 'not run'}</Badge>
              <Badge variant="outline">{signal.screen_model ?? '—'}</Badge>
            </Group>
            <Text size="sm">{signal.screen_rationale ?? 'No screener rationale recorded.'}</Text>
          </Paper>

          <Divider label="Validator" labelPosition="left" />
          {signal.validations.length === 0 ? (
            <EmptyState
              compact
              title="Not validated"
              description="Only screened signals reach the strong validator, and a fast-path signal skips it by rule."
            />
          ) : (
            signal.validations.map((v) => <ValidationCard key={v.id} validation={v} />)
          )}

          {signal.run || signal.proposal ? (
            <>
              <Divider label="Downstream" labelPosition="left" />
              <Group gap="lg">
                {signal.run_id ? (
                  <Anchor href={`/decisions?run=${encodeURIComponent(signal.run_id)}`} size="sm">
                    <Group gap={4}>
                      <IconExternalLink size={14} /> research run {signal.run_id}
                    </Group>
                  </Anchor>
                ) : null}
                {signal.proposal_run_id ? (
                  <Anchor href={`/decisions?run=${encodeURIComponent(signal.proposal_run_id)}`} size="sm">
                    <Group gap={4}>
                      <IconExternalLink size={14} /> proposal {signal.proposal_run_id}
                    </Group>
                  </Anchor>
                ) : null}
              </Group>
            </>
          ) : null}
        </Stack>
      ) : null}
    </Drawer>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <Stack gap={0}>
      <Text size="xs" c="dimmed" tt="uppercase">
        {label}
      </Text>
      <Text size="lg" fw={600} ff="monospace">
        {value}
      </Text>
    </Stack>
  );
}

function ValidationCard({ validation }: { validation: ValidationRow }) {
  const verdictColor =
    validation.verdict === 'valid' ? 'teal' : validation.verdict === 'invalid' ? 'red' : 'yellow';
  return (
    <Paper withBorder p="sm" radius="sm">
      <Stack gap="xs">
        <Group justify="space-between" wrap="nowrap">
          <Group gap="xs">
            <Badge color={verdictColor}>{validation.verdict}</Badge>
            <Text size="sm" ff="monospace">
              confidence {formatNumber(validation.confidence, 2)}
            </Text>
            {validation.escalated ? <Badge variant="light" color="violet">escalated</Badge> : null}
          </Group>
          <Text size="xs" c="dimmed">
            {validation.provider} / {validation.model} · {formatUtcStamp(validation.ts_utc)}
          </Text>
        </Group>

        {validation.error ? <Alert color="red">{validation.error}</Alert> : null}
        {validation.thesis ? <Text size="sm">{validation.thesis}</Text> : null}

        {validation.reasons.length > 0 ? (
          <div>
            <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
              Reasons
            </Text>
            <List size="sm" spacing={2}>
              {validation.reasons.map((r, i) => (
                <List.Item key={i}>{r}</List.Item>
              ))}
            </List>
          </div>
        ) : null}

        <div>
          <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
            Counter-evidence
          </Text>
          {validation.counter_evidence.length === 0 ? (
            <Text size="sm" c="orange">
              None recorded — treat this verdict with extra suspicion.
            </Text>
          ) : (
            <List size="sm" spacing={2}>
              {validation.counter_evidence.map((r, i) => (
                <List.Item key={i}>{r}</List.Item>
              ))}
            </List>
          )}
        </div>

        {validation.invalidation ? (
          <Alert variant="light" color="gray" title="Invalidation">
            {validation.invalidation}
          </Alert>
        ) : null}

        <Group gap="lg">
          <Text size="xs" c="dimmed">
            horizon {validation.horizon_hours ?? '—'} h
          </Text>
          <Text size="xs" c="dimmed">
            suggested {validation.suggested?.direction ?? '—'}
          </Text>
          {validation.outcome_resolved_at ? (
            <Text size="xs" c={validation.outcome_hit ? 'teal' : 'red'}>
              outcome {formatNumber(validation.outcome_ret, 2)}% ·{' '}
              {validation.outcome_hit ? 'hit' : 'miss'}
            </Text>
          ) : (
            <Text size="xs" c="dimmed">
              outcome pending
            </Text>
          )}
          {validation.pack_path ? (
            <Tooltip label="The deterministic evidence pack the model was shown" withArrow>
              <Code>{validation.pack_path}</Code>
            </Tooltip>
          ) : null}
        </Group>
      </Stack>
    </Paper>
  );
}
