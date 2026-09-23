/**
 * Typed wrappers over `/api/llm/*` (spec 5.2), mirroring
 * `console/services/llm_service.py`.
 *
 * Nothing here ever carries a credential: a provider card says whether one is *present*,
 * and a probe returns a redacted verdict. There is no field on any of these types that
 * could hold a key, which is the point — the type is the contract.
 */
import type {
  ApiClient,
  CircuitState,
  MonthTotals,
  ProviderKey,
  ProvidersResponse,
  RateLimitState,
} from '@/api';

/**
 * The provider DTOs live in `src/api/contracts.ts` because the header chip reads the same
 * endpoint; they are re-exported here so this page's imports stay page-local.
 */
export type {
  AuthMode,
  CircuitState,
  MonthTotals,
  ProviderCard,
  ProviderKey,
  ProvidersResponse,
} from '@/api';

export type RateLimit = RateLimitState;

export type UsageGroup = 'task' | 'model' | 'provider' | 'auth' | 'day';

export interface ProbeResult {
  url: string;
  ok: boolean;
  version: string | null;
  error: string | null;
  latency_ms: number;
}

export interface GuidanceOption {
  title: string;
  shell: string;
  commands: string[];
  then: string;
}

export interface OllamaGuidance {
  reachable: boolean;
  base_url: string | null;
  tried: string[];
  wsl_gateway: string | null;
  problem: string;
  options: GuidanceOption[];
  note: string;
}

export interface OllamaDetection {
  base_url: string | null;
  version: string | null;
  ok: boolean;
  cached: boolean;
  results: ProbeResult[];
  guidance: OllamaGuidance;
}

export interface OllamaModel {
  name: string;
  size: number | null;
  parameter_size: string | null;
  quantization: string | null;
  family: string | null;
  modified_at: string | null;
  declared_in_models_yaml: boolean;
}

export interface ChainEntry {
  alias: string;
  provider: string | null;
  id: string | null;
  tier: number | null;
  declared: boolean;
  local?: boolean;
}

export interface TaskRouting {
  task: string;
  chain: ChainEntry[];
  escalation: ChainEntry | null;
  tools: 'none' | 'read_only' | 'skill_rw';
  min_tier: number;
  /** The floor in `runs/llm/types.py`. No config can lower it. */
  code_min_tier: number;
  effective_min_tier: number;
  local_forbidden: boolean;
  allow_local: boolean;
  local_mode: string | null;
  retry: number;
  effort: string | null;
  max_turns: number | null;
  max_usd_per_run: number | null;
  monthly_budget_usd: number | null;
  deadline_s: number | null;
  on_all_failed: string | null;
  /** True when the tier-1 overlay replaced the head of the chain. */
  overlay_head: boolean;
}

export interface RoutingResponse {
  tasks: TaskRouting[];
  switching: Record<string, unknown>;
  budget: { mode: string; monthly_total_usd: number; throttle_at_pct: number };
  shadow: Record<string, unknown>;
  models: Record<string, { provider: string; id: string; tier: number } | null>;
}

export interface UsageRow {
  bucket: string;
  calls: number;
  ok_calls: number;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  avg_latency_ms: number;
  success_rate: number;
}

export interface SwitchRow {
  id: number;
  ts_utc: string;
  task: string;
  run_ref: string | null;
  stage: string | null;
  from_provider: string | null;
  from_model: string | null;
  to_provider: string | null;
  to_model: string | null;
  reason: string;
  detail: string | null;
}

export interface PlaygroundAttempt {
  idx: number;
  model: string;
  status: string;
  error: string | null;
  latency_ms: number;
  cost_usd: number | null;
}

export interface PlaygroundResult {
  ok: boolean;
  text: string | null;
  served: string | null;
  switched: boolean;
  failure: string | null;
  fallback_action: string | null;
  attempts: PlaygroundAttempt[];
  max_usd_per_run: number | null;
}

export interface TestVerdict {
  target: string;
  ok: boolean;
  detail: string;
  latency_ms: number;
}

export const llmKeys = {
  providers: ['llm', 'providers'] as const,
  routing: ['llm', 'routing'] as const,
  detect: ['llm', 'ollama', 'detect'] as const,
  models: ['llm', 'ollama', 'models'] as const,
  usage: (group: UsageGroup) => ['llm', 'usage', group] as const,
  switches: ['llm', 'switches'] as const,
};

export function llmApi(client: ApiClient) {
  return {
    providers: () => client.get<ProvidersResponse>('/llm/providers'),
    testProvider: (key: ProviderKey) =>
      client.post<TestVerdict>(`/llm/providers/${key}/test`),
    resetCircuit: (key: ProviderKey) =>
      client.post<{ provider_key: string; state: CircuitState }>(
        `/llm/providers/${key}/circuit/reset`,
      ),
    detect: () => client.get<OllamaDetection>('/llm/ollama/detect'),
    models: () => client.get<{ models: OllamaModel[] }>('/llm/ollama/models'),
    pull: (model: string) =>
      client.post<{ job_id?: string; model: string }>('/llm/ollama/pull', { model }),
    routing: () => client.get<RoutingResponse>('/llm/routing'),
    usage: (group: UsageGroup, since?: string) =>
      client.get<{ group: UsageGroup; rows: UsageRow[]; month: MonthTotals }>(
        '/llm/usage',
        since ? { group, since } : { group },
      ),
    switches: (task?: string) =>
      client.get<{ switches: SwitchRow[] }>('/llm/switches', task ? { task } : undefined),
    playground: (body: { task: string; prompt: string; model_ref?: string }) =>
      client.post<PlaygroundResult>('/llm/playground', body),
  };
}

export type LlmApi = ReturnType<typeof llmApi>;
