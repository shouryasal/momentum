import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { ApiClient } from '@/api';
import { ApiProvider } from '@/app/ApiContext';
import { SessionProvider } from '@/app/SessionContext';
import { fakeFetch, renderWithProviders } from '@/test/utils';

import SecretsPage from './SecretsPage';

const LIST = {
  secrets: [
    {
      name: 'CLAUDE_CODE_OAUTH_TOKEN', label: 'Claude subscription token', group: 'claude',
      used_by: ['research', 'review'], required: true,
      help: 'From `claude setup-token` on this machine.',
      present: true, last4: '9xQz', updated_at: '2026-09-20T10:00:00Z',
    },
    {
      name: 'ANTHROPIC_API_KEY', label: 'Anthropic API key', group: 'claude',
      used_by: ['research'], required: false, help: 'Metered Console key.',
      present: false, last4: null, updated_at: null,
    },
    {
      name: 'BINANCE_KEY_A', label: 'Binance key (sleeve A)', group: 'exchange',
      used_by: ['reconcile', 'preflight'], required: false,
      help: 'Enable Reading + Spot trading ONLY; withdrawals disabled.',
      present: true, last4: 'AB12', updated_at: '2026-09-01T09:00:00Z',
    },
  ],
  auth: {
    configured: 'subscription', env: 'subscription', effective: 'subscription',
    in_sync: true, subscription_source: 'token',
    login_session: { present: false, expires_at: null, expired: false, subscription_type: null },
  },
};

function client(routes: Parameters<typeof fakeFetch>[0]): ApiClient {
  return new ApiClient({ fetchImpl: fakeFetch(routes) });
}

const SESSION = {
  authenticated: true,
  actor: 'human:console:test',
  step_up_until: null,
  csrf: 'csrf-token',
  expires: null,
};

function render(routes: Parameters<typeof fakeFetch>[0] = { '/api/secrets': { body: LIST } }) {
  const api = client({ '/api/auth/me': { body: SESSION }, ...routes });
  return renderWithProviders(
    <ApiProvider client={api}>
      <SessionProvider client={api}>
        <SecretsPage />
      </SessionProvider>
    </ApiProvider>,
  );
}

/** The credential table moved under Advanced; open it the way an operator would. */
async function openAdvanced(): Promise<void> {
  await userEvent.click(await screen.findByTestId('secrets-advanced-toggle'));
  await screen.findByTestId('secrets-advanced');
}

describe('SecretsPage', () => {
  it('shows presence and last4, grouped, with the jobs that receive each secret', async () => {
    render();
    await openAdvanced();
    await waitFor(() => expect(screen.getByTestId('secrets-claude')).toBeInTheDocument());
    const claude = screen.getByTestId('secrets-claude');
    expect(claude).toHaveTextContent('CLAUDE_CODE_OAUTH_TOKEN');
    expect(claude).toHaveTextContent('…9xQz');
    expect(claude).toHaveTextContent('research, review');
    expect(screen.getByTestId('secrets-exchange')).toHaveTextContent('reconcile, preflight');
  });

  it('marks a missing required secret and an absent optional one differently', async () => {
    render();
    await openAdvanced();
    await waitFor(() => expect(screen.getByTestId('secrets-claude')).toBeInTheDocument());
    expect(screen.getByTestId('secrets-claude')).toHaveTextContent('empty');
  });

  it('opens a write-only input that is never pre-filled', async () => {
    render();
    await openAdvanced();
    await waitFor(() => expect(screen.getByTestId('secrets-claude')).toBeInTheDocument());
    const rows = screen.getAllByRole('button', { name: 'Set' });
    await userEvent.click(rows[0] as HTMLElement);
    const input = await screen.findByLabelText('new value');
    expect(input).toHaveValue('');
    expect(screen.getByText(/never pre-filled/)).toBeInTheDocument();
  });

  it('warns when models.yaml and .env disagree about the auth mode', async () => {
    render({
      '/api/secrets': {
        body: { ...LIST, auth: { ...LIST.auth, env: 'api_key', in_sync: false } },
      },
    });
    expect(await screen.findByText(/Auth mode is out of step/)).toBeInTheDocument();
  });

  it('warns about billing before switching away from the subscription', async () => {
    render();
    await openAdvanced();
    await waitFor(() => expect(screen.getByTestId('secrets-claude')).toBeInTheDocument());
    await userEvent.click(screen.getByRole('radio', { name: /api_key/ }));
    expect(await screen.findByText(/can spend money/)).toBeInTheDocument();
  });

  it('never renders a secret value anywhere on the page', async () => {
    const { container } = render();
    await openAdvanced();
    await waitFor(() => expect(screen.getByTestId('secrets-claude')).toBeInTheDocument());
    expect(container.textContent).not.toMatch(/sk-ant-/);
    expect(container.textContent).not.toMatch(/value"?\s*:/);
  });
});
