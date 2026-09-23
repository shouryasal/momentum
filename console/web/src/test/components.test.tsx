import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ApiClient, ApiError } from '../api';
import { SessionProvider } from '../app/SessionContext';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { DataTable } from '../components/DataTable';
import { DiffView } from '../components/DiffView';
import { EmptyState } from '../components/EmptyState';
import { JsonViewer } from '../components/JsonViewer';
import { KillButton } from '../components/KillButton';
import { SSEIndicator } from '../components/SSEIndicator';
import { StatCard } from '../components/StatCard';
import { languageForPath } from '../components/CodeEditor';
import { renderWithProviders } from './utils';

describe('DataTable', () => {
  const rows = [
    { id: 'a', sleeve: 'a', nav: 10_120 },
    { id: 'b', sleeve: 'b', nav: 9_880 },
  ];
  const columns = [
    { key: 'sleeve', header: 'Sleeve', render: (row: (typeof rows)[number]) => row.sleeve },
    {
      key: 'nav',
      header: 'NAV',
      render: (row: (typeof rows)[number]) => String(row.nav),
      sortValue: (row: (typeof rows)[number]) => row.nav,
    },
  ];

  it('renders rows and sorts by a sortable column', async () => {
    const user = userEvent.setup();
    renderWithProviders(<DataTable columns={columns} rows={rows} rowKey={(row) => row.id} />);
    expect(screen.getAllByRole('row')).toHaveLength(3);

    await user.click(screen.getByTestId('sort-nav'));
    const cells = screen.getAllByRole('cell').map((cell) => cell.textContent);
    expect(cells[1]).toBe('9880');
  });

  it('shows the shared empty state', () => {
    renderWithProviders(<DataTable columns={columns} rows={[]} rowKey={() => 'x'} emptyTitle="No trades" />);
    expect(screen.getByTestId('empty-state')).toHaveTextContent('No trades');
  });

  it('shows a loader while loading', () => {
    renderWithProviders(<DataTable columns={columns} rows={[]} rowKey={() => 'x'} loading />);
    expect(screen.getByTestId('data-table-loading')).toBeInTheDocument();
  });
});

describe('StatCard / EmptyState / JsonViewer / SSEIndicator', () => {
  it('renders a stat with a delta', () => {
    renderWithProviders(<StatCard label="NAV" value="$10,120" delta={{ value: '+1.2%', positive: true }} />);
    expect(screen.getByTestId('stat-card')).toHaveTextContent('NAV');
    expect(screen.getByText('+1.2%')).toBeInTheDocument();
  });

  it('runs the empty-state action', async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    renderWithProviders(<EmptyState title="Nothing" action={{ label: 'Scan now', onClick }} />);
    await user.click(screen.getByText('Scan now'));
    expect(onClick).toHaveBeenCalled();
  });

  it('expands and collapses JSON branches', async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <JsonViewer value={{ gate: { ok: true, checks: ['nav_valid'] } }} defaultExpandedDepth={3} />,
    );
    expect(screen.getByText('checks')).toBeInTheDocument();
    await user.click(screen.getByText('gate'));
    expect(screen.queryByText('checks')).toBeNull();
  });

  it('reflects the stream status and retries on click', async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    renderWithProviders(<SSEIndicator status="reconnecting" attempt={3} onRetry={onRetry} />);
    const indicator = screen.getByTestId('sse-indicator');
    expect(indicator).toHaveAttribute('data-status', 'reconnecting');
    await user.click(indicator);
    expect(onRetry).toHaveBeenCalled();
  });
});

describe('DiffView', () => {
  it('counts added and removed lines', () => {
    renderWithProviders(<DiffView before={'a\nb'} after={'a\nc'} />);
    expect(screen.getByTestId('diff-view')).toHaveTextContent('+1');
    expect(screen.getByTestId('diff-view')).toHaveTextContent('−1');
  });
});

