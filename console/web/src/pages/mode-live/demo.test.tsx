import { describe, expect, it } from 'vitest';

import type { SleeveMode as HeaderSleeveMode } from '@/api/contracts';
import {
  isOnAnExchange,
  MODE_MEANING as HEADER_MEANING,
  modeLabel,
} from '@/app/ModeBadges';
import { MODE_COLORS } from '@/theme';

import {
  allowedTargets,
  badgeFor,
  confirmPhraseFor,
  DEFAULT_CONFIRM_TEMPLATE,
  DEFAULT_DEMO_CONFIRM_TEMPLATE,
  isDemo,
  isLive,
  isVenueBound,
  MODE_MEANING,
  pnlBasis,
  stateColour,
} from './api';

/**
 * DEMO on the Mode page.
 *
 * Two properties, and they are the reason this file exists separately from `mode.test.tsx`:
 *
 * 1. **DEMO is unmistakable, and unmistakably not LIVE.** Different word, different
 *    colour, different confirmation phrase, and a P&L basis that is never `live`.
 * 2. **The mirror of `ops.modes` is exact.** The client validates the typed phrase before
 *    it is sent; a mirror that asks for the wrong words turns a safety gate into a puzzle,
 *    and a mirror that accepts the *live* words for a demo arming turns it into a hazard.
 */

describe('demo is its own state, not a flavour of live', () => {
  it('knows the demo states', () => {
    expect(isDemo('DEMO_PROPOSE')).toBe(true);
    expect(isDemo('DEMO_EXECUTE')).toBe(true);
    expect(isDemo('LIVE_EXECUTE')).toBe(false);
    expect(isDemo('TEST')).toBe(false);
  });

  it('never reports a demo state as live', () => {
    expect(isLive('DEMO_PROPOSE')).toBe(false);
    expect(isLive('DEMO_EXECUTE')).toBe(false);
  });

  it('treats demo as venue-bound, exactly as live is', () => {
    expect(isVenueBound('DEMO_PROPOSE')).toBe(true);
    expect(isVenueBound('LIVE_PROPOSE')).toBe(true);
    expect(isVenueBound('TEST')).toBe(false);
  });
});

describe('the badge', () => {
  it('says DEMO, and says something different for TEST and LIVE', () => {
    expect(badgeFor('DEMO_PROPOSE')).toBe('DEMO');
    expect(badgeFor('DEMO_EXECUTE')).toBe('DEMO');
    expect(badgeFor('TEST')).toBe('TEST');
    expect(badgeFor('LIVE_EXECUTE')).toBe('LIVE');
    expect(new Set(['DEMO', 'TEST', 'LIVE']).size).toBe(3);
  });

  it('shows a transition in flight instead of the destination', () => {
    expect(badgeFor('ARMING')).toBe('TRANSITIONING');
    expect(badgeFor('DEMO_EXECUTE', true)).toBe('TRANSITIONING');
  });

  it('gives demo a colour of its own, shared with neither test nor live', () => {
    const demo = [stateColour('DEMO_PROPOSE'), stateColour('DEMO_EXECUTE')];
    const others = [
      stateColour('TEST'),
      stateColour('LIVE_PROPOSE'),
      stateColour('LIVE_EXECUTE'),
      stateColour('ARMING'),
    ];
    for (const colour of demo) {
      expect(others).not.toContain(colour);
    }
  });

  it('spells out what each badge means, in words', () => {
    expect(MODE_MEANING.DEMO).toMatch(/REAL orders/);
    expect(MODE_MEANING.DEMO).toMatch(/FAKE money/);
    expect(MODE_MEANING.DEMO).toMatch(/demo-api\.binance\.com/);
    expect(MODE_MEANING.DEMO).toMatch(/never live performance/);
    expect(MODE_MEANING.TEST).toMatch(/No exchange/);
    expect(MODE_MEANING.LIVE).toMatch(/REAL money/);
  });
});

describe('the header badge (app shell)', () => {
  const sleeve = (state: string, is_live = false): HeaderSleeveMode => ({
    sleeve: 'a',
    state,
    submode: null,
    run_id: null,
    seed_usdt: 500,
    is_live,
  });

  it('names the demo states instead of falling through to the raw string', () => {
    expect(modeLabel(sleeve('DEMO_PROPOSE'))).toBe('DEMO·PROPOSE');
    expect(modeLabel(sleeve('DEMO_EXECUTE'))).toBe('DEMO·EXECUTE');
  });

  it('explains what demo means instead of calling it unrecognised', () => {
    for (const state of ['DEMO_PROPOSE', 'DEMO_EXECUTE']) {
      expect(HEADER_MEANING[state]).toBeDefined();
      expect(HEADER_MEANING[state]).toMatch(/demo-api\.binance\.com/);
      expect(HEADER_MEANING[state]).toMatch(/FAKE money/);
      expect(HEADER_MEANING[state]).not.toMatch(/Real money/);
    }
  });

  it('gives the demo states a colour of their own', () => {
    expect(MODE_COLORS.DEMO_PROPOSE).toBeDefined();
    expect(MODE_COLORS.DEMO_EXECUTE).toBeDefined();
    for (const key of ['DEMO_PROPOSE', 'DEMO_EXECUTE']) {
      for (const other of ['TEST', 'LIVE_PROPOSE', 'LIVE_EXECUTE', 'ARMING', 'DISARMING']) {
        expect(MODE_COLORS[key]).not.toBe(MODE_COLORS[other]);
      }
    }
  });

  it('fills the badge for demo (it is on an exchange) without calling it live', () => {
    expect(isOnAnExchange(sleeve('DEMO_EXECUTE'))).toBe(true);
    expect(isOnAnExchange(sleeve('LIVE_EXECUTE', true))).toBe(true);
    expect(isOnAnExchange(sleeve('TEST'))).toBe(false);
    // `data-live` is real money only, and stays false for demo.
    expect(sleeve('DEMO_EXECUTE').is_live).toBe(false);
  });
});

