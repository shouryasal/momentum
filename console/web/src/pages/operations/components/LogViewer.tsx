import { Badge, Card, Code, Group, NumberInput, ScrollArea, Select, Stack, Switch, Text } from '@mantine/core';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api } from '@/api';
import { useTopicEvents } from '@/app/EventStreamContext';
import { EmptyState } from '@/components';
import { formatRelative } from '@/lib/format';

import { type LogFile, type LogTail } from '../api';

/** How much of a log the browser keeps. The server's window is its own, smaller, bound. */
export const MAX_CLIENT_LINES = 5000;

/**
 * `GET /api/logs/{name}` also names the topic that continues the tail and says whether a
 * follower was armed. Declared here rather than in `../api` so the shared `LogTail` stays
 * the shape of the file's contents.
 */
interface LogTailBody extends LogTail {
  topic?: string;
  following?: boolean;
}

/** Payload of one `log:<name>` event (`console/events.py: LogFollower.poll`). */
interface LogLines {
  name: string;
  lines: string[];
  offset: number;
  dropped: number;
  truncated?: boolean;
}

export interface LogFollow {
  lines: string[];
  /** Lines the *server* dropped to keep one event bounded. */
  dropped: number;
  /** The server confirmed a tail-follower is armed for this file. */
  following: boolean;
  error: string | null;
  /** Re-read the tail and re-arm the follower. */
  reload: () => void;
}

/**
 * Follow one log file: read its tail once, then stream what is appended.
 *
 * `GET /api/logs/{name}` returns the tail **and** starts a server-side tail-follower from
 * exactly where that tail ended, publishing on `log:<name>`. So the client fetches once
 * and subscribes; there is no polling interval to choose and no window where a line is
 * written twice or not at all.
 *
 * The `log:<name>` topic is parametric, so it is not in the shell's fixed `SSE_TOPICS`;
 * `useTopicEvents` adds it to the shell's *one* connection while this hook is mounted and
 * drops it again afterwards. No page opens an `EventSource` of its own.
 */
export function useLogFollow(
  name: string | null,
  { tail = 200, enabled = true, stream: wantStream = true }:
    { tail?: number; enabled?: boolean; stream?: boolean } = {},
): LogFollow {
  const [lines, setLines] = useState<string[]>([]);
  const [dropped, setDropped] = useState(0);
  const [following, setFollowing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  const streaming = Boolean(name) && enabled && wantStream;

  useEffect(() => {
    if (!name || !enabled) {
      setLines([]);
      setDropped(0);
      setFollowing(false);
      return undefined;
    }
    let live = true;
    void (async () => {
      try {
        // `follow` is what starts (or re-arms) the server-side follower. A snapshot asks
        // for none, so reading a log without watching it costs no thread.
        const body = await api.get<LogTailBody>(`/logs/${encodeURIComponent(name)}`, {
          tail,
          follow: wantStream,
        });
        if (!live) return;
        setLines(body.text ? body.text.split('\n') : []);
        setDropped(0);
        setFollowing(Boolean(body.following));
        setError(null);
      } catch (e) {
        if (live) {
          setError((e as Error).message);
          setFollowing(false);
        }
      }
    })();
    return () => {
      live = false;
    };
  }, [name, tail, enabled, wantStream, nonce]);

  const topics = useMemo(() => (streaming && name ? [`log:${name}`] : []), [streaming, name]);

  useTopicEvents(topics, (event) => {
    const payload = event.payload as unknown as LogLines;
    if (!payload || !Array.isArray(payload.lines)) return;
    if (payload.dropped) setDropped((current) => current + payload.dropped);
    setLines((current) => {
      const next = payload.truncated ? [...payload.lines] : [...current, ...payload.lines];
      return next.length > MAX_CLIENT_LINES ? next.slice(-MAX_CLIENT_LINES) : next;
    });
  });

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { lines, dropped, following, error, reload };
}

/**
 * Tail of a cron log.
 *
 * Every byte arrives already redacted — the server never sends a raw token, because a log
 * viewer that echoes one is a secret-exfiltration endpoint. Follow mode streams the new
 * lines over SSE instead of re-reading the whole tail on a timer: the same bytes, once
 * each, as they are written.
 */
export function LogViewer({ logs, loading }: { logs: LogFile[]; loading: boolean }) {
  const [name, setName] = useState<string | null>(null);
  const [tail, setTail] = useState<number>(200);
  const [follow, setFollow] = useState(false);
  const viewport = useRef<HTMLDivElement>(null);

  const selected = name ?? logs[0]?.name ?? null;
  const view = useLogFollow(selected, { tail, stream: follow });

  useEffect(() => {
    if (viewport.current) {
      viewport.current.scrollTop = viewport.current.scrollHeight;
    }
  }, [view.lines]);

  if (!loading && logs.length === 0) {
    return (
      <EmptyState
        title="No logs yet"
        description="Nothing has written to logs/ on this host. Cron creates them on first run."
      />
    );
  }

  return (
    <Stack gap="sm">
      <Group align="flex-end">
        <Select
          label="Log"
          data={logs.map((f) => ({ value: f.name, label: f.name }))}
          value={selected}
          onChange={setName}
          searchable
          w={260}
        />
        <NumberInput
          label="Lines"
          value={tail}
          onChange={(v) => setTail(typeof v === 'number' ? v : 200)}
          min={10}
          max={2000}
          step={100}
          w={120}
        />
        <Switch
          label="Follow"
          checked={follow}
          onChange={(e) => setFollow(e.currentTarget.checked)}
          mb={6}
        />
        <Badge variant="light" mb={8}>
          redacted
        </Badge>
        {follow ? (
          <Badge
            mb={8}
            variant="light"
            color={view.following ? 'teal' : 'yellow'}
            data-testid="log-stream-status"
          >
            {view.following ? 'streaming' : 'arming…'}
          </Badge>
        ) : null}
      </Group>

      {logs
        .filter((f) => f.name === selected)
        .map((f) => (
          <Text key={f.name} size="xs" c="dimmed">
            {(f.bytes / 1024).toFixed(1)} KiB · modified {formatRelative(f.modified_utc)}
          </Text>
        ))}

      {view.error ? <Text c="red">{view.error}</Text> : null}
      {view.dropped > 0 ? (
        <Text size="xs" c="yellow">
          {view.dropped} line(s) were dropped to keep the stream bounded — reload to see the
          current tail.
        </Text>
      ) : null}

      <Card withBorder padding={0} radius="md">
        <ScrollArea h={460} viewportRef={viewport}>
          <Code block style={{ whiteSpace: 'pre', fontSize: 12 }}>
            {view.lines.length > 0 ? view.lines.join('\n') : '(empty)'}
          </Code>
        </ScrollArea>
      </Card>
    </Stack>
  );
}

export default LogViewer;
