import { act, render } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it } from 'vitest';

import {
  CommandRegistry,
  commandRegistry,
  usePageCommands,
  useRegisterCommands,
  type CommandContext,
} from '../app/commandRegistry';

const NOOP: CommandContext = { navigate: () => {}, close: () => {} };

afterEach(() => commandRegistry.clear());

describe('useRegisterCommands', () => {
  it('registers once when the entries only change identity, not content', () => {
    const registry = new CommandRegistry();
    let registrations = 0;
    registry.subscribe(() => {
      registrations += 1;
    });

    function Page({ n }: { n: number }) {
      // A fresh array on every render — what a page that builds its list inline produces.
      useRegisterCommands([{ id: 'x', title: 'Scan now', group: 'Signals', run: () => {} }], registry);
      return <span>{n}</span>;
    }

    const view = render(<Page n={1} />);
    const afterFirst = registrations;
    view.rerender(<Page n={2} />);
    view.rerender(<Page n={3} />);

    expect(registrations).toBe(afterFirst);
    expect(registry.list()).toHaveLength(1);
  });

  it('runs the latest closure, not the one captured at registration', () => {
    const registry = new CommandRegistry();

    function Page() {
      const [count, setCount] = useState(0);
      useRegisterCommands(
        [{ id: 'inc', title: 'Increment', group: 'Test', run: () => setCount(count + 1) }],
        registry,
      );
      return <span data-testid="count">{count}</span>;
    }

    const view = render(<Page />);
    act(() => registry.list()[0]?.run(NOOP));
    expect(view.getByTestId('count')).toHaveTextContent('1');
    act(() => registry.list()[0]?.run(NOOP));
    // A stale closure would set 1 again; the ref-backed one sees count === 1.
    expect(view.getByTestId('count')).toHaveTextContent('2');
  });

  it('re-registers when a title changes, so the palette shows the new wording', () => {
    const registry = new CommandRegistry();

    function Page({ title }: { title: string }) {
      useRegisterCommands([{ id: 'x', title, group: 'Test', run: () => {} }], registry);
      return null;
    }

    const view = render(<Page title="Lint" />);
    view.rerender(<Page title="Lint skill-smith" />);
    expect(registry.list()[0]?.title).toBe('Lint skill-smith');
  });

  it('unregisters when the page unmounts', () => {
    const registry = new CommandRegistry();
    function Page() {
      useRegisterCommands([{ id: 'x', title: 'X', group: 'Test', run: () => {} }], registry);
      return null;
    }
    const view = render(<Page />);
    expect(registry.list()).toHaveLength(1);
    view.unmount();
    expect(registry.list()).toHaveLength(0);
  });
});

describe('usePageCommands', () => {
  it('namespaces the ids and groups the entries under the page title', () => {
    function Page() {
      usePageCommands('signals', [{ id: 'scan-now', title: 'Scan now', run: () => {} }]);
      return null;
    }
    render(<Page />);
    const entry = commandRegistry.list().find((command) => command.id === 'page:signals:scan-now');
    expect(entry).toBeDefined();
    expect(entry?.group).toBe('Signals');
    expect(entry?.keywords).toContain('signals');
  });
});