describe('P&L basis', () => {
  it('never labels demo results as live performance', () => {
    expect(pnlBasis('DEMO_PROPOSE')).toBe('demo');
    expect(pnlBasis('DEMO_EXECUTE')).toBe('demo');
    expect(pnlBasis('LIVE_EXECUTE')).toBe('live');
    expect(pnlBasis('TEST')).toBe('paper');
  });
});

describe('allowedTargets mirrors ops.modes.ALLOWED', () => {
  it('offers both ladders from TEST', () => {
    expect(allowedTargets('TEST')).toEqual([
      'DEMO_PROPOSE',
      'DEMO_EXECUTE',
      'LIVE_PROPOSE',
      'LIVE_EXECUTE',
    ]);
  });

  it('never offers a demo sleeve a jump straight to live', () => {
    for (const state of ['DEMO_PROPOSE', 'DEMO_EXECUTE']) {
      expect(allowedTargets(state).filter(isLive)).toEqual([]);
    }
  });

  it('never offers a live sleeve a sideways step into demo', () => {
    for (const state of ['LIVE_PROPOSE', 'LIVE_EXECUTE']) {
      expect(allowedTargets(state).filter(isDemo)).toEqual([]);
    }
  });

  it('always offers the way back to TEST', () => {
    for (const state of ['DEMO_PROPOSE', 'DEMO_EXECUTE', 'LIVE_PROPOSE', 'LIVE_EXECUTE']) {
      expect(allowedTargets(state)).toContain('TEST');
    }
  });

  it('lets demo move between propose and execute', () => {
    expect(allowedTargets('DEMO_PROPOSE')).toEqual(['DEMO_EXECUTE', 'TEST']);
    expect(allowedTargets('DEMO_EXECUTE')).toEqual(['DEMO_PROPOSE', 'TEST']);
  });
});

describe('confirmPhraseFor: demo and live share no words', () => {
  const base = {
    sleeve: 'a',
    flatten: true,
    seed: 500,
    template: DEFAULT_CONFIRM_TEMPLATE,
    demoTemplate: DEFAULT_DEMO_CONFIRM_TEMPLATE,
  } as const;

  it('asks for the demo phrase when arming demo', () => {
    expect(confirmPhraseFor({ ...base, from: 'TEST', target: 'DEMO_PROPOSE' })).toBe(
      'GO DEMO A 500 USDT',
    );
  });

  it('still asks for the live phrase when arming live', () => {
    expect(confirmPhraseFor({ ...base, from: 'TEST', target: 'LIVE_PROPOSE' })).toBe(
      'GO LIVE A 500 USDT',
    );
  });

  it('never returns the same phrase for a demo and a live arming', () => {
    const demo = confirmPhraseFor({ ...base, from: 'TEST', target: 'DEMO_EXECUTE' });
    const live = confirmPhraseFor({ ...base, from: 'TEST', target: 'LIVE_EXECUTE' });
    expect(demo).not.toBe(live);
    expect(demo).toMatch(/^GO DEMO /);
    expect(live).toMatch(/^GO LIVE /);
  });

  it('has its own words for dropping the approval gate on demo', () => {
    expect(confirmPhraseFor({ ...base, from: 'DEMO_PROPOSE', target: 'DEMO_EXECUTE' })).toBe(
      'EXECUTE ON DEMO WITHOUT APPROVAL',
    );
    expect(
      confirmPhraseFor({ ...base, from: 'DEMO_PROPOSE', target: 'DEMO_EXECUTE' }),
    ).not.toBe(confirmPhraseFor({ ...base, from: 'LIVE_PROPOSE', target: 'LIVE_EXECUTE' }));
  });

  it('has its own words for de-risking on demo', () => {
    expect(confirmPhraseFor({ ...base, from: 'DEMO_EXECUTE', target: 'DEMO_PROPOSE' })).toBe(
      'BACK TO DEMO PROPOSE',
    );
  });

  it('needs no phrase to leave demo while flattening', () => {
    expect(confirmPhraseFor({ ...base, from: 'DEMO_EXECUTE', target: 'TEST' })).toBe('');
  });

  it('needs the typed override to leave demo positions unmanaged', () => {
    expect(
      confirmPhraseFor({ ...base, from: 'DEMO_EXECUTE', target: 'TEST', flatten: false }),
    ).toBe('LEAVE POSITIONS UNMANAGED');
  });
});
