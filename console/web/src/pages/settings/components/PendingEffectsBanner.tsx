import { Alert, Badge, Button, Group, Stack, Text } from '@mantine/core';
import { IconAlertTriangle, IconPlayerPlay } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { errorMessage } from '@/api';

import { configApi, configKeys, type EffectsBanner } from '../api';

export interface PendingEffectsBannerProps {
  /** Pass the banner a page already has, to avoid a second fetch on first paint. */
  initial?: EffectsBanner;
}

/**
 * "Saved, but not yet in force."
 *
 * A save-only write leaves the file changed and the running system unchanged. That gap is
 * the most dangerous state the console can be in, so it is shown persistently until the
 * effects run — or, for `reset_required`, until the operator resets the test run.
 */
export function PendingEffectsBanner({ initial }: PendingEffectsBannerProps) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: configKeys.effects,
    queryFn: () => configApi.effects(),
    ...(initial
      ? { initialData: { catalogue: [], ...initial } }
      : {}),
    refetchInterval: 30_000,
  });

  const apply = useMutation({
    mutationFn: () => configApi.applyEffects(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['config'] });
    },
  });

  const banner = query.data;
  if (!banner || banner.count === 0) return null;

  return (
    <Alert
      color={banner.needs_reset ? 'orange' : 'yellow'}
      icon={<IconAlertTriangle size={18} />}
      title="Saved changes are not in force yet"
      data-testid="pending-effects"
    >
      <Stack gap="xs">
        <Group gap="xs" wrap="wrap">
          {banner.pending.map((item) => (
            <Badge
              key={item.effect}
              variant="light"
              color={item.auto_applicable ? 'yellow' : 'orange'}
              title={`${item.detail} (queued ${item.since_utc})`}
            >
              {item.title}
            </Badge>
          ))}
        </Group>
        {banner.needs_reset ? (
          <Text size="sm">
            A test-run reset is required for at least one change. Reset from the Test Lab —
            it archives the run's database, so it is never done for you.
          </Text>
        ) : null}
        {apply.isError ? (
          <Text size="sm" c="red">
            {errorMessage(apply.error)}
          </Text>
        ) : null}
        {banner.applicable.length > 0 ? (
          <Group>
            <Button
              size="xs"
              leftSection={<IconPlayerPlay size={14} />}
              loading={apply.isPending}
              onClick={() => apply.mutate()}
              data-testid="apply-pending-effects"
            >
              Apply now ({banner.applicable.length})
            </Button>
            <Text size="xs" c="dimmed">
              Runs under the ops lock: regenerate first, then restart what reads the files.
            </Text>
          </Group>
        ) : null}
        {apply.data ? (
          <Stack gap={2}>
            {apply.data.results.map((result) => (
              <Text key={result.effect} size="xs" c={result.status === 'applied' ? 'teal' : 'red'}>
                {result.title}: {result.status} — {result.detail}
              </Text>
            ))}
          </Stack>
        ) : null}
      </Stack>
    </Alert>
  );
}
