import { describe, expect, it } from 'vitest';

import {
  allowedTargets,
  confirmPhraseFor,
  DEFAULT_CONFIRM_TEMPLATE,
  isLive,
  isTransient,
  stateColour,
  statusColour,
} from './api';
import { COMPARE_FIELDS, formatMetric } from '../test-lab/api';

/**
 * The confirmation phrase is computed on the server and echoed by `POST /mode/preflight`;
 * the client mirrors it only so the field can be validated as the operator types. These
 * tests pin the mirror to `ops.modes.confirm_phrase_for`, because a client that asks for
 * the wrong phrase turns a safety gate into a puzzle.
 */
describe('confirmPhraseFor', () => {
  const base = { sleeve: 'a', flatten: true, seed: 500, template: DEFAULT_CONFIRM_TEMPLATE };

  it('asks for the sleeve and the seed when arming', () => {
    expect(confirmPhraseFor({ ...base, from: 'TEST', target: 'LIVE_PROPOSE' })).toBe(
      'GO LIVE A 500 USDT',
    );
  });

  it('asks for the execute phrase when dropping the approval gate', () => {
    expect(confirmPhraseFor({ ...base, from: 'LIVE_PROPOSE', target: 'LIVE_EXECUTE' })).toBe(
      'EXECUTE WITHOUT APPROVAL',
    );
  });

  it('asks for a lighter phrase when de-risking', () => {
    expect(confirmPhraseFor({ ...base, from: 'LIVE_EXECUTE', target: 'LIVE_PROPOSE' })).toBe(
      'BACK TO PROPOSE',
    );
  });

  it('needs no phrase to leave live while flattening', () => {
    expect(confirmPhraseFor({ ...base, from: 'LIVE_PROPOSE', target: 'TEST' })).toBe('');
  });

  it('needs a typed override to leave positions unmanaged', () => {
    expect(
      confirmPhraseFor({ ...base, from: 'LIVE_PROPOSE', target: 'TEST', flatten: false }),
    ).toBe('LEAVE POSITIONS UNMANAGED');
  });
});

describe('allowedTargets', () => {
  it('lets TEST arm either way', () => {
    expect(allowedTargets('TEST')).toEqual(['LIVE_PROPOSE', 'LIVE_EXECUTE']);
  });

  it('only lets a stuck sleeve go back to TEST', () => {
    expect(allowedTargets('ARMING')).toEqual(['TEST']);
    expect(allowedTargets('DISARMING')).toEqual(['TEST']);
  });

  it('lets a live sleeve change sub-mode or stand down', () => {
    expect(allowedTargets('LIVE_PROPOSE')).toEqual(['LIVE_EXECUTE', 'TEST']);
    expect(allowedTargets('LIVE_EXECUTE')).toEqual(['LIVE_PROPOSE', 'TEST']);
  });
});

describe('state predicates and colours', () => {
  it('knows which states are live', () => {
    expect(isLive('LIVE_PROPOSE')).toBe(true);
    expect(isLive('LIVE_EXECUTE')).toBe(true);
    expect(isLive('ARMING')).toBe(false);
    expect(isLive('TEST')).toBe(false);
  });

  it('knows which states are in flight', () => {
    expect(isTransient('ARMING')).toBe(true);
    expect(isTransient('DISARMING')).toBe(true);
    expect(isTransient('TEST')).toBe(false);
  });

  it('escalates colour with risk', () => {
    expect(stateColour('TEST')).toBe('blue');
    expect(stateColour('LIVE_PROPOSE')).toBe('orange');
    expect(stateColour('LIVE_EXECUTE')).toBe('red');
  });

  it('maps check status to colour', () => {
    expect(statusColour('pass')).toBe('teal');
    expect(statusColour('warn')).toBe('yellow');
    expect(statusColour('fail')).toBe('red');
    expect(statusColour('skip')).toBe('gray');
  });
});

describe('formatMetric', () => {
  it('renders a dash rather than a misleading zero', () => {
    expect(formatMetric(null)).toBe('—');
    expect(formatMetric(undefined, '%')).toBe('—');
  });

  it('keeps two decimals on small numbers and none on large ones', () => {
    expect(formatMetric(12.3456, '%')).toBe('12.35%');
    expect(formatMetric(1234.5)).toBe('1235');
  });

  it('covers every compare column', () => {
    expect(COMPARE_FIELDS.length).toBeGreaterThan(10);
    expect(COMPARE_FIELDS.map((field) => field.key)).toContain('excess_return_pct');
  });
});
