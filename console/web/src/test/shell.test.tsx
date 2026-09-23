import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';

import { ApiClient } from '../api/client';
import type { MetaResponse } from '../api/contracts';
import { App } from '../App';
import { AppProviders } from '../app/Providers';
import { ROUTES, pageModuleKey } from '../routes';
import { FakeEventSource, fakeFetch, testQueryClient } from './utils';

const META: MetaResponse = {
  version: '2.0.0',
  schema_version: 3,
  config_version: 2,
  started_at: '2026-09-22T04:00:00Z',
  now: '2026-09-22T04:30:00Z',
  port: 8787,
  host: '127.0.0.1',
  automated_run: false,
  git: {
    available: true,
    branch: 'claude/code-building-plan-qxzqsg',
    commit: 'abc1234abc1234',
    short: 'abc1234',
    dirty: false,
    error: null,
  },
  modes: [
    {
      sleeve: 'a',
      state: 'TEST',
      submode: null,
      run_id: '2026-09-22T08:30+04:00',
      seed_usdt: 10_000,
      is_live: false,
    },
    {
      sleeve: 'b',
      state: 'LIVE_PROPOSE',
      submode: 'propose',
      run_id: '2026-09-22T08:30+04:00',
      seed_usdt: null,
      is_live: true,
    },
  ],
  mode_verified: true,
  mode_reason: 'ok',
  kill: { engaged: false, reason: null, since: null, path: 'ops/killdir/KILL' },
  bless: { ok: true, reason: 'ok', changed: [], missing: [], blessed_at: null, blessed_by: null },
  invariants: [
    {
      key: 'gate_in_order_path',
      title: 'gate-in-order-path',
      status: 'ok',
      enforced_by: 'strategies/riskgate.py:check_entry',
      detail: null,
    },
    {
      key: 'console_localhost_only',
      title: 'console-127.0.0.1',
      status: 'ok',
      enforced_by: 'console/__main__.py:main',
      detail: null,
    },
  ],
};

/** `console/routers/approvals.PendingResponse`: no count, and decided rows stay in the list. */
const PENDING = {
  requires_approval: true,
  ttl_hours: 12,
  items: [
    {
      run_id: '2026-09-22T08:30+04:00',
      ts_utc: '2026-09-22T04:30:00Z',
      valid: true,
      abstain: false,
      status: 'pending',
      decision: null,
      decided_utc: null,
      actor: null,
      channel: null,
      note: null,
      applied: false,
      expires_utc: '2026-09-22T16:30:00Z',
      seconds_left: 3600,
    },
    {
      run_id: '2026-09-21T08:30+04:00',
      ts_utc: '2026-09-21T04:30:00Z',
      valid: true,
      abstain: false,
      status: 'approved',
      decision: 'approve',
      decided_utc: '2026-09-21T05:00:00Z',
      actor: 'human:console',
      channel: 'console',
      note: null,
      applied: true,
      expires_utc: '2026-09-21T16:30:00Z',
      seconds_left: 0,
    },
  ],
};

/** `console/routers/llm.ProvidersResponse`: cards with a credential and a breaker. */
const PROVIDERS = {
  auth_mode: 'subscription',
  providers: [
    {
      key: 'claude:subscription',
      kind: 'claude_sdk',
      enabled: true,
      credential_present: true,
      auth_source: 'token',
      detail: 'CLAUDE_CODE_OAUTH_TOKEN (claude setup-token)',
      circuit: 'closed',
      consecutive_failures: 0,
      open_until: null,
      last_ok_utc: '2026-09-22T04:00:00Z',
      last_error: null,
      degraded_until: null,
    },
    {
      key: 'ollama',
      kind: 'ollama',
      enabled: true,
      credential_present: true,
      auth_source: 'local',
      detail: 'http://127.0.0.1:11434',
      base_url: 'http://127.0.0.1:11434',
      circuit: 'closed',
      consecutive_failures: 0,
      open_until: null,
      last_ok_utc: null,
      last_error: null,
      degraded_until: null,
    },
  ],
  rate_limit: { status: 'allowed', utilization: 0.42, resets_at: '2026-09-22T09:00:00Z' },
  month: { month: '2026-09', total_usd: 12.5, by_provider: {} },
};

