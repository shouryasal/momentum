import { Badge, Group, NavLink, ScrollArea, Stack, Text, TextInput } from '@mantine/core';
import { IconLock, IconSearch } from '@tabler/icons-react';
import { useMemo, useState } from 'react';

import type { FieldMeta } from '../api';

export interface Section {
  /** Top-level schema property — the sub-tree the form renders. */
  key: string;
  title: string;
  group: string;
  description: string;
  protected: boolean;
  leaves: number;
  effects: string[];
}

/** The section tree is derived from the schema, grouped by `x-group` (spec 12 page 16). */
export function sectionsOf(ui: FieldMeta[]): Section[] {
  const tops = new Map<string, Section>();
  for (const meta of ui) {
    if (!meta.path) continue;
    const [key] = meta.path.split('.');
    if (!key) continue;
    const existing = tops.get(key);
    if (!existing) {
      tops.set(key, {
        key,
        title: meta.path === key ? meta.title : key.replace(/_/g, ' '),
        group: (meta.group || key).split('.')[0] ?? key,
        description: meta.path === key ? meta.description : '',
        protected: meta.protected,
        leaves: meta.path === key ? 0 : 1,
        effects: [...meta.effects],
      });
      continue;
    }
    if (meta.path === key) {
      existing.title = meta.title;
      existing.description = meta.description;
    } else {
      existing.leaves += 1;
    }
    existing.protected = existing.protected || meta.protected;
    for (const effect of meta.effects) {
      if (!existing.effects.includes(effect)) existing.effects.push(effect);
    }
  }
  return [...tops.values()].sort((a, b) => a.group.localeCompare(b.group) || a.key.localeCompare(b.key));
}

export interface SectionTreeProps {
  sections: Section[];
  active: string | null;
  onSelect: (key: string) => void;
  /** Dotted paths the current draft has changed, so the tree can mark dirty sections. */
  dirtyPaths: string[];
}

export function SectionTree({ sections, active, onSelect, dirtyPaths }: SectionTreeProps) {
  const [filter, setFilter] = useState('');

  const dirtyTops = useMemo(
    () => new Set(dirtyPaths.map((path) => path.split('.')[0])),
    [dirtyPaths],
  );

  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return sections;
    return sections.filter(
      (section) =>
        section.key.includes(needle) ||
        section.title.toLowerCase().includes(needle) ||
        section.group.toLowerCase().includes(needle),
    );
  }, [filter, sections]);

  const groups = useMemo(() => {
    const out = new Map<string, Section[]>();
    for (const section of shown) {
      const list = out.get(section.group) ?? [];
      list.push(section);
      out.set(section.group, list);
    }
    return [...out.entries()];
  }, [shown]);

  return (
    <Stack gap="xs" data-testid="section-tree">
      <TextInput
        size="xs"
        placeholder="Filter sections"
        leftSection={<IconSearch size={14} />}
        value={filter}
        onChange={(event) => setFilter(event.currentTarget.value)}
      />
      <ScrollArea.Autosize mah={620} type="hover">
        <Stack gap={2}>
          {groups.map(([group, items]) => (
            <div key={group}>
              <Text size="xs" c="dimmed" tt="uppercase" fw={700} mt="xs" mb={4}>
                {group}
              </Text>
              {items.map((section) => (
                <NavLink
                  key={section.key}
                  active={section.key === active}
                  onClick={() => onSelect(section.key)}
                  label={
                    <Group gap={6} wrap="nowrap">
                      <Text size="sm">{section.title}</Text>
                      {section.protected ? <IconLock size={12} /> : null}
                      {dirtyTops.has(section.key) ? (
                        <Badge size="xs" color="yellow" variant="filled">
                          edited
                        </Badge>
                      ) : null}
                    </Group>
                  }
                  description={`${section.leaves} field${section.leaves === 1 ? '' : 's'}`}
                />
              ))}
            </div>
          ))}
        </Stack>
      </ScrollArea.Autosize>
    </Stack>
  );
}
