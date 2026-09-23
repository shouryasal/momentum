import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';

import { ApiClient } from '../api/client';
import { ApiProvider } from '../app/ApiContext';
import { CommandPalette, useShellCommands } from '../app/CommandPalette';
import { commandRegistry } from '../app/commandRegistry';
import { ROUTES } from '../routes';
import type { SearchResponse } from '../api/contracts';
import { fakeFetch, renderWithProviders } from './utils';

afterEach(() => commandRegistry.clear());

function hit(kind: string, id: string, title: string, route: string, subtitle = '') {
  return { kind, id, title, subtitle, route, group: kind, score: 50 };
}

/** Renders the palette with a scripted `/api/search`, optionally with the shell's nav commands. */
function renderPalette(response: SearchResponse, { shellCommands = true } = {}) {
  const client = new ApiClient({
    fetchImpl: fakeFetch({ '/api/search': { body: response } }),
  });
  const Harness = shellCommands ? WithShellCommands : BarePalette;
  return renderWithProviders(
    <ApiProvider client={client}>
      <Harness />
    </ApiProvider>,
  );
}

function BarePalette() {
  return <CommandPalette opened onClose={() => undefined} />;
}

function WithShellCommands() {
  useShellCommands();
  return <BarePalette />;
}

describe('CommandPalette', () => {
  it('shows a server page hit, which now carries a route the shell can navigate to', async () => {
    const user = userEvent.setup();
    // `search_service.PAGES` used to say `models`/`/models`; the registry says
    // `ai-models`/`/ai-models`, so this hit was dropped wholesale. Both halves agree now.
    const page = hit('page', 'ai-models', 'AI & Models', '/ai-models');
    renderPalette({ query: 'models', count: 1, results: [page] }, { shellCommands: false });

    await user.type(screen.getByTestId('palette-input'), 'models');
    const item = await screen.findByTestId('palette-item-hit:page:ai-models');
    expect(item).toHaveTextContent('AI & Models');
    expect(ROUTES.some((route) => route.path === page.route && route.id === page.id)).toBe(true);
  });

  it('does not list a page twice when the shell already offers it', async () => {
    const user = userEvent.setup();
    renderPalette({
      query: 'invariants',
      count: 1,
      results: [hit('page', 'invariants', 'Invariants', '/invariants')],
    });

    await user.type(screen.getByTestId('palette-input'), 'invariants');
    expect(await screen.findByTestId('palette-item-nav:invariants')).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByTestId('palette-item-hit:page:invariants')).toBeNull(),
    );
  });

  it('keeps showing config, run and skill hits', async () => {
    const user = userEvent.setup();
    renderPalette({
      query: 'max_weight',
      count: 2,
      results: [
        hit('config', 'earn:risk.max_weight.<key>', 'risk.max_weight.<key>',
            '/settings?file=earn&path=risk.max_weight.%3Ckey%3E', 'Per-asset weight ceiling'),
        hit('skill', 'risk-gate', 'risk-gate', '/skills?name=risk-gate'),
      ],
    });

    await user.type(screen.getByTestId('palette-input'), 'max_weight');
    expect(
      await screen.findByTestId('palette-item-hit:config:earn:risk.max_weight.<key>'),
    ).toBeInTheDocument();
    expect(screen.getByTestId('palette-item-hit:skill:risk-gate')).toBeInTheDocument();
  });

  it('drops a hit with no route rather than navigating nowhere', async () => {
    const user = userEvent.setup();
    renderPalette({
      query: 'orphan',
      count: 1,
      results: [hit('run', 'orphan-run', 'orphan run', '')],
    });

    await user.type(screen.getByTestId('palette-input'), 'orphan');
    await waitFor(() => expect(screen.queryByTestId('palette-item-hit:run:orphan-run')).toBeNull());
  });
});
