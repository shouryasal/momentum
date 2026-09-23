/**
 * One research run, stage by stage, plus its proposal, its provider switches and the
 * rendered trace (`runs/trace.py`).
 *
 * The stage table shows requested AND served model side by side, with the chain index and
 * what it switched from, because a fallback is the single most important thing to notice
 * about a decision after the decision itself.
 */
import {
  Alert,
  Badge,
  Button,
  Code,
  Divider,
  Drawer,
  Group,
  Loader,
  Paper,
  ScrollArea,
  Stack,
  Table,
  Tabs,
  Text,
  Textarea,
  Title,
  Tooltip,
} from '@mantine/core';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';

import { shellKeys } from '@/api';
import { useApi } from '@/app/ApiContext';
import { ConfirmDialog, EmptyState, JsonViewer } from '@/components';
import { formatNumber, formatUsd, formatUtcStamp } from '@/lib/format';

import { STATUS_COLOR, decisionKeys, decisionsApi } from '../api';

export interface RunDrawerProps {
  runId: string | null;
  onClose: () => void;
}

/**
 * One decision's story, with no container around it.
 *
 * The Decisions screen shows this inside the shared `DetailPane`, beside the list, so a
 * click never takes the operator off the screen they are reading.  {@link RunDrawer}
 * keeps the old drawer for any caller that still wants one; both render this.
 */
