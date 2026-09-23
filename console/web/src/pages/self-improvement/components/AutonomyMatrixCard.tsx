import {
  Alert, Badge, Button, Card, Group, List, NumberInput, Select, Stack, Switch, Table, Text,
  Title,
} from '@mantine/core';
import { useEffect, useState } from 'react';

import { KIND_LABELS, type AutonomyMatrix, type AutonomySetting } from '../api';

const SETTINGS: AutonomySetting[] = ['auto', 'approve', 'off'];

const SETTING_COLOUR: Record<AutonomySetting, string> = {
  auto: 'teal',
  approve: 'yellow',
  off: 'gray',
};

export interface AutonomyMatrixCardProps {
  matrix: AutonomyMatrix;
  saving?: boolean;
  error?: string | null;
  onSave: (body: Partial<AutonomyMatrix>) => void;
}

/**
 * The kind × mode matrix.
 *
 * Saving writes `autonomy.*` in `config/earn.yaml` through the config store, so it is
 * step-up protected and audited like any other protected setting. The invariants listed at
 * the bottom are code, not settings — no value chosen here can switch them off.
 */
export function AutonomyMatrixCard({ matrix, saving, error, onSave }: AutonomyMatrixCardProps) {
  const [draft, setDraft] = useState(matrix);
  useEffect(() => setDraft(matrix), [matrix]);

  const setKind = (kind: string, column: 'test' | 'live', value: AutonomySetting) => {
    setDraft((current) => ({
      ...current,
      kinds: {
        ...current.kinds,
        [kind]: { ...(current.kinds[kind] ?? { test: 'approve', live: 'approve' }), [column]: value },
      },
    }));
  };

  const dirty = JSON.stringify(draft) !== JSON.stringify(matrix);

  return (
    <Card withBorder padding="md">
      <Group justify="space-between" mb="sm">
        <Title order={4}>Autonomy</Title>
        <Badge color={matrix.mode === 'live' ? 'red' : 'blue'} variant="light">
          effective mode: {matrix.mode}
        </Badge>
      </Group>

      {error ? (
        <Alert color="red" mb="sm" title="The matrix was not saved">
          {error}
        </Alert>
      ) : null}

      <Stack gap="xs" mb="md">
        <Switch
          label="Merge tier-1 changes automatically at all"
          description="Off holds every change for you, whatever the matrix says."
          checked={draft.tier1_auto_merge}
          onChange={(event) =>
            setDraft({ ...draft, tier1_auto_merge: event.currentTarget.checked })
          }
        />
        <Switch
          label="While a sleeve is live, use the live column"
          description="And never let a non-revert kind stay on auto there."
          checked={draft.live_forces_human}
          onChange={(event) =>
            setDraft({ ...draft, live_forces_human: event.currentTarget.checked })
          }
        />
        <NumberInput
          label="Automatic merges per week"
          description="A ceiling on how fast the system may change itself."
          min={0}
          max={50}
          value={draft.max_auto_merges_per_week}
          onChange={(value) =>
            setDraft({ ...draft, max_auto_merges_per_week: Number(value) || 0 })
          }
          w={260}
        />
      </Stack>

      <Table withTableBorder>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Change kind</Table.Th>
            <Table.Th>In TEST</Table.Th>
            <Table.Th>While LIVE</Table.Th>
            <Table.Th>Effective now</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {Object.keys(matrix.kinds).map((kind) => (
            <Table.Tr key={kind}>
              <Table.Td>
                <Text size="sm">{KIND_LABELS[kind] ?? kind}</Text>
                <Text size="xs" c="dimmed" ff="monospace">
                  {kind}
                </Text>
              </Table.Td>
              {(['test', 'live'] as const).map((column) => (
                <Table.Td key={column}>
                  <Select
                    aria-label={`${kind} ${column}`}
                    data={SETTINGS}
                    value={draft.kinds[kind]?.[column] ?? 'approve'}
                    onChange={(value) =>
                      value && setKind(kind, column, value as AutonomySetting)
                    }
                    size="xs"
                    w={110}
                    allowDeselect={false}
                  />
                </Table.Td>
              ))}
              <Table.Td>
                <Badge
                  color={SETTING_COLOUR[matrix.effective[kind] ?? 'approve']}
                  variant="light"
                >
                  {matrix.effective[kind] ?? 'approve'}
                </Badge>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>

      <Title order={5} mt="lg" mb="xs">
        Auto-revert
      </Title>
      <Group align="flex-end" gap="md">
        <Switch
          label="Enabled"
          checked={draft.auto_revert.enabled}
          onChange={(event) =>
            setDraft({
              ...draft,
              auto_revert: { ...draft.auto_revert, enabled: event.currentTarget.checked },
            })
          }
        />
        <NumberInput
          label="Window (days)"
          min={1}
          value={draft.auto_revert.window_days}
          onChange={(value) =>
            setDraft({
              ...draft,
              auto_revert: { ...draft.auto_revert, window_days: Number(value) || 1 },
            })
          }
          w={130}
        />
        <NumberInput
          label="Validity drop (pp)"
          min={1}
          value={draft.auto_revert.validity_drop_pct}
          onChange={(value) =>
            setDraft({
              ...draft,
              auto_revert: { ...draft.auto_revert, validity_drop_pct: Number(value) || 1 },
            })
          }
          w={150}
        />
        <NumberInput
          label="Extra gate breaches"
          min={1}
          value={draft.auto_revert.breach_increase}
          onChange={(value) =>
            setDraft({
              ...draft,
              auto_revert: { ...draft.auto_revert, breach_increase: Number(value) || 1 },
            })
          }
          w={170}
        />
      </Group>

      <Group justify="space-between" mt="lg">
        <List size="xs" c="dimmed" spacing={2}>
          {matrix.invariants.map((line) => (
            <List.Item key={line}>{line}</List.Item>
          ))}
        </List>
        <Button disabled={!dirty} loading={saving} onClick={() => onSave(draft)}>
          Save matrix
        </Button>
      </Group>
    </Card>
  );
}
