/**
 * The sign-in surface, from the operator's side.
 *
 * These tests are written against the two things the owner asked for — *where do I sign in
 * for Claude*, and *local models should come from the backend* — so they assert what is on
 * the screen, not how it got there: a button that starts the flow, a link that says it must
 * be opened on Windows, an explanation for every way it can end, three auth sources with
 * their billing consequences, and a local-model line with nothing to type into.
 *
 * The one invariant asserted everywhere: no token ever reaches the DOM.
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { ApiClient } from '@/api';
import { ApiProvider } from '@/app/ApiContext';
import { SessionProvider } from '@/app/SessionContext';
import { ViewModeProvider, type ViewMode } from '@/app/ViewModeContext';
import { renderWithProviders } from '@/test/utils';

import SecretsPage from './SecretsPage';
import type { LocalModelStatus, SignInStatus } from './api';

const LIST = {
  secrets: [
    {
      name: 'CLAUDE_CODE_OAUTH_TOKEN', label: 'Claude subscription token', group: 'claude',
      used_by: ['research'], required: true, help: 'From `claude setup-token`.',
      present: false, last4: null, updated_at: null,
    },
    {
      name: 'ANTHROPIC_API_KEY', label: 'Anthropic API key', group: 'claude',
      used_by: ['research'], required: false, help: 'Metered Console key.',
      present: true, last4: 'KEY9', updated_at: '2026-09-01T00:00:00Z',
    },
    {
      name: 'BINANCE_KEY_A', label: 'Binance key (sleeve A)', group: 'exchange',
      used_by: ['reconcile'], required: false, help: 'Read + spot only.',
      present: true, last4: 'AB12', updated_at: '2026-09-01T09:00:00Z',
    },
    {
      name: 'TELEGRAM_BOT_TOKEN', label: 'Telegram bot token', group: 'telegram',
      used_by: ['telegram'], required: false, help: 'The ops bot.',
      present: false, last4: null, updated_at: null,
    },
  ],
  auth: {
    configured: 'subscription', env: 'subscription', effective: 'subscription',
    in_sync: true, subscription_source: 'token',
    login_session: { present: false, expires_at: null, expired: false, subscription_type: null },
  },
};

const CLI = { present: true, path: '/home/x/.local/bin/claude', version: '2.1.280', pty: true };

const IDLE: SignInStatus = {
  state: 'idle',
  phase: 'idle',
  session: null,
  cli: CLI,
  credential: { name: 'CLAUDE_CODE_OAUTH_TOKEN', present: false, last4: null, updated_at: null },
};

/** The link the real CLI prints — claude.com, with the platform callback as redirect. */
const URL =
  'https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a'
  + '&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback'
  + '&state=8c1f0b7a';

function session(over: Partial<NonNullable<SignInStatus['session']>>): SignInStatus {
  const state = over.state ?? 'starting';
  // The server composes `failed:<reason>`; the fixture does the same so a test cannot
  // assert a phase the real API would never send.
  const phase = over.phase ?? (over.reason ? `${state}:${over.reason}` : state);
  return {
    ...IDLE,
    state,
    phase,
    session: {
      id: 's1', state: 'starting', message: '', url: null, reason: null,
      actor: 'human:console', started_at: '2026-09-23T10:00:00Z',
      updated_at: '2026-09-23T10:00:00Z', finished_at: null, last4: null,
      deadline_at: null, exit_code: null, attempts: 0, attempts_left: 3, terminal: false,
      ...over,
      phase,
    },
  };
}

const LOCAL_OK: LocalModelStatus = {
  state: 'connected', line: 'local model: connected, llama3.1:8b, 41 tok/s',
  model: 'llama3.1:8b', base_url: 'http://172.29.0.1:11434', version: '0.34.1',
  tok_per_s: 41, reason: null, fix_command: null, fix_shell: null, pull: null,
  tried: ['http://127.0.0.1:11434', 'http://172.29.0.1:11434'], configurable: false,
};

const LOCAL_DEAD: LocalModelStatus = {
  ...LOCAL_OK,
  state: 'unreachable',
  line: 'local model: unreachable — Ollama is not listening where WSL can see it',
  base_url: null, version: null, tok_per_s: null,
  reason: 'Ollama runs on Windows and binds 127.0.0.1, which inside WSL2 is not the host.',
  fix_command: '[Environment]::SetEnvironmentVariable("OLLAMA_HOST","0.0.0.0","User")',
  fix_shell: 'PowerShell (Windows, not WSL)',
};

