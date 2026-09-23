/**
 * Settings page transport: the `/api/config` surface, typed.
 *
 * Nothing here knows any config key. The form, the section tree, the search index and the
 * effect badges are all generated from `schema` + `ui`, which come from the pydantic model,
 * so a new config key needs no change in this folder.
 */

import { api } from '@/api';
import type { JsonSchema } from '@/components';

export type ConfigFormat = 'yaml' | 'json';
export type ConfigKind = 'schema' | 'plain' | 'params' | 'overlay';

export interface ConfigFileRow {
  id: string;
  rel: string;
  title: string;
  description: string;
  kind: ConfigKind;
  format: ConfigFormat;
  editable: boolean;
  read_only_reason: string | null;
  blessed_file: boolean;
  exists: boolean;
  sha: string | null;
  has_schema: boolean;
  bless_ok: boolean | null;
}

/** One schema leaf, flattened to a dotted path — the source of the section tree. */
export interface FieldMeta {
  path: string;
  title: string;
  description: string;
  type: string;
  tier: string;
  group: string;
  unit: string | null;
  widget: string | null;
  effects: string[];
  protected: boolean;
  help_md: string | null;
  enum: unknown[] | null;
  default: unknown;
  minimum: number | null;
  maximum: number | null;
  deprecated: boolean;
}

export interface EffectInfo {
  effect: string;
  title: string;
  detail: string;
  auto_applicable: boolean;
}

export interface PendingEffect extends EffectInfo {
  since_utc: string;
  source: string;
  reason: string | null;
  audit_id: number | null;
}

export interface EffectsBanner {
  pending: PendingEffect[];
  count: number;
  needs_reset: boolean;
  applicable: string[];
}

export interface BlessInfo {
  ok: boolean;
  reason: string;
  blessed_at: string | null;
  blessed_by: string | null;
  changed: string[];
  file_is_blessed: boolean;
}

export interface BlameEntry {
  actor: string;
  ts_utc: string;
  audit_id: number;
  reason: string | null;
}

export interface ConfigDocument {
  id: string;
  rel: string;
  title: string;
  description: string;
  kind: ConfigKind;
  format: ConfigFormat;
  editable: boolean;
  read_only_reason: string | null;
  schema: JsonSchema | null;
  ui: FieldMeta[];
  groups: string[];
  values: Record<string, unknown>;
  raw: string;
  sha: string;
  blame: Record<string, BlameEntry>;
  bless: BlessInfo;
  confirm_phrase: string;
  live_sleeves: string[];
  banner: EffectsBanner;
}

export interface ConfigIssue {
  loc: string;
  msg: string;
  kind: string;
}

export interface PreviewResult {
  file_id: string;
  valid: boolean;
  errors: ConfigIssue[];
  diff: string;
  changed_paths: string[];
  protected_changed: string[];
  locked_paths: string[];
  effects: string[];
  effect_details: EffectInfo[];
  restarts: string[];
  requires_stepup: boolean;
  requires_confirm: boolean;
  confirm_phrase: string;
  reflowed: boolean;
  base_sha: string;
  new_sha: string;
  new_text: string;
}

export interface EffectResult {
  effect: string;
  status: 'applied' | 'failed' | 'skipped' | 'manual';
  detail: string;
  title: string;
}

export interface SaveResult {
  file_id: string;
  rel: string;
  sha: string;
  before_sha: string;
  audit_id: number | null;
  changed_paths: string[];
  protected_changed: string[];
  effects: string[];
  restarts: string[];
  diff: string;
  blessed: boolean;
  git_commit: string | null;
  reflowed: boolean;
  effects_applied: boolean;
  effects_result: EffectResult[];
  banner: EffectsBanner;
}

export interface HistoryEntry {
  id: number;
  ts_utc: string;
  actor: string;
  reason: string | null;
  before_sha: string | null;
  after_sha: string;
  changed_paths: string[];
  effects: string[];
  protected_changed: boolean;
  applied: boolean;
  git_commit: string | null;
  diff: string;
  revertable: boolean;
}

export interface PatchOp {
  op: 'replace' | 'add' | 'remove';
  path: string;
  value?: unknown;
}

export interface DriftReport {
  ok: boolean;
  generators: Array<{ generator: string; ok: boolean | null; detail: string }>;
  bless: { ok: boolean; reason: string; changed: string[] };
  banner: EffectsBanner;
}

export interface SaveRequest {
  base_sha: string;
  reason: string;
  patch?: PatchOp[];
  raw?: string;
  commit?: boolean;
  apply_effects?: boolean;
  confirm_phrase?: string;
}

export const configApi = {
  list: () =>
    api.get<{ files: ConfigFileRow[]; effects: EffectInfo[]; banner: EffectsBanner }>('/config'),
  get: (id: string) => api.get<ConfigDocument>(`/config/${id}`),
  preview: (id: string, body: { patch?: PatchOp[]; raw?: string; base_sha?: string }) =>
    api.post<PreviewResult>(`/config/${id}/preview`, body),
  save: (id: string, body: SaveRequest) => api.put<SaveResult>(`/config/${id}`, body),
  history: (id: string) => api.get<{ file_id: string; entries: HistoryEntry[] }>(
    `/config/${id}/history`,
  ),
  revert: (id: string, body: { audit_id: number; confirm_phrase?: string;
    apply_effects?: boolean; commit?: boolean }) =>
    api.post<SaveResult>(`/config/${id}/revert`, body),
  defaults: (id: string, paths: string[]) =>
    api.post<{ file_id: string; defaults: Record<string, unknown> }>(
      `/config/${id}/defaults`,
      { paths },
    ),
  effects: () => api.get<{ catalogue: EffectInfo[] } & EffectsBanner>('/config/effects'),
  applyEffects: (effects?: string[]) =>
    api.post<{ results: EffectResult[]; banner: EffectsBanner }>('/config/effects/apply', {
      effects: effects ?? null,
    }),
  drift: () => api.get<DriftReport>('/config/drift'),
};

export const configKeys = {
  list: ['config', 'list'] as const,
  file: (id: string) => ['config', 'file', id] as const,
  history: (id: string) => ['config', 'history', id] as const,
  effects: ['config', 'effects'] as const,
  drift: ['config', 'drift'] as const,
};
