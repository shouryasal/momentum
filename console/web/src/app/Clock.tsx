import { Stack, Text, Tooltip } from '@mantine/core';
import { useEffect, useState } from 'react';

import { formatGulfClock, formatUtcClock } from '../lib/format';

/** Gulf (Asia/Dubai, UTC+4, no DST) over UTC — schedules are quoted in Gulf time. */
export function Clock({ now }: { now?: Date }) {
  const [tick, setTick] = useState(() => now ?? new Date());
  useEffect(() => {
    if (now) return;
    const timer = setInterval(() => setTick(new Date()), 1000);
    return () => clearInterval(timer);
  }, [now]);
  const current = now ?? tick;
  return (
    <Tooltip label="Gulf time (Asia/Dubai) over UTC">
      <Stack gap={0} data-testid="clock">
        <Text size="xs" fw={600} ff="monospace" lh={1.1}>
          {formatGulfClock(current)} +04
        </Text>
        <Text size="xs" c="dimmed" ff="monospace" lh={1.1}>
          {formatUtcClock(current)} Z
        </Text>
      </Stack>
    </Tooltip>
  );
}