interface Call { method: string; path: string; body: unknown }

/**
 * A `fetch` stub that knows about methods, so a POST and a GET on one path can differ —
 * which is exactly the shape of the sign-in route.
 */
function stub(
  routes: Record<string, { status?: number; body?: unknown }>,
  calls: Call[] = [],
): typeof fetch {
  return (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = (typeof input === 'string' ? input : input.toString()).split('?')[0] ?? '';
    const method = (init?.method ?? 'GET').toUpperCase();
    calls.push({
      method,
      path: url,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    const match = routes[`${method} ${url}`] ?? routes[url];
    if (!match) {
      return new Response(
        JSON.stringify({ error: { code: 'not_found', message: url, detail: null } }),
        { status: 404, headers: { 'content-type': 'application/json' } },
      );
    }
    return new Response(match.body === undefined ? '' : JSON.stringify(match.body), {
      status: match.status ?? 200,
      headers: { 'content-type': 'application/json' },
    });
  }) as unknown as typeof fetch;
}

function render(options: {
  signIn?: SignInStatus;
  local?: LocalModelStatus;
  list?: unknown;
  view?: ViewMode;
  steppedUp?: boolean;
  extra?: Record<string, { status?: number; body?: unknown }>;
} = {}) {
  const calls: Call[] = [];
  const api = new ApiClient({
    fetchImpl: stub({
      '/api/auth/me': {
        body: {
          authenticated: true,
          actor: 'human:console:test',
          step_up_until: options.steppedUp ? '2099-01-01T00:00:00Z' : null,
          csrf: 'csrf-token',
          expires: null,
        },
      },
      '/api/secrets': { body: options.list ?? LIST },
      'GET /api/llm/claude/signin': { body: options.signIn ?? IDLE },
      '/api/llm/local-model': { body: options.local ?? LOCAL_OK },
      ...(options.extra ?? {}),
    }, calls),
  });
  const ui = (
    <ApiProvider client={api}>
      <SessionProvider client={api}>
        <SecretsPage />
      </SessionProvider>
    </ApiProvider>
  );
  return {
    calls,
    ...renderWithProviders(
      options.view
        ? <ViewModeProvider initial={options.view}>{ui}</ViewModeProvider>
        : ui,
    ),
  };
}

/**
 * Open the sign-in dialog and return it.
 *
 * The page renders four `ConfirmDialog`s and they share one test id, so the dialog is
 * located from the one thing only this one contains — its plan list.
 */
async function openDialog(): Promise<HTMLElement> {
  await userEvent.click(await screen.findByTestId('claude-signin-button'));
  const plan = await screen.findByTestId('signin-plan');
  return plan.closest('[data-testid="confirm-dialog"]') as HTMLElement;
}

/** Open it and confirm, which is what actually starts the run. */
async function start(confirmLabel = 'Start sign-in') {
  await openDialog();
  await userEvent.click(await screen.findByRole('button', { name: confirmLabel }));
}

/**
 * Open the Advanced section, where the three-source table, the auth mode and the probes
 * live now. They are not gone — the owner said the page had too much content, so the page
 * leads with sign-in and the exchange keys and this is one click down it.
 */
async function openAdvanced(): Promise<void> {
  await userEvent.click(await screen.findByTestId('secrets-advanced-toggle'));
  await screen.findByTestId('secrets-advanced');
}

describe('Claude access', () => {
  it('says plainly whether the system can talk to Claude at all', async () => {
    render();
    expect(await screen.findByTestId('claude-verdict')).toHaveTextContent('not signed in');
    expect(screen.getByText(/will abstain until you sign in/)).toBeInTheDocument();
  });

  it('shows all three sources under Advanced, which is in effect, and what each costs', async () => {
    render();
    // The front page is sign-in, the keys and the local model; this is behind Advanced.
    expect(screen.queryByTestId('claude-sources')).toBeNull();
    await openAdvanced();
    const table = await screen.findByTestId('claude-sources');
    const token = within(table).getByTestId('claude-source-token');
    expect(token).toHaveTextContent('Subscription token');
    expect(token).toHaveTextContent('in use');
    expect(token).toHaveTextContent(/Included in your Claude subscription/);

    const login = within(table).getByTestId('claude-source-login');
    expect(login).toHaveTextContent('~/.claude/.credentials.json');
    expect(login).toHaveTextContent(/expires/i);

    const key = within(table).getByTestId('claude-source-api_key');
    expect(key).toHaveTextContent('set …KEY9');
    expect(key).toHaveTextContent('standby');
    expect(key).toHaveTextContent(/Metered/);
  });

  it('marks the API key as the declared fallback in auto mode', async () => {
    render({
      list: { ...LIST, auth: { ...LIST.auth, effective: 'auto', configured: 'auto', env: 'auto' } },
    });
    await openAdvanced();
    const key = await screen.findByTestId('claude-source-api_key');
    expect(key).toHaveTextContent('fallback');
  });

  it('treats the CLI login session as the one in effect when that is the source', async () => {
    render({
      list: {
        ...LIST,
        auth: {
          ...LIST.auth,
          subscription_source: 'login',
          login_session: {
            present: true, expires_at: '2026-10-01T00:00:00Z', expired: false,
            subscription_type: 'max',
          },
        },
      },
    });
    await openAdvanced();
    expect(await screen.findByTestId('claude-source-login')).toHaveTextContent('in use');
    expect(screen.getByTestId('claude-source-token')).toHaveTextContent('standby');
  });
});

describe('signing in', () => {
  it('asks for the console token before it starts anything', async () => {
    render();
    const dialog = await openDialog();
    // ConfirmDialog(requireStepUp) collects and performs the step-up itself.
    expect(within(dialog).getByLabelText(/console token/i)).toBeInTheDocument();
    expect(within(dialog).getByTestId('signin-plan')).toHaveTextContent('Windows');
    expect(within(dialog).getByText(/never shown here, logged, or sent to your browser/))
      .toBeInTheDocument();
  });

  it('starts the flow on the server, with no terminal for the operator to open', async () => {
    const { calls } = render({
      steppedUp: true,
      extra: { 'POST /api/llm/claude/signin': { body: session({ state: 'starting' }) } },
    });
    await userEvent.click(await screen.findByTestId('claude-signin-button'));
    await userEvent.click(await screen.findByRole('button', { name: 'Start sign-in' }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'POST' && c.path === '/api/llm/claude/signin'))
        .toBe(true));
    expect(calls.find((c) => c.method === 'POST' && c.path === '/api/llm/claude/signin')?.body)
      .toEqual({ replace: false });
  });

  it('warns that signing in again replaces the credential that is already there', async () => {
    render({
      signIn: {
        ...IDLE,
        credential: {
          name: 'CLAUDE_CODE_OAUTH_TOKEN', present: true, last4: '9xQz',
          updated_at: '2026-09-20T10:00:00Z',
        },
      },
    });
    await userEvent.click(await screen.findByTestId('claude-signin-button'));
    expect(await screen.findByText(/already set/)).toHaveTextContent('…9xQz');
    expect(screen.getByRole('button', { name: 'Replace credential' })).toBeInTheDocument();
  });

  it('refuses to start when the CLI is not installed, and says how to install it', async () => {
    render({ signIn: { ...IDLE, cli: { ...CLI, present: false } } });
    const dialog = await openDialog();
    expect(within(dialog).getByText(/not installed in WSL/)).toBeInTheDocument();
    expect(within(dialog).getByText('npm i -g @anthropic-ai/claude-code')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Start sign-in' })).toBeDisabled();
  });

  it('shows the link as something to open on Windows, with a copy button', async () => {
    render({
      signIn: session({
        state: 'url_ready',
        url: URL,
        message: 'Open the link in your Windows browser and approve it.',
        deadline_at: '2099-01-01T00:00:00Z',
      }),
    });
    await userEvent.click(await screen.findByTestId('claude-signin-button'));
    const link = await screen.findByTestId('signin-link');
    expect(link).toHaveTextContent(/Windows browser/);
    expect(link).toHaveTextContent(/waiting inside WSL/);
    expect(within(link).getByRole('link', { name: /Open the approval page/ }))
      .toHaveAttribute('href', URL);
    expect(within(link).getByRole('button', { name: /Copy link/ })).toBeInTheDocument();
  });

  it('asks for the code the browser shows and sends it to the waiting CLI', async () => {
    const waiting = session({
      state: 'awaiting_code',
      url: URL,
      message: 'Approve the link, then paste the code the browser shows you.',
      deadline_at: '2099-01-01T00:00:00Z',
    });
    const { calls } = render({
      signIn: waiting,
      extra: {
        'POST /api/llm/claude/signin/code': { body: session({ state: 'exchanging' }) },
      },
    });
    await userEvent.click(await screen.findByTestId('claude-signin-button'));

    // The link stays visible — the operator may still need to open it — but the step
    // that used to be missing is now on screen.
    expect(await screen.findByTestId('signin-link')).toBeInTheDocument();
    const box = await screen.findByTestId('signin-code');
    expect(box).toHaveTextContent(/authorization code/i);

    await userEvent.type(within(box).getByLabelText(/Authorization code/i), 'code-4e17');
    await userEvent.click(within(box).getByRole('button', { name: /Submit code/ }));

    await waitFor(() =>
      expect(calls.some((c) => c.path === '/api/llm/claude/signin/code')).toBe(true));
    expect(calls.find((c) => c.path === '/api/llm/claude/signin/code'))
      .toMatchObject({ method: 'POST', body: { code: 'code-4e17' } });
  });

  it('turns a rejected code into a retry rather than an ending', async () => {
    render({
      signIn: session({
        state: 'awaiting_code',
        url: URL,
        attempts: 1,
        attempts_left: 2,
        message: 'That code was not accepted. Copy it again from the browser page',
      }),
    });
    await userEvent.click(await screen.findByTestId('claude-signin-button'));
    const retry = await screen.findByTestId('signin-code-retry');
    expect(retry).toHaveTextContent('not accepted');
    expect(retry).toHaveTextContent('2 attempts left');
    expect(screen.queryByTestId('signin-failed')).toBeNull();
    expect(screen.getByRole('button', { name: /Submit code/ })).toBeInTheDocument();
  });

  it('names the phase it is in, so a stuck flow is not just a spinner', async () => {
    render({
      signIn: session({ state: 'exchanging', url: URL, message: 'Checking the code…' }),
    });
    await userEvent.click(await screen.findByTestId('claude-signin-button'));
    expect(await screen.findByTestId('signin-phase')).toHaveTextContent('phase: exchanging');
  });

  it('explains a code that was never pasted back', async () => {
    const outcome = session({
      state: 'failed', reason: 'no_code', terminal: true, url: URL,
      message: 'The CLI asked for the code from the browser and never got one.',
    });
    render({
      signIn: outcome, steppedUp: true,
      extra: { 'POST /api/llm/claude/signin': { body: outcome } },
    });
    await start();
    const failed = await screen.findByTestId('signin-failed');
    expect(failed).toHaveTextContent('never pasted back');
    expect(failed).toHaveTextContent('Nothing was written');
    expect(await screen.findByTestId('signin-phase'))
      .toHaveTextContent('phase: failed:no_code');
  });

  it('explains a timeout as "nothing was written", not as a crash', async () => {
    const outcome = session({
      state: 'failed', reason: 'not_approved', terminal: true,
      url: 'https://claude.ai/oauth/authorize?code=abc',
      message: 'Nobody approved the link in time.',
    });
    render({
      signIn: outcome, steppedUp: true,
      extra: { 'POST /api/llm/claude/signin': { body: outcome } },
    });
    await start();
    const failed = await screen.findByTestId('signin-failed');
    expect(failed).toHaveTextContent('The link was never approved');
    expect(failed).toHaveTextContent('Nothing was written');
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });

  it('reports a non-zero CLI exit with its code and the command to reproduce it', async () => {
    const outcome = session({
      state: 'failed', reason: 'cli_failed', terminal: true, exit_code: 3,
      message: '`claude setup-token` exited with code 3.',
    });
    render({
      signIn: outcome, steppedUp: true,
      extra: { 'POST /api/llm/claude/signin': { body: outcome } },
    });
    await start();
    const failed = await screen.findByTestId('signin-failed');
    expect(failed).toHaveTextContent('exited with code 3');
    expect(within(failed).getByText('claude setup-token')).toBeInTheDocument();
  });

  it('confirms success with last4 only', async () => {
    const outcome: SignInStatus = {
      ...session({ state: 'done', terminal: true, last4: 'ABCD', message: 'Signed in.' }),
      credential: {
        name: 'CLAUDE_CODE_OAUTH_TOKEN', present: true, last4: 'ABCD',
        updated_at: '2026-09-23T10:00:05Z',
      },
    };
    const { container } = render({
      signIn: outcome, steppedUp: true,
      extra: { 'POST /api/llm/claude/signin': { body: outcome } },
    });
    await start('Replace credential');
    const done = await screen.findByTestId('signin-done');
    expect(done).toHaveTextContent('…ABCD');
    expect(container.textContent).not.toMatch(/sk-ant/);
  });

  it('leaves the reason on the page after the modal is closed', async () => {
    render({
      signIn: session({
        state: 'failed', reason: 'no_token', terminal: true,
        message: 'The CLI finished without printing a token.',
      }),
    });
    const note = await screen.findByTestId('claude-last-attempt');
    expect(note).toHaveTextContent(/without printing a token/i);
    expect(note).toHaveTextContent('Nothing was written');
  });
});

describe('the local model', () => {
  it('is one read-only line with no field to type a URL into', async () => {
    const { container } = render();
    const card = await screen.findByTestId('local-model');
    expect(within(card).getByTestId('local-model-line'))
      .toHaveTextContent('local model: connected, llama3.1:8b, 41 tok/s');
    expect(within(card).queryByRole('textbox')).toBeNull();
    expect(container.textContent).toMatch(/Nothing here is a\s+setting/);
  });

  it('gives exactly one command, for exactly one shell, when it is unreachable', async () => {
    render({ local: LOCAL_DEAD });
    const card = await screen.findByTestId('local-model');
    expect(within(card).getByTestId('local-model-state')).toHaveTextContent('unreachable');
    expect(within(card).getByTestId('local-model-reason')).toHaveTextContent(/WSL2/);
    expect(within(card).getAllByTestId('command-line')).toHaveLength(1);
    expect(within(card).getAllByText(/PowerShell \(Windows, not WSL\)/).length)
      .toBeGreaterThan(0);
  });

  it('shows a download as progress, not as an error', async () => {
    render({
      local: {
        ...LOCAL_OK, state: 'pulling',
        line: 'local model: downloading llama3.1:8b (37%) — nothing for you to do',
        pull: { job_id: 'job-1', progress: 0.37, message: 'pulling manifest' },
      },
    });
    const card = await screen.findByTestId('local-model');
    expect(within(card).getByTestId('local-model-state')).toHaveTextContent('pulling');
    expect(within(card).getByTestId('local-model-line')).toHaveTextContent('nothing for you to do');
  });
});

/**
 * The owner's words about this page were "has too much content". These two hold the page
 * to the shape that answers it: three things to look at, and one click to everything else.
 */
describe('a short page, with everything else one click down', () => {
  it('leads with three things and nothing more', async () => {
    render();
    expect(await screen.findByTestId('claude-access')).toBeInTheDocument();
    expect(screen.getByTestId('exchange-keys')).toBeInTheDocument();
    expect(screen.getByTestId('local-model')).toBeInTheDocument();

    // Everything the owner called content: not on the page until asked for.
    expect(screen.queryByTestId('secrets-advanced')).toBeNull();
    expect(screen.queryByTestId('claude-sources')).toBeNull();
    expect(screen.queryByTestId('auth-mode-card')).toBeNull();
    expect(screen.queryByTestId('secrets-claude')).toBeNull();
    expect(screen.queryByTestId('secrets-telegram')).toBeNull();
    expect(screen.queryByTestId('secrets-freqtrade')).toBeNull();
    expect(screen.queryByRole('radio', { name: /api_key/ })).toBeNull();
  });

  it('says whether a key is added, without the variable name or the last four', async () => {
    render();
    const keys = await screen.findByTestId('exchange-keys');
    expect(within(keys).getByTestId('key-pair-a')).toHaveTextContent('added');
    // BINANCE_SECRET_A is absent from the fixture, so the pair is not complete.
    expect(within(keys).getByTestId('key-pair-b')).toHaveTextContent('not added yet');
    expect(keys.textContent).not.toMatch(/BINANCE_/);
    expect(keys.textContent).not.toMatch(/AB12/);
  });

  it('still reaches every credential, the auth mode and the probes under Advanced', async () => {
    render();
    await openAdvanced();
    expect(await screen.findByTestId('secrets-telegram')).toBeInTheDocument();
    expect(screen.getByTestId('secrets-claude')).toBeInTheDocument();
    expect(screen.getByTestId('secrets-exchange')).toBeInTheDocument();
    expect(screen.getByTestId('auth-mode-card')).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: /api_key/ })).toBeInTheDocument();
    expect(screen.getByTestId('claude-sources')).toBeInTheDocument();
  });
});