describe('ConfirmDialog', () => {
  it('requires the typed phrase before confirming', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    renderWithProviders(
      <ConfirmDialog
        opened
        onClose={() => undefined}
        title="Reset test run"
        confirmPhrase="RESET"
        onConfirm={onConfirm}
      />,
    );
    expect(screen.getByTestId('confirm-submit')).toBeDisabled();
    await user.type(screen.getByTestId('confirm-phrase'), 'RESET');
    expect(screen.getByTestId('confirm-submit')).toBeEnabled();
    await user.click(screen.getByTestId('confirm-submit'));
    // The typed phrase is handed back, not swallowed: Settings → History → Revert has to
    // forward what the operator typed instead of the phrase the server told the page.
    expect(onConfirm).toHaveBeenCalledWith({ phrase: 'RESET' });
  });

  it('asks for a step-up token when the session is not stepped up', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    renderWithProviders(
      <ConfirmDialog opened onClose={() => undefined} title="Go live" requireStepUp onConfirm={onConfirm} />,
    );
    expect(screen.getByTestId('confirm-submit')).toBeDisabled();
    await user.type(screen.getByTestId('confirm-stepup'), 'token-123');
    await user.click(screen.getByTestId('confirm-submit'));
    expect(onConfirm).toHaveBeenCalledWith({ stepUpToken: 'token-123' });
  });

  it('skips the token when the step-up window is already open', () => {
    renderWithProviders(
      <ConfirmDialog
        opened
        onClose={() => undefined}
        title="Go live"
        requireStepUp
        stepUpSatisfied
        onConfirm={() => undefined}
      />,
    );
    expect(screen.queryByTestId('confirm-stepup')).toBeNull();
    expect(screen.getByTestId('confirm-submit')).toBeEnabled();
  });

  /**
   * The root cause of the five broken dialogs: the token was collected and handed to the
   * caller, and five callers dropped it, so the guarded request went out with no step-up
   * and came back 403. The dialog opens the window itself now — a caller cannot forget.
   */
  it('opens the step-up window itself before running the action', async () => {
    const user = userEvent.setup();
    const calls: Array<{ url: string; method: string; body: unknown }> = [];
    const client = new ApiClient({
      fetchImpl: (async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = typeof input === 'string' ? input : input.toString();
        calls.push({
          url,
          method: init?.method ?? 'GET',
          body: init?.body ? JSON.parse(String(init.body)) : null,
        });
        if (url.includes('/auth/me')) {
          return new Response(
            JSON.stringify({ authenticated: true, actor: 'human:console:s', csrf: 'c',
              step_up_until: null, expires: null }),
            { status: 200, headers: { 'content-type': 'application/json' } },
          );
        }
        return new Response(JSON.stringify({ step_up_until: '2099-01-01T00:00:00Z' }), {
          status: 200, headers: { 'content-type': 'application/json' },
        });
      }) as unknown as typeof fetch,
    });
    const order: string[] = [];
    renderWithProviders(
      <SessionProvider client={client}>
        <ConfirmDialog
          opened
          onClose={() => undefined}
          title="Install the crontab"
          requireStepUp
          onConfirm={() => {
            order.push('action');
          }}
        />
      </SessionProvider>,
    );
    await waitFor(() => expect(calls.some((c) => c.url.includes('/auth/me'))).toBe(true));
    await user.type(screen.getByTestId('confirm-stepup'), 'console-token');
    await user.click(screen.getByTestId('confirm-submit'));
    await waitFor(() => expect(order).toEqual(['action']));
    const stepUp = calls.find((c) => c.url.includes('/auth/step-up'));
    expect(stepUp, 'the dialog must POST /auth/step-up before the guarded action').toBeTruthy();
    expect(stepUp?.method).toBe('POST');
    expect(stepUp?.body).toEqual({ token: 'console-token' });
  });

  it('takes the step-up window from the session when the caller does not say', async () => {
    const client = new ApiClient({
      fetchImpl: (async () =>
        new Response(
          JSON.stringify({ authenticated: true, actor: 'a', csrf: 'c',
            step_up_until: '2099-01-01T00:00:00Z', expires: null }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        )) as unknown as typeof fetch,
    });
    renderWithProviders(
      <SessionProvider client={client}>
        <ConfirmDialog
          opened
          onClose={() => undefined}
          title="Restart"
          requireStepUp
          onConfirm={() => undefined}
        />
      </SessionProvider>,
    );
    // SchedulePanel and ChangeDetail passed no `stepUpSatisfied` at all, so the token box
    // was shown — and discarded — even while the session was stepped up.
    await waitFor(() => expect(screen.queryByTestId('confirm-stepup')).toBeNull());
  });

  it('names the field a 422 rejected instead of one opaque sentence', async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <ConfirmDialog
        opened
        onClose={() => undefined}
        title="Engage KILL"
        onConfirm={() => {
          throw new ApiError(422, {
            code: 'invalid',
            message: 'request body failed validation',
            detail: { errors: [{ loc: ['body', 'reason'], msg: 'String should have at least 3 characters' }] },
          });
        }}
      />,
    );
    await user.click(screen.getByTestId('confirm-submit'));
    await waitFor(() =>
      expect(screen.getByTestId('confirm-error-fields')).toHaveTextContent(
        'reason: String should have at least 3 characters',
      ),
    );
  });

  it('surfaces a failed confirmation', async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <ConfirmDialog
        opened
        onClose={() => undefined}
        title="Apply"
        onConfirm={() => {
          throw new Error('ops lock held');
        }}
      />,
    );
    await user.click(screen.getByTestId('confirm-submit'));
    await waitFor(() => expect(screen.getByTestId('confirm-error')).toHaveTextContent('ops lock held'));
  });
});

