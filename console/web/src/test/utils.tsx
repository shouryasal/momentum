import { MantineProvider } from '@mantine/core';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, type RenderOptions, type RenderResult } from '@testing-library/react';
import type { ReactElement, ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';

import type { EventSourceLike } from '../api/sse';
import { theme } from '../theme';

/** Test QueryClient: no retries, no cache bleed between tests. */
export function testQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

export function renderWithProviders(
  ui: ReactElement,
  options: { route?: string; queryClient?: QueryClient } & RenderOptions = {},
): RenderResult & { queryClient: QueryClient } {
  const { route = '/', queryClient = testQueryClient(), ...rest } = options;
  const Wrapper = ({ children }: { children: ReactNode }) => (
    <MantineProvider theme={theme} defaultColorScheme="dark" env="test">
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
      </QueryClientProvider>
    </MantineProvider>
  );
  return { ...render(ui, { wrapper: Wrapper, ...rest }), queryClient };
}

/** Minimal scriptable `EventSource` for the SSE tests. */
export class FakeEventSource implements EventSourceLike {
  static instances: FakeEventSource[] = [];
  readonly url: string;
  closed = false;
  private listeners = new Map<string, Array<(event: MessageEvent) => void>>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  static reset(): void {
    FakeEventSource.instances = [];
  }

  static get last(): FakeEventSource | undefined {
    return FakeEventSource.instances[FakeEventSource.instances.length - 1];
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void): void {
    const bucket = this.listeners.get(type) ?? [];
    bucket.push(listener);
    this.listeners.set(type, bucket);
  }

  removeEventListener(type: string, listener: (event: MessageEvent) => void): void {
    const bucket = this.listeners.get(type) ?? [];
    this.listeners.set(
      type,
      bucket.filter((entry) => entry !== listener),
    );
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, data?: unknown, lastEventId = ''): void {
    const event = {
      data: typeof data === 'string' ? data : JSON.stringify(data ?? {}),
      lastEventId,
      type,
    } as MessageEvent;
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

/** Build a `fetch` stub from a path -> response map. */
export function fakeFetch(
  routes: Record<string, { status?: number; body?: unknown; contentType?: string }>,
): typeof fetch {
  return (async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const path = url.split('?')[0] ?? url;
    const match = routes[path] ?? routes[url];
    if (!match) {
      return new Response(JSON.stringify({ error: { code: 'not_found', message: path, detail: null } }), {
        status: 404,
        headers: { 'content-type': 'application/json' },
      });
    }
    return new Response(match.body === undefined ? '' : JSON.stringify(match.body), {
      status: match.status ?? 200,
      headers: { 'content-type': match.contentType ?? 'application/json' },
    });
  }) as unknown as typeof fetch;
}
