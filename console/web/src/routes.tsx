import {
  IconActivityHeartbeat,
  IconAdjustments,
  IconAlertHexagon,
  IconBook2,
  IconBrain,
  IconChartCandle,
  IconChartLine,
  IconClipboardCheck,
  IconCpu,
  IconDashboard,
  IconFlask,
  IconHistory,
  IconKey,
  IconListCheck,
  IconPrompt,
  IconServerCog,
  IconShieldCheck,
  IconSparkles,
  IconTestPipe,
  IconToggleRight,
  IconWallet,
} from '@tabler/icons-react';
import { lazy, type ComponentType, type LazyExoticComponent } from 'react';

export type NavGroup = 'Trading' | 'Intelligence' | 'Control' | 'System';

export interface RouteDef {
  /** Also the folder name a feature package creates under `src/pages/`. */
  id: string;
  path: string;
  title: string;
  group: NavGroup;
  icon: ComponentType<{ size?: number | string }>;
  description: string;
  /** Work package that ships the page (spec 13). */
  owner: string;
  /** Row number in spec §12's page table; the navigation is ordered by it. */
  page: number;
}

/**
 * The 21 pages of spec §12.
 *
 * The list is in navigation order: groups in {@link NAV_GROUPS} order, and inside a group
 * the pages in the order of the spec's table (`page`).  The grouping is the shell's, the
 * sequence is the spec's, and `src/test/routes.test.tsx` holds both to it.
 */
export const ROUTES: RouteDef[] = [
  { page: 1, id: 'overview', path: '/', title: 'Overview', group: 'Trading', icon: IconDashboard, description: 'NAV, exposure, gate activity, what changed today', owner: 'P7' },
  { page: 2, id: 'portfolio', path: '/portfolio', title: 'Portfolio', group: 'Trading', icon: IconWallet, description: 'Positions, orders, trades, fills and reconciliation', owner: 'P2' },
  { page: 3, id: 'performance', path: '/performance', title: 'Performance', group: 'Trading', icon: IconChartLine, description: 'NAV vs benchmark, attribution, what-if', owner: 'P5' },
  { page: 4, id: 'test-lab', path: '/test-lab', title: 'Test Lab', group: 'Trading', icon: IconTestPipe, description: 'Test runs, reset wizard, run comparison', owner: 'P5' },
  { page: 5, id: 'backtest-lab', path: '/backtest-lab', title: 'Backtest Lab', group: 'Trading', icon: IconFlask, description: 'Backtests and walk-forward runs', owner: 'P5' },
  { page: 6, id: 'charts', path: '/charts', title: 'Charts', group: 'Trading', icon: IconChartCandle, description: 'Candles with fill, signal and gate markers', owner: 'P2' },

  { page: 7, id: 'signals', path: '/signals', title: 'Signals', group: 'Intelligence', icon: IconActivityHeartbeat, description: 'Detector funnel, screening and validation', owner: 'P4' },
  { page: 8, id: 'decisions', path: '/decisions', title: 'Decisions', group: 'Intelligence', icon: IconClipboardCheck, description: 'Research runs, proposals, approvals, traces', owner: 'P4' },
  { page: 10, id: 'ai-models', path: '/ai-models', title: 'AI & Models', group: 'Intelligence', icon: IconBrain, description: 'Providers, routing, usage, playground', owner: 'P3' },
  { page: 11, id: 'skills', path: '/skills', title: 'Skills', group: 'Intelligence', icon: IconSparkles, description: 'Skill files, lint/test/eval/trial, bindings', owner: 'P6' },
  { page: 12, id: 'prompts', path: '/prompts', title: 'Prompts', group: 'Intelligence', icon: IconPrompt, description: 'Prompt families, versions and activation', owner: 'P6' },
  { page: 13, id: 'self-improvement', path: '/self-improvement', title: 'Self-Improvement', group: 'Intelligence', icon: IconHistory, description: 'Change queue, verified evidence, autonomy matrix', owner: 'P6' },
  { page: 14, id: 'knowledge', path: '/knowledge', title: 'Knowledge', group: 'Intelligence', icon: IconBook2, description: 'Briefs, news, dossiers, market state, reports', owner: 'P4' },

  { page: 9, id: 'risk', path: '/risk', title: 'Risk', group: 'Control', icon: IconShieldCheck, description: 'Limits, gate decisions, flags, locks, resume wizard', owner: 'P2' },
  { page: 16, id: 'settings', path: '/settings', title: 'Settings', group: 'Control', icon: IconAdjustments, description: 'Schema-driven config editing with preview and effects', owner: 'P7' },
  { page: 17, id: 'setup', path: '/setup', title: 'Setup wizard', group: 'Control', icon: IconListCheck, description: 'First-run decisions and the week-1 gate', owner: 'P7' },
  { page: 18, id: 'mode-live', path: '/mode', title: 'Mode & Live', group: 'Control', icon: IconToggleRight, description: 'Per-sleeve mode, preflight, transitions', owner: 'P5' },
  { page: 19, id: 'secrets', path: '/secrets', title: 'Secrets', group: 'Control', icon: IconKey, description: 'Presence-only secret management and probes', owner: 'P3' },

  { page: 15, id: 'operations', path: '/operations', title: 'Operations', group: 'System', icon: IconServerCog, description: 'Jobs, crontab, containers, host readiness, logs', owner: 'P1' },
  { page: 20, id: 'audit', path: '/audit', title: 'Audit', group: 'System', icon: IconCpu, description: 'Unified audit, config and transition timeline', owner: 'P7' },
  { page: 21, id: 'invariants', path: '/invariants', title: 'Invariants', group: 'System', icon: IconAlertHexagon, description: 'Safety invariants and the code that enforces them', owner: 'P7' },
];

