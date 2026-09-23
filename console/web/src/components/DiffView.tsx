import { Badge, Box, Group, Paper, ScrollArea, Text } from '@mantine/core';
import { useMemo } from 'react';

import { collapseContext, diffLines, diffStats, type DiffLine } from '../lib/diff';

export interface DiffViewProps {
  before: string;
  after: string;
  beforeLabel?: string;
  afterLabel?: string;
  /** Unchanged lines kept around each hunk; `null` shows the whole file. */
  context?: number | null;
  maxHeight?: number;
}

const BG: Record<DiffLine['kind'], string> = {
  add: 'var(--mantine-color-teal-light)',
  del: 'var(--mantine-color-red-light)',
  context: 'transparent',
};

const SIGN: Record<DiffLine['kind'], string> = { add: '+', del: '-', context: ' ' };

/** Unified diff for config previews, change diffs and prompt versions. */
export function DiffView({
  before,
  after,
  beforeLabel = 'current',
  afterLabel = 'proposed',
  context = 3,
  maxHeight = 420,
}: DiffViewProps) {
  const lines = useMemo(() => diffLines(before, after), [before, after]);
  const stats = useMemo(() => diffStats(lines), [lines]);
  const rendered = useMemo(
    () => (context === null ? lines : collapseContext(lines, context)),
    [lines, context],
  );

  return (
    <Paper radius="md" data-testid="diff-view">
      <Group justify="space-between" p="xs" bg="var(--mantine-color-default-hover)">
        <Group gap="xs">
          <Text size="xs" c="dimmed">
            {beforeLabel} → {afterLabel}
          </Text>
        </Group>
        <Group gap={6}>
          <Badge color="teal" variant="light" size="sm">
            +{stats.added}
          </Badge>
          <Badge color="red" variant="light" size="sm">
            −{stats.removed}
          </Badge>
          {!stats.changed ? (
            <Badge color="gray" variant="light" size="sm">
              no changes
            </Badge>
          ) : null}
        </Group>
      </Group>
      <ScrollArea.Autosize mah={maxHeight}>
        <Box component="pre" m={0} p={0} style={{ fontSize: 12, lineHeight: 1.5 }}>
          {rendered.map((line, index) =>
            line === 'gap' ? (
              <Box key={`gap-${index}`} px="xs" c="dimmed" bg="var(--mantine-color-default-hover)">
                ⋯
              </Box>
            ) : (
              <Box
                key={`${line.kind}-${line.leftNo ?? 'x'}-${line.rightNo ?? 'x'}-${index}`}
                px="xs"
                bg={BG[line.kind]}
                data-kind={line.kind}
                style={{ whiteSpace: 'pre-wrap', fontFamily: 'var(--mantine-font-family-monospace)' }}
              >
                <Text span c="dimmed" ff="monospace" size="xs" mr="sm">
                  {String(line.leftNo ?? '').padStart(4)} {String(line.rightNo ?? '').padStart(4)}
                </Text>
                {SIGN[line.kind]}
                {line.text}
              </Box>
            ),
          )}
        </Box>
      </ScrollArea.Autosize>
    </Paper>
  );
}