describe('KillButton', () => {
  it('needs a reason and sends the flatten choice', async () => {
    const user = userEvent.setup();
    const onKill = vi.fn();
    renderWithProviders(
      <KillButton engaged={false} onKill={onKill} onResume={() => undefined} />,
    );
    await user.click(screen.getByTestId('kill-button'));
    expect(screen.getByTestId('confirm-submit')).toBeDisabled();
    await user.type(screen.getByTestId('kill-reason'), 'exchange outage');
    await user.click(screen.getByTestId('kill-flatten'));
    await user.click(screen.getByTestId('confirm-submit'));
    expect(onKill).toHaveBeenCalledWith({ reason: 'exchange outage', flatten: true });
  });

  /**
   * `console.contracts.KillRequest.reason` is `min_length=3`. The button used to enable on
   * one character, so during an incident the operator typed "x", clicked Engage KILL, and
   * got "request body failed validation" back with trading still running.
   */
  it('will not send a reason the server rejects', async () => {
    const user = userEvent.setup();
    const onKill = vi.fn();
    renderWithProviders(
      <KillButton engaged={false} onKill={onKill} onResume={() => undefined} />,
    );
    await user.click(screen.getByTestId('kill-button'));
    await user.type(screen.getByTestId('kill-reason'), 'x');
    expect(screen.getByTestId('confirm-submit')).toBeDisabled();
    expect(screen.getByText(/at least 3 characters/)).toBeInTheDocument();
    await user.type(screen.getByTestId('kill-reason'), 'yz');
    expect(screen.getByTestId('confirm-submit')).toBeEnabled();
    await user.click(screen.getByTestId('confirm-submit'));
    expect(onKill).toHaveBeenCalledWith({ reason: 'xyz', flatten: false });
  });

  it('caps the reason at what the server accepts', async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <KillButton engaged={false} onKill={vi.fn()} onResume={() => undefined} />,
    );
    await user.click(screen.getByTestId('kill-button'));
    expect(screen.getByTestId('kill-reason')).toHaveAttribute('maxlength', '500');
  });

  it('asks for the typed phrase and step-up before resuming', async () => {
    const user = userEvent.setup();
    const onResume = vi.fn();
    renderWithProviders(
      <KillButton engaged reason="manual" onKill={() => undefined} onResume={onResume} />,
    );
    await user.click(screen.getByTestId('resume-button'));
    expect(screen.getByTestId('confirm-submit')).toBeDisabled();
    await user.type(screen.getByTestId('confirm-phrase'), 'RESUME TRADING');
    await user.type(screen.getByTestId('confirm-stepup'), 'token-9');
    await user.click(screen.getByTestId('confirm-submit'));
    expect(onResume).toHaveBeenCalledWith({ confirmPhrase: 'RESUME TRADING', stepUpToken: 'token-9' });
  });
});

describe('CodeEditor helpers', () => {
  it('maps file names to CodeMirror languages', () => {
    expect(languageForPath('config/earn.yaml')).toBe('yaml');
    expect(languageForPath('SKILL.md')).toBe('markdown');
    expect(languageForPath('scripts/tca.py')).toBe('python');
    expect(languageForPath('config/riskgate.json')).toBe('json');
    expect(languageForPath('notes.txt')).toBe('text');
  });
});
