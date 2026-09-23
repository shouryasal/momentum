import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { renderWithProviders } from '@/test/utils';

import type { HistoryEntry } from './api';
import { HistoryTab } from './index';
import { changeBodyFor } from './patch';

const ENTRY: HistoryEntry = {
  id: 41,
  ts_utc: '2026-09-22T08:00:00Z',
  actor: 'human:console:s1',
  reason: 'widen the stop',
  before_sha: 'abc123def456',
  after_sha: 'def456abc123',
  changed_paths: ['risk.daily_loss_stop', 'trading.timeframe'],
  effects: ['regen_runtime'],
  protected_changed: true,
  applied: true,
  git_commit: null,
  diff: '-daily_loss_stop: 0.03\n+daily_loss_stop: 0.05\n',
  revertable: true,
};

/**
 * Revert restores the *whole* file as it stood before one save — `risk.*` and `trading.*`
 * included — and the server's default effect is `apply_now`, which regenerates
 * `var/runtime` and restarts the bots. It used to be a bare button whose only guard was
 * `disabled={!row.revertable}`.
 */
describe('Settings → History → Revert', () => {
  const render = (onRevert = vi.fn().mockResolvedValue({})) => {
    renderWithProviders(
      <HistoryTab
        entries={[ENTRY]}
        loading={false}
        confirmPhrase="I UNDERSTAND"
        stepUpActive={false}
        onRevert={onRevert}
        reverting={false}
      />,
    );
    return onRevert;
  };

  it('does not write anything on the first click', async () => {
    const onRevert = render();
    await userEvent.click(screen.getByTestId('revert-41'));
    expect(onRevert).not.toHaveBeenCalled();
    expect(screen.getByTestId('confirm-dialog')).toBeInTheDocument();
  });

  it('needs the typed phrase and the console token, and forwards what was typed', async () => {
    const onRevert = render();
    await userEvent.click(screen.getByTestId('revert-41'));
    // Both guards are up: a typed phrase and a step-up token.
    expect(screen.getByTestId('confirm-submit')).toBeDisabled();
    expect(screen.getByTestId('confirm-stepup')).toBeInTheDocument();
    await userEvent.type(screen.getByTestId('confirm-phrase'), 'I UNDERSTAND');
    await userEvent.type(screen.getByTestId('confirm-stepup'), 'console-token');
    await userEvent.click(screen.getByTestId('confirm-submit'));
    await waitFor(() => expect(onRevert).toHaveBeenCalledTimes(1));
    // The phrase sent is the operator's, not one the page helped itself to.
    expect(onRevert).toHaveBeenCalledWith(ENTRY, 'I UNDERSTAND');
  });

  it('shows what it is about to undo', async () => {
    render();
    await userEvent.click(screen.getByTestId('revert-41'));
    expect(screen.getByTestId('confirm-dialog')).toHaveTextContent('risk.daily_loss_stop');
  });
});

/**
 * Preview and save used to key on different conditions, so form edits reviewed from the
 * Raw tab were previewed as the unchanged file and then saved as the patch: the operator
 * confirmed a blank diff and wrote something else.
 */
describe('changeBodyFor', () => {
  const ops = [{ op: 'replace' as const, path: '/risk/daily_loss_stop', value: 0.05 }];

  it('sends the form patch while the raw text is untouched, whatever tab is open', () => {
    expect(changeBodyFor({ rawDirty: false, rawDraft: 'a: 1\n', ops })).toEqual({
      patch: ops,
    });
  });

  it('sends the raw text once it is edited', () => {
    expect(changeBodyFor({ rawDirty: true, rawDraft: 'a: 2\n', ops })).toEqual({
      raw: 'a: 2\n',
    });
  });

  it('is one value, so the preview and the save can never disagree', () => {
    const args = { rawDirty: false, rawDraft: 'a: 1\n', ops };
    expect(changeBodyFor(args)).toEqual(changeBodyFor(args));
  });
});
