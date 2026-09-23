import { Alert, Badge, Card, Group, SimpleGrid, Stack, Text } from '@mantine/core';

import type { Wallet } from '../api';

const usdt = (v: number) => (Number.isFinite(v) ? `${v.toFixed(2)} USDT` : '—');

function Figure({ label, value, help }: { label: string; value: string; help?: string }) {
  return (
    <Stack gap={0}>
      <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
        {label}
      </Text>
      <Text size="lg" ff="monospace">
        {value}
      </Text>
      {help ? (
        <Text size="xs" c="dimmed">
          {help}
        </Text>
      ) : null}
    </Stack>
  );
}

/**
 * Ledger view beside exchange view.
 *
 * They are deliberately separate numbers: the ledger is what this bot believes it owns
 * (starting balance + realised P&L − staked + reserved + positions); the exchange is the
 * whole account. A gap beyond the tolerance is the reconciliation mismatch that blocks
 * entries — which is the only reason to show both.
 */
export function WalletPanel({ wallet }: { wallet: Wallet }) {
  const { ledger, exchange, reconcile } = wallet;
  return (
    <Card withBorder padding="md">
      <Group justify="space-between" mb="sm">
        <Text fw={600}>Wallet</Text>
        <Badge color={reconcile.mismatch ? 'red' : 'teal'} variant="light">
          {reconcile.mismatch ? 'reconcile mismatch' : 'reconciled'}
        </Badge>
      </Group>
      <SimpleGrid cols={{ base: 2, sm: 3 }} spacing="md">
        <Figure label="ledger NAV" value={usdt(ledger.nav)} help="what the gate enforces" />
        <Figure label="ledger cash" value={usdt(ledger.cash)} />
        <Figure label="reserved" value={usdt(ledger.reserved)}
          help="USDT in resting entry orders" />
        <Figure label="positions" value={usdt(ledger.positions)} />
        <Figure label="free USDT" value={usdt(ledger.free_usdt)} />
        <Figure label="exchange total" value={usdt(exchange.total)} help="whole account" />
      </SimpleGrid>
      {reconcile.mismatch ? (
        <Alert mt="md" color="red" title="Ledger and exchange disagree">
          Difference {usdt(reconcile.delta_usdt)} exceeds the tolerance{' '}
          {usdt(reconcile.tolerance_usdt)} (dust {usdt(reconcile.dust_usdt)}).
          {reconcile.block_on_mismatch
            ? ' Entries are blocked until this is resolved.'
            : ' Entries are NOT blocked — risk.reconcile.block_on_mismatch is off.'}
        </Alert>
      ) : (
        <Text size="xs" c="dimmed" mt="sm">
          Difference {usdt(reconcile.delta_usdt)}, within {usdt(reconcile.tolerance_usdt)}.
        </Text>
      )}
    </Card>
  );
}
