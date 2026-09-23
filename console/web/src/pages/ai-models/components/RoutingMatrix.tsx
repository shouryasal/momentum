import { Badge, Card, Code, Group, Stack, Table, Text, Title, Tooltip } from '@mantine/core';
import { IconLock } from '@tabler/icons-react';

import type { ChainEntry, RoutingResponse } from '../api';

function ChainBadge({ entry, head }: { entry: ChainEntry; head: boolean }) {
  return (
    <Tooltip label={`${entry.id ?? 'undeclared'} · tier ${entry.tier ?? '?'}`} withArrow>
      <Badge
        variant={head ? 'filled' : 'light'}
        color={entry.local ? 'grape' : entry.declared ? 'blue' : 'red'}
      >
        {entry.alias}
      </Badge>
    </Tooltip>
  );
}

/**
 * Task × chain, read-only.
 *
 * The two tier columns are separate on purpose: `min_tier` is what `models.yaml` asks
 * for, `code_min_tier` is the floor in `runs/llm/types.py` that no config edit and no
 * tier-1 overlay can lower. Showing them side by side is how an operator can tell that
 * a lowered config floor changed nothing.
 */
export function RoutingMatrix({ routing }: { routing: RoutingResponse }) {
  return (
    <Stack gap="md">
      <Card withBorder padding="md" radius="md">
        <Group justify="space-between" mb="sm">
          <Title order={5}>Routing matrix</Title>
          <Group gap="xs">
            <Badge variant="light" color="gray">
              budget {routing.budget.mode} · ${routing.budget.monthly_total_usd}/mo
            </Badge>
            <Badge variant="light" color="gray">
              max {String(routing.switching.max_attempts_per_call ?? '?')} attempts/call
            </Badge>
          </Group>
        </Group>

        <Table.ScrollContainer minWidth={900}>
          <Table striped withTableBorder data-testid="routing-matrix">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>task</Table.Th>
                <Table.Th>chain (first that can serve, does)</Table.Th>
                <Table.Th>tools</Table.Th>
                <Table.Th>min tier</Table.Th>
                <Table.Th>budgets</Table.Th>
                <Table.Th>on all failed</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {routing.tasks.map((task) => (
                <Table.Tr key={task.task}>
                  <Table.Td>
                    <Group gap={4}>
                      <Code>{task.task}</Code>
                      {task.overlay_head ? (
                        <Tooltip label="head replaced by the tier-1 overlay">
                          <Badge size="xs" color="orange" variant="light">overlay</Badge>
                        </Tooltip>
                      ) : null}
                    </Group>
                  </Table.Td>
                  <Table.Td>
                    <Group gap={4}>
                      {task.escalation ? (
                        <Tooltip label="prepended on a hard case, trigger or gray zone">
                          <Badge variant="outline" color="orange">
                            ↑ {task.escalation.alias}
                          </Badge>
                        </Tooltip>
                      ) : null}
                      {task.chain.map((entry, index) => (
                        <ChainBadge key={entry.alias} entry={entry} head={index === 0} />
                      ))}
                      {task.local_mode ? (
                        <Tooltip label="a local model may serve this with a context pack">
                          <Badge size="xs" variant="light" color="grape">
                            {task.local_mode}
                          </Badge>
                        </Tooltip>
                      ) : null}
                    </Group>
                  </Table.Td>
                  <Table.Td><Code>{task.tools}</Code></Table.Td>
                  <Table.Td>
                    <Group gap={4} wrap="nowrap">
                      <Text size="sm">{task.effective_min_tier}</Text>
                      {task.code_min_tier > 1 ? (
                        <Tooltip
                          label={`code floor ${task.code_min_tier} (runs/llm/types.py: ` +
                            'MIN_TIER_FLOOR) — config cannot lower it'}
                        >
                          <IconLock size={13} />
                        </Tooltip>
                      ) : null}
                      {task.local_forbidden ? (
                        <Tooltip label="no local model may ever serve this task">
                          <Badge size="xs" color="red" variant="light">no local</Badge>
                        </Tooltip>
                      ) : null}
                    </Group>
                  </Table.Td>
                  <Table.Td>
                    <Text size="xs" c="dimmed">
                      {task.max_usd_per_run != null ? `$${task.max_usd_per_run}/run` : '—'}
                      {task.monthly_budget_usd != null
                        ? ` · $${task.monthly_budget_usd}/mo`
                        : ''}
                      <br />
                      {task.max_turns ?? '?'} turns · {task.deadline_s ?? '?'}s ·{' '}
                      {task.retry} retry · {task.effort ?? 'default'} effort
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Badge variant="light" color="gray">
                      {task.on_all_failed ?? 'abstain'}
                    </Badge>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
        <Text size="xs" c="dimmed" mt="sm">
          Edit chains in Settings → models.yaml. Every switch down a chain is journaled in
          provider_switches and shown on the Switches tab — there is no silent substitution.
        </Text>
      </Card>
    </Stack>
  );
}

export default RoutingMatrix;
