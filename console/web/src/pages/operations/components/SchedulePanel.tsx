import { Alert, Badge, Button, Card, Code, Group, Stack, Text } from '@mantine/core';
import { IconDownload } from '@tabler/icons-react';
import { useCallback, useState } from 'react';

import { ConfirmDialog, DiffView } from '@/components';

import { opsApi, type CrontabStatus, type SystemdUnit } from '../api';

export interface SchedulePanelProps {
  crontab: CrontabStatus | null;
  units: SystemdUnit[];
  loading: boolean;
  onChanged: () => void;
}

/**
 * Crontab drift and install.
 *
 * The crontab is rendered from `ops.schedules` + `research.slots` by
 * `ops/gen_ops_files.py`, never hand-written: the hand-written one hard-coded a home
 * directory, left the venv off `PATH` and fired research half an hour after the configured
 * slot. Two drifts matter and are shown separately — the *installed* crontab vs the render,
 * and the *committed template* vs the render.
 */
export function SchedulePanel({ crontab, units, loading, onChanged }: SchedulePanelProps) {
  const [confirming, setConfirming] = useState(false);
  const [result, setResult] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const install = useCallback(async () => {
    setError(null);
    try {
      const out = await opsApi.installCrontab();
      setResult([
        `crontab installed; units staged in ${out.staged_in}`,
        ...out.sudo,
      ]);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    }
  }, [onChanged]);

  if (loading || !crontab) return <Text c="dimmed">Rendering the crontab…</Text>;

  return (
    <Stack gap="md">
      <Group justify="space-between">
        <Group gap="xs">
          <Badge color={crontab.in_sync ? 'teal' : 'yellow'}>
            {crontab.in_sync ? 'installed crontab matches' : 'installed crontab differs'}
          </Badge>
          <Badge color={crontab.templates_in_sync ? 'teal' : 'yellow'}>
            {crontab.templates_in_sync ? 'committed files match' : 'committed files drift'}
          </Badge>
        </Group>
        <Button
          size="xs"
          leftSection={<IconDownload size={14} />}
          disabled={crontab.in_sync}
          onClick={() => setConfirming(true)}
        >
          Install
        </Button>
      </Group>

      {error ? <Alert color="red">{error}</Alert> : null}
      {result ? (
        <Alert color="teal" title="Installed">
          <Stack gap={2}>
            {result.map((line) => (
              <Code key={line}>{line}</Code>
            ))}
          </Stack>
        </Alert>
      ) : null}

      {crontab.in_sync ? (
        <Card withBorder padding="sm" radius="md">
          <Code block>{crontab.rendered}</Code>
        </Card>
      ) : (
        <DiffView
          before={crontab.rendered.slice(0, 0) || ''}
          after={crontab.diff}
          beforeLabel="installed"
          afterLabel="rendered"
          context={null}
          maxHeight={420}
        />
      )}

      {crontab.templates
        .filter((t) => t.reason !== 'ok')
        .map((t) => (
          <Alert key={t.path} color="yellow" title={`${t.path}: ${t.reason}`}>
            <Code block>{t.diff || 'run: python -m ops.gen_ops_files --write'}</Code>
          </Alert>
        ))}

      <Stack gap="xs">
        <Text fw={600}>systemd units</Text>
        {units.map((unit) => (
          <Card key={unit.name} withBorder padding="sm" radius="md">
            <Group justify="space-between">
              <Stack gap={0}>
                <Text fw={600}>{unit.name}</Text>
                <Text size="xs" c="dimmed">
                  {unit.path}
                </Text>
              </Stack>
              <Group gap="xs">
                <Badge color={unit.active === 'active' ? 'teal' : 'gray'}>{unit.active}</Badge>
                <Badge variant="light">{unit.enabled}</Badge>
              </Group>
            </Group>
          </Card>
        ))}
      </Stack>

      <ConfirmDialog
        opened={confirming}
        onClose={() => setConfirming(false)}
        title="Install the rendered crontab"
        description={
          <Text size="sm">
            This replaces your user crontab with the rendering above and stages the systemd
            units for a manual <Code>sudo cp</Code>. It takes the ops lock.
          </Text>
        }
        requireStepUp
        danger
        confirmLabel="Install"
        onConfirm={async () => {
          await install();
          setConfirming(false);
        }}
      />
    </Stack>
  );
}

export default SchedulePanel;
