import { act, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { ApiClient } from '@/api';
import { ApiProvider } from '@/app/ApiContext';
import { fakeFetch, renderWithProviders } from '@/test/utils';

import PromptsPage from './index';

const V2 = {
  path: 'research.v2.md',
  version: 2,
  version_id: 'research.v2',
  sha: 'a'.repeat(64),
  bytes: 120,
  tokens: 40,
  snapshots: 12,
  immutable: true,
  active: false,
  placeholders: ['STATE'],
};

const V3 = { ...V2, path: 'research.v3.md', version: 3, version_id: 'research.v3', snapshots: 0, immutable: false, active: true };

function client() {
  return new ApiClient({
    fetchImpl: fakeFetch({
      '/api/prompts': {
        body: {
          families: [{ family: 'research', active: 'research.v3', versions: [V2, V3] }],
          active: { research: 'research.v3' },
        },
      },
      '/api/prompts/research.v3.md': {
        body: {
          path: 'research.v3.md',
          content: '# research v3\n\n{{STATE}}\n',
          sha: 'b'.repeat(64),
          version_id: 'research.v3',
          snapshots: 0,
          immutable: false,
          tokens: 40,
          placeholders: ['STATE'],
          snapshots_using: [],
        },
      },
      '/api/prompts/research.v2.md': {
        body: {
          path: 'research.v2.md',
          content: '# research v2\n\n{{STATE}}\n',
          sha: 'c'.repeat(64),
          version_id: 'research.v2',
          snapshots: 12,
          immutable: true,
          tokens: 40,
          placeholders: ['STATE'],
          snapshots_using: [],
        },
      },
    }),
  });
}

describe('PromptsPage', () => {
  it('lists a family with its versions and marks the active one', async () => {
    renderWithProviders(
      <ApiProvider client={client()}>
        <PromptsPage />
      </ApiProvider>,
    );
    await waitFor(() => expect(screen.getByText('research')).toBeInTheDocument());
    expect(screen.getByText('research.v2')).toBeInTheDocument();
    expect(screen.getByText('research.v3')).toBeInTheDocument();
    expect(screen.getByText('active')).toBeInTheDocument();
  });

  it('explains why a used version cannot be edited in place', async () => {
    renderWithProviders(
      <ApiProvider client={client()}>
        <PromptsPage />
      </ApiProvider>,
      { route: '/prompts' },
    );
    await waitFor(() => expect(screen.getByText('research.v2')).toBeInTheDocument());
    const link = screen.getByText('research.v2');
    // A bare .click() updates state outside React's batching; act() keeps the console clean.
    act(() => link.click());
    await waitFor(() =>
      expect(screen.getByText('This version is immutable')).toBeInTheDocument(),
    );
    expect(screen.getByLabelText('Save as the next version')).toBeDisabled();
  });
});
