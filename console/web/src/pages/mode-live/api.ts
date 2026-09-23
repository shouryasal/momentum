import { api } from '@/api';

export type SleeveId = 'a' | 'b';
export type ModeTarget =
  | 'TEST'
  | 'DEMO_PROPOSE'
  | 'DEMO_EXECUTE'
  | 'LIVE_PROPOSE'
  | 'LIVE_EXECUTE';

/** How a sleeve's results may be described. `demo` is never `live`. */
export type PnlBasis = 'paper' | 'demo' | 'live';
export type ModeBadge = 'TEST' | 'DEMO' | 'LIVE' | 'TRANSITIONING';

export interface SleeveModeState {
  sleeve: string;
  state: string;
  submode: string | null;
  run_id: string | null;
  seed_usdt: number;
  label: string | null;
  mode: string;
  since: string | null;
  days: number | null;
  transition_in_progress: boolean;
  max_seed_usdt: number;
  /** `live` | `demo` | null — the one Binance this state may reach. */
  venue: string | null;
  /** e.g. `demo-api.binance.com`. Shown so the venue is never inferred from a colour. */
  venue_host: string | null;
  /** Real money. False for demo. */
  is_live: boolean;
  is_demo: boolean;
  badge: ModeBadge;
  pnl_basis: PnlBasis;
  seed_ceilings: Record<string, number>;
  confirm_phrases: Record<string, string>;
  allowed_targets: string[];
}

export interface ModeResponse {
  verified: boolean;
  reason: string;
  phase: string;
  set_at: string | null;
  set_by: string | null;
  any_live: boolean;
  any_demo: boolean;
  sleeves: SleeveModeState[];
}

export type CheckStatus = 'pass' | 'warn' | 'fail' | 'skip';

export interface PreflightItem {
  id: string;
  title: string;
  blocking: boolean;
  status: CheckStatus;
  detail: string;
  evidence: Record<string, unknown>;
  overridden: boolean;
}

export interface PreflightResponse {
  preflight_id: string;
  ok: boolean;
  created_utc: string;
  expires_utc: string;
  confirm_phrase: string;
  items: PreflightItem[];
}

export interface TransitionStep {
  step: string;
  status: string;
  detail?: string;
  ts_utc?: string;
}

export interface TransitionResponse {
  transition_id: number;
  sleeve: string;
  from_state: string;
  to_state: string;
  run_id: string;
  status: string;
  steps: TransitionStep[];
}

export interface TransitionRow {
  id: number;
  sleeve: string;
  from_state: string;
  to_state: string;
  started_utc: string;
  finished_utc: string | null;
  status: string;
  actor: string;
  error: string | null;
  steps: TransitionStep[];
}

export interface PreflightRequestBody {
  sleeve: SleeveId;
  target: ModeTarget;
  submode?: 'propose' | 'execute' | null;
  seed_usdt?: number | null;
  override_reason?: string | null;
}

export interface TransitionRequestBody extends PreflightRequestBody {
  preflight_id?: string | null;
  confirm_phrase: string;
  flatten?: boolean | null;
  label?: string | null;
  notes?: string | null;
}

/** The twelve steps of spec 2.2, in order, so the progress list is stable before it fills. */
export const TRANSITION_STEPS = [
  'lock',
  'preflight',
  'mark_transient',
  'stopentry',
  'flatten',
  'close_run',
  'write_mode',
  'regen',
  'compose',
  'verify',
  'reconcile',
  'open_run',
] as const;

export const STEP_LABELS: Record<string, string> = {
  lock: 'Take the ops lock',
  preflight: 'Re-run the preflight',
  mark_transient: 'Mark the sleeve in transition',
  stopentry: 'Stop entries, cancel resting orders',
  flatten: 'Flatten positions',
  close_run: 'Close the outgoing run',
  write_mode: 'Write the signed mode file',
  regen: 'Regenerate var/runtime',
  compose: 'Recreate the container',
  verify: 'Verify /show_config',
  reconcile: 'Reconcile ledger vs exchange',
  open_run: 'Open the new run',
  rollback: 'Roll back',
  kill: 'Engage the kill switch',
};

export const modeApi = {
  read: () => api.get<ModeResponse>('/mode'),
  preflight: (body: PreflightRequestBody) => api.post<PreflightResponse>('/mode/preflight', body),
  transition: (body: TransitionRequestBody) => api.post<TransitionResponse>('/mode/transition', body),
  history: (limit = 25) => api.get<TransitionRow[]>('/mode/transitions', { limit }),
  /**
   * `POST`, not `GET`: it rewrites the signed mode file, regenerates `var/runtime` and
   * engages the kill switch. The console's CSRF and automated-run middleware both key on
   * the verb, so as a `GET` it sat outside every guard the app claims for a state change.
   */
  recover: () => api.post<Array<Record<string, unknown>>>('/mode/recover', {}),
};

/** Real money. Deliberately false for the demo states — demo P&L is not live performance. */
export function isLive(state: string): boolean {
  return state === 'LIVE_PROPOSE' || state === 'LIVE_EXECUTE';
}

/** Real orders on `demo-api.binance.com` with fake money. */
export function isDemo(state: string): boolean {
  return state === 'DEMO_PROPOSE' || state === 'DEMO_EXECUTE';
}

/** Reaches a venue at all — needs a seed, a preflight and a typed phrase. */
export function isVenueBound(state: string): boolean {
  return isLive(state) || isDemo(state);
}

