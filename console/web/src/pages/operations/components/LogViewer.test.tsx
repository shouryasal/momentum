import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { EventStreamProvider } from '@/app/EventStreamContext';
import { FakeEventSource, fakeFetch, renderWithProviders } from '@/test/utils';

import { LogViewer } from './LogViewer';

const LOGS = [{ name: 'research.log', bytes: 2048, modified_utc: '2026-09-22T04:30:00Z' }];

const TAIL = {
  name: 'research.log',
  bytes: 2048,
  lines: 2,
  text: 'already here\nsecond line',
  topic: 'log:research.log',
  following: true,
};

function renderViewer() {
  return renderWithProviders(
    <EventStreamProvider factory={(url) => new FakeEventSource(url)}>
      <LogViewer logs={LOGS} loading={false} />
    </EventStreamProvider>,
  );
}

function emit(payload: Record<string, unknown>) {
  const source = FakeEventSource.last;
  if (!source) throw new Error('no stream was opened');
  act(() => {
    source.emit('log:research.log', {
      topic: 'log:research.log',
      id: '7',
      ts: '2026-09-22T04:31:00Z',
      payload,
    });
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  FakeEventSource.reset();
});

describe('LogViewer', () => {
  it('shows the tail without following until asked', async () => {
    vi.stubGlobal('fetch', fakeFetch({ '/api/logs/research.log': { body: TAIL } }));
    renderViewer();
    await waitFor(() => expect(screen.getByText(/already here/)).toBeInTheDocument());
    expect(screen.queryByTestId('log-stream-status')).not.toBeInTheDocument();
  });

  it('follows on the shell stream and appends the lines the server sends', async () => {
    vi.stubGlobal('fetch', fakeFetch({ '/api/logs/research.log': { body: TAIL } }));
    renderViewer();
    await waitFor(() => expect(screen.getByText(/already here/)).toBeInTheDocument());

    await userEvent.click(screen.getByLabelText('Follow'));
    await waitFor(() =>
      expect(screen.getByTestId('log-stream-status')).toHaveTextContent('streaming'),
    );
    // The parametric topic joined the shell's one connection: no page-owned EventSource.
    await waitFor(() =>
      expect(decodeURIComponent(FakeEventSource.last?.url ?? '')).toContain('log:research.log'),
    );

    emit({ name: 'research.log', lines: ['a streamed line'], offset: 2100, dropped: 0 });
    await waitFor(() => expect(screen.getByText(/a streamed line/)).toBeInTheDocument());
    // Appended, not replaced: the tail the client already had is still there.
    expect(screen.getByText(/already here/)).toBeInTheDocument();
  });

  it('replaces the view when the server says the file was truncated', async () => {
    vi.stubGlobal('fetch', fakeFetch({ '/api/logs/research.log': { body: TAIL } }));
    renderViewer();
    await userEvent.click(screen.getByLabelText('Follow'));
    await waitFor(() =>
      expect(decodeURIComponent(FakeEventSource.last?.url ?? '')).toContain('log:research.log'),
    );

    emit({ name: 'research.log', lines: ['fresh'], offset: 6, dropped: 0, truncated: true });
    await waitFor(() => expect(screen.getByText(/fresh/)).toBeInTheDocument());
    expect(screen.queryByText(/already here/)).not.toBeInTheDocument();
  });

  it('says so when the server dropped lines to stay bounded', async () => {
    vi.stubGlobal('fetch', fakeFetch({ '/api/logs/research.log': { body: TAIL } }));
    renderViewer();
    await userEvent.click(screen.getByLabelText('Follow'));
    await waitFor(() =>
      expect(decodeURIComponent(FakeEventSource.last?.url ?? '')).toContain('log:research.log'),
    );

    emit({ name: 'research.log', lines: ['tail end'], offset: 9000, dropped: 42 });
    await waitFor(() => expect(screen.getByText(/42 line\(s\) were dropped/)).toBeInTheDocument());
  });

  it('shows the refusal when the log cannot be read', async () => {
    vi.stubGlobal('fetch', fakeFetch({}));
    renderViewer();
    await waitFor(() => expect(screen.getByText(/research.log/)).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText(/\(empty\)/)).toBeInTheDocument());
  });
});
