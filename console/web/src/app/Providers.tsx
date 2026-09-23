import { MantineProvider } from '@mantine/core';
import { Notifications } from '@mantine/notifications';
import { QueryClientProvider, type QueryClient } from '@tanstack/react-query';
import { useMemo, type ReactNode } from 'react';

import { api, type ApiClient } from '../api/client';
import { createQueryClient } from '../api/queryClient';
import type { EventSourceFactory } from '../api/sse';
import { theme } from '../theme';
import { ApiProvider } from './ApiContext';
import { EventStreamProvider } from './EventStreamContext';
import { SessionProvider, useSession } from './SessionContext';
import { ViewModeProvider, type ViewMode } from './ViewModeContext';

export interface AppProvidersProps {
  children: ReactNode;
  client?: ApiClient;
  queryClient?: QueryClient;
  /** Injected in tests; production uses the real `EventSource`. */
  sseFactory?: EventSourceFactory;
  /** Force the stream on/off; by default it follows the session. */
  sseEnabled?: boolean;
  /** Start in a known view; by default the operator's stored preference, else Simple. */
  viewMode?: ViewMode;
}

function AuthedEventStream({
  children,
  factory,
  enabled,
}: {
  children: ReactNode;
  factory?: EventSourceFactory;
  enabled?: boolean;
}) {
  const { authenticated } = useSession();
  return (
    <EventStreamProvider
      enabled={enabled ?? authenticated}
      {...(factory ? { factory } : {})}
    >
      {children}
    </EventStreamProvider>
  );
}

/** Mantine + React Query + session + SSE, in the order the shell needs them. */
export function AppProviders({
  children,
  client = api,
  queryClient,
  sseFactory,
  sseEnabled,
  viewMode,
}: AppProvidersProps) {
  const qc = useMemo(() => queryClient ?? createQueryClient(), [queryClient]);
  return (
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <Notifications position="top-right" limit={4} />
      <QueryClientProvider client={qc}>
        <ApiProvider client={client}>
          <SessionProvider client={client}>
            <ViewModeProvider {...(viewMode ? { initial: viewMode } : {})}>
              <AuthedEventStream
                {...(sseFactory ? { factory: sseFactory } : {})}
                {...(sseEnabled === undefined ? {} : { enabled: sseEnabled })}
              >
                {children}
              </AuthedEventStream>
            </ViewModeProvider>
          </SessionProvider>
        </ApiProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}
