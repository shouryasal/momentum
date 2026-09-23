import { Center, Group, Loader, ScrollArea, Table, Text, UnstyledButton } from '@mantine/core';
import { IconArrowDown, IconArrowUp, IconArrowsSort } from '@tabler/icons-react';
import { useMemo, useState, type ReactNode } from 'react';

import { EmptyState } from './EmptyState';

export interface DataTableColumn<T> {
  key: string;
  header: ReactNode;
  render: (row: T, index: number) => ReactNode;
  /** Return a comparable value to make the column sortable. */
  sortValue?: (row: T) => string | number | null;
  align?: 'left' | 'right' | 'center';
  width?: number | string;
}

export interface DataTableProps<T> {
  columns: Array<DataTableColumn<T>>;
  rows: T[];
  rowKey: (row: T, index: number) => string;
  loading?: boolean;
  emptyTitle?: string;
  emptyDescription?: ReactNode;
  onRowClick?: (row: T) => void;
  /**
   * Double-click, for a table where going deeper is a second gesture.
   *
   * The owner's words were *"if i double click i should see reasoning"*: single click
   * selects the row and leaves the list where it is, double click opens the story behind
   * it.  Enter and Space do the same as a double click, because a gesture that only works
   * with a mouse is not available to everyone.
   */
  onRowDoubleClick?: (row: T) => void;
  /** The {@link DataTableProps.rowKey} of the selected row, highlighted. */
  selectedKey?: string | null;
  maxHeight?: number | string;
  caption?: ReactNode;
  dense?: boolean;
}

type SortState = { key: string; dir: 'asc' | 'desc' } | null;

/** The one table in the console: sortable headers, loading state, shared empty state. */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  loading,
  emptyTitle = 'Nothing to show',
  emptyDescription,
  onRowClick,
  onRowDoubleClick,
  selectedKey,
  maxHeight,
  caption,
  dense,
}: DataTableProps<T>) {
  const [sort, setSort] = useState<SortState>(null);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const column = columns.find((c) => c.key === sort.key);
    if (!column?.sortValue) return rows;
    const factor = sort.dir === 'asc' ? 1 : -1;
    return [...rows].sort((a, b) => {
      const va = column.sortValue?.(a) ?? null;
      const vb = column.sortValue?.(b) ?? null;
      if (va === null && vb === null) return 0;
      if (va === null) return 1;
      if (vb === null) return -1;
      if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * factor;
      return String(va).localeCompare(String(vb)) * factor;
    });
  }, [rows, sort, columns]);

  if (loading) {
    return (
      <Center p="xl" data-testid="data-table-loading">
        <Loader size="sm" />
      </Center>
    );
  }
  if (rows.length === 0) {
    return <EmptyState title={emptyTitle} {...(emptyDescription ? { description: emptyDescription } : {})} compact />;
  }

  const toggle = (key: string) =>
    setSort((current) =>
      current?.key === key
        ? current.dir === 'asc'
          ? { key, dir: 'desc' }
          : null
        : { key, dir: 'asc' },
    );

  const table = (
    <Table
      data-testid="data-table"
      highlightOnHover={Boolean(onRowClick || onRowDoubleClick)}
      verticalSpacing={dense ? 4 : 'xs'}
      horizontalSpacing={dense ? 6 : 'sm'}
      stickyHeader
    >
      {caption ? <Table.Caption>{caption}</Table.Caption> : null}
      <Table.Thead>
        <Table.Tr>
          {columns.map((column) => (
            <Table.Th key={column.key} style={{ width: column.width, textAlign: column.align ?? 'left' }}>
              {column.sortValue ? (
                <UnstyledButton onClick={() => toggle(column.key)} data-testid={`sort-${column.key}`}>
                  <Group gap={4} wrap="nowrap">
                    <Text size="xs" fw={700} tt="uppercase">
                      {column.header}
                    </Text>
                    {sort?.key === column.key ? (
                      sort.dir === 'asc' ? (
                        <IconArrowUp size={12} />
                      ) : (
                        <IconArrowDown size={12} />
                      )
                    ) : (
                      <IconArrowsSort size={12} opacity={0.4} />
                    )}
                  </Group>
                </UnstyledButton>
              ) : (
                <Text size="xs" fw={700} tt="uppercase">
                  {column.header}
                </Text>
              )}
            </Table.Th>
          ))}
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {sorted.map((row, index) => {
          const key = rowKey(row, index);
          const interactive = Boolean(onRowClick || onRowDoubleClick);
          const selected = selectedKey !== undefined && selectedKey !== null && selectedKey === key;
          return (
            <Table.Tr
              key={key}
              data-testid={`data-row-${key}`}
              data-selected={selected ? 'true' : undefined}
              aria-selected={interactive ? selected : undefined}
              tabIndex={onRowDoubleClick ? 0 : undefined}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              onDoubleClick={onRowDoubleClick ? () => onRowDoubleClick(row) : undefined}
              onKeyDown={
                onRowDoubleClick
                  ? (event) => {
                      if (event.key !== 'Enter' && event.key !== ' ') return;
                      event.preventDefault();
                      onRowDoubleClick(row);
                    }
                  : undefined
              }
              style={{
                ...(interactive ? { cursor: 'pointer' } : {}),
                ...(selected ? { background: 'var(--mantine-color-default-hover)' } : {}),
              }}
            >
              {columns.map((column) => (
                <Table.Td key={column.key} style={{ textAlign: column.align ?? 'left' }}>
                  {column.render(row, index)}
                </Table.Td>
              ))}
            </Table.Tr>
          );
        })}
      </Table.Tbody>
    </Table>
  );

  return maxHeight ? <ScrollArea.Autosize mah={maxHeight}>{table}</ScrollArea.Autosize> : table;
}
