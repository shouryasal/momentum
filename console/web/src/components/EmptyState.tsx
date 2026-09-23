import { Button, Paper, Stack, Text, ThemeIcon, Title } from '@mantine/core';
import { IconInbox } from '@tabler/icons-react';
import type { ReactNode } from 'react';

export interface EmptyStateProps {
  title: string;
  description?: ReactNode;
  icon?: ReactNode;
  action?: { label: string; onClick: () => void };
  compact?: boolean;
}

/** Shared "nothing here yet" block — every table and drawer uses the same one. */
export function EmptyState({ title, description, icon, action, compact }: EmptyStateProps) {
  return (
    <Paper p={compact ? 'sm' : 'xl'} radius="md" data-testid="empty-state">
      <Stack align="center" gap="xs">
        <ThemeIcon variant="light" size={compact ? 32 : 44} radius="xl" color="gray">
          {icon ?? <IconInbox size={compact ? 18 : 24} />}
        </ThemeIcon>
        <Title order={5}>{title}</Title>
        {description ? (
          <Text c="dimmed" size="sm" ta="center">
            {description}
          </Text>
        ) : null}
        {action ? (
          <Button variant="light" size="xs" onClick={action.onClick}>
            {action.label}
          </Button>
        ) : null}
      </Stack>
    </Paper>
  );
}
