/**
 * Self-Improvement page API types and hooks.
 *
 * The one thing this page exists to show: what the model *claimed* about its own change
 * next to what `evals/verify_change.py` *recomputed*, with the disagreements in red.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import type { ApiClient } from '@/api';
import { useApi } from '@/app/ApiContext';

export type ChangeStatus =
  | 'proposed' | 'verifying' | 'held' | 'auto_merged' | 'approved'
  | 'rejected' | 'reverted' | 'superseded';

export interface ChangeRow {
  change_id: string;
  proposed_at: string;
  kind: string;
  op: string | null;
  target: string;
  status: ChangeStatus;
  author_model: string | null;
  author_run_id: string | null;
  decided_at: string | null;
  decided_by: string | null;
  reason: string | null;
  merge_commit: string | null;
  branch: string | null;
  revert_of: string | null;
  reverted_by: string | null;
}

export interface ChangeList {
  counts: Record<string, number>;
  items: ChangeRow[];
}

export interface EvidenceRow {
  field: string;
  claimed: unknown;
  verified: unknown;
  mismatch: boolean;
  delta_pct: number | null;
}

export interface CheckRow {
  name: string;
  verdict: 'pass' | 'fail' | 'hold' | 'skip';
  detail: string;
  data?: Record<string, unknown>;
}

export interface ChangeEvent {
  id: number;
  ts_utc: string;
  event: string;
  actor: string;
  commit_sha: string | null;
  note: string | null;
}

export interface ChangeDetail extends ChangeRow {
  claimed_evidence: Record<string, unknown>;
  verified_evidence: Record<string, unknown>;
  evidence: EvidenceRow[];
  mismatch_count: number;
  checks: CheckRow[];
  events: ChangeEvent[];
  diff: string;
  change_file: Record<string, unknown> | null;
  can_revert: boolean;
  can_decide: boolean;
}

export type AutonomySetting = 'auto' | 'approve' | 'off';

export interface AutonomyMatrix {
  tier1_auto_merge: boolean;
  live_forces_human: boolean;
  max_auto_merges_per_week: number;
  auto_revert: {
    enabled: boolean;
    window_days: number;
    validity_drop_pct: number;
    breach_increase: number;
  };
  kinds: Record<string, { test: AutonomySetting; live: AutonomySetting }>;
  mode: 'test' | 'live';
  effective: Record<string, AutonomySetting>;
  invariants: string[];
}

export interface TimelinePoint {
  change_id: string;
  ts_utc: string;
  event: string;
  commit_sha: string | null;
}

export interface RecurringCause {
  recurrence_key: string;
  weeks: number;
  events: number;
  escalated: number;
}

export const improvementKeys = {
  list: (status?: string) => ['changes', 'list', status ?? 'all'] as const,
  detail: (id: string) => ['changes', 'detail', id] as const,
  autonomy: ['changes', 'autonomy'] as const,
  timeline: ['changes', 'timeline'] as const,
};

export function useChanges(status?: string) {
  const client = useApi();
  return useQuery({
    queryKey: improvementKeys.list(status),
    queryFn: () =>
      client.get<ChangeList>('/changes', status ? { status } : undefined),
  });
}

export function useChange(id: string | null) {
  const client = useApi();
  return useQuery({
    queryKey: improvementKeys.detail(id ?? ''),
    queryFn: () => client.get<ChangeDetail>(`/changes/${encodeURIComponent(id ?? '')}`),
    enabled: Boolean(id),
  });
}

export function useAutonomy() {
  const client = useApi();
  return useQuery({
    queryKey: improvementKeys.autonomy,
    queryFn: () => client.get<AutonomyMatrix>('/autonomy'),
  });
}

export function useTimeline() {
  const client = useApi();
  return useQuery({
    queryKey: improvementKeys.timeline,
    queryFn: () =>
      client.get<{ items: TimelinePoint[]; recurring_causes: RecurringCause[] }>(
        '/changes/timeline',
      ),
  });
}

export type DecisionAction = 'approve' | 'reject' | 'revert' | 'attach';

interface DecisionVars {
  id: string;
  action: DecisionAction;
  note?: string;
  reason?: string;
  task?: string;
}

function decisionBody(vars: DecisionVars): Record<string, unknown> {
  if (vars.action === 'revert') return { reason: vars.reason ?? '' };
  if (vars.action === 'attach') return { task: vars.task ?? '' };
  return { note: vars.note ?? null };
}

export function useChangeDecision() {
  const client: ApiClient = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vars: DecisionVars) =>
      client.post<{ change_id: string; status: string; reason: string }>(
        `/changes/${encodeURIComponent(vars.id)}/${vars.action}`,
        decisionBody(vars),
      ),
    onSuccess: (_data, vars) => {
      void queryClient.invalidateQueries({ queryKey: ['changes'] });
      void queryClient.invalidateQueries({ queryKey: improvementKeys.detail(vars.id) });
    },
  });
}

export function useSaveAutonomy() {
  const client = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<AutonomyMatrix>) => client.put<unknown>('/autonomy', body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: improvementKeys.autonomy });
    },
  });
}

/** Human-readable label for a matrix key, so the UI never shows a bare slug. */
export const KIND_LABELS: Record<string, string> = {
  params: 'Sleeve parameters',
  prompt: 'Prompts',
  skill_edit: 'Skill body / tests',
  skill_new: 'New skill',
  skill_bind: 'Bind a skill to a task',
  model: 'Model promotion',
  revert: 'Revert a merged change',
};

export const STATUS_COLOURS: Record<string, string> = {
  proposed: 'blue',
  verifying: 'indigo',
  held: 'yellow',
  auto_merged: 'teal',
  approved: 'green',
  rejected: 'gray',
  reverted: 'orange',
  superseded: 'gray',
};
