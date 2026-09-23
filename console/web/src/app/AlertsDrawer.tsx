import { ActionIcon, Badge, Button, Drawer, Group, Paper, ScrollArea, Stack, Text } from '@mantine/core';
import { IconX } from '@tabler/icons-react';

import { EmptyState } from '../components/EmptyState';
import { formatRelative, formatUtcStamp } from '../lib/format';
import { SEVERITY_COLORS } from '../theme';
import { useEvents } from './EventStreamContext';

/** Alerts drawer fed by the `alert` SSE topic (spec 12). */
export function AlertsDrawer({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  const { alerts, markAllRead, dismiss } = useEvents();

  return (
    <Drawer
      opened={opened}
      onClose={() => {
        markAllRead();
        onClose();
      }}
      position="right"
      size="md"
      title="Alerts"
      data-testid="alerts-drawer"
    >
      <Stack gap="xs">
        <Group justify="space-between">
          <Text size="xs" c="dimmed">
            Live from the `alert` topic
          </Text>
          <Button size="compact-xs" variant="subtle" onClick={markAllRead}>
            Mark all read
          </Button>
        </Group>
        {alerts.length === 0 ? (
          <EmptyState title="No alerts" description="Incidents and gate breaches show up here." compact />
        ) : (
          <ScrollArea.Autosize mah="75vh">
            <Stack gap="xs">
              {alerts.map((alert) => (
                <Paper key={alert.id} p="xs" radius="sm" data-testid="alert-item" data-read={alert.read}>
                  <Group justify="space-between" wrap="nowrap" align="flex-start">
                    <Stack gap={2}>
                      <Group gap={6}>
                        <Badge size="xs" color={SEVERITY_COLORS[alert.payload.severity] ?? 'gray'}>
                          {alert.payload.severity}
                        </Badge>
                        <Text size="sm" fw={600}>
                          {alert.payload.title}
                        </Text>
                      </Group>
                      <Text size="sm">{alert.payload.message}</Text>
                      <Text size="xs" c="dimmed" title={formatUtcStamp(alert.ts)}>
                        {alert.payload.source ? `${alert.payload.source} · ` : ''}
                        {formatRelative(alert.ts)}
                      </Text>
                    </Stack>
                    <ActionIcon variant="subtle" aria-label="dismiss" onClick={() => dismiss(alert.id)}>
                      <IconX size={14} />
                    </ActionIcon>
                  </Group>
                </Paper>
              ))}
            </Stack>
          </ScrollArea.Autosize>
        )}
      </Stack>
    </Drawer>
  );
}
