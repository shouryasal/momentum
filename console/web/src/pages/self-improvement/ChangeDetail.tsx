import {
  Alert, Badge, Button, Code, Drawer, Group, Loader, ScrollArea, Stack, Table, Tabs, Text,
  Textarea, Title,
} from '@mantine/core';
import { useState } from 'react';

import { ConfirmDialog } from '@/components';

import {
  STATUS_COLOURS,
  useChange,
  useChangeDecision,
  type DecisionAction,
} from './api';
import { ChecksList, EvidenceTable } from './components/EvidenceTable';

export interface ChangeDetailDrawerProps {
  changeId: string | null;
  onClose: () => void;
}

/** One change, end to end: what it is, what the gate checked, and what a human may do. */
export function ChangeDetailDrawer({ changeId, onClose }: ChangeDetailDrawerProps) {
  const { data, isLoading, error } = useChange(changeId);
  const decision = useChangeDecision();
  const [note, setNote] = useState('');
  const [confirming, setConfirming] = useState<DecisionAction | null>(null);
  const [task, setTask] = useState('');

  const act = (action: DecisionAction) => {
    if (!changeId) return;
    decision.mutate(
      { id: changeId, action, note, reason: note, task },
      { onSettled: () => setConfirming(null) },
    );
  };

  return (
    <Drawer
      opened={Boolean(changeId)}
      onClose={onClose}
      position="right"
      size="xl"
      title={<Text fw={600}>{changeId ?? ''}</Text>}
    >
      {isLoading ? <Loader /> : null}
      {error ? (
        <Alert color="red" title="Could not load the change">
          {String(error)}
        </Alert>
      ) : null}

      {data ? (
        <Stack gap="md">
          <Group gap="xs">
            <Badge color={STATUS_COLOURS[data.status] ?? 'gray'}>{data.status}</Badge>
            <Badge variant="light">
              {data.kind}
              {data.op ? ` · ${data.op}` : ''}
            </Badge>
            <Code>{data.target}</Code>
            {data.mismatch_count > 0 ? (
              <Badge color="red">{data.mismatch_count} evidence mismatch(es)</Badge>
            ) : null}
          </Group>

          {data.reason ? (
            <Alert
              color={data.status === 'rejected' ? 'red' : 'yellow'}
              title="Why it is where it is"
            >
              {data.reason}
            </Alert>
          ) : null}

          <Tabs defaultValue="evidence" keepMounted={false}>
            <Tabs.List>
              <Tabs.Tab value="evidence">Evidence</Tabs.Tab>
              <Tabs.Tab value="checks">Checks</Tabs.Tab>
              <Tabs.Tab value="diff">Diff</Tabs.Tab>
              <Tabs.Tab value="events">Timeline</Tabs.Tab>
            </Tabs.List>

            <Tabs.Panel value="evidence" pt="sm">
              <Text size="sm" c="dimmed" mb="xs">
                The model&apos;s claims never gate anything. Only the recomputed column does.
              </Text>
              <EvidenceTable rows={data.evidence} />
            </Tabs.Panel>

            <Tabs.Panel value="checks" pt="sm">
              <ChecksList checks={data.checks} />
            </Tabs.Panel>

            <Tabs.Panel value="diff" pt="sm">
              <ScrollArea.Autosize mah={420}>
                <Code block style={{ whiteSpace: 'pre' }}>
                  {data.diff || 'no diff available'}
                </Code>
              </ScrollArea.Autosize>
            </Tabs.Panel>

            <Tabs.Panel value="events" pt="sm">
              <Table>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>When</Table.Th>
                    <Table.Th>Event</Table.Th>
                    <Table.Th>Actor</Table.Th>
                    <Table.Th>Note</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {data.events.map((event) => (
                    <Table.Tr key={event.id}>
                      <Table.Td>{event.ts_utc}</Table.Td>
                      <Table.Td>
                        <Badge variant="light">{event.event}</Badge>
                      </Table.Td>
                      <Table.Td>{event.actor}</Table.Td>
                      <Table.Td>{event.note ?? ''}</Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Tabs.Panel>
          </Tabs>

          <Title order={5}>Decide</Title>
          <Textarea
            label="Note / reason"
            description="Stored on the change and in the audit log."
            value={note}
            onChange={(event) => setNote(event.currentTarget.value)}
            autosize
            minRows={2}
          />
          {decision.error ? (
            <Alert color="red" title="That did not go through">
              {String(decision.error)}
            </Alert>
          ) : null}
          <Group>
            <Button
              disabled={!data.can_decide}
              loading={decision.isPending}
              onClick={() => act('approve')}
            >
              Approve &amp; merge
            </Button>
            <Button
              variant="default"
              disabled={!data.can_decide}
              loading={decision.isPending}
              onClick={() => act('reject')}
            >
              Reject
            </Button>
            <Button
              color="orange"
              variant="light"
              disabled={!data.can_revert}
              onClick={() => setConfirming('revert')}
            >
              Revert
            </Button>
            {data.kind === 'skill' && data.op === 'create' ? (
              <Group gap="xs">
                <Textarea
                  aria-label="binding task"
                  placeholder="review"
                  value={task}
                  onChange={(event) => setTask(event.currentTarget.value)}
                  autosize
                  minRows={1}
                  w={160}
                />
                <Button variant="light" disabled={!task} onClick={() => setConfirming('attach')}>
                  Attach
                </Button>
              </Group>
            ) : null}
          </Group>

          <ConfirmDialog
            opened={confirming === 'revert'}
            onClose={() => setConfirming(null)}
            onConfirm={() => act('revert')}
            title="Revert this change?"
            confirmLabel="Revert"
            requireStepUp
            danger
            description={
              <Text size="sm">
                This writes a revert commit on the live branch under the ops lock. The
                original commit stays in history; the change is recorded as reverted.
              </Text>
            }
          />

          <ConfirmDialog
            opened={confirming === 'attach'}
            onClose={() => setConfirming(null)}
            onConfirm={() => act('attach')}
            title={`Bind this skill to "${task}"?`}
            confirmLabel="Bind"
            requireStepUp
            description={
              <Text size="sm">
                An incubating skill is loaded by nothing until it is bound. Binding it means
                the <Code>{task}</Code> task will load it on its next run.
              </Text>
            }
          />
        </Stack>
      ) : null}
    </Drawer>
  );
}