export function isTransient(state: string): boolean {
  return state === 'ARMING' || state === 'DISARMING';
}

/**
 * Colour is a *hint*, never the statement — every surface that uses it also prints the
 * badge word and the venue host, because an operator must be able to tell DEMO from LIVE
 * without trusting a screen's colour rendering.
 *
 * Demo gets the violet family (`violet` propose, `pink` execute): nowhere near live's
 * red/orange, nowhere near test's blue, and nowhere near the transient yellow.
 */
export function stateColour(state: string): string {
  if (state === 'LIVE_EXECUTE') return 'red';
  if (state === 'LIVE_PROPOSE') return 'orange';
  if (state === 'DEMO_EXECUTE') return 'pink';
  if (state === 'DEMO_PROPOSE') return 'violet';
  if (isTransient(state)) return 'yellow';
  return 'blue';
}

/** `TEST` | `DEMO` | `LIVE` | `TRANSITIONING` — mirrors `console.services.mode_service`. */
export function badgeFor(state: string, transitioning = false): ModeBadge {
  if (transitioning || isTransient(state)) return 'TRANSITIONING';
  if (isLive(state)) return 'LIVE';
  if (isDemo(state)) return 'DEMO';
  return 'TEST';
}

/** One line under the badge saying, in words, what this state actually does. */
export const MODE_MEANING: Record<ModeBadge, string> = {
  TEST: 'Dry run. No exchange, no key, no orders.',
  DEMO: 'REAL orders on Binance Spot Demo Mode (demo-api.binance.com) with FAKE money. Results are a rehearsal, never live performance.',
  LIVE: 'REAL orders on Binance (api.binance.com) with REAL money.',
  TRANSITIONING: 'Mid-transition. Recover the sleeve before doing anything else.',
};

/** How this sleeve's P&L may be labelled. Never widen `demo` to `live`. */
export function pnlBasis(state: string): PnlBasis {
  if (isLive(state)) return 'live';
  if (isDemo(state)) return 'demo';
  return 'paper';
}

export function statusColour(status: CheckStatus | string): string {
  if (status === 'pass' || status === 'ok') return 'teal';
  if (status === 'warn') return 'yellow';
  if (status === 'fail' || status === 'failed') return 'red';
  return 'gray';
}

/**
 * The allowed next states, mirroring `ops.modes.ALLOWED`.
 *
 * Note what is missing: there is no DEMO -> LIVE edge. The live gates are measured on a
 * TEST run, so going live from demo means standing down to TEST first and re-running the
 * whole live preflight from a known state.
 */
export function allowedTargets(state: string): ModeTarget[] {
  switch (state) {
    case 'TEST':
      return ['DEMO_PROPOSE', 'DEMO_EXECUTE', 'LIVE_PROPOSE', 'LIVE_EXECUTE'];
    case 'DEMO_PROPOSE':
      return ['DEMO_EXECUTE', 'TEST'];
    case 'DEMO_EXECUTE':
      return ['DEMO_PROPOSE', 'TEST'];
    case 'LIVE_PROPOSE':
      return ['LIVE_EXECUTE', 'TEST'];
    case 'LIVE_EXECUTE':
      return ['LIVE_PROPOSE', 'TEST'];
    default:
      return ['TEST'];
  }
}

/**
 * Mirrors `ops.modes.confirm_phrase_for` so the field can be validated before sending.
 *
 * Demo and live phrases share no words. That is the point: a phrase typed from muscle
 * memory must not be able to move a sleeve to the wrong venue in either direction. The
 * server is still the authority — `POST /mode/preflight` echoes the real phrase, and this
 * mirror is only so the input can go red before the operator presses the button.
 */
export function confirmPhraseFor(args: {
  sleeve: string;
  from: string;
  target: ModeTarget;
  seed: number;
  flatten: boolean;
  template: string;
  demoTemplate?: string;
}): string {
  const { sleeve, from, target, seed, flatten, template } = args;
  const demoTemplate = args.demoTemplate ?? DEFAULT_DEMO_CONFIRM_TEMPLATE;
  const fill = (tpl: string) =>
    tpl.replace('{sleeve}', sleeve.toUpperCase()).replace('{seed}', formatSeed(seed));
  if (isLive(target) && !isLive(from)) return fill(template);
  if (isDemo(target) && !isDemo(from)) return fill(demoTemplate);
  if (target === 'LIVE_EXECUTE' && from === 'LIVE_PROPOSE') return 'EXECUTE WITHOUT APPROVAL';
  if (target === 'LIVE_PROPOSE' && from === 'LIVE_EXECUTE') return 'BACK TO PROPOSE';
  if (target === 'DEMO_EXECUTE' && from === 'DEMO_PROPOSE') {
    return 'EXECUTE ON DEMO WITHOUT APPROVAL';
  }
  if (target === 'DEMO_PROPOSE' && from === 'DEMO_EXECUTE') return 'BACK TO DEMO PROPOSE';
  if (target === 'TEST' && isVenueBound(from)) {
    return flatten ? '' : 'LEAVE POSITIONS UNMANAGED';
  }
  return '';
}

export function formatSeed(seed: number): string {
  return Number.isInteger(seed) ? String(seed) : String(seed);
}

export const DEFAULT_CONFIRM_TEMPLATE = 'GO LIVE {sleeve} {seed} USDT';
export const DEFAULT_DEMO_CONFIRM_TEMPLATE = 'GO DEMO {sleeve} {seed} USDT';
