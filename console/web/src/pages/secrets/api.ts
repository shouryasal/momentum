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

/* ------------------------------------------------------------------ signing in to Claude */

/**
 * `GET|POST|DELETE /api/llm/claude/signin` and `POST /api/llm/claude/signin/code`.
 *
 * Same asymmetry as the rest of this file, for the same reason: the flow that *captures*
 * a token is server-side from end to end, and the browser only ever learns what state it
 * is in. There is no `token` field on any type below — that is the contract, not an
 * oversight, and `tests/test_console/test_claude_signin.py` pins it on the other side.
 *
 * The code goes the other way, and only the other way: `submitCode` posts what the
 * browser showed, and nothing on the way back carries it.
 */
export type SignInState =
  | 'idle'
  /** The CLI is spawning; no link yet. */
  | 'starting'
  /** The link is parsed and the operator has to approve it in a browser. */
  | 'url_ready'
  /** The CLI is at its prompt, waiting for the code the callback page showed. */
  | 'awaiting_code'
  /** The code went in; the CLI is redeeming it and the console is storing the result. */
  | 'exchanging'
  | 'done'
  | 'failed'
  | 'cancelled';

/** The closed set of reasons a sign-in stopped; the modal explains each one. */
export type SignInReason =
  | 'cli_missing'
  | 'unsupported'
  | 'no_link'
  | 'not_approved'
  | 'no_code'
  | 'bad_code'
  | 'cli_failed'
  | 'no_token'
  | 'cancelled'
  | 'store_failed';

export interface SignInSession {
  id: string;
  state: Exclude<SignInState, 'idle'>;
  /** The state, or `failed:<reason>` — one string per distinct phase, for the poll. */
  phase: string;
  message: string;
  url: string | null;
  reason: SignInReason | null;
  actor: string;
  started_at: string;
  updated_at: string;
  finished_at: string | null;
  last4: string | null;
  deadline_at: string | null;
  exit_code: number | null;
  attempts: number;
  attempts_left: number;
  terminal: boolean;
}

export interface SignInCli {
  present: boolean;
  path: string | null;
  version: string | null;
  pty: boolean;
}

export interface SignInCredential {
  name: string;
  present: boolean;
  last4: string | null;
  updated_at: string | null;
}

export interface SignInStatus {
  state: SignInState;
  phase: string;
  session: SignInSession | null;
  cli: SignInCli;
  credential: SignInCredential;
}

/* ------------------------------------------------------------------ the local model */

export type LocalModelState =
  | 'connected'
  | 'pulling'
  | 'missing'
  | 'unreachable'
  | 'disabled'
  | 'unknown';

/**
 * `GET /api/llm/local-model` — read-only on purpose.
 *
 * The backend detects the endpoint, picks the model `models.yaml` already routes to and
 * pulls it when it is missing. There is no setter here because there is no setter on the
 * server: the operator should never type a base URL.
 */
export interface LocalModelStatus {
  state: LocalModelState;
  line: string;
  model: string | null;
  base_url: string | null;
  version: string | null;
  tok_per_s: number | null;
  reason: string | null;
  fix_command: string | null;
  fix_shell: string | null;
  pull: { job_id: string; progress: number; message: string | null } | null;
  tried: string[];
  configurable: false;
}

export const secretKeys = {
  list: ['secrets', 'list'] as const,
  signIn: ['secrets', 'claude-signin'] as const,
  localModel: ['secrets', 'local-model'] as const,
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
    signInStatus: () => client.get<SignInStatus>('/llm/claude/signin'),
    startSignIn: (replace: boolean) =>
      client.post<SignInStatus>('/llm/claude/signin', { replace }),
    submitSignInCode: (code: string) =>
      client.post<SignInStatus>('/llm/claude/signin/code', { code }),
    cancelSignIn: () => client.del<SignInStatus>('/llm/claude/signin'),
    localModel: () => client.get<LocalModelStatus>('/llm/local-model'),
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
