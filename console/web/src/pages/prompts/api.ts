/** Prompts page API types and hooks. */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useApi } from '@/app/ApiContext';

export interface PromptVersion {
  path: string;
  version: number;
  version_id: string;
  sha: string;
  bytes: number;
  tokens: number;
  snapshots: number;
  immutable: boolean;
  active: boolean;
  placeholders: string[];
}

export interface PromptFamily {
  family: string;
  active: string | null;
  versions: PromptVersion[];
}

export interface PromptDetail {
  path: string;
  content: string;
  sha: string;
  version_id: string;
  snapshots: number;
  immutable: boolean;
  tokens: number;
  placeholders: string[];
  snapshots_using: Array<{ run_id: string; ts_utc: string; model: string; valid: number }>;
}

export interface RenderResult {
  path: string;
  text: string;
  tokens: number;
  budget_tokens: number | null;
  over_budget: boolean;
  unresolved_placeholders: string[];
}

export const promptKeys = {
  list: ['prompts', 'list'] as const,
  detail: (path: string) => ['prompts', 'detail', path] as const,
  diff: (a: string, b: string) => ['prompts', 'diff', a, b] as const,
};

export function usePrompts() {
  const client = useApi();
  return useQuery({
    queryKey: promptKeys.list,
    queryFn: () =>
      client.get<{ families: PromptFamily[]; active: Record<string, string> }>('/prompts'),
  });
}

export function usePrompt(path: string | null) {
  const client = useApi();
  return useQuery({
    queryKey: promptKeys.detail(path ?? ''),
    queryFn: () => client.get<PromptDetail>(`/prompts/${path ?? ''}`),
    enabled: Boolean(path),
  });
}

export function usePromptDiff(left: string | null, right: string | null) {
  const client = useApi();
  return useQuery({
    queryKey: promptKeys.diff(left ?? '', right ?? ''),
    queryFn: () =>
      client.get<{ left: string; right: string; diff: string }>('/prompts/diff', {
        left: left ?? '',
        right: right ?? '',
      }),
    enabled: Boolean(left && right && left !== right),
  });
}

export function useSavePrompt() {
  const client = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vars: {
      path: string;
      content: string;
      as_new_version: boolean;
      base_sha: string | null;
    }) =>
      client.put<{ version_id: string; path?: string; sha: string }>(
        `/prompts/${vars.path}`,
        {
          content: vars.content,
          as_new_version: vars.as_new_version,
          base_sha: vars.base_sha,
        },
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['prompts'] });
    },
  });
}

export function useActivatePrompt() {
  const client = useApi();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (vars: { family: string; version: string }) =>
      client.put<{ family: string; version_id: string; overlay: string }>('/prompts/active', vars),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['prompts'] });
    },
  });
}

export function useRenderPrompt() {
  const client = useApi();
  return useMutation({
    mutationFn: (vars: { path: string; context: Record<string, string> }) =>
      client.post<RenderResult>(`/prompts/${vars.path}/render`, { context: vars.context }),
  });
}
