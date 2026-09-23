import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EventStreamProvider } from '@/app/EventStreamContext';
import { SessionProvider } from '@/app/SessionContext';
import { FakeEventSource, renderWithProviders } from '@/test/utils';

import SetupPage from './index';

/**
 * The week-1 gate is the only step that watches something run.
 *
 * The healthcheck is detached and can take minutes, so the wizard follows the log the job
 * writes to instead of telling the human to go and look. This asserts the loop end to end
 * from the page's side: start the job, learn the file it writes, join that topic on the
 * shell's stream, and render what arrives.
 */

const EARN = {
  values: {
    modes: { test: { seed_usdt: { a: 10000, b: 10000 } }, live: { max_seed_usdt: {} } },
    backup: { dest: '~/earn-backups', mirror_dest: null },
    research: { slots: ['08:30'] },
    git: { live_branch: 'main' },
    paper: { anchor_date: '2026-01-01' },
    telegram: { enabled: false },
  },
  confirm_phrase: 'I UNDERSTAND',
  sha: 'abc',
};

function route(url: string): unknown | undefined {
  if (url.includes('/api/auth/me')) {
    return { authenticated: true, csrf: 'csrf', expires_at: '2099-01-01T00:00:00Z' };
  }
  if (url.includes('/api/config/earn')) return EARN;
  if (url.includes('/api/config/models')) return { values: { auth: {} }, confirm_phrase: '', sha: 'd' };
  if (url.includes('/api/ops/jobs/healthcheck/run')) {
    return { id: 4, job: 'healthcheck', status: 'running', log: 'health.log', pid: 1234 };
  }
  if (url.includes('/api/logs/health.log')) {
    return {
      name: 'health.log',
      bytes: 12,
      lines: 1,
      text: 'starting up',
      topic: 'log:health.log',
      following: true,
    };
  }
  if (url.includes('/api/ops/host')) return {};
  return undefined;
}

function stubFetch() {
  vi.stubGlobal('fetch', (async (input: RequestInfo | URL) => {
    const body = route(String(input));
    return new Response(JSON.stringify(body ?? { error: { code: 'not_found', message: '', detail: null } }), {
      status: body === undefined ? 404 : 200,
      headers: { 'content-type': 'application/json' },
    });
  }) as unknown as typeof fetch);
}

afterEach(() => {
  vi.unstubAllGlobals();
  FakeEventSource.reset();
});

describe('Setup week-1 gate', () => {
  it('runs the healthcheck and streams its log instead of polling', async () => {
    stubFetch();
    renderWithProviders(
      <SessionProvider>
        <EventStreamProvider factory={(url) => new FakeEventSource(url)}>
          <SetupPage />
        </EventStreamProvider>
      </SessionProvider>,
    );
    await waitFor(() => expect(screen.getByTestId('setup-page')).toBeInTheDocument());
    await userEvent.click(screen.getByText('Week-1 gate'));

    await userEvent.click(screen.getByRole('button', { name: /Run the week-1 gate/ }));
    await waitFor(() =>
      expect(screen.getByText(/started healthcheck .* logs\/health\.log/)).toBeInTheDocument(),
    );
    await waitFor(() => expect(screen.getByTestId('gate-output')).toHaveTextContent('starting up'));
    await waitFor(() =>
      expect(screen.getByTestId('gate-stream-status')).toHaveTextContent('streaming logs/health.log'),
    );

    const source = FakeEventSource.last;
    expect(decodeURIComponent(source?.url ?? '')).toContain('log:health.log');
    act(() => {
      source?.emit('log:health.log', {
        topic: 'log:health.log',
        id: '3',
        ts: '2026-09-22T04:30:00Z',
        payload: { name: 'health.log', lines: ['all checks passed'], offset: 40, dropped: 0 },
      });
    });
    await waitFor(() => expect(screen.getByTestId('gate-output')).toHaveTextContent('all checks passed'));
  });
});
