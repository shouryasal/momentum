import { Badge, Group, Kbd, Modal, ScrollArea, Stack, Text, TextInput, UnstyledButton } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { shellKeys } from '../api/queryClient';
import { DEVELOPER_ROUTE_IDS, ROUTES, isPrimaryRoute, routeBlurb } from '../routes';
import { useEndpoints } from './ApiContext';
import { useViewMode } from './ViewModeContext';
import {
  searchCommands,
  useCommands,
  useRegisterCommands,
  type Command,
} from './commandRegistry';

/**
 * Navigation commands for every page; feature pages add their own actions.
 *
 * Every route is here in both views, which is what makes the operator navigation safe to
 * shorten: a screen that is not one of the five destinations is still one Ctrl-K away, and
 * the subtitle is the plain-words line rather than the builder's summary, so searching for
 * what you want to do finds the screen that does it.
 */
export function useShellCommands(): void {
  const view = useViewMode();
  const commands = useMemo<Command[]>(
    () => [
      ...ROUTES.map((route) => ({
        id: `nav:${route.id}`,
        title: isPrimaryRoute(route.id) ? route.title : `${route.title} (developer)`,
        subtitle: routeBlurb(route.id),
        group: 'Pages',
        keywords: [route.path, route.id, route.group, route.description],
        run: (ctx: { navigate: (to: string) => void; close: () => void }) => {
          ctx.navigate(route.path);
          ctx.close();
        },
      })),
      {
        id: 'view:developer',
        title: view.developer ? 'Hide the developer screens' : 'Show the developer screens',
        subtitle: `The other ${DEVELOPER_ROUTE_IDS.length} screens: labs, internals, audit`,
        group: 'Console',
        keywords: ['advanced', 'builder', 'nav', 'navigation'],
        run: (ctx) => {
          view.toggle();
          ctx.close();
        },
      },
    ],
    [view],
  );
  useRegisterCommands(commands);
}

export interface CommandPaletteProps {
  opened: boolean;
  onClose: () => void;
  /** Disable the `/api/search` lookup (tests, or before the backend is up). */
  serverSearch?: boolean;
}

/** Ctrl-K over the command registry plus the server index (config paths, runs, …). */
export function CommandPalette({ opened, onClose, serverSearch = true }: CommandPaletteProps) {
  const navigate = useNavigate();
  const calls = useEndpoints();
  const registered = useCommands();
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);

  useEffect(() => {
    if (!opened) {
      setQuery('');
      setActive(0);
    }
  }, [opened]);

  const remote = useQuery({
    queryKey: shellKeys.search(query),
    queryFn: ({ signal }) => calls.search(query, signal),
    enabled: opened && serverSearch && query.trim().length >= 2,
    staleTime: 15_000,
    retry: false,
  });

  const local = useMemo(() => searchCommands(registered, query), [registered, query]);

  /** Routes the local registry is already offering for this query — one entry per target. */
  const localRoutes = useMemo(
    () => new Set(local.map((command) => command.id.replace(/^nav:/, ''))),
    [local],
  );

  const remoteCommands = useMemo<Command[]>(
    () =>
      (remote.data?.results ?? [])
        // Server page hits are navigable again: `search_service.PAGES` now carries the same
        // ids and paths as `routes.tsx` (pinned by `tests/test_console/test_search.py`), so
        // a page found by its description or its group opens the right route. The only
        // filter left is de-duplication — the shell registers a navigation command per
        // route, and the same page must not appear twice in one list.
        .filter((hit) => hit.route && !(hit.kind === 'page' && localRoutes.has(hit.id)))
        .map((hit) => ({
          id: `hit:${hit.kind}:${hit.id}`,
          title: hit.title,
          ...(hit.subtitle ? { subtitle: hit.subtitle } : {}),
          group: hit.kind,
          run: (ctx) => {
            ctx.navigate(hit.route);
            ctx.close();
          },
        })),
    [remote.data, localRoutes],
  );

  const items = useMemo(() => [...local, ...remoteCommands], [local, remoteCommands]);

  useEffect(() => {
    setActive(0);
  }, [query, items.length]);

  const run = (command: Command | undefined) => {
    if (!command) return;
    command.run({ navigate, close: onClose });
  };

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title={null}
      withCloseButton={false}
      size="lg"
      data-testid="command-palette"
    >
      <Stack gap="xs">
        <TextInput
          placeholder="Search pages, config paths, runs, signals, changes, skills…"
          value={query}
          autoFocus
          data-testid="palette-input"
          onChange={(event) => setQuery(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === 'ArrowDown') {
              event.preventDefault();
              setActive((index) => Math.min(index + 1, items.length - 1));
            } else if (event.key === 'ArrowUp') {
              event.preventDefault();
              setActive((index) => Math.max(index - 1, 0));
            } else if (event.key === 'Enter') {
              event.preventDefault();
              run(items[active]);
            }
          }}
        />
        {remote.isError ? (
          <Text size="xs" c="orange" data-testid="palette-remote-error">
            The server index is unavailable — showing pages and page actions only.
          </Text>
        ) : null}
        <ScrollArea.Autosize mah={360}>
          <Stack gap={2}>
            {items.length === 0 ? (
              <Text size="sm" c="dimmed" p="sm">
                {remote.isFetching ? 'Searching…' : `Nothing matches “${query}”.`}
              </Text>
            ) : (
              items.map((command, index) => (
                <UnstyledButton
                  key={command.id}
                  p="xs"
                  data-testid={`palette-item-${command.id}`}
                  data-active={index === active ? 'true' : 'false'}
                  bg={index === active ? 'var(--mantine-color-default-hover)' : undefined}
                  onMouseEnter={() => setActive(index)}
                  onClick={() => run(command)}
                >
                  <Group justify="space-between" wrap="nowrap">
                    <Stack gap={0}>
                      <Text size="sm">{command.title}</Text>
                      {command.subtitle ? (
                        <Text size="xs" c="dimmed">
                          {command.subtitle}
                        </Text>
                      ) : null}
                    </Stack>
                    <Badge size="xs" variant="light">
                      {command.group}
                    </Badge>
                  </Group>
                </UnstyledButton>
              ))
            )}
          </Stack>
        </ScrollArea.Autosize>
        <Group gap={6} justify="flex-end">
          <Text size="xs" c="dimmed">
            <Kbd>↑</Kbd> <Kbd>↓</Kbd> to move, <Kbd>↵</Kbd> to open, <Kbd>esc</Kbd> to close
          </Text>
        </Group>
      </Stack>
    </Modal>
  );
}
