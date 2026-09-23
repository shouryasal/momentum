import { Alert, Badge, Card, Group, SimpleGrid, Stack, Text, Tooltip } from '@mantine/core';

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
export function WalletPanel({ wallet }: { wallet: Wallet | null | undefined }) {
  /**
   * A fresh install has no wallet row yet.
   *
   * Destructuring it unconditionally threw, and this component renders inside the Trading
   * screen — so "no money recorded yet" came out as a broken screen. Nothing the server
   * sends may be assumed to be there.
   */
  if (!wallet?.ledger || !wallet.exchange || !wallet.reconcile) {
    return (
      <Card withBorder padding="md">
        <Text fw={600}>The money</Text>
        <Text size="sm" c="dimmed">
          Nothing has been recorded for this bot yet, so there is no balance to compare
          against the exchange.
        </Text>
      </Card>
    );
  }
  const { ledger, exchange, reconcile } = wallet;
  return (
    <Card withBorder padding="md">
      <Group justify="space-between" mb="sm">
        <Text fw={600}>The money</Text>
        <Tooltip
          label={
            reconcile.mismatch
              ? "The system's own books and the exchange disagree by more than the allowed gap. New entries are blocked until they agree."
              : "The system's own books agree with what the exchange says you hold."
          }
          multiline
          w={300}
        >
          <Badge color={reconcile.mismatch ? 'red' : 'teal'} variant="light">
            {reconcile.mismatch ? 'books disagree with the exchange' : 'books agree with the exchange'}
          </Badge>
        </Tooltip>
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
