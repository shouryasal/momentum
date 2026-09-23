/**
 * No screen may name itself, or anything on it, in a word the owner would have to ask about.
 *
 * The owner had to ask what "sleeve A" and "sleeve B" meant. That is a naming bug, and a
 * naming bug comes back the moment someone adds a screen unless something fails. So this
 * sweeps every piece of operator-facing copy the console ships — the navigation labels,
 * the one-line explanation under every screen's title, the safety strip, the Setup cards —
 * against `lib/plain.BUILDER_TERMS`, and it walks the rendered shell as well, because a
 * label can be built at render time.
 *
 * What the rule is NOT: a ban on the words themselves. The raw identifier stays visible —
 * `sleeve a` is what the logs, the run ids and the API say, and hiding it would make the
 * console harder to reconcile with them. It just may not be the *name* of anything. A
 * builder word inside a sentence is fine when `<Explain>` carries its meaning, and every
 * such word has a glossary entry that this file also holds to.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';

import { ApiClient } from '../api/client';
import { App } from '../App';
import { AppProviders } from '../app/Providers';
import { PILL_LABEL, PILL_MEANING, SAFETY_PILLS } from '../app/SafetyStrip';
import { MODE_MEANING } from '../app/ModeBadges';
import { BUILDER_TERMS, GLOSSARY, builderTermsIn, explain } from '../lib/plain';
import { SETUP_CARDS } from '../pages/settings/components/SetupPanel';
import { PAGE_BLURB, PRIMARY_NAV, ROUTES, routeBlurb } from '../routes';
import { FakeEventSource, testQueryClient } from './utils';

/** One place to say what failed, with the replacement to reach for. */
function offence(where: string, text: string): string {
  const found = builderTermsIn(text);
  return `${where} says ${found.join(', ')} — ${text}`;
}

describe('the words the operator reads', () => {
  it('names each destination in plain words', () => {
    for (const entry of PRIMARY_NAV) {
      expect(builderTermsIn(entry.label), offence(`nav label "${entry.label}"`, entry.label)).toEqual(
        [],
      );
      expect(builderTermsIn(entry.blurb), offence(`nav blurb for ${entry.label}`, entry.blurb)).toEqual(
        [],
      );
    }
  });

  it('gives every screen one line of explanation, in plain words', () => {
    for (const route of ROUTES) {
      const blurb = routeBlurb(route.id);
      expect(blurb.length, `${route.id} has no explanation`).toBeGreaterThan(20);
      expect(builderTermsIn(blurb), offence(`the blurb for ${route.id}`, blurb)).toEqual([]);
    }
    // Every route has an entry of its own, not a fallback to the builder's summary.
    expect(Object.keys(PAGE_BLURB).sort()).toEqual(ROUTES.map((route) => route.id).sort());
  });

  it('labels the safety strip with the promise, not the key', () => {
    for (const pill of SAFETY_PILLS) {
      const label = PILL_LABEL[pill];
      expect(label, `${pill} has no plain label`).toBeTruthy();
      expect(builderTermsIn(label as string), offence(`the ${pill} pill`, label as string)).toEqual(
        [],
      );
      // The key itself stays available, in the sentence behind the pill.
      expect(PILL_MEANING[pill], `${pill} has no explanation`).toBeTruthy();
    }
  });

  it('says what every mode means, and names the Setup cards plainly', () => {
    for (const [state, meaning] of Object.entries(MODE_MEANING)) {
      expect(meaning.length, `${state} has no explanation`).toBeGreaterThan(20);
    }
    for (const card of SETUP_CARDS) {
      expect(builderTermsIn(card.title), offence(`the ${card.id} card`, card.title)).toEqual([]);
      expect(builderTermsIn(card.blurb), offence(`the ${card.id} card`, card.blurb)).toEqual([]);
      expect(card.blurb.length, `${card.id} has no explanation`).toBeGreaterThan(20);
    }
  });

  it('keeps one sentence for every builder word it cannot avoid printing', () => {
    for (const term of BUILDER_TERMS) {
      // A multi-word term is explained by the word it is built from.
      const head = term.split(' ')[0] as string;
      expect(
        explain(term) ?? explain(head),
        `"${term}" is on the list of words to avoid but nothing explains it`,
      ).toBeTruthy();
    }
    for (const [term, sentence] of Object.entries(GLOSSARY)) {
      expect(sentence.length, `the glossary entry for "${term}" is too short to help`).toBeGreaterThan(
        30,
      );
    }
  });

  it('matches whole words only, so "management" is not "gate" and "navigate" is not "nav"', () => {
    expect(builderTermsIn('Risk management and navigation')).toEqual([]);
    expect(builderTermsIn('the safety check refused it')).toEqual([]);
    expect(builderTermsIn('Sleeve A holds BTC')).toContain('sleeve');
    expect(builderTermsIn('the gate rejected it')).toContain('gate');
  });
});

