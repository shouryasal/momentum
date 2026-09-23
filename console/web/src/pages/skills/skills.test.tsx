import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ApiClient } from '@/api';
import { ApiProvider } from '@/app/ApiContext';
import { fakeFetch, renderWithProviders } from '@/test/utils';

import SkillsPage from './index';
import { NewSkillModal } from './components/NewSkillModal';

const SKILL = {
  name: 'post-mortem',
  status: 'bound',
  origin: 'human',
  bindings: ['review', 'daily_review'],
  policy: { body: 'gated', scripts: 'human', tests: 'gated' },
  has_tests: true,
  test_files: 1,
  has_evals: false,
  has_scripts: true,
  updated_at: null,
  last_change: null,
};

describe('NewSkillModal', () => {
  it('refuses a description shorter than the lint floor', async () => {
    const onCreate = vi.fn();
    renderWithProviders(
      <NewSkillModal opened onClose={vi.fn()} onCreate={onCreate} />,
    );
    await userEvent.type(screen.getByLabelText('Name'), 'trade-forensics');
    await userEvent.type(screen.getByLabelText('Description'), 'too short');
    expect(screen.getByRole('button', { name: /Create/ })).toBeDisabled();
    expect(onCreate).not.toHaveBeenCalled();
  });

  it('enables creation once the name and description pass', async () => {
    const onCreate = vi.fn();
    renderWithProviders(
      <NewSkillModal opened onClose={vi.fn()} onCreate={onCreate} />,
    );
    await userEvent.type(screen.getByLabelText('Name'), 'trade-forensics');
    await userEvent.type(
      screen.getByLabelText('Description'),
      'Reconstructs one trade end to end from the journal. Triggers on trade forensics.',
    );
    const button = screen.getByRole('button', { name: /Create/ });
    expect(button).toBeEnabled();
    await userEvent.click(button);
    expect(onCreate).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'trade-forensics' }),
    );
  });
});

describe('SkillsPage', () => {
  it('shows each skill with what loads it and who may edit which part', async () => {
    const client = new ApiClient({
      fetchImpl: fakeFetch({ '/api/skills': { body: { items: [SKILL] } } }),
    });
    renderWithProviders(
      <ApiProvider client={client}>
        <SkillsPage />
      </ApiProvider>,
    );
    await waitFor(() => expect(screen.getByText('post-mortem')).toBeInTheDocument());
    expect(screen.getByText('review')).toBeInTheDocument();
    expect(screen.getByText('gated/human/gated')).toBeInTheDocument();
  });
});

/**
 * The four buttons are jobs now: the POST returns a handle and the page follows it.
 * What matters is that the page never shows a verdict it did not get, and never sits on
 * "running" — a job always reaches a terminal status, including when it dies.
 */
describe('SkillsPage checks run as jobs', () => {
  const LINT_JOB = {
    id: 'job1',
    job: 'skills.lint:post-mortem',
    status: 'ok',
    progress: 1,
    message: 'lint ok',
    error: null,
    started_at: '2026-09-22T04:30:00Z',
    finished_at: '2026-09-22T04:30:01Z',
    actor: 'human:console',
    terminal: true,
    output: ['no findings'],
    dropped: 0,
    result: { name: 'post-mortem', ok: true, findings: [] },
    labels: { area: 'skills', kind: 'lint', skill: 'post-mortem' },
  };

  async function openSkill(routes: Parameters<typeof fakeFetch>[0]) {
    const client = new ApiClient({
      fetchImpl: fakeFetch({ '/api/skills': { body: { items: [SKILL] } }, ...routes }),
    });
    renderWithProviders(
      <ApiProvider client={client}>
        <SkillsPage />
      </ApiProvider>,
    );
    await waitFor(() => expect(screen.getByText('post-mortem')).toBeInTheDocument());
    await userEvent.click(screen.getByText('post-mortem'));
    return client;
  }

  it('starts a lint job, follows it and shows the findings only when it ends', async () => {
    await openSkill({
      '/api/skills/post-mortem/lint': {
        body: { job_id: 'job1', skill: 'post-mortem', kind: 'lint', status: 'queued', topic: 'job' },
      },
      '/api/skills/jobs/job1': { body: LINT_JOB },
    });
    await userEvent.click(screen.getByRole('button', { name: 'Lint' }));
    await waitFor(() => expect(screen.getByText('Lint clean')).toBeInTheDocument());
    expect(screen.getByTestId('check-progress')).toHaveTextContent('ok');
    expect(screen.getByTestId('check-output')).toHaveTextContent('no findings');
  });

  it('shows a job that died as failed, with its error', async () => {
    await openSkill({
      '/api/skills/post-mortem/test': {
        body: { job_id: 'job2', skill: 'post-mortem', kind: 'test', status: 'running', topic: 'job' },
      },
      '/api/skills/jobs/job2': {
        body: {
          ...LINT_JOB,
          id: 'job2',
          job: 'skills.test:post-mortem',
          status: 'failed',
          error: 'the job thread died without finishing',
          result: null,
          output: ['collecting ...'],
        },
      },
    });
    await userEvent.click(screen.getByRole('button', { name: 'Test' }));
    await waitFor(() =>
      expect(screen.getByText('the job thread died without finishing')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('check-progress')).toHaveTextContent('failed');
    // No verdict card: a failed job never produced one.
    expect(screen.queryByText('pytest passed')).not.toBeInTheDocument();
  });

  it('offers cancel only while a check is running, and says when it was stopped', async () => {
    const running = {
      ...LINT_JOB,
      id: 'job3',
      job: 'skills.eval:post-mortem',
      status: 'running',
      terminal: false,
      progress: 0.5,
      message: 'case 1/2',
      result: null,
    };
    let served = 0;
    const client = new ApiClient({
      fetchImpl: (async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const json = (body: unknown) =>
          new Response(JSON.stringify(body), {
            status: 200,
            headers: { 'content-type': 'application/json' },
          });
        if (url.includes('/api/skills/jobs/job3/cancel')) return json({ id: 'job3', cancelled: true, status: 'running' });
        if (url.includes('/api/skills/jobs/job3')) {
          served += 1;
          return json(served > 1 ? { ...running, status: 'cancelled', terminal: true } : running);
        }
        if (url.includes('/api/skills/post-mortem/eval')) {
          return json({ job_id: 'job3', skill: 'post-mortem', kind: 'eval', status: 'queued', topic: 'job' });
        }
        if (url.includes('/api/skills')) return json({ items: [SKILL] });
        void init;
        return json({});
      }) as unknown as typeof fetch,
    });
    renderWithProviders(
      <ApiProvider client={client}>
        <SkillsPage />
      </ApiProvider>,
    );
    await waitFor(() => expect(screen.getByText('post-mortem')).toBeInTheDocument());
    await userEvent.click(screen.getByText('post-mortem'));
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Eval' }));
    const cancel = await screen.findByRole('button', { name: 'Cancel' });
    await userEvent.click(cancel);
    await waitFor(() =>
      expect(screen.getByText(/Stopped before it finished/)).toBeInTheDocument(),
    );
  });
});
