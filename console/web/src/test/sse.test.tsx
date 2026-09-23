import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { StreamEvent } from '../api/contracts';
import { backoffDelay, EventStream, useEventStream } from '../api/sse';
import { FakeEventSource } from './utils';

beforeEach(() => {
  FakeEventSource.reset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('backoffDelay', () => {
  it('grows exponentially and stops at the cap', () => {
    const noJitter = { baseMs: 1000, maxMs: 30_000, factor: 2, jitter: 0 };
    expect(backoffDelay(0, noJitter, () => 0.5)).toBe(1000);
    expect(backoffDelay(1, noJitter, () => 0.5)).toBe(2000);
    expect(backoffDelay(3, noJitter, () => 0.5)).toBe(8000);
    expect(backoffDelay(10, noJitter, () => 0.5)).toBe(30_000);
  });

  it('applies jitter inside the requested band', () => {
    const options = { baseMs: 1000, maxMs: 30_000, factor: 2, jitter: 0.5 };
    expect(backoffDelay(0, options, () => 0)).toBe(750);
    expect(backoffDelay(0, options, () => 1)).toBe(1250);
  });
});

describe('EventStream', () => {
  it('subscribes to every requested topic and passes the topic list to the server', () => {
    const events: StreamEvent[] = [];
    const stream = new EventStream({
      topics: ['alert', 'kill'],
      onEvent: (event) => events.push(event),
      factory: (url) => new FakeEventSource(url),
    });
    stream.start();

    const source = FakeEventSource.last;
    expect(source?.url).toBe('/api/stream?topics=alert%2Ckill');

    source?.emit('open');
    expect(stream.getStatus()).toBe('open');

    source?.emit('alert', {
      topic: 'alert',
      id: '17',
      ts: '2026-09-22T04:30:00Z',
      payload: { severity: 'critical', title: 'Gate breach', message: 'max dd', source: 'gate', key: null },
    });

    expect(events).toHaveLength(1);
    expect(events[0]?.topic).toBe('alert');
    expect(stream.getLastEventId()).toBe('17');
    stream.close();
    expect(source?.closed).toBe(true);
  });

  it('wraps a bare payload frame and keeps the event topic', () => {
    const events: StreamEvent[] = [];
    const stream = new EventStream({
      topics: ['mode'],
      onEvent: (event) => events.push(event),
      factory: (url) => new FakeEventSource(url),
    });
    stream.start();
    FakeEventSource.last?.emit('mode', { sleeve: 'a', state: 'TEST' }, '42');

    expect(events[0]?.topic).toBe('mode');
    expect(events[0]?.id).toBe('42');
    expect(events[0]?.payload).toEqual({ sleeve: 'a', state: 'TEST' });
    stream.close();
  });

  it('ignores frames that are not JSON', () => {
    const events: StreamEvent[] = [];
    const stream = new EventStream({
      topics: ['alert'],
      onEvent: (event) => events.push(event),
      factory: (url) => new FakeEventSource(url),
    });
    stream.start();
    FakeEventSource.last?.emit('alert', ': keep-alive');
    expect(events).toHaveLength(0);
    stream.close();
  });

  it('reconnects with backoff and resumes from the last event id', () => {
    vi.useFakeTimers();
    const stream = new EventStream({
      topics: ['kill'],
      onEvent: () => undefined,
      factory: (url) => new FakeEventSource(url),
      backoff: { baseMs: 1000, maxMs: 8000, factor: 2, jitter: 0 },
      random: () => 0.5,
    });
    stream.start();
    const first = FakeEventSource.last;
    first?.emit('open');
    first?.emit('kill', { topic: 'kill', id: '99', ts: '2026-09-22T04:30:00Z', payload: {} });

    first?.emit('error');
    expect(stream.getStatus()).toBe('reconnecting');
    expect(FakeEventSource.instances).toHaveLength(1);

    vi.advanceTimersByTime(1000);
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.last?.url).toContain('last_event_id=99');

    // Second failure waits twice as long.
    FakeEventSource.last?.emit('error');
    vi.advanceTimersByTime(1999);
    expect(FakeEventSource.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeEventSource.instances).toHaveLength(3);

    stream.close();
    expect(stream.getStatus()).toBe('closed');
  });

  it('stops reconnecting once closed', () => {
    vi.useFakeTimers();
    const stream = new EventStream({
      topics: ['bot'],
      onEvent: () => undefined,
      factory: (url) => new FakeEventSource(url),
      backoff: { baseMs: 500, maxMs: 500, factor: 1, jitter: 0 },
    });
    stream.start();
    FakeEventSource.last?.emit('error');
    stream.close();
    vi.advanceTimersByTime(5000);
    expect(FakeEventSource.instances).toHaveLength(1);
  });
});

describe('useEventStream', () => {
  it('connects, delivers events and closes on unmount', async () => {
    const seen: StreamEvent[] = [];
    const { result, unmount } = renderHook(() =>
      useEventStream({
        topics: ['alert'],
        onEvent: (event) => seen.push(event),
        factory: (url) => new FakeEventSource(url),
      }),
    );

    await waitFor(() => expect(FakeEventSource.last).toBeDefined());
    act(() => {
      FakeEventSource.last?.emit('open');
      FakeEventSource.last?.emit('alert', {
        topic: 'alert',
        id: '1',
        ts: '2026-09-22T04:30:00Z',
        payload: { severity: 'info', title: 'hi', message: 'there', source: null, key: null },
      });
    });

    await waitFor(() => expect(result.current.status).toBe('open'));
    expect(seen).toHaveLength(1);
    expect(result.current.lastEventId).toBe('1');

    const source = FakeEventSource.last;
    unmount();
    expect(source?.closed).toBe(true);
  });

  it('stays idle when disabled', () => {
    renderHook(() =>
      useEventStream({
        topics: ['alert'],
        onEvent: () => undefined,
        enabled: false,
        factory: (url) => new FakeEventSource(url),
      }),
    );
    expect(FakeEventSource.instances).toHaveLength(0);
  });
});
