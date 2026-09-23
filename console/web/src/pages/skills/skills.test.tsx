import { fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { ApiClient } from '@/api';
import { ApiProvider } from '@/app/ApiContext';
import { fakeFetch, renderWithProviders } from '@/test/utils';

import SkillsPage, { skillHeadline } from './index';
import { NewSkillModal } from './components/NewSkillModal';
import { SkillEditor } from './components/SkillEditor';

const SKILL = {
  name: 'post-mortem',
  title: 'Grade the week',
  description:
    'Grades each trading decision on process and outcome separately, and keeps the lessons file.',
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

/** A description that clears the modal's 40-character lint floor. */
const DESCRIPTION =
  'Reconstructs one trade end to end from the journal. Triggers on trade forensics.';

/**
 * Set a controlled Mantine input in ONE event.
 *
 * `userEvent.type` dispatches a keystroke per character and every one of them re-renders
 * the whole modal, so the 79-character description cost ~1.5 s on an idle machine and blew
 * vitest's 5 s timeout whenever this file ran alongside the rest of the suite — a red test
 * that depended on how busy the box was, not on the code. Each field here reads only
 * `event.currentTarget.value`, so one change event is the same input with none of the wall
 * clock and none of the variance.
 */
function fill(field: HTMLElement, value: string): void {
  fireEvent.change(field, { target: { value } });
}

describe('NewSkillModal', () => {
  it('refuses a description shorter than the lint floor', () => {
    const onCreate = vi.fn();
    renderWithProviders(
      <NewSkillModal opened onClose={vi.fn()} onCreate={onCreate} />,
    );
    fill(screen.getByLabelText('Name'), 'trade-forensics');
    fill(screen.getByLabelText('Description'), 'too short');
    expect(screen.getByRole('button', { name: /Create/ })).toBeDisabled();
    expect(onCreate).not.toHaveBeenCalled();
  });

  it('enables creation once the name and description pass', async () => {
    const onCreate = vi.fn();
    renderWithProviders(
      <NewSkillModal opened onClose={vi.fn()} onCreate={onCreate} />,
    );
    fill(screen.getByLabelText('Name'), 'trade-forensics');
    fill(screen.getByLabelText('Description'), DESCRIPTION);
    const button = screen.getByRole('button', { name: /Create/ });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    // `POST /api/skills` is step-up guarded, so Create now opens the shared
    // confirm-and-step-up dialog instead of posting straight from the onClick.
    expect(onCreate).not.toHaveBeenCalled();
    fill(await screen.findByTestId('confirm-stepup'), 'console-token');
    fireEvent.click(screen.getByTestId('confirm-submit'));
    await waitFor(() =>
      expect(onCreate).toHaveBeenCalledWith(
        expect.objectContaining({ name: 'trade-forensics' }),
      ),
    );
  });
});

describe('SkillsPage', () => {
  /**
   * The list leads with what the procedure DOES, in its own words, because that is the
   * only question an operator has about something they did not write. Everything a
   * builder needs — who may edit which part, the tests, the origin, the files — moved
   * into the detail pane that opens on the row.
   */
  it('leads with what each procedure does, and what loads it', async () => {
    const client = new ApiClient({
      fetchImpl: fakeFetch({ '/api/skills': { body: { items: [SKILL] } } }),
    });
    renderWithProviders(
      <ApiProvider client={client}>
        <SkillsPage />
      </ApiProvider>,
    );
    await waitFor(() => expect(screen.getByText('Grade the week')).toBeInTheDocument());
    expect(screen.getByText(/Grades each trading decision/)).toBeInTheDocument();
    expect(screen.getByText('review')).toBeInTheDocument();
    // With no `title:`, the headline is the first sentence of what the skill says it does.
    expect(
      skillHeadline({ name: 'tca', description: 'Analyses execution quality. Triggers on tca.' }),
    ).toBe('Analyses execution quality.');
    expect(skillHeadline({ name: 'tca' })).toBe('tca');
    // The raw name is still on the row, as the secondary label.
    expect(screen.getByText('post-mortem')).toBeInTheDocument();
    expect(screen.queryByText('gated/human/gated')).not.toBeInTheDocument();
  });

  it('opens the whole procedure in the detail pane, and deep-links to it', async () => {
    const client = new ApiClient({
      fetchImpl: fakeFetch({ '/api/skills': { body: { items: [SKILL] } } }),
    });
    renderWithProviders(
      <ApiProvider client={client}>
        <SkillsPage />
      </ApiProvider>,
      { route: '/skills?detail=skill:post-mortem' },
    );
    expect(await screen.findByTestId('detail-pane')).toBeInTheDocument();
    expect(screen.getByTestId('detail-pane-title')).toHaveTextContent('Grade the week');
    expect(screen.getByTestId('detail-pane-raw-id')).toHaveTextContent('post-mortem');
    expect(await screen.findByText('gated/human/gated')).toBeInTheDocument();
  });

  it('closes the pane with the Escape key', async () => {
    const client = new ApiClient({
      fetchImpl: fakeFetch({ '/api/skills': { body: { items: [SKILL] } } }),
    });
    renderWithProviders(
      <ApiProvider client={client}>
        <SkillsPage />
      </ApiProvider>,
      { route: '/skills?detail=skill:post-mortem' },
    );
    await screen.findByTestId('detail-pane');
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByTestId('detail-pane')).not.toBeInTheDocument());
  });
});

/**
 * The tier-2 banner used to say `scripts/**` whatever file was open, so an operator
 * editing `tests/test_contract.py` — tier 2 since the gate started holding test changes —
 * was told the file they had in front of them was freely editable. The sentence has to
 * come from the tier the server sent.
 */
describe('SkillEditor names the tier-2 part the server reported', () => {
  function openFile(path: string, tier: 'tier1' | 'tier2') {
    const client = new ApiClient({
      fetchImpl: fakeFetch({
        '/api/skills/post-mortem/tree': {
          body: {
            skill: 'post-mortem',
            files: [{ path, size: 12, tier, editable: true, sha: 'abc' }],
          },
        },
        [`/api/skills/post-mortem/files/${path}`]: {
          body: {
            skill: 'post-mortem',
            path,
            content: '# hi\n',
            sha: 'abc',
            tier,
            requires_step_up: tier === 'tier2',
          },
        },
      }),
    });
    renderWithProviders(
      <ApiProvider client={client}>
        <SkillEditor name="post-mortem" />
      </ApiProvider>,
    );
  }

  it('says tests/** for a tests file, not scripts/**', async () => {
    openFile('tests/test_contract.py', 'tier2');
    const notice = await screen.findByTestId('tier2-notice');
    expect(notice).toHaveTextContent('tests/**');
    expect(notice).not.toHaveTextContent('scripts/**');
  });

  it('still says scripts/** for a scripts file', async () => {
    openFile('scripts/grade.py', 'tier2');
    expect(await screen.findByTestId('tier2-notice')).toHaveTextContent('scripts/**');
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
    await waitFor(() => expect(screen.getByText('Grade the week')).toBeInTheDocument());
    await userEvent.click(screen.getByText('Grade the week'));
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
    await waitFor(() => expect(screen.getByText('Grade the week')).toBeInTheDocument());
    await userEvent.click(screen.getByText('Grade the week'));
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Eval' }));
    const cancel = await screen.findByRole('button', { name: 'Cancel' });
    await userEvent.click(cancel);
    await waitFor(() =>
      expect(screen.getByText(/Stopped before it finished/)).toBeInTheDocument(),
    );
  });
});
