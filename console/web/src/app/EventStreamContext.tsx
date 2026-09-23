import { useQueryClient } from '@tanstack/react-query';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';

import { SSE_TOPICS, type AlertPayload, type StreamEvent } from '../api/contracts';
import { shellKeys } from '../api/queryClient';
import { useEventStream, type EventSourceFactory, type StreamStatus } from '../api/sse';

export interface AlertEntry {
  id: string;
  ts: string;
  payload: AlertPayload;
  read: boolean;
}

/** What a page gets from {@link useTopicEvents}; identity never changes. */
export interface EventSubscription {
  subscribe: (topics: readonly string[], handler: (event: StreamEvent) => void) => () => void;
}

export interface EventStreamState extends EventSubscription {
  status: StreamStatus;
  attempt: number;
  retryNow: () => void;
  alerts: AlertEntry[];
  unread: number;
  markAllRead: () => void;
  dismiss: (id: string) => void;
  /** Topics beyond {@link SSE_TOPICS} the one connection is currently carrying. */
  extraTopics: readonly string[];
}

const EventStreamContext = createContext<EventStreamState | null>(null);
/**
 * Subscription is a second context on purpose: its value is created once and never
 * replaced, so a page's `useTopicEvents` effect re-runs only when *its* topics change —
 * not every time an alert arrives and the provider re-renders.
 */
const SubscribeContext = createContext<EventSubscription | null>(null);

/** Refetch triggers: an event carries ids only, the client refetches detail (spec 5.3). */
const INVALIDATE: Record<string, readonly (readonly unknown[])[]> = {
  kill: [shellKeys.meta, shellKeys.safetyStrip],
  mode: [shellKeys.meta, shellKeys.safetyStrip],
  transition: [shellKeys.meta, shellKeys.safetyStrip],
  config: [shellKeys.meta, shellKeys.safetyStrip],
  health: [shellKeys.health],
  approval: [shellKeys.approvals],
  proposal: [shellKeys.approvals],
  change: [shellKeys.approvals],
  provider_switch: [shellKeys.providers],
};

const MAX_ALERTS = 200;

export function EventStreamProvider({
  children,
  enabled = true,
  topics = SSE_TOPICS,
  factory,
}: {
  children: ReactNode;
  enabled?: boolean;
  topics?: readonly string[];
  factory?: EventSourceFactory;
}) {
  const queryClient = useQueryClient();
  const [alerts, setAlerts] = useState<AlertEntry[]>([]);
  const subscribers = useRef(new Set<{ topics: readonly string[]; handler: (event: StreamEvent) => void }>());
  /**
   * Parametric topics — today only `log:<name>` — are not in the fixed vocabulary, so the
   * stream cannot ask for them up front. A page that wants one says so by subscribing;
   * the connection is rebuilt with the wider set and drops it again when the last page
   * that wanted it goes away. Reference-counted, because two tabs may watch one log.
   */
  const extraCounts = useRef(new Map<string, number>());
  const [extraTopics, setExtraTopics] = useState<readonly string[]>([]);
  const baseKey = useMemo(() => [...topics].join(','), [topics]);
  const base = useMemo(() => new Set(topics), [baseKey]); // eslint-disable-line react-hooks/exhaustive-deps
  const baseRef = useRef(base);
  baseRef.current = base;

  const syncExtras = useCallback(() => {
    setExtraTopics((current) => {
      const next = [...extraCounts.current.keys()].sort();
      // Same set ⇒ same array: a new identity here would reconnect the stream for nothing.
      return next.join(',') === [...current].join(',') ? current : next;
    });
  }, []);

  const onEvent = useCallback(
    (event: StreamEvent) => {
      for (const key of INVALIDATE[event.topic] ?? []) {
        void queryClient.invalidateQueries({ queryKey: key });
      }
      if (event.topic === 'alert') {
        const payload = event.payload as unknown as AlertPayload;
        setAlerts((current) =>
          [{ id: event.id || `${event.ts}-${current.length}`, ts: event.ts, payload, read: false }, ...current].slice(
            0,
            MAX_ALERTS,
          ),
        );
      }
      for (const subscriber of subscribers.current) {
        if (subscriber.topics.length === 0 || subscriber.topics.includes(event.topic)) {
          subscriber.handler(event);
        }
      }
    },
    [queryClient],
  );

  const allTopics = useMemo(
    () => (extraTopics.length === 0 ? topics : [...topics, ...extraTopics]),
    [topics, extraTopics],
  );

  const stream = useEventStream({
    topics: allTopics,
    onEvent,
    enabled,
    ...(factory ? { factory } : {}),
  });

  // Created once: see SubscribeContext above.
  const subscription = useRef<EventSubscription | null>(null);
  if (subscription.current === null) {
    subscription.current = {
      subscribe: (wanted: readonly string[], handler: (event: StreamEvent) => void) => {
        const entry = { topics: wanted, handler };
        subscribers.current.add(entry);
        const added = wanted.filter((topic) => !baseRef.current.has(topic));
        for (const topic of added) {
          extraCounts.current.set(topic, (extraCounts.current.get(topic) ?? 0) + 1);
        }
        if (added.length) syncExtras();
        return () => {
          subscribers.current.delete(entry);
          for (const topic of added) {
            const left = (extraCounts.current.get(topic) ?? 1) - 1;
            if (left <= 0) extraCounts.current.delete(topic);
            else extraCounts.current.set(topic, left);
          }
          if (added.length) syncExtras();
        };
      },
    };
  }
  const subscribe = subscription.current.subscribe;

  const value: EventStreamState = useMemo(
    () => ({
      status: stream.status,
      attempt: stream.attempt,
      retryNow: stream.retryNow,
      alerts,
      unread: alerts.filter((alert) => !alert.read).length,
      markAllRead: () => setAlerts((current) => current.map((alert) => ({ ...alert, read: true }))),
      dismiss: (id: string) => setAlerts((current) => current.filter((alert) => alert.id !== id)),
      subscribe,
      extraTopics,
    }),
    [stream.status, stream.attempt, stream.retryNow, alerts, subscribe, extraTopics],
  );

  return (
    <SubscribeContext.Provider value={subscription.current}>
      <EventStreamContext.Provider value={value}>{children}</EventStreamContext.Provider>
    </SubscribeContext.Provider>
  );
}

export function useEvents(): EventStreamState {
  const context = useContext(EventStreamContext);
  if (!context) throw new Error('useEvents must be used inside <EventStreamProvider>');
  return context;
}

/**
 * Subscribe a page to SSE topics on the shell's single connection.
 *
 * Pages must not call `useEventStream` themselves: each call opens its own `EventSource`,
 * and the browser caps concurrent connections per origin — three pages doing that is three
 * streams the backend has to fan out to.  Outside the shell (a page rendered directly in a
 * unit test) there is no stream to share and this is a no-op.
 *
 * A topic outside `SSE_TOPICS` — a `log:<name>` tail-follower — is fine: the provider adds
 * it to the one connection while somebody wants it, so a page still never opens its own.
 */
export function useTopicEvents(
  topics: readonly string[],
  handler: (event: StreamEvent) => void,
): void {
  const subscription = useContext(SubscribeContext);
  const latest = useRef(handler);
  latest.current = handler;
  const key = [...topics].join(',');

  useEffect(() => {
    if (!subscription) return;
    return subscription.subscribe(
      key.split(',').filter(Boolean),
      (event) => latest.current(event),
    );
  }, [subscription, key]);
}
