/**
 * The pot, counted across every restart — three numbers and the run-by-run table.
 *
 * The 15-minute ledger this page used to lead with (`WalletPanel`) is the bot's view of
 * its *current* database, and it restarts from the seed whenever the bot gets a fresh one:
 * on 2026-09-23 23:45Z that hid a 69.77 loss from the first evening.  This card reads the
 * bot's databases themselves, so a restart is a row in the table below rather than a
 * number that quietly vanished.
 */
import { Badge, Card, Group, SimpleGrid, Stack, Table, Text, Tooltip } from '@mantine/core';

import { formatUsd, formatUtcStamp } from '@/lib/format';

import type { SleevePot } from '../api';

const signed = (v: number | null | undefined) =>
  v === null || v === undefined || !Number.isFinite(v) ? '—' : v >= 0 ? `+${formatUsd(v)}` : formatUsd(v);

function Figure({ label, value, help, tone }: {
  label: string;
  value: string;
  help: string;
  tone?: 'up' | 'down';
}) {
  return (
    <Stack gap={0}>
      <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
        {label}
      </Text>
      <Text size="lg" ff="monospace" c={tone === 'up' ? 'teal' : tone === 'down' ? 'red' : undefined}>
        {value}
      </Text>
      <Text size="xs" c="dimmed">
        {help}
      </Text>
    </Stack>
  );
}

const DEFINITIONS = {
  cumulative: 'Seed, plus every closed trade in every run database net of fees, plus open positions marked to market. Counted across every restart.',
  sinceRun: 'Closed trades, net of fees, since the bot started on its current database. This is the number that resets on a restart.',
  openMark: 'Profit or loss on the positions still open, at the newest price the system has.',
};

export function PotCard({ pot }: { pot: SleevePot | null | undefined }) {
  if (!pot) return null;
  const tone = (v: number | null) => (v === null ? undefined : v >= 0 ? 'up' : 'down');
  const gap = pot.ledger_gap_usdt;
  return (
    <Card withBorder padding="md" data-testid="pot-card">
      <Group justify="space-between" mb="sm" align="flex-start">
        <Stack gap={0}>
          <Text fw={600}>What the money put in is worth</Text>
          <Text size="xs" c="dimmed">
            Seed {formatUsd(pot.seed_usdt)}
            {pot.seed_source === 'run' ? ' (recorded at run start)' : ' (configured)'} ·{' '}
            {pot.runs.length === 0
              ? 'no run database yet'
              : pot.restarts === 0
                ? 'one run database, no restarts'
                : `${pot.runs.length} run databases, ${pot.restarts} restart${pot.restarts === 1 ? '' : 's'}`}
          </Text>
        </Stack>
        {pot.fully_priced ? null : (
          <Tooltip label={`Could not price: ${pot.unpriced.join(', ')}. The total leaves them out.`} withArrow>
            <Badge color="orange" variant="light">
              partly unpriced
            </Badge>
          </Tooltip>
        )}
      </Group>

      <SimpleGrid cols={{ base: 1, sm: 3 }} spacing="md" data-testid="pot-figures">
        <Figure
          label="worth now, all runs"
          value={pot.cumulative_net_usdt === null ? 'not set' : formatUsd(pot.cumulative_net_usdt)}
          help={`${signed(pot.gain_usdt)} against the seed. ${DEFINITIONS.cumulative}`}
          tone={tone(pot.gain_usdt)}
        />
        <Figure
          label="since the current run started"
          value={signed(pot.realised_current_run_usdt)}
          help={`${DEFINITIONS.sinceRun} Earlier runs: ${signed(pot.realised_earlier_runs_usdt)}.`}
          tone={tone(pot.realised_current_run_usdt)}
        />
        <Figure
          label="open positions, marked to market"
          value={signed(pot.open_mark_usdt)}
          help={`${DEFINITIONS.openMark} ${pot.open.length === 0 ? 'Nothing is open.' : `${pot.open.length} open.`}`}
          tone={tone(pot.open_mark_usdt)}
        />
      </SimpleGrid>

      {gap !== null && Math.abs(gap) >= 0.005 ? (
        <Text size="xs" c="dimmed" mt="sm" data-testid="pot-ledger-gap">
          The 15-minute ledger says {formatUsd(pot.ledger_nav_usdt)} — {signed(gap)} away from the
          figure above — because it restarts from the seed whenever the bot gets a fresh database.
        </Text>
      ) : null}

      {pot.runs.length > 0 ? (
        <Table mt="md" withTableBorder striped highlightOnHover data-testid="pot-runs">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Run</Table.Th>
              <Table.Th>Started (UTC)</Table.Th>
              <Table.Th>Ended (UTC)</Table.Th>
              <Table.Th ta="right">Trades</Table.Th>
              <Table.Th ta="right">Realised</Table.Th>
              <Table.Th ta="right">Fees</Table.Th>
              <Table.Th ta="right">Before fees</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {pot.runs.map((run, index) => (
              <Table.Tr key={run.run} data-testid={`pot-run-${run.run}`}>
                <Table.Td>
                  <Group gap={6} wrap="nowrap">
                    <Tooltip label={`${run.db} · ${run.strategy ?? 'no trades yet'}`} withArrow>
                      <Text size="xs">
                        run {index + 1} of {pot.runs.length}
                        {run.strategy ? ` · ${run.strategy}` : ''}
                      </Text>
                    </Tooltip>
                    {run.current ? (
                      <Badge size="xs" color="teal" variant="light">
                        current
                      </Badge>
                    ) : null}
                    {run.error ? (
                      <Badge size="xs" color="red" variant="light">
                        unreadable
                      </Badge>
                    ) : null}
                  </Group>
                </Table.Td>
                <Table.Td>
                  <Text size="xs">{run.started_utc ? formatUtcStamp(run.started_utc) : 'no trades yet'}</Text>
                </Table.Td>
                <Table.Td>
                  <Text size="xs">
                    {run.ended_utc ? formatUtcStamp(run.ended_utc) : run.current ? 'still running' : '—'}
                  </Text>
                </Table.Td>
                <Table.Td ta="right">
                  <Text size="xs" ff="monospace">
                    {run.closed_trades}
                    {run.open_trades ? ` (+${run.open_trades} open)` : ''}
                  </Text>
                </Table.Td>
                <Table.Td ta="right">
                  <Text size="xs" ff="monospace" c={run.realised_usdt >= 0 ? 'teal' : 'red'}>
                    {signed(run.realised_usdt)}
                  </Text>
                </Table.Td>
                <Table.Td ta="right">
                  <Text size="xs" ff="monospace">
                    {formatUsd(run.fees_usdt)}
                  </Text>
                </Table.Td>
                <Table.Td ta="right">
                  <Text size="xs" ff="monospace">
                    {signed(run.gross_usdt)}
                  </Text>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      ) : (
        <Text size="xs" c="dimmed" mt="sm">
          This bot has no run database yet, so the pot is the seed.
        </Text>
      )}
    </Card>
  );
}
