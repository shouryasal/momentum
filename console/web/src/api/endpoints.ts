import { api, type ApiClient } from './client';
import type {
  ApprovalsPending,
  AuthState,
  HealthResponse,
  HealthSnapshot,
  KillRequest,
  KillResponse,
  LoginResponse,
  MetaResponse,
  Ok,
  ProvidersResponse,
  SafetyStripResponse,
  SearchResponse,
  StepUpResponse,
} from './contracts';

/**
 * Thin typed wrappers over the endpoints the shell itself calls (spec 5.2).
 * Feature pages add their own modules under `src/pages/**`.
 */
export function endpoints(client: ApiClient = api) {
  return {
    login: (token: string) =>
      client.post<LoginResponse>('/auth/login', { token }, { skipAuthRedirect: true }),
    logout: () => client.post<Ok>('/auth/logout'),
    me: () => client.get<AuthState>('/auth/me'),
    stepUp: (token: string) => client.post<StepUpResponse>('/auth/step-up', { token }),

    health: () => client.get<HealthResponse>('/health'),
    /** P1 owns this shape (`console/routers/ops.py`); the shell only derives the dot. */
    opsHealth: () => client.get<HealthSnapshot>('/ops/health'),
    meta: () => client.get<MetaResponse>('/meta'),
    /** The header safety strip; P7 owns `console/routers/invariants.py`. */
    safetyStrip: () => client.get<SafetyStripResponse>('/invariants/strip'),
    approvals: () => client.get<ApprovalsPending>('/approvals/pending'),
    providers: () => client.get<ProvidersResponse>('/llm/providers'),
    search: (q: string, signal?: AbortSignal) =>
      client.get<SearchResponse>('/search', { q }, signal),

    kill: (request: KillRequest) => client.post<KillResponse>('/kill', request),
    resume: (confirmPhrase: string) => client.del<Ok>('/kill', { confirm_phrase: confirmPhrase }),
  };
}

export type Endpoints = ReturnType<typeof endpoints>;
