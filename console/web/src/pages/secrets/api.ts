/**
 * Typed wrappers over `/api/secrets/*`.
 *
 * Read the types and the rule is obvious: there is no `value` field anywhere, and no
 * function that fetches one. A secret goes **in** through `setSecret` and only ever comes
 * back as `present` + `last4`.
 */
import type { ApiClient } from '@/api';

export type AuthMode = 'subscription' | 'api_key' | 'auto';

export type TestTarget =
  | 'claude_subscription'
  | 'claude_login'
  | 'claude_api_key'
  | 'ollama'
  | 'telegram'
  | 'binance_a'
  | 'binance_b'
  | 'freqtrade_a'
  | 'freqtrade_b';

export const TEST_TARGETS: TestTarget[] = [
  'claude_subscription', 'claude_login', 'claude_api_key', 'ollama', 'telegram',
  'binance_a', 'binance_b', 'freqtrade_a', 'freqtrade_b',
];

export interface SecretRow {
  name: string;
  label: string;
  group: string;
  used_by: string[];
  required: boolean;
  help: string;
  present: boolean;
  last4: string | null;
  updated_at: string | null;
}

export interface LoginSession {
  present: boolean;
  expires_at: string | null;
  expired: boolean;
  subscription_type: string | null;
}

export interface AuthState {
  configured: AuthMode | null;
  env: AuthMode | null;
  effective: AuthMode;
  in_sync: boolean;
  subscription_source: 'token' | 'login';
  login_session: LoginSession;
}

export interface SecretList {
  secrets: SecretRow[];
  auth: AuthState;
}

export interface SecretState {
  name: string;
  present: boolean;
  last4: string | null;
  updated_at: string | null;
}

export interface TestVerdict {
  target: string;
  ok: boolean;
  detail: string;
  latency_ms: number;
}

export const secretKeys = {
  list: ['secrets', 'list'] as const,
};

export function secretsApi(client: ApiClient) {
  return {
    list: () => client.get<SecretList>('/secrets'),
    setSecret: (name: string, value: string) =>
      client.put<SecretState>(`/secrets/${name}`, { value }),
    deleteSecret: (name: string) => client.del<SecretState>(`/secrets/${name}`),
    setAuthMode: (mode: AuthMode, reason?: string) =>
      client.put<AuthState>('/secrets/auth-mode', reason ? { mode, reason } : { mode }),
    test: (target: TestTarget) => client.post<TestVerdict>(`/secrets/test/${target}`),
  };
}

export type SecretsApi = ReturnType<typeof secretsApi>;

/** Which probe belongs to a secret row, so each row gets a meaningful Test button. */
export const TARGET_FOR_SECRET: Record<string, TestTarget> = {
  CLAUDE_CODE_OAUTH_TOKEN: 'claude_subscription',
  ANTHROPIC_API_KEY: 'claude_api_key',
  TELEGRAM_BOT_TOKEN: 'telegram',
  TELEGRAM_CHAT_ID: 'telegram',
  BINANCE_KEY_A: 'binance_a',
  BINANCE_SECRET_A: 'binance_a',
  BINANCE_KEY_B: 'binance_b',
  BINANCE_SECRET_B: 'binance_b',
  FT_API_PASSWORD_A: 'freqtrade_a',
  FT_API_PASSWORD_B: 'freqtrade_b',
};
