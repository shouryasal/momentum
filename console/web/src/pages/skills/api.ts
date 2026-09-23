/** Skills page API types and hooks. */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useState } from 'react';

import { useApi } from '@/app/ApiContext';
import { useTopicEvents } from '@/app/EventStreamContext';

export type SkillStatus = 'incubating' | 'bound' | 'archived' | 'unbound' | 'template';

export interface SkillPolicy {
  body: 'human' | 'gated';
  scripts: 'human' | 'gated';
  tests: 'human' | 'gated';
}

export interface SkillRow {
  name: string;
  status: SkillStatus;
  origin: string;
  bindings: string[];
  policy: SkillPolicy;
  has_tests: boolean;
  test_files: number;
  has_evals: boolean;
  has_scripts: boolean;
  updated_at: string | null;
  last_change: { change_id: string; status: string; decided_at: string | null } | null;
}

export interface SkillFileNode {
  path: string;
  size: number;
  tier: 'tier1' | 'tier2';
  editable: boolean;
  sha: string;
}

export interface SkillFile {
  skill: string;
  path: string;
  content: string;
  sha: string;
  tier: 'tier1' | 'tier2';
  requires_step_up: boolean;
}

export interface LintFinding {
  code: string;
  message: string;
  path: string;
  severity: 'error' | 'warn';
}

export interface LintResult {
  name: string;
  ok: boolean;
  findings: LintFinding[];
}

export interface TestResult {
  skill: string;
  ok: boolean;
  log: string;
}

export interface EvalCase {
  id: string;
  status: 'pass' | 'fail' | 'skip' | 'error';
  detail: string;
}

export interface EvalResult {
  name: string;
  total: number;
  passed: number;
  skipped: number;
  pass_rate: number;
  min_pass_rate: number;
  ok: boolean;
  cases: EvalCase[];
}

export interface TrialResult {
  skill: string;
  worktree: string;
  branch: string;
  ok: boolean;
  text: string | null;
  error: string | null;
}

export const skillKeys = {
  list: ['skills', 'list'] as const,
  tree: (name: string) => ['skills', 'tree', name] as const,
  file: (name: string, path: string) => ['skills', 'file', name, path] as const,
};

export function useSkills() {
  const client = useApi();
  return useQuery({
    queryKey: skillKeys.list,
    queryFn: () => client.get<{ items: SkillRow[] }>('/skills'),
  });
}

export function useSkillTree(name: string | null) {
  const client = useApi();
  return useQuery({
    queryKey: skillKeys.tree(name ?? ''),
    queryFn: () =>
      client.get<{ skill: string; files: SkillFileNode[] }>(
        `/skills/${encodeURIComponent(name ?? '')}/tree`,
      ),
    enabled: Boolean(name),
  });
}

export function useSkillFile(name: string | null, path: string | null) {
  const client = useApi();
  return useQuery({
    queryKey: skillKeys.file(name ?? '', path ?? ''),
    queryFn: () =>
      client.get<SkillFile>(
        `/skills/${encodeURIComponent(name ?? '')}/files/${path ?? ''}`,
      ),
    enabled: Boolean(name && path),
  });
}

export function useSaveSkillFile(name: string) {
  const client = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vars: { path: string; content: string; base_sha: string | null }) =>
      client.put<{ sha: string; lint_ok: boolean; findings: LintFinding[] }>(
        `/skills/${encodeURIComponent(name)}/files/${vars.path}`,
        { content: vars.content, base_sha: vars.base_sha },
      ),
    onSuccess: (_data, vars) => {
      void queryClient.invalidateQueries({ queryKey: skillKeys.file(name, vars.path) });
      void queryClient.invalidateQueries({ queryKey: skillKeys.tree(name) });
    },
  });
}

export function useCreateSkill() {
  const client = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; description: string; title?: string }) =>
      client.post<{ name: string; status: string; files: string[] }>('/skills', body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: skillKeys.list });
    },
  });
}

export function useArchiveSkill() {
  const client = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vars: { name: string; restore: boolean }) =>
      client.post<{ name: string; status: string }>(
        `/skills/${encodeURIComponent(vars.name)}/archive`,
        { restore: vars.restore },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: skillKeys.list });
    },
  });
}

export function useSetBindings() {
  const client = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vars: { name: string; tasks: string[] }) =>
      client.put<unknown>(`/skills/${encodeURIComponent(vars.name)}/bindings`, {
        tasks: vars.tasks,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: skillKeys.list });
    },
  });
}

export type CheckKind = 'lint' | 'test' | 'eval' | 'trial';

export type CheckResult = LintResult | TestResult | EvalResult | TrialResult;

/** What a check POST hands back now: a handle, never the findings. */
export interface CheckStarted {
  job_id: string | null;
  skill: string;
  kind: CheckKind;
  status: JobStatus;
  topic: string;
}

export type JobStatus = 'queued' | 'running' | 'ok' | 'failed' | 'cancelled';

/** `GET /api/skills/jobs/{job_id}` — the same state the `job` SSE topic announces. */
export interface CheckJob {
  id: string;
  job: string;
  status: JobStatus;
  progress: number;
  message: string | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  actor: string;
  terminal: boolean;
  output: string[];
  dropped: number;
  result: CheckResult | null;
  labels?: { area?: string; kind?: CheckKind; skill?: string };
}

/** One `job` SSE event. `event` says whether it carries a status or output lines. */
export interface JobEvent {
  event?: 'status' | 'output';
  id?: string;
  job?: string;
  status?: JobStatus;
  progress?: number;
  message?: string | null;
  error?: string | null;
  lines?: string[];
  dropped?: number;
  labels?: { area?: string; kind?: CheckKind; skill?: string };
}

