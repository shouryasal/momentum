import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

import { api, type ApiClient } from '../api/client';
import type { AuthState } from '../api/contracts';
import { endpoints } from '../api/endpoints';

export interface SessionState {
  session: AuthState | null;
  loading: boolean;
  authenticated: boolean;
  /** True while the step-up window is still open (spec 5.4 `console.stepup_minutes`). */
  stepUpActive: boolean;
  login: (token: string) => Promise<void>;
  logout: () => Promise<void>;
  stepUp: (token: string) => Promise<void>;
  refresh: () => Promise<void>;
}

const SessionContext = createContext<SessionState | null>(null);

const ANONYMOUS: AuthState = {
  authenticated: false,
  actor: null,
  step_up_until: null,
  csrf: null,
  expires: null,
};

export function SessionProvider({
  children,
  client = api,
}: {
  children: ReactNode;
  client?: ApiClient;
}) {
  const calls = useMemo(() => endpoints(client), [client]);
  const [session, setSession] = useState<AuthState | null>(null);
  const [loading, setLoading] = useState(true);
  const [now, setNow] = useState(() => Date.now());

  const refresh = useCallback(async () => {
    try {
      const me = await client.request<AuthState>('/auth/me', {
        method: 'GET',
        skipAuthRedirect: true,
      });
      setSession(me);
      client.setCsrf(me.csrf ?? client.getCsrf());
    } catch {
      setSession(ANONYMOUS);
      client.setCsrf(null);
    } finally {
      setLoading(false);
    }
  }, [client]);

  useEffect(() => {
    client.setUnauthorizedHandler(() => {
      setSession(ANONYMOUS);
      client.setCsrf(null);
    });
    void refresh();
    return () => client.setUnauthorizedHandler(null);
  }, [client, refresh]);

  // Re-render every 15 s so the step-up lock expires on its own.
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 15_000);
    return () => clearInterval(timer);
  }, []);

  const login = useCallback(
    async (token: string) => {
      const result = await calls.login(token);
      client.setCsrf(result.csrf);
      await refresh();
    },
    [calls, client, refresh],
  );

  const logout = useCallback(async () => {
    try {
      await calls.logout();
    } finally {
      client.setCsrf(null);
      setSession(ANONYMOUS);
    }
  }, [calls, client]);

  const stepUp = useCallback(
    async (token: string) => {
      const result = await calls.stepUp(token);
      setSession((current) => ({ ...(current ?? ANONYMOUS), step_up_until: result.step_up_until }));
    },
    [calls],
  );

  const stepUpActive = useMemo(() => {
    const until = session?.step_up_until;
    if (!until) return false;
    const parsed = Date.parse(until);
    return Number.isFinite(parsed) && parsed > now;
  }, [session, now]);

  const value: SessionState = {
    session,
    loading,
    authenticated: session?.authenticated === true,
    stepUpActive,
    login,
    logout,
    stepUp,
    refresh,
  };

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionState {
  const context = useContext(SessionContext);
  if (!context) throw new Error('useSession must be used inside <SessionProvider>');
  return context;
}

/**
 * The session when there is one, `null` otherwise.
 *
 * Shared components (`ConfirmDialog`) need the step-up window and the step-up call, but
 * they are also rendered standalone in unit tests and in storybook-style harnesses. This
 * is the only way to read the session without making every such caller wrap a provider.
 */
export function useOptionalSession(): SessionState | null {
  return useContext(SessionContext);
}
