import { Box, Code, Group, Text, UnstyledButton } from '@mantine/core';
import { IconChevronDown, IconChevronRight } from '@tabler/icons-react';
import { useState } from 'react';

export interface JsonViewerProps {
  value: unknown;
  /** Levels expanded on first render. */
  defaultExpandedDepth?: number;
  label?: string;
}

function typeOf(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function Scalar({ value }: { value: unknown }) {
  const kind = typeOf(value);
  const color =
    kind === 'string' ? 'teal' : kind === 'number' ? 'blue' : kind === 'boolean' ? 'grape' : 'dimmed';
  return (
    <Text span size="sm" c={color} ff="monospace">
      {kind === 'string' ? `"${String(value)}"` : String(value)}
    </Text>
  );
}

function Node({ name, value, depth, expandDepth }: { name: string | null; value: unknown; depth: number; expandDepth: number }) {
  const kind = typeOf(value);
  const branch = kind === 'object' || kind === 'array';
  const [open, setOpen] = useState(depth < expandDepth);

  if (!branch) {
    return (
      <Group gap={6} wrap="nowrap" pl={depth * 14}>
        {name !== null ? (
          <Text span size="sm" fw={600} ff="monospace">
            {name}:
          </Text>
        ) : null}
        <Scalar value={value} />
      </Group>
    );
  }

  const entries = Array.isArray(value)
    ? value.map((item, index) => [String(index), item] as const)
    : Object.entries(value as Record<string, unknown>);

  return (
    <Box pl={depth * 14}>
      <UnstyledButton onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <Group gap={4} wrap="nowrap">
          {open ? <IconChevronDown size={14} /> : <IconChevronRight size={14} />}
          {name !== null ? (
            <Text span size="sm" fw={600} ff="monospace">
              {name}
            </Text>
          ) : null}
          <Text span size="xs" c="dimmed">
            {kind === 'array' ? `[${entries.length}]` : `{${entries.length}}`}
          </Text>
        </Group>
      </UnstyledButton>
      {open
        ? entries.map(([key, item]) => (
            <Node key={key} name={key} value={item} depth={depth + 1} expandDepth={expandDepth} />
          ))
        : null}
    </Box>
  );
}

/** Collapsible JSON tree for payloads, traces and SSE events. */
export function JsonViewer({ value, defaultExpandedDepth = 1, label }: JsonViewerProps) {
  if (value === undefined) {
    return (
      <Code block data-testid="json-viewer">
        undefined
      </Code>
    );
  }
  return (
    <Box data-testid="json-viewer">
      {label ? (
        <Text size="xs" c="dimmed" mb={4}>
          {label}
        </Text>
      ) : null}
      <Node name={null} value={value} depth={0} expandDepth={defaultExpandedDepth} />
    </Box>
  );
}