/* ------------------------------------------------------------------ the rendered shell */

const META = {
  version: '2.0.0',
  schema_version: 3,
  config_version: 2,
  started_at: '2026-09-22T04:00:00Z',
  now: '2026-09-22T04:30:00Z',
  port: 8787,
  host: '127.0.0.1',
  automated_run: false,
  git: { available: false, branch: null, commit: null, short: null, dirty: false, error: null },
  modes: [
    { sleeve: 'a', state: 'TEST', submode: null, run_id: null, seed_usdt: 10_000, is_live: false },
    { sleeve: 'b', state: 'TEST', submode: null, run_id: null, seed_usdt: null, is_live: false },
  ],
  mode_verified: true,
  mode_reason: 'ok',
  kill: { engaged: false, reason: null, since: null, path: 'ops/killdir/KILL' },
  bless: { ok: true, reason: 'ok', changed: [], missing: [], blessed_at: null, blessed_by: null },
  invariants: [],
};

function backend(): typeof fetch {
  return (async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const path = (url.split('?')[0] ?? url).replace(/^\/api/, '');
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    if (path === '/auth/me') {
      return json({
        authenticated: true,
        actor: 'human:console',
        step_up_until: null,
        csrf: 'c',
        expires: '2026-09-23T04:30:00Z',
      });
    }
    if (path === '/meta') return json(META);
    if (path === '/invariants/strip') {
      return json({
        ok: true,
        counts: { ok: 5 },
        strip: Object.fromEntries(SAFETY_PILLS.map((pill) => [pill, 'ok'])),
      });
    }
    return json({ items: [], rows: [], ok: true });
  }) as unknown as typeof fetch;
}

beforeEach(() => {
  FakeEventSource.reset();
});

describe('the rendered shell', () => {
  it('shows only plain words in the navigation and the safety strip', async () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <AppProviders
          client={new ApiClient({ fetchImpl: backend() })}
          queryClient={testQueryClient()}
          sseFactory={(url) => new FakeEventSource(url)}
          viewMode="operator"
        >
          <App />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByTestId('app-header');

    const nav = screen.getByTestId('nav-primary');
    expect(builderTermsIn(nav.textContent ?? ''), offence('the navigation', nav.textContent ?? '')).toEqual(
      [],
    );

    const strip = await screen.findByTestId('safety-strip');
    expect(
      builderTermsIn(strip.textContent ?? ''),
      offence('the safety badge', strip.textContent ?? ''),
    ).toEqual([]);

    // The five promises read plainly too, wherever they are opened from.
    await userEvent.click(within(strip).getByTestId('safety-badge'));
    const promises = await screen.findByTestId('safety-detail');
    await waitFor(() =>
      expect(within(promises).getByTestId(`safety-${SAFETY_PILLS[0]}`)).toBeInTheDocument(),
    );
    expect(
      builderTermsIn(promises.textContent ?? ''),
      offence('the safety promises', promises.textContent ?? ''),
    ).toEqual([]);

    // The bots are named on the badges, and the raw id is not the name.
    expect(await screen.findByTestId('mode-badge-a')).toHaveTextContent('Rules bot');
    expect(screen.getByTestId('mode-badge-b')).toHaveTextContent('AI bot');
  });
});