/** A route no feature package has shipped yet, so the chrome tests stay hermetic. */
const UNBUILT = ROUTES.find((route) => pageModuleKey(route.id) === null);
const SAFE_ROUTE = UNBUILT?.path ?? '/';

function renderShell(
  overrides: Record<string, { status?: number; body?: unknown }> = {},
  route = SAFE_ROUTE,
) {
  const client = new ApiClient({
    fetchImpl: fakeFetch({
      '/api/auth/me': {
        body: {
          authenticated: true,
          actor: 'human:console',
          step_up_until: null,
          csrf: 'csrf-1',
          expires: '2026-09-23T04:30:00Z',
        },
      },
      '/api/meta': { body: META },
      '/api/ops/health': {
        body: {
          as_of: '2026-09-22T04:30:00Z',
          freshness_minutes: { 'BTC/USDT-1h': 12 },
          data_age_minutes: 12,
          staleness_limit_min: 120,
          open_incidents: 0,
          undelivered_alerts: 0,
        },
      },
      '/api/approvals/pending': { body: PENDING },
      '/api/llm/providers': { body: PROVIDERS },
      '/api/invariants/strip': {
        body: {
          ok: false,
          counts: { ok: 1, fail: 1 },
          strip: {
            'gate-in-order-path': 'ok',
            'console-127.0.0.1': 'ok',
            'automated-runs-cannot-change-limits': 'ok',
            'config-blessed': 'fail',
          },
        },
      },
      ...overrides,
    }),
  });
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

beforeEach(() => {
  FakeEventSource.reset();
});

describe('shell', () => {
  it('shows the login screen when there is no session', async () => {
    renderShell({ '/api/auth/me': { status: 401, body: { error: { code: 'unauthorized', message: 'no session', detail: null } } } });
    expect(await screen.findByTestId('login-page')).toBeInTheDocument();
  });

  it('renders the global chrome once authenticated', async () => {
    renderShell();
    expect(await screen.findByTestId('app-header')).toBeInTheDocument();

    const badgeA = await screen.findByTestId('mode-badge-a');
    expect(badgeA).toHaveTextContent('Rules bot: TEST · seed 10,000');
    expect(await screen.findByTestId('mode-badge-b')).toHaveTextContent('AI bot: LIVE·PROPOSE');
    expect(screen.getByTestId('mode-badge-b')).toHaveAttribute('data-live', 'true');

    // One small badge, not five pills of text. It carries the worst status, and the five
    // promises are behind it — the guarantees matter, the permanent wall of text did not.
    expect(screen.getByTestId('safety-strip')).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId('safety-badge')).toHaveAttribute('data-status', 'fail'),
    );
    await userEvent.click(screen.getByTestId('safety-badge'));
    const promises = await screen.findByTestId('safety-detail');
    expect(within(promises).getByTestId('safety-gate-in-order-path')).toHaveAttribute(
      'data-status',
      'ok',
    );
    expect(within(promises).getByTestId('safety-config-blessed')).toHaveAttribute(
      'data-status',
      'fail',
    );
    // A pill the invariants service did not report is unknown, never "ok".
    expect(within(promises).getByTestId('safety-live-entry-human-only')).toHaveAttribute(
      'data-status',
      'unknown',
    );
    await userEvent.click(screen.getByTestId('safety-badge'));

    expect(screen.getByTestId('kill-button')).toBeInTheDocument();
    expect(screen.getByTestId('sse-indicator')).toBeInTheDocument();
    expect(screen.getByTestId('stepup-lock')).toHaveAttribute('data-active', 'false');
    expect(screen.getByTestId('clock')).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId('provider-chip')).toHaveTextContent('Claude subscription ✓'),
    );
    expect(screen.getByTestId('provider-chip')).toHaveTextContent('RL 42%');
    expect(screen.getByTestId('provider-chip')).toHaveAttribute('data-status', 'ok');
    // The operator navigation: five destinations, and the developer screens behind the
    // corner switch rather than in the list.
    expect(screen.getByTestId('nav-overview')).toBeInTheDocument();
    expect(screen.getByTestId('nav-primary')).toBeInTheDocument();
    expect(screen.queryByTestId('nav-invariants')).not.toBeInTheDocument();
    expect(screen.getByTestId('view-mode-toggle')).toHaveAttribute('data-view', 'operator');
  });

  it('brings the developer screens into the navigation, and takes them out again', async () => {
    const user = userEvent.setup();
    renderShell();
    await screen.findByTestId('app-header');

    await user.click(screen.getByTestId('view-mode-toggle'));
    expect(screen.getByTestId('view-mode-toggle')).toHaveAttribute('data-view', 'developer');
    expect(screen.getByTestId('nav-invariants')).toBeInTheDocument();
    expect(screen.getByTestId('nav-audit')).toBeInTheDocument();
    // The five destinations do not move when the developer area opens.
    expect(screen.getByTestId('nav-overview')).toBeInTheDocument();
    expect(screen.getByTestId('nav-skills')).toBeInTheDocument();

    await user.click(screen.getByTestId('view-mode-toggle'));
    expect(screen.queryByTestId('nav-invariants')).not.toBeInTheDocument();
    expect(screen.getByTestId('nav-overview')).toBeInTheDocument();
  });

  it('counts only the approvals a human can still act on', async () => {
    renderShell();
    // Two rows come back; one is already approved and expired, so the badge reads 1.
    await waitFor(() =>
      expect(screen.getByTestId('approvals-counter')).toHaveAttribute('data-pending', '1'),
    );
  });

  it('renders the not-built-yet placeholder for a route no package has shipped', async () => {
    if (!UNBUILT) return; // every page has landed
    renderShell({}, UNBUILT.path);
    expect(await screen.findByTestId(`not-built-${UNBUILT.id}`)).toBeInTheDocument();
  });

  it('opens the command palette and lists every page', async () => {
    const user = userEvent.setup();
    renderShell();
    await user.click(await screen.findByTestId('palette-button'));
    const palette = await screen.findByTestId('command-palette');
    expect(palette).toBeInTheDocument();
    await user.type(await screen.findByTestId('palette-input'), 'invar');
    expect(await screen.findByTestId('palette-item-nav:invariants')).toBeInTheDocument();
  });

  it('turns the header red and offers Resume when KILL is engaged', async () => {
    renderShell({
      '/api/meta': {
        body: {
          ...META,
          kill: {
            engaged: true,
            reason: 'exchange outage',
            since: '2026-09-22T04:00:00Z',
            path: 'ops/killdir/KILL',
          },
        },
      },
    });
    expect(await screen.findByTestId('kill-banner')).toHaveTextContent('exchange outage');
    expect(screen.getByTestId('resume-button')).toBeInTheDocument();
    expect(screen.getByTestId('app-header')).toHaveAttribute('data-killed', 'true');
  });

  it('opens the alerts drawer and shows alerts pushed over SSE', async () => {
    const user = userEvent.setup();
    renderShell();
    await screen.findByTestId('app-header');
    await waitFor(() => expect(FakeEventSource.last).toBeDefined());

    // An SSE frame is a state update from outside React, so it is delivered inside act().
    act(() => {
      FakeEventSource.last?.emit('open');
      FakeEventSource.last?.emit('alert', {
        topic: 'alert',
        id: '1',
        ts: '2026-09-22T04:31:00Z',
        payload: {
          severity: 'critical',
          title: 'Reconcile mismatch',
          message: 'ledger vs exchange above tolerance',
          source: 'reconcile',
          key: null,
        },
      });
    });

    await user.click(screen.getByTestId('alerts-button'));
    expect(await screen.findByTestId('alerts-drawer')).toBeInTheDocument();
    expect(await screen.findByText('Reconcile mismatch')).toBeInTheDocument();
  });
});
