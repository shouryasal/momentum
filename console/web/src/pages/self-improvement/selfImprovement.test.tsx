import { screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { ApiClient } from '@/api';
import { ApiProvider } from '@/app/ApiContext';
import { fakeFetch, renderWithProviders } from '@/test/utils';

import SelfImprovementPage from './index';
import { AutonomyMatrixCard } from './components/AutonomyMatrixCard';
import { EvidenceTable } from './components/EvidenceTable';
import type { AutonomyMatrix, EvidenceRow } from './api';

const MATRIX: AutonomyMatrix = {
  tier1_auto_merge: true,
  live_forces_human: true,
  max_auto_merges_per_week: 5,
  auto_revert: { enabled: true, window_days: 7, validity_drop_pct: 10, breach_increase: 1 },
  kinds: {
    params: { test: 'auto', live: 'approve' },
    skill_new: { test: 'approve', live: 'approve' },
    revert: { test: 'auto', live: 'auto' },
  },
  mode: 'live',
  effective: { params: 'approve', skill_new: 'approve', revert: 'auto' },
  invariants: ['a skill_new touching scripts/** is always held for a human'],
};

describe('EvidenceTable', () => {
  it('flags a claim that does not survive recomputation', () => {
    const rows: EvidenceRow[] = [
      { field: 'backtest.years', claimed: 2, verified: 2, mismatch: false, delta_pct: null },
      {
        field: 'walk_forward.out_sample_delta',
        claimed: 4.0,
        verified: 0.5,
        mismatch: true,
        delta_pct: 700,
      },
    ];
    renderWithProviders(<EvidenceTable rows={rows} />);
    expect(screen.getByText(/off by 700%/)).toBeInTheDocument();
    expect(screen.getByText('backtest.years')).toBeInTheDocument();
  });
});

describe('AutonomyMatrixCard', () => {
  it('shows the effective setting for the current mode, not just the stored one', () => {
    renderWithProviders(<AutonomyMatrixCard matrix={MATRIX} onSave={vi.fn()} />);
    expect(screen.getByText('effective mode: live')).toBeInTheDocument();
    // revert stays auto in live; params is downgraded to approve.
    expect(screen.getByText('Revert a merged change')).toBeInTheDocument();
  });

  it('keeps Save disabled until something actually changes', () => {
    renderWithProviders(<AutonomyMatrixCard matrix={MATRIX} onSave={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Save matrix' })).toBeDisabled();
  });
});

describe('SelfImprovementPage', () => {
  it('lists the queue with its statuses', async () => {
    const client = new ApiClient({
      fetchImpl: fakeFetch({
        '/api/changes': {
          body: {
            counts: { held: 1, auto_merged: 2, rejected: 0, reverted: 0 },
            items: [
              {
                change_id: '2026-09-27-vol-down',
                proposed_at: '2026-09-27T16:30:00Z',
                kind: 'params',
                op: 'edit',
                target: 'config/params-sleeve-a.json',
                status: 'held',
                author_model: 'claude-fable-5-1',
                author_run_id: 'review-2026-W39',
                decided_at: null,
                decided_by: null,
                reason: 'autonomy[params][live] = approve',
                merge_commit: null,
                branch: 'review/2026-W39',
                revert_of: null,
                reverted_by: null,
              },
            ],
          },
        },
        '/api/autonomy': { body: MATRIX },
        '/api/changes/timeline': { body: { items: [], recurring_causes: [] } },
      }),
    });

    renderWithProviders(
      <ApiProvider client={client}>
        <SelfImprovementPage />
      </ApiProvider>,
    );

    await waitFor(() => expect(screen.getByText('2026-09-27-vol-down')).toBeInTheDocument());
    expect(screen.getByText('autonomy[params][live] = approve')).toBeInTheDocument();
  });
});