export const NAV_GROUPS: NavGroup[] = ['Trading', 'Intelligence', 'Control', 'System'];

/**
 * Feature packages drop `src/pages/<route id>/index.tsx` (default export) — or
 * `src/pages/<route id>.tsx` — and the route picks it up without any edit here.
 */
const pageModules = import.meta.glob([
  // Only the three entry-point shapes below are routes.  Globbing every `.tsx` under
  // `src/pages` made each page's private components a lazy chunk of its own as well as
  // part of its page chunk, which is waste in the bundle and a second module instance in
  // dev.  Test files are excluded for the same reason: they must never reach the bundle.
  '/src/pages/*/index.tsx',
  '/src/pages/*.tsx',
  '/src/pages/*/*Page.tsx',
  '!/src/pages/**/*.test.tsx',
  '!/src/pages/**/__tests__/**',
]) as Record<
  string,
  () => Promise<{ default: ComponentType }>
>;

/** Discovered page modules, keyed by their path from the project root. */
export function pageModulePaths(): string[] {
  return Object.keys(pageModules).sort();
}

/** `self-improvement` -> `SelfImprovement`. */
function pascalCase(id: string): string {
  return id
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join('');
}

/**
 * Accepted module paths for a page, in priority order.  The first is the documented
 * convention; the other two exist because feature packages also ship
 * `src/pages/<id>.tsx` and `src/pages/<id>/<Name>Page.tsx`.
 */
export function pageModuleCandidates(id: string): string[] {
  return [
    `/src/pages/${id}/index.tsx`,
    `/src/pages/${id}.tsx`,
    `/src/pages/${id}/${pascalCase(id)}Page.tsx`,
  ];
}

export function pageModuleKey(id: string): string | null {
  return pageModuleCandidates(id).find((key) => key in pageModules) ?? null;
}

/** `null` when no package has shipped the page yet -> the shell renders the placeholder. */
export function resolvePageComponent(id: string): LazyExoticComponent<ComponentType> | null {
  const key = pageModuleKey(id);
  if (!key) return null;
  const loader = pageModules[key];
  if (!loader) return null;
  return lazy(loader);
}

export function routeById(id: string): RouteDef | undefined {
  return ROUTES.find((route) => route.id === id);
}

export function routeByPath(path: string): RouteDef | undefined {
  return ROUTES.find((route) => route.path === path);
}