export function useStartCheck(name: string) {
  const client = useApi();
  return useMutation({
    mutationFn: (vars: { kind: CheckKind; prompt?: string; strict?: boolean }) =>
      client.post<CheckStarted>(
        `/skills/${encodeURIComponent(name)}/${vars.kind}${
          vars.kind === 'lint' && vars.strict === false ? '?strict=false' : ''
        }`,
        vars.kind === 'trial' ? { prompt: vars.prompt ?? '' } : {},
      ),
  });
}

export function useCancelCheck() {
  const client = useApi();
  return useMutation({
    mutationFn: (jobId: string) =>
      client.post<{ id: string; cancelled: boolean; status: JobStatus }>(
        `/skills/jobs/${encodeURIComponent(jobId)}/cancel`,
        {},
      ),
  });
}

export function fetchCheckJob(
  client: ReturnType<typeof useApi>,
  jobId: string,
): Promise<CheckJob> {
  return client.get<CheckJob>(`/skills/jobs/${encodeURIComponent(jobId)}`);
}

const TERMINAL_STATUSES: readonly JobStatus[] = ['ok', 'failed', 'cancelled'];
/** How much output the page keeps. The server keeps its own, smaller, window. */
export const MAX_OUTPUT_LINES = 2000;
/** Fallback refresh while a job runs. SSE is what makes it feel live; this is the net. */
const FALLBACK_POLL_MS = 2000;

export interface CheckRun {
  jobId: string | null;
  kind: CheckKind | null;
  status: JobStatus | null;
  progress: number;
  message: string | null;
  error: string | null;
  output: string[];
  result: CheckResult | null;
  running: boolean;
  starting: boolean;
  start: (vars: { kind: CheckKind; prompt?: string; strict?: boolean }) => void;
  cancel: () => void;
  cancelling: boolean;
}

/**
 * Run one check as a job and follow it.
 *
 * The four buttons return a `job_id`; progress, output and the verdict arrive on the
 * shell's `job` SSE topic, filtered to this run's id. `GET /api/skills/jobs/{id}` is
 * read when the job ends (for the result) and, while it runs, on a slow fallback timer —
 * so a page rendered outside the shell, or one whose stream dropped, still reaches a
 * verdict instead of spinning forever.
 */
export function useCheckRun(name: string): CheckRun {
  const client = useApi();
  const starter = useStartCheck(name);
  const canceller = useCancelCheck();
  const [job, setJob] = useState<{ id: string; kind: CheckKind } | null>(null);
  const [status, setStatus] = useState<JobStatus | null>(null);
  const [progress, setProgress] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [output, setOutput] = useState<string[]>([]);
  const [result, setResult] = useState<CheckResult | null>(null);
  const [refresh, setRefresh] = useState(0);

  const terminal = status !== null && TERMINAL_STATUSES.includes(status);

  useTopicEvents(['job'], (event) => {
    const payload = event.payload as unknown as JobEvent;
    if (!job || !payload || payload.id !== job.id) return;
    if (payload.event === 'output') {
      setOutput((current) => [...current, ...(payload.lines ?? [])].slice(-MAX_OUTPUT_LINES));
      return;
    }
    if (payload.status) setStatus(payload.status);
    if (typeof payload.progress === 'number') setProgress(payload.progress);
    setMessage(payload.message ?? null);
    setError(payload.error ?? null);
  });

  useEffect(() => {
    if (!job) return undefined;
    let live = true;
    const load = async () => {
      try {
        const detail = await fetchCheckJob(client, job.id);
        if (!live) return;
        setStatus(detail.status);
        setProgress(detail.progress);
        setMessage(detail.message);
        setError(detail.error);
        setResult(detail.result);
        if (detail.output.length) setOutput(detail.output.slice(-MAX_OUTPUT_LINES));
      } catch (e) {
        if (live) setError((e as Error).message);
      }
    };
    void load();
    if (terminal) return () => { live = false; };
    const timer = window.setInterval(() => void load(), FALLBACK_POLL_MS);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [client, job, terminal, refresh]);

  const start = useCallback(
    (vars: { kind: CheckKind; prompt?: string; strict?: boolean }) => {
      setStatus('queued');
      setProgress(0);
      setMessage(null);
      setError(null);
      setOutput([]);
      setResult(null);
      setJob(null);
      starter.mutate(vars, {
        onSuccess: (started) => {
          if (started.job_id) setJob({ id: started.job_id, kind: vars.kind });
          else setStatus(started.status);
        },
        onError: (e) => {
          setStatus('failed');
          setError((e as Error).message);
        },
      });
    },
    [starter],
  );

  const cancel = useCallback(() => {
    if (!job) return;
    // Read the run back straight away: a cancel that takes a poll interval to show up
    // reads as a button that did nothing.
    canceller.mutate(job.id, { onSettled: () => setRefresh((n) => n + 1) });
  }, [canceller, job]);

  return {
    jobId: job?.id ?? null,
    kind: job?.kind ?? null,
    status,
    progress,
    message,
    error,
    output,
    result,
    running: status !== null && !terminal,
    starting: starter.isPending,
    start,
    cancel,
    cancelling: canceller.isPending,
  };
}

export const STATUS_COLOURS: Record<string, string> = {
  bound: 'teal',
  incubating: 'blue',
  unbound: 'gray',
  archived: 'orange',
  template: 'grape',
};
