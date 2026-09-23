/** Typed wrappers over `/api/knowledge/*` and `/api/reports*`. */
import type { ApiClient } from '@/api';

export interface BriefEntry {
  date: string;
  path: string;
  bytes: number;
  row: Record<string, unknown> | null;
}

export interface NewsItem {
  id: number;
  url_hash: string;
  source: string;
  source_class: 'primary' | 'secondary';
  title: string;
  url: string;
  published_at: string | null;
  fetched_at: string;
  assets: string[];
  event_class: string | null;
  cluster_id: string | null;
  corroborated: boolean;
  corroborating_sources: number;
  classified_by: string | null;
  claim_verified: number | null;
  label_source: string;
}

export interface SourceReliability {
  source: string;
  n_unconfirmed: number;
  n_corroborated_later: number;
  n_falsified: number;
  n_claims_checked: number;
  n_claims_verified: number;
}

export interface DossierEntry {
  asset: string;
  path: string;
  bytes: number;
}

export interface ReportEntry {
  path: string;
  bytes: number;
  modified: string;
}

export interface StateResponse {
  latest: Record<string, unknown> | null;
  path: string;
  snapshots?: Array<{ id: number; ts_utc: string; regime: string | null; producer: string }>;
}

export function knowledgeApi(client: ApiClient) {
  return {
    briefs: () => client.get<{ briefs: BriefEntry[] }>('/knowledge/briefs'),
    brief: (date: string) => client.get<string>(`/knowledge/briefs/${encodeURIComponent(date)}`),
    news: (params: { hours?: number; event_class?: string | null; corroborated?: boolean | null }) =>
      client.get<{ news: NewsItem[] }>('/knowledge/news', {
        hours: params.hours ?? 48,
        event_class: params.event_class ?? null,
        corroborated: params.corroborated ?? null,
        limit: 300,
      }),
    sources: () => client.get<{ sources: SourceReliability[] }>('/knowledge/sources'),
    state: (history = 20) => client.get<StateResponse>('/knowledge/state', { history }),
    dossiers: () => client.get<{ dossiers: DossierEntry[] }>('/knowledge/dossiers'),
    dossier: (asset: string) => client.get<string>(`/knowledge/dossiers/${encodeURIComponent(asset)}`),
    incidents: (days = 30) =>
      client.get<{ incidents: Array<Record<string, unknown>> }>('/knowledge/incidents', { days }),
    grades: () => client.get<{ grades: Array<Record<string, unknown>> }>('/knowledge/grades'),
    reports: () => client.get<{ reports: ReportEntry[] }>('/reports'),
    report: (path: string) => client.get<string>(`/reports/${path}`),
  };
}

export const knowledgeKeys = {
  all: ['knowledge'] as const,
  briefs: ['knowledge', 'briefs'] as const,
  brief: (date: string) => ['knowledge', 'brief', date] as const,
  news: (hours: number, eventClass: string | null, corroborated: boolean | null) =>
    ['knowledge', 'news', hours, eventClass, corroborated] as const,
  sources: ['knowledge', 'sources'] as const,
  state: ['knowledge', 'state'] as const,
  dossiers: ['knowledge', 'dossiers'] as const,
  dossier: (asset: string) => ['knowledge', 'dossier', asset] as const,
  incidents: ['knowledge', 'incidents'] as const,
  grades: ['knowledge', 'grades'] as const,
  reports: ['knowledge', 'reports'] as const,
};
