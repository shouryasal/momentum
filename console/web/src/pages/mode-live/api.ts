import { api } from '@/api';

export type SleeveId = 'a' | 'b';
export type ModeTarget = 'TEST' | 'LIVE_PROPOSE' | 'LIVE_EXECUTE';

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
}

export interface ModeResponse {
  verified: boolean;
  reason: string;
  phase: string;
  set_at: string | null;
  set_by: string | null;
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
  recover: () => api.get<Array<Record<string, unknown>>>('/mode/recover'),
};

export function isLive(state: string): boolean {
  return state === 'LIVE_PROPOSE' || state === 'LIVE_EXECUTE';
}

export function isTransient(state: string): boolean {
  return state === 'ARMING' || state === 'DISARMING';
}

export function stateColour(state: string): string {
  if (state === 'LIVE_EXECUTE') return 'red';
  if (state === 'LIVE_PROPOSE') return 'orange';
  if (isTransient(state)) return 'yellow';
  return 'blue';
}

export function statusColour(status: CheckStatus | string): string {
  if (status === 'pass' || status === 'ok') return 'teal';
  if (status === 'warn') return 'yellow';
  if (status === 'fail' || status === 'failed') return 'red';
  return 'gray';
}

/** The allowed next states, mirroring `ops.modes.ALLOWED`. */
export function allowedTargets(state: string): ModeTarget[] {
  switch (state) {
    case 'TEST':
      return ['LIVE_PROPOSE', 'LIVE_EXECUTE'];
    case 'LIVE_PROPOSE':
      return ['LIVE_EXECUTE', 'TEST'];
    case 'LIVE_EXECUTE':
      return ['LIVE_PROPOSE', 'TEST'];
    default:
      return ['TEST'];
  }
}

/** Mirrors `ops.modes.confirm_phrase_for` so the field can be validated before sending. */
export function confirmPhraseFor(args: {
  sleeve: string;
  from: string;
  target: ModeTarget;
  seed: number;
  flatten: boolean;
  template: string;
}): string {
  const { sleeve, from, target, seed, flatten, template } = args;
  if (isLive(target) && !isLive(from)) {
    return template
      .replace('{sleeve}', sleeve.toUpperCase())
      .replace('{seed}', formatSeed(seed));
  }
  if (target === 'LIVE_EXECUTE' && from === 'LIVE_PROPOSE') return 'EXECUTE WITHOUT APPROVAL';
  if (target === 'LIVE_PROPOSE' && from === 'LIVE_EXECUTE') return 'BACK TO PROPOSE';
  if (target === 'TEST' && isLive(from)) return flatten ? '' : 'LEAVE POSITIONS UNMANAGED';
  return '';
}

export function formatSeed(seed: number): string {
  return Number.isInteger(seed) ? String(seed) : String(seed);
}

export const DEFAULT_CONFIRM_TEMPLATE = 'GO LIVE {sleeve} {seed} USDT';
