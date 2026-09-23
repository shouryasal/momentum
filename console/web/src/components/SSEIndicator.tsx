import { ActionIcon, Group, Text, Tooltip } from '@mantine/core';
import { IconPlugConnected, IconPlugConnectedX, IconRefresh } from '@tabler/icons-react';

import type { StreamStatus } from '../api/sse';

const LABEL: Record<StreamStatus, string> = {
  idle: 'Event stream idle',
  connecting: 'Connecting to the event stream…',
  open: 'Live events connected',
  reconnecting: 'Event stream lost — reconnecting',
  closed: 'Event stream closed',
};

const COLOR: Record<StreamStatus, string> = {
  idle: 'gray',
  connecting: 'yellow',
  open: 'teal',
  reconnecting: 'orange',
  closed: 'red',
};

export interface SSEIndicatorProps {
  status: StreamStatus;
  attempt?: number;
  onRetry?: () => void;
  showLabel?: boolean;
}

/** Header dot for `GET /api/stream` (spec 12): colour = connection state, click = retry. */
export function SSEIndicator({ status, attempt = 0, onRetry, showLabel }: SSEIndicatorProps) {
  const label = attempt > 0 && status === 'reconnecting' ? `${LABEL[status]} (attempt ${attempt})` : LABEL[status];
  const connected = status === 'open';
  return (
    <Tooltip label={label}>
      <Group gap={6} wrap="nowrap">
        <ActionIcon
          variant="subtle"
          color={COLOR[status]}
          aria-label={label}
          data-testid="sse-indicator"
          data-status={status}
          onClick={onRetry}
        >
          {connected ? <IconPlugConnected size={18} /> : status === 'reconnecting' ? <IconRefresh size={18} /> : <IconPlugConnectedX size={18} />}
        </ActionIcon>
        {showLabel ? (
          <Text size="xs" c={COLOR[status]}>
            {status}
          </Text>
        ) : null}
      </Group>
    </Tooltip>
  );
}
