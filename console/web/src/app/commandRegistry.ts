import { useEffect, useMemo, useRef, useState } from 'react';

import { routeById } from '../routes';

/** A Ctrl-K entry.  Pages register their own; the shell registers navigation + actions. */
export interface Command {
  id: string;
  title: string;
  subtitle?: string;
  group: string;
  keywords?: string[];
  /** `navigate` is the router's push; commands that mutate use their own closures. */
  run: (ctx: CommandContext) => void;
}

export interface CommandContext {
  navigate: (to: string) => void;
  close: () => void;
}

type Listener = () => void;

/**
 * Global command registry.  P1..P7 pages call {@link useRegisterCommands} from their own
 * modules, so extending Ctrl-K never touches `App.tsx`.
 */
export class CommandRegistry {
  private commands = new Map<string, Command>();
  private listeners = new Set<Listener>();

  register(commands: Command[]): () => void {
    for (const command of commands) this.commands.set(command.id, command);
    this.emit();
    return () => {
      for (const command of commands) this.commands.delete(command.id);
      this.emit();
    };
  }

  list(): Command[] {
    return [...this.commands.values()];
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  clear(): void {
    this.commands.clear();
    this.emit();
  }

  private emit(): void {
    for (const listener of this.listeners) listener();
  }
}

export const commandRegistry = new CommandRegistry();

/** Substring first, then subsequence ("ovw" matches "Overview"); -1 means no match. */
export function scoreCommand(command: Command, query: string): number {
  const q = query.trim().toLowerCase();
  if (q === '') return 0;
  const haystacks = [command.title, command.subtitle ?? '', command.group, ...(command.keywords ?? [])];
  let best = -1;
  for (const [index, raw] of haystacks.entries()) {
    const text = raw.toLowerCase();
    const weight = index === 0 ? 0 : 5 + index;
    const at = text.indexOf(q);
    if (at >= 0) {
      const score = 100 - at - weight;
      if (score > best) best = score;
      continue;
    }
    let cursor = 0;
    for (const char of q) {
      const found = text.indexOf(char, cursor);
      if (found < 0) {
        cursor = -1;
        break;
      }
      cursor = found + 1;
    }
    if (cursor > 0) {
      const score = 40 - weight;
      if (score > best) best = score;
    }
  }
  return best;
}

export function searchCommands(commands: Command[], query: string, limit = 30): Command[] {
  if (query.trim() === '') return commands.slice(0, limit);
  return commands
    .map((command) => ({ command, score: scoreCommand(command, query) }))
    .filter((entry) => entry.score >= 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, limit)
    .map((entry) => entry.command);
}

/** Subscribe a component to the registry contents. */
export function useCommands(registry: CommandRegistry = commandRegistry): Command[] {
  const [version, setVersion] = useState(0);
  useEffect(() => registry.subscribe(() => setVersion((v) => v + 1)), [registry]);
  return useMemo(() => registry.list(), [registry, version]);
}

/** Everything a palette entry displays; `run` is deliberately excluded. */
function describe(commands: Command[]): string {
  return commands
    .map((command) =>
      [command.id, command.title, command.subtitle ?? '', command.group, (command.keywords ?? []).join(',')].join(''),
    )
    .join('');
}

/**
 * Register commands for the lifetime of a component (used by feature pages).
 *
 * A page builds its command list inline, so the array identity changes on every render.
 * Registration therefore keys on what the entries *show*, and `run` is called through a
 * ref — otherwise every keystroke on a page would unregister and re-register its commands
 * (and wake every palette subscriber) while still closing over stale state.
 */
export function useRegisterCommands(
  commands: Command[],
  registry: CommandRegistry = commandRegistry,
): void {
  const latest = useRef(commands);
  latest.current = commands;
  const shape = describe(commands);

  useEffect(() => {
    const proxies = latest.current.map((command) => ({
      ...command,
      run: (ctx: CommandContext) => {
        const live = latest.current.find((entry) => entry.id === command.id);
        (live ?? command).run(ctx);
      },
    }));
    return registry.register(proxies);
  }, [shape, registry]);
}

/** A page-scoped palette entry: no group (the page title is the group), no id prefix. */
export type PageCommand = Omit<Command, 'group' | 'id'> & { id: string };

/**
 * The registration every page makes: its own actions, grouped under its title and
 * namespaced by its route id, live only while the page is mounted.
 *
 * `run` receives the palette context, so a command may navigate, open a modal or fire a
 * mutation the page already owns — the palette never needs to know which.
 */
export function usePageCommands(pageId: string, commands: PageCommand[]): void {
  const route = routeById(pageId);
  const group = route?.title ?? pageId;
  const scoped = commands.map((command) => ({
    ...command,
    id: `page:${pageId}:${command.id}`,
    group,
    keywords: [...(command.keywords ?? []), pageId, group],
  }));
  useRegisterCommands(scoped);
}
