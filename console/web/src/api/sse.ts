import { useEffect, useMemo, useRef, useState } from 'react';

import { API_BASE } from './client';
import type { StreamEvent } from './contracts';

export type StreamStatus = 'idle' | 'connecting' | 'open' | 'reconnecting' | 'closed';

/** The slice of `EventSource` the stream uses; tests inject a fake. */
export interface EventSourceLike {
  addEventListener(type: string, listener: (event: MessageEvent) => void): void;
  removeEventListener(type: string, listener: (event: MessageEvent) => void): void;
  close(): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;

export interface BackoffOptions {
  baseMs: number;
  maxMs: number;
  factor: number;
  /** 0..1 — fraction of the delay that is randomised, to avoid a thundering herd. */
  jitter: number;
}

export const DEFAULT_BACKOFF: BackoffOptions = {
  baseMs: 1_000,
  maxMs: 30_000,
  factor: 2,
  jitter: 0.2,
};

/** Exponential backoff with jitter; `attempt` is 0-based. */
export function backoffDelay(
  attempt: number,
  options: BackoffOptions = DEFAULT_BACKOFF,
  random: () => number = Math.random,
): number {
  const raw = options.baseMs * options.factor ** Math.max(0, attempt);
  const capped = Math.min(raw, options.maxMs);
  const spread = capped * options.jitter;
  return Math.round(capped - spread / 2 + random() * spread);
}

export interface EventStreamOptions {
  topics: readonly string[];
  onEvent: (event: StreamEvent) => void;
  onStatus?: (status: StreamStatus) => void;
  factory?: EventSourceFactory;
  backoff?: BackoffOptions;
  base?: string;
  random?: () => number;
}

function defaultFactory(url: string): EventSourceLike {
  return new EventSource(url, { withCredentials: true });
}

function parseEvent(raw: string, fallbackTopic: string, id: string | null): StreamEvent | null {
  let data: unknown;
  try {
    data = JSON.parse(raw) as unknown;
  } catch {
    return null;
  }
  if (!data || typeof data !== 'object') return null;
  const obj = data as Partial<StreamEvent> & Record<string, unknown>;
  return {
    topic: typeof obj.topic === 'string' ? obj.topic : fallbackTopic,
    id: typeof obj.id === 'string' ? obj.id : (id ?? ''),
    ts: typeof obj.ts === 'string' ? obj.ts : new Date().toISOString(),
    payload: ('payload' in obj ? (obj.payload ?? {}) : obj) as Record<string, unknown>,
  };
}

/**
 * `GET /api/stream?topics=...` with automatic reconnect (spec 5.3).
 *
 * The browser resends `Last-Event-ID` itself on its own reconnect; when we rebuild the
 * source after a hard failure we pass the last id we saw as a query parameter so the
 * server can resume from the same cursor.
 */
export class EventStream {
  private source: EventSourceLike | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  private closed = false;
  private lastEventId: string | null = null;
  private status: StreamStatus = 'idle';
  private readonly listeners: Array<[string, (event: MessageEvent) => void]> = [];

  constructor(private readonly options: EventStreamOptions) {}

  getStatus(): StreamStatus {
    return this.status;
  }

  getLastEventId(): string | null {
    return this.lastEventId;
  }

  getAttempt(): number {
    return this.attempt;
  }

  start(): void {
    this.closed = false;
    this.connect();
  }

  close(): void {
    this.closed = true;
    this.clearTimer();
    this.teardown();
    this.setStatus('closed');
  }

  /** Drop the current connection and reconnect immediately (used by the SSE indicator). */
  retryNow(): void {
    if (this.closed) return;
    this.clearTimer();
    this.teardown();
    this.attempt = 0;
    this.connect();
  }

  private url(): string {
    const base = this.options.base ?? API_BASE;
    const params = new URLSearchParams({ topics: this.options.topics.join(',') });
    if (this.lastEventId) params.set('last_event_id', this.lastEventId);
    return `${base}/stream?${params.toString()}`;
  }

