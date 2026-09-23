/** Typed wrappers over `/api/runs`, `/api/proposals` and `/api/jobs/research/run`. */
import type { ApiClient } from '@/api';

export interface StageRow {
  stage: string;
  started_utc: string;
  finished_utc: string | null;
  requested_model: string | null;
  served_model: string | null;
  provider: string | null;
  chain_index: number | null;
  switched_from: string | null;
  auth_source: string | null;
  effort: string | null;
  escalated: boolean;
  escalation_reasons: string[];
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  num_turns: number | null;
  prompt_version: string | null;
  status: string;
  error: string | null;
}

export interface RunSummary {
  run_id: string;
  started_utc: string;
  finished_utc: string | null;
  cost_usd: number | null;
  escalated: boolean;
  signal_id: string | null;
  trigger_reason: string | null;
  status: string;
  stages: StageRow[];
}

export interface ProposalRow {
  run_id: string;
  shadow: boolean;
  ts_utc: string;
  path: string | null;
  prompt_version: string | null;
  model: string | null;
  module: string | null;
  targets: Record<string, number>;
  exposure_scale: number | null;
  confidence: number | null;
  abstain: boolean;
  horizon_days: number | null;
  rationale: string[];
  invalidation: string | null;
  hard_case_flags: string[];
  valid: boolean;
  invalid_reason: string | null;
  consumed_status: string | null;
  signal_id: string | null;
  approval_status: string | null;
}

export interface ProviderSwitch {
  ts_utc: string;
  task: string;
  stage: string | null;
  from_provider: string | null;
  from_model: string | null;
  to_provider: string | null;
  to_model: string | null;
  reason: string;
  detail: string | null;
}

export interface RunDetail {
  run_id: string;
  stages: StageRow[];
  proposal: ProposalRow | null;
  provider_switches: ProviderSwitch[];
  signal: Record<string, unknown> | null;
}

export interface PendingApproval {
  run_id: string;
  ts_utc: string;
  path: string | null;
  module: string | null;
  confidence: number | null;
  abstain: boolean;
  signal_id: string | null;
  approval_status: string | null;
  expires_utc: string | null;
  seconds_left: number | null;
}

/** `console/routers/approvals.py::DecisionResponse`. */
export interface ApprovalDecision {
  run_id: string;
  decision: 'approve' | 'reject';
  actor: string;
  channel: string;
  decided_utc: string;
  expires_utc: string;
  path: string | null;
  note: string | null;
  proposal_sha256: string | null;
}

export function decisionsApi(client: ApiClient) {
  return {
    runs: (sinceDays?: number) =>
      client.get<{ runs: RunSummary[] }>('/runs', { since_days: sinceDays ?? null, limit: 100 }),
    run: (runId: string) => client.get<RunDetail>(`/runs/${encodeURIComponent(runId)}`),
    /** Markdown, not JSON: `readBody` returns the raw text for a non-JSON content type. */
    trace: (runId: string) => client.get<string>(`/runs/${encodeURIComponent(runId)}/trace`),
    proposals: (sinceDays?: number) =>
      client.get<{ proposals: ProposalRow[] }>('/proposals', {
        since_days: sinceDays ?? null,
        limit: 120,
      }),
    pending: () => client.get<{ pending: PendingApproval[] }>('/proposals/pending'),
    /**
     * The two writes the approvals queue exists for.
     *
     * Both endpoints have existed since P5 and nothing in the SPA called them, so in
     * LIVE·PROPOSE the operator watched the 6h TTL run out on the page the header points
     * them at and had to fall back to Telegram. They are session-tier, not step-up, by
     * design (`console/routers/approvals.py`): making approval costly pushes the decision
     * to Telegram, where there is no step-up at all.
     */
    approve: (runId: string, note?: string) =>
      client.post<ApprovalDecision>(`/proposals/${encodeURIComponent(runId)}/approve`, {
        note: note?.trim() ? note.trim() : null,
      }),
    reject: (runId: string, note?: string) =>
      client.post<ApprovalDecision>(`/proposals/${encodeURIComponent(runId)}/reject`, {
        note: note?.trim() ? note.trim() : null,
      }),
    runResearch: (body: { slot?: string; signal_id?: string } = {}) =>
      client.post<{ spawned: boolean; pid: number | null; slot: string }>(
        '/jobs/research/run',
        body,
      ),
  };
}

export const decisionKeys = {
  all: ['decisions'] as const,
  runs: (days: number) => ['decisions', 'runs', days] as const,
  run: (id: string) => ['decisions', 'run', id] as const,
  trace: (id: string) => ['decisions', 'trace', id] as const,
  proposals: (days: number) => ['decisions', 'proposals', days] as const,
  pending: ['decisions', 'pending'] as const,
};

export const STATUS_COLOR: Record<string, string> = {
  success: 'teal',
  failed: 'red',
  skipped: 'gray',
  throttled: 'yellow',
  killed: 'orange',
  missed: 'red',
};
