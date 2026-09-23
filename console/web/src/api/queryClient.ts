import { QueryClient } from '@tanstack/react-query';

import { ApiError } from './errors';

/** Never retry a request the server deliberately refused. */
function shouldRetry(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
  return failureCount < 2;
}

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        gcTime: 5 * 60_000,
        refetchOnWindowFocus: false,
        retry: shouldRetry,
      },
      mutations: {
        retry: false,
      },
    },
  });
}

/** Query keys used by the shell; pages own their own namespaces. */
export const shellKeys = {
  session: ['auth', 'me'] as const,
  meta: ['meta'] as const,
  health: ['ops', 'health'] as const,
  approvals: ['approvals', 'pending'] as const,
  providers: ['llm', 'providers'] as const,
  safetyStrip: ['invariants', 'strip'] as const,
  search: (q: string) => ['search', q] as const,
};
