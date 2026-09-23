import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { ApiClient } from '@/api';
import { ApiProvider } from '@/app/ApiContext';
import { renderWithProviders } from '@/test/utils';

import DecisionsPage from './index';

const PENDING = {
  run_id: 'research-2026-09-22T16',
  ts_utc: '2026-09-22T16:00:00Z',
  path: 'proposals/research-2026-09-22T16.json',
  module: 'trend',
  confidence: 0.62,
  abstain: false,
  signal_id: null,
  approval_status: 'pending',
  expires_utc: '2026-09-22T22:00:00Z',
  seconds_left: 5400,
};

function clientWithSpy() {
  const calls: Array<{ url: string; method: string; body: unknown }> = [];
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input.toString();
    const method = init?.method ?? 'GET';
    calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : null });
    const json = (body: unknown) =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    if (url.startsWith('/api/proposals/pending')) return json({ pending: [PENDING] });
    if (url.startsWith('/api/proposals?')) return json({ proposals: [] });
    if (url.startsWith('/api/runs')) return json({ runs: [] });
    if (url.includes('/approve') || url.includes('/reject')) {
      return json({
        run_id: PENDING.run_id, decision: url.includes('/approve') ? 'approve' : 'reject',
        actor: 'human:console:s1', channel: 'console',
        decided_utc: '2026-09-22T17:00:00Z', expires_utc: PENDING.expires_utc,
        path: PENDING.path, note: null, proposal_sha256: null,
      });
    }
    return json({});
  }) as unknown as typeof fetch;
  return { calls, client: new ApiClient({ fetchImpl }) };
}

async function openApprovals() {
  const { calls, client } = clientWithSpy();
  renderWithProviders(
    <ApiProvider client={client}>
      <DecisionsPage />
    </ApiProvider>,
  );
  await userEvent.click(await screen.findByRole('tab', { name: /Waiting on you/ }));
  await screen.findByText(PENDING.run_id);
  return calls;
}

/**
 * `POST /api/proposals/{run_id}/approve|reject` existed since P5 and nothing in the SPA
 * called them: in LIVE·PROPOSE the operator watched the 6 h TTL run out on the very page
 * the header badge points at, and had to fall back to Telegram.
 */
describe('Decisions → Approvals queue', () => {
  it('approves a pending proposal from the queue', async () => {
    const calls = await openApprovals();
    await userEvent.click(screen.getByTestId(`approve-${PENDING.run_id}`));
    await userEvent.type(screen.getByTestId('approval-note'), 'trend intact');
    await userEvent.click(screen.getByTestId('confirm-submit'));
    await waitFor(() =>
      expect(calls.some((c) => c.url.includes('/approve'))).toBe(true));
    const approve = calls.find((c) => c.url.includes('/approve'));
    expect(approve?.method).toBe('POST');
    expect(approve?.url).toContain(encodeURIComponent(PENDING.run_id));
    expect(approve?.body).toEqual({ note: 'trend intact' });
  });

  it('rejects one too, and sends no note when none was typed', async () => {
    const calls = await openApprovals();
    await userEvent.click(screen.getByTestId(`reject-${PENDING.run_id}`));
    await userEvent.click(screen.getByTestId('confirm-submit'));
    await waitFor(() => expect(calls.some((c) => c.url.includes('/reject'))).toBe(true));
    expect(calls.find((c) => c.url.includes('/reject'))?.body).toEqual({ note: null });
  });

  it('asks before it signs anything', async () => {
    const calls = await openApprovals();
    await userEvent.click(screen.getByTestId(`approve-${PENDING.run_id}`));
    expect(screen.getByTestId('confirm-dialog')).toBeInTheDocument();
    expect(calls.some((c) => c.url.includes('/approve'))).toBe(false);
  });

  it('re-reads the queue and the header badge once a decision lands', async () => {
    const calls = await openApprovals();
    const before = calls.filter((c) => c.url.startsWith('/api/proposals/pending')).length;
    await userEvent.click(screen.getByTestId(`approve-${PENDING.run_id}`));
    await userEvent.click(screen.getByTestId('confirm-submit'));
    await waitFor(() =>
      expect(
        calls.filter((c) => c.url.startsWith('/api/proposals/pending')).length,
      ).toBeGreaterThan(before),
    );
  });
});
