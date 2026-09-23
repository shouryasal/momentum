/**
 * The test that would have caught "I click on secrets and nothing happens".
 *
 * The owner opened the running console and reported that no click did anything. The API
 * was healthy, the page chunk was in the build, and nothing was logged — because the bug
 * was neither in a page nor in the server. `resolvePageComponent` minted a fresh
 * `React.lazy()` on every render, and React Router v7 navigates inside `startTransition`:
 * the incoming screen suspended, React threw the attempted render away and kept the
 * committed one on show, then retried from a fresh fiber, which minted another
 * never-loaded lazy, which suspended again. The URL moved, the chunk was fetched once, no
 * error was thrown, and the screen never changed. Every unit test passed, because every
 * unit test mounted one page directly.
 *
 * So the tests here drive the shell the way a person does — click the navigation, land on
 * the screen — and then walk every registered route to prove each one draws its own page
 * rather than a blank panel or the error boundary. A dead route, a page that throws on an
 * empty install, and a navigation that does not navigate all fail here.
 *
 * Also pinned: the chrome an operator must never lose. The kill switch, the per-bot mode
 * badges, the health dot, the count of things waiting on them and the safety strip are
 * asserted on every screen, in both the operator view and the developer area.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';

import { ApiClient } from '../api/client';
import { App } from '../App';
import { AppProviders } from '../app/Providers';
import { DEVELOPER_ROUTE_IDS, PRIMARY_NAV, ROUTES, routeById } from '../routes';
import { FakeEventSource, testQueryClient } from './utils';

/**
 * A backend that answers everything with an empty 200.
 *
 * A fresh install is the state the owner will actually meet first, and "empty" must render
 * as an empty screen, never as a broken one. Anything a page cannot survive without shows
 * up here as a failure.
 */
function emptyBackend(): typeof fetch {
  return (async (input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString();
    const path = (url.split('?')[0] ?? url).replace(/^\/api/, '');
    const body: Record<string, unknown> = {
      items: [],
      rows: [],
      runs: [],
      proposals: [],
      pending: [],
      results: [],
      files: [],
      points: [],
      entries: [],
      series: {},
      positions: [],
      orders: [],
      fills: [],
      providers: [],
      invariants: [],
      strip: {},
      jobs: [],
      changes: [],
      skills: [],
      signals: [],
      ok: true,
    };
    if (path === '/auth/me') {
      return json({
        authenticated: true,
        actor: 'human:console',
        step_up_until: null,
        csrf: 'csrf-1',
        expires: '2026-09-23T04:30:00Z',
      });
    }
    if (path === '/meta') return json(META);
    return json(body);
  }) as unknown as typeof fetch;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

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
    {
      sleeve: 'a',
      state: 'TEST',
      submode: null,
      run_id: null,
      seed_usdt: 10_000,
      is_live: false,
    },
    { sleeve: 'b', state: 'TEST', submode: null, run_id: null, seed_usdt: null, is_live: false },
  ],
  mode_verified: true,
  mode_reason: 'ok',
  kill: { engaged: false, reason: null, since: null, path: 'ops/killdir/KILL' },
  bless: { ok: true, reason: 'ok', changed: [], missing: [], blessed_at: null, blessed_by: null },
  invariants: [],
};

function renderApp(route: string) {
  const client = new ApiClient({ fetchImpl: emptyBackend() });
  return render(
    <MemoryRouter initialEntries={[route]}>
      <AppProviders
        client={client}
        queryClient={testQueryClient()}
        sseFactory={(url) => new FakeEventSource(url)}
        viewMode="operator"
      >
        <App />
      </AppProviders>
    </MemoryRouter>,
  );
}

