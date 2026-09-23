import { Group, Stack, Text, Title } from '@mantine/core';
import type { ReactNode } from 'react';

export interface PageIntroProps {
  title: string;
  /** One line, in plain words, saying what this screen shows. Required on purpose. */
  blurb: string;
  /** Buttons, selectors or badges that belong with the heading. */
  actions?: ReactNode;
  /** Optional second line: a caveat, a link, a count. */
  children?: ReactNode;
}

/**
 * Every screen's heading: its name, and one line saying what you are looking at.
 *
 * The owner's complaint was that the console assumed you already knew what each page was
 * for.  A screen without a sentence of explanation is the bug; this component makes the
 * sentence a required prop so a new screen cannot ship without one.
 */
export function PageIntro({ title, blurb, actions, children }: PageIntroProps) {
  return (
    <Group justify="space-between" align="flex-end" wrap="wrap" data-testid="page-intro">
      <Stack gap={2} style={{ minWidth: 260, flex: 1 }}>
        <Title order={3}>{title}</Title>
        <Text size="sm" c="dimmed" data-testid="page-blurb">
          {blurb}
        </Text>
        {children}
      </Stack>
      {actions ? <Group gap="xs">{actions}</Group> : null}
    </Group>
  );
}