  private setStatus(status: StreamStatus): void {
    if (this.status === status) return;
    this.status = status;
    this.options.onStatus?.(status);
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private teardown(): void {
    if (!this.source) return;
    for (const [type, listener] of this.listeners) {
      this.source.removeEventListener(type, listener);
    }
    this.listeners.length = 0;
    this.source.close();
    this.source = null;
  }

  private on(type: string, listener: (event: MessageEvent) => void): void {
    this.source?.addEventListener(type, listener);
    this.listeners.push([type, listener]);
  }

  private connect(): void {
    if (this.closed) return;
    this.setStatus(this.attempt === 0 ? 'connecting' : 'reconnecting');
    const factory = this.options.factory ?? defaultFactory;
    this.source = factory(this.url());

    this.on('open', () => {
      this.attempt = 0;
      this.setStatus('open');
    });
    this.on('error', () => {
      this.scheduleReconnect();
    });

    const handle = (topic: string) => (event: MessageEvent) => {
      const parsed = parseEvent(String(event.data ?? ''), topic, event.lastEventId || null);
      if (!parsed) return;
      if (parsed.id) this.lastEventId = parsed.id;
      this.options.onEvent(parsed);
    };
    // Servers may publish either named events (one per topic) or default `message` frames.
    this.on('message', handle('message'));
    for (const topic of this.options.topics) this.on(topic, handle(topic));
  }

  private scheduleReconnect(): void {
    if (this.closed || this.timer !== null) return;
    this.teardown();
    this.setStatus('reconnecting');
    const delay = backoffDelay(this.attempt, this.options.backoff ?? DEFAULT_BACKOFF, this.options.random ?? Math.random);
    this.attempt += 1;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.connect();
    }, delay);
  }
}

export interface UseEventStreamResult {
  status: StreamStatus;
  lastEventId: string | null;
  attempt: number;
  retryNow: () => void;
}

export interface UseEventStreamOptions {
  topics: readonly string[];
  onEvent: (event: StreamEvent) => void;
  enabled?: boolean;
  factory?: EventSourceFactory;
  backoff?: BackoffOptions;
}

/** React binding for {@link EventStream}; the handler may change without reconnecting. */
export function useEventStream(options: UseEventStreamOptions): UseEventStreamResult {
  const { topics, onEvent, enabled = true, factory, backoff } = options;
  const [status, setStatus] = useState<StreamStatus>('idle');
  const [attempt, setAttempt] = useState(0);
  const [lastEventId, setLastEventId] = useState<string | null>(null);
  const handlerRef = useRef(onEvent);
  const streamRef = useRef<EventStream | null>(null);
  // Held in refs so an inline `factory`/`backoff`/`onEvent` never forces a reconnect.
  const factoryRef = useRef(factory);
  const backoffRef = useRef(backoff);
  handlerRef.current = onEvent;
  factoryRef.current = factory;
  backoffRef.current = backoff;

  const topicKey = useMemo(() => [...topics].join(','), [topics]);

  useEffect(() => {
    if (!enabled) {
      setStatus('idle');
      return;
    }
    const stream = new EventStream({
      topics: topicKey.split(',').filter(Boolean),
      onEvent: (event) => {
        setLastEventId(event.id || null);
        handlerRef.current(event);
      },
      onStatus: (next) => {
        setStatus(next);
        setAttempt(stream.getAttempt());
      },
      ...(factoryRef.current ? { factory: factoryRef.current } : {}),
      ...(backoffRef.current ? { backoff: backoffRef.current } : {}),
    });
    streamRef.current = stream;
    stream.start();
    return () => {
      stream.close();
      streamRef.current = null;
    };
  }, [topicKey, enabled]);

  return {
    status,
    lastEventId,
    attempt,
    retryNow: () => streamRef.current?.retryNow(),
  };
}
