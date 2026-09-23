import { Alert, Code, List, Stack, Text, Title } from '@mantine/core';
import { IconTool } from '@tabler/icons-react';

import type { RouteDef } from '../routes';

/**
 * Placeholder every route falls back to until its work package ships
 * `src/pages/<id>/index.tsx`.  It states plainly that nothing is wired up, so a blank
 * panel is never mistaken for "no data".
 */
export function NotBuiltYet({ route }: { route: RouteDef }) {
  return (
    <Stack gap="md" data-testid={`not-built-${route.id}`}>
      <Title order={2}>{route.title}</Title>
      <Alert color="yellow" icon={<IconTool size={18} />} title="Not built yet">
        <Stack gap="xs">
          <Text size="sm">{route.description}</Text>
          <Text size="sm">
            This page ships with work package <strong>{route.owner}</strong>. The shell renders
            this placeholder until that package adds its module.
          </Text>
          <List size="sm" spacing={2}>
            <List.Item>
              Create <Code>src/pages/{route.id}/index.tsx</Code> with a default export.
            </List.Item>
            <List.Item>
              The route registry picks it up through <Code>import.meta.glob</Code> — no edit to{' '}
              <Code>App.tsx</Code> or <Code>routes.tsx</Code> is needed.
            </List.Item>
          </List>
        </Stack>
      </Alert>
    </Stack>
  );
}