export function RunDetailBody({ runId }: { runId: string | null }) {
  const client = useApi();
  const api = useMemo(() => decisionsApi(client), [client]);
  const queryClient = useQueryClient();
  const [pendingDecision, setPendingDecision] = useState<'approve' | 'reject' | null>(null);
  const [note, setNote] = useState('');

  const detail = useQuery({
    queryKey: decisionKeys.run(runId ?? ''),
    queryFn: () => api.run(runId as string),
    enabled: Boolean(runId),
  });
  const trace = useQuery({
    queryKey: decisionKeys.trace(runId ?? ''),
    queryFn: () => api.trace(runId as string),
    enabled: Boolean(runId),
  });

  const run = detail.data;

  /** The same two writes the approvals queue makes, on the drawer the queue opens. */
  const decide = useMutation({
    mutationFn: (decision: 'approve' | 'reject') =>
      decision === 'approve'
        ? api.approve(runId as string, note)
        : api.reject(runId as string, note),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: decisionKeys.pending });
      void queryClient.invalidateQueries({ queryKey: shellKeys.approvals });
      void queryClient.invalidateQueries({ queryKey: decisionKeys.run(runId ?? '') });
      setNote('');
    },
  });

  return (
    <>
      {detail.isLoading ? <Loader /> : null}
      {detail.isError ? <Alert color="red">Could not load this run.</Alert> : null}
      {run ? (
        <Tabs defaultValue="stages" keepMounted={false}>
          <Tabs.List>
            <Tabs.Tab value="stages">Steps taken</Tabs.Tab>
            <Tabs.Tab value="proposal">The plan</Tabs.Tab>
            <Tabs.Tab value="signal">What was noticed</Tabs.Tab>
            <Tabs.Tab value="trace">Full transcript</Tabs.Tab>
          </Tabs.List>

          <Tabs.Panel value="stages" pt="sm">
            <ScrollArea.Autosize mah={620}>
              <Table striped withTableBorder>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Stage</Table.Th>
                    <Table.Th>Requested → served</Table.Th>
                    <Table.Th>Provider / auth</Table.Th>
                    <Table.Th>Effort</Table.Th>
                    <Table.Th style={{ textAlign: 'right' }}>Tokens</Table.Th>
                    <Table.Th style={{ textAlign: 'right' }}>Cost</Table.Th>
                    <Table.Th>Status</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {run.stages.map((s) => {
                    const mismatch =
                      s.served_model && s.requested_model && s.served_model !== s.requested_model;
                    return (
                      <Table.Tr key={s.stage}>
                        <Table.Td>
                          <Stack gap={0}>
                            <Text size="sm" fw={500}>
                              {s.stage}
                            </Text>
                            <Text size="xs" c="dimmed">
                              {formatUtcStamp(s.started_utc)}
                            </Text>
                          </Stack>
                        </Table.Td>
                        <Table.Td>
                          <Stack gap={0}>
                            <Text size="sm">
                              {s.requested_model ?? '—'}{' '}
                              <Text span c="dimmed">
                                →
                              </Text>{' '}
                              <Text span c={mismatch ? 'orange' : undefined} fw={mismatch ? 600 : 400}>
                                {s.served_model ?? '—'}
                              </Text>
                            </Text>
                            {s.switched_from ? (
                              <Text size="xs" c="orange">
                                switched from {s.switched_from}
                                {s.chain_index !== null ? ` (chain ${s.chain_index})` : ''}
                              </Text>
                            ) : null}
                            {s.escalation_reasons.length > 0 ? (
                              <Group gap={4}>
                                {s.escalation_reasons.map((r) => (
                                  <Badge key={r} size="xs" color="violet" variant="light">
                                    {r}
                                  </Badge>
                                ))}
                              </Group>
                            ) : null}
                          </Stack>
                        </Table.Td>
                        <Table.Td>
                          <Stack gap={0}>
                            <Text size="sm">{s.provider ?? '—'}</Text>
                            <Text size="xs" c="dimmed">
                              {s.auth_source === 'none' ? 'subscription' : s.auth_source ?? '—'}
                            </Text>
                          </Stack>
                        </Table.Td>
                        <Table.Td>{s.effort ?? '—'}</Table.Td>
                        <Table.Td style={{ textAlign: 'right', fontFamily: 'monospace' }}>
                          {(s.input_tokens ?? 0) + (s.output_tokens ?? 0) || '—'}
                        </Table.Td>
                        <Table.Td style={{ textAlign: 'right', fontFamily: 'monospace' }}>
                          {formatUsd(s.cost_usd)}
                        </Table.Td>
                        <Table.Td>
                          <Stack gap={2}>
                            <Badge size="sm" color={STATUS_COLOR[s.status] ?? 'gray'} variant="light">
                              {s.status}
                            </Badge>
                            {s.error ? (
                              <Text size="xs" c="red" lineClamp={2}>
                                {s.error}
                              </Text>
                            ) : null}
                          </Stack>
                        </Table.Td>
                      </Table.Tr>
                    );
                  })}
                </Table.Tbody>
              </Table>
            </ScrollArea.Autosize>

            {run.provider_switches.length > 0 ? (
              <>
                <Divider my="sm" label="Provider switches" labelPosition="left" />
                <Stack gap={4}>
                  {run.provider_switches.map((sw, i) => (
                    <Paper key={i} withBorder p="xs" radius="sm">
                      <Group gap="sm">
                        <Badge size="sm" color="orange" variant="light">
                          {sw.reason}
                        </Badge>
                        <Text size="sm">
                          {sw.from_provider}/{sw.from_model} → {sw.to_provider}/{sw.to_model}
                        </Text>
                        <Text size="xs" c="dimmed">
                          {sw.detail ?? ''} {formatUtcStamp(sw.ts_utc)}
                        </Text>
                      </Group>
                    </Paper>
                  ))}
                </Stack>
              </>
            ) : null}
          </Tabs.Panel>

          <Tabs.Panel value="proposal" pt="sm">
            {run.proposal ? (
              <Stack gap="sm">
                <Group gap="xs">
                  <Badge>{run.proposal.module ?? '—'}</Badge>
                  {run.proposal.abstain ? (
                    <Tooltip
                      label="Claude declined to suggest a trade, because the inputs were stale or contradicted each other. Doing nothing is the safe default."
                      multiline
                      w={300}
                    >
                      <Badge color="gray">chose not to trade</Badge>
                    </Tooltip>
                  ) : null}
                  <Text size="sm">
                    confidence {formatNumber(run.proposal.confidence, 2)} out of 1
                  </Text>
                  <Text size="sm">
                    position size x{formatNumber(run.proposal.exposure_scale, 2)} of normal
                  </Text>
                  <Text size="sm">
                    meant to hold for {run.proposal.horizon_days ?? '—'} days
                  </Text>
                  {run.proposal.approval_status && run.proposal.approval_status !== 'n/a' ? (
                    <Badge color="orange" variant="light">
                      {run.proposal.approval_status}
                    </Badge>
                  ) : null}
                </Group>
                {run.proposal.path ? <Code>{run.proposal.path}</Code> : null}
                <JsonViewer value={run.proposal.targets} label="targets" />
                <div>
                  <Title order={6}>Rationale</Title>
                  {run.proposal.rationale.map((r, i) => (
                    <Text key={i} size="sm">
                      • {r}
                    </Text>
                  ))}
                </div>
                {run.proposal.invalidation ? (
                  <Alert variant="light" color="gray" title="Invalidation">
                    {run.proposal.invalidation}
                  </Alert>
                ) : null}
                {run.proposal.invalid_reason ? (
                  <Alert color="red" title="Rejected by the host validator">
                    {run.proposal.invalid_reason}
                  </Alert>
                ) : null}
                {run.proposal.approval_status === 'pending' ? (
                  <Group gap="xs">
                    <Button
                      size="compact-sm"
                      disabled={decide.isPending}
                      onClick={() => setPendingDecision('approve')}
                      data-testid="drawer-approve"
                    >
                      Approve
                    </Button>
                    <Button
                      size="compact-sm"
                      variant="default"
                      disabled={decide.isPending}
                      onClick={() => setPendingDecision('reject')}
                      data-testid="drawer-reject"
                    >
                      Reject
                    </Button>
                  </Group>
                ) : null}
                <ConfirmDialog
                  opened={pendingDecision !== null}
                  onClose={() => setPendingDecision(null)}
                  title={
                    pendingDecision === 'approve'
                      ? `Approve ${runId ?? ''}?`
                      : `Reject ${runId ?? ''}?`
                  }
                  confirmLabel={pendingDecision === 'approve' ? 'Approve' : 'Reject'}
                  danger={pendingDecision === 'approve'}
                  description={
                    pendingDecision === 'approve'
                      ? 'Writes an HMAC-signed approval file the bot verifies before it acts on this proposal.'
                      : 'Removes any approval file for this run and records it rejected.'
                  }
                  onConfirm={async () => {
                    if (pendingDecision) await decide.mutateAsync(pendingDecision);
                    setPendingDecision(null);
                  }}
                >
                  <Textarea
                    label="Note"
                    description="Why, for the audit trail. Optional."
                    value={note}
                    onChange={(event) => setNote(event.currentTarget.value)}
                    autosize
                    minRows={2}
                  />
                </ConfirmDialog>
              </Stack>
            ) : (
              <EmptyState
                compact
                title="No proposal"
                description="The decide stage did not produce one that passed host validation; the previous targets stand."
              />
            )}
          </Tabs.Panel>

          <Tabs.Panel value="signal" pt="sm">
            {run.signal ? (
              <JsonViewer value={run.signal} label="signal" defaultExpandedDepth={1} />
            ) : (
              <EmptyState compact title="Scheduled run" description="No signal fired this run." />
            )}
          </Tabs.Panel>

          <Tabs.Panel value="trace" pt="sm">
            {trace.isLoading ? <Loader /> : null}
            {trace.data ? (
              <ScrollArea.Autosize mah={620}>
                <Code block style={{ whiteSpace: 'pre-wrap' }}>
                  {trace.data}
                </Code>
              </ScrollArea.Autosize>
            ) : null}
          </Tabs.Panel>
        </Tabs>
      ) : null}
    </>
  );
}

/** The same story in a right-hand drawer, for callers that are not master/detail yet. */
export function RunDrawer({ runId, onClose }: RunDrawerProps) {
  return (
    <Drawer
      opened={Boolean(runId)}
      onClose={onClose}
      position="right"
      size="xl"
      title={
        <Text fw={600} ff="monospace">
          {runId ?? 'Run'}
        </Text>
      }
    >
      <RunDetailBody runId={runId} />
    </Drawer>
  );
}