/** The chrome that has to be on screen whatever else is. */
async function expectGlobalChrome() {
  expect(await screen.findByTestId('app-header')).toBeInTheDocument();
  expect(screen.getByTestId('kill-button')).toBeInTheDocument();
  expect(screen.getByTestId('health-dot')).toBeInTheDocument();
  expect(screen.getByTestId('approvals-counter')).toBeInTheDocument();
  expect(screen.getByTestId('safety-strip')).toBeInTheDocument();
  // The badges arrive with `GET /api/meta`, a moment after the header itself.
  expect(await screen.findByTestId('mode-badge-a')).toBeInTheDocument();
  expect(await screen.findByTestId('mode-badge-b')).toBeInTheDocument();
}

/** The screen drew its own page: something is in main, and it is not the boundary. */
async function expectScreenDrawn(routeId: string) {
  const main = document.querySelector('main');
  expect(main, `${routeId}: the shell has no main element`).not.toBeNull();
  await waitFor(
    () => {
      expect(
        screen.queryByTestId('route-error'),
        `${routeId}: the screen threw and fell back to the error boundary`,
      ).not.toBeInTheDocument();
      expect(
        (main as HTMLElement).textContent?.trim().length ?? 0,
        `${routeId}: the screen rendered nothing at all`,
      ).toBeGreaterThan(0);
    },
    { timeout: 5000 },
  );
}

beforeEach(() => {
  FakeEventSource.reset();
});

describe('every registered route renders its own page', () => {
  it.each(ROUTES.map((route) => [route.id, route.path] as const))(
    '%s (%s) draws a screen, not a blank panel or an error',
    async (id, path) => {
      renderApp(path);
      await expectGlobalChrome();
      await expectScreenDrawn(id);
      // Every page ships a module; none of them should still be the placeholder.
      expect(screen.queryByTestId(`not-built-${id}`)).not.toBeInTheDocument();
    },
    20_000,
  );
});

describe('the operator navigation', () => {
  it('offers the four destinations plus Setup, and nothing else', async () => {
    renderApp('/');
    await expectGlobalChrome();
    const nav = screen.getByTestId('nav-primary');
    const links = within(nav).getAllByRole('link');
    expect(links.map((link) => link.textContent?.trim())).toEqual(
      PRIMARY_NAV.map((entry) => entry.label),
    );
    expect(links).toHaveLength(5);
  });

  /**
   * Clicking a navigation item has to land on that screen. This is the regression test
   * for the bug itself: it drives the shell, not a page, and it checks the rendered
   * screen rather than the URL — the URL moved while the console was broken.
   */
  it.each(
    PRIMARY_NAV.filter((entry) => entry.routeId !== 'overview').map(
      (entry) => [entry.routeId, entry.label] as const,
    ),
  )('clicking %s lands on that screen', async (routeId) => {
    const user = userEvent.setup();
    renderApp('/');
    await expectGlobalChrome();
    await expectScreenDrawn('overview');
    const before = document.querySelector('main')?.textContent ?? '';

    await user.click(screen.getByTestId(`nav-${routeId}`));

    await waitFor(
      () => {
        expect(screen.queryByTestId('route-error')).not.toBeInTheDocument();
        expect(
          document.querySelector('main')?.textContent ?? '',
          `clicking ${routeId} left the previous screen on show`,
        ).not.toBe(before);
      },
      { timeout: 5000 },
    );
  });

  it('keeps every developer route reachable by its address with the area closed', async () => {
    for (const id of ['invariants', 'audit', 'operations']) {
      const route = routeById(id);
      expect(route).toBeDefined();
      const { unmount } = renderApp(route!.path);
      await expectGlobalChrome();
      // Not in the navigation…
      expect(screen.queryByTestId(`nav-${id}`)).not.toBeInTheDocument();
      // …but very much on screen.
      await expectScreenDrawn(id);
      unmount();
    }
  });

  it('holds every route that is not a destination in the developer area', () => {
    const primary = PRIMARY_NAV.map((entry) => entry.routeId);
    expect([...primary, ...DEVELOPER_ROUTE_IDS].sort()).toEqual(
      ROUTES.map((route) => route.id).sort(),
    );
    expect(DEVELOPER_ROUTE_IDS).toHaveLength(ROUTES.length - primary.length);
  });
});
