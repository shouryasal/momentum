/**
 * What it should earn, and what it is losing for reasons that are not the strategy.
 *
 * The owner asked to "keep checking what is likely profit we are getting and what are gaps
 * making us miss profit". The honest answer has two halves and this card keeps them apart:
 *
 *   A. **Likely profit is a declared expectation, not a measurement.** The active profile
 *      has a costed backtest of its own; days of realised results are noise against it, and
 *      the card says so in the same breath as the number, every time.
 *   B. **Three realised numbers** for the last 24 hours — made, paid in fees, and what
 *      simply holding BTC would have made — each with its definition on the card.
 *   D. **The top three gaps** as three plain sentences: what was lost, how much, and why.
 *
 * C, the ten gaps line by line with the query behind every number, is behind one button
 * and is rendered only while open, so the front of Home stays the money and nothing else.
 *
 * Placement. Home is five blocks by contract (`overview.test.tsx` fails on a sixth), and
 * this is about the money — what it should be earning and what it is leaking — so it sits
 * inside the money block as its own full-width row rather than as a sixth panel.
 *
 * The server writes every sentence. This file formats numbers and never invents a word the
 * ledger did not send, so the same sentence reads the same in the nightly markdown.
 */
import { Button, Card, Group, Loader, SegmentedControl, Stack, Table, Text } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';

import {
  api,
  errorMessage,
  type ProfitGapsGap,
  type ProfitGapsLedger,
  type ProfitGapsLine,
  type ProfitGapsResponse,
  type ProfitGapsWindowKey,
} from '@/api';
import { StatCard } from '@/components';

import { money, signedMoney } from './money';

export const profitGapsApi = {
  get: () => api.get<ProfitGapsResponse>('/profit-gaps'),
};

export const profitGapsKeys = {
  ledger: ['profit-gaps'] as const,
};

/** The window the front of the card reads; the details can switch. */
export const FRONT_WINDOW: ProfitGapsWindowKey = 'last_24h';

export const NOT_COMPUTED = 'The profit and gap ledger has not been computed yet.';
export const NOTHING_LOST =
  'Nothing in the last 24 hours was lost to a reason that is not the strategy.';

/** True when the server sent a ledger, not some other page's payload or an empty body. */
export function isProfitGapsResponse(value: unknown): value is ProfitGapsResponse {
  if (!value || typeof value !== 'object') return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.generated_utc === 'string' &&
    typeof v.windows === 'object' &&
    v.windows !== null &&
    typeof v.cached === 'boolean'
  );
}

function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}%`;
}

/** A: one sentence — the declared expectation, and that days of results cannot test it. */
export function expectationLine(ledger: ProfitGapsLedger): string {
  const e = ledger.expected;
  const profile = e.profile === 'shipped' ? 'the shipped configuration' : `the ${e.profile} profile`;
  return (
    `Expected: ${pct(e.expected_per_30d_pct)} per 30 days on ${profile}, ` +
    `planned worst drawdown ${pct(e.planned_max_drawdown_pct)}. ${e.note}`
  );
}

export interface RealisedTile {
  key: string;
  label: string;
  value: string;
  hint: string;
  positive: boolean | null;
}

/** B: the three numbers, each with the sentence the server defines it by. */
export function realisedTiles(ledger: ProfitGapsLedger): RealisedTile[] {
  const r = ledger.realised;
  const hours = Math.round(ledger.window.hours);
  const span = hours === 24 ? 'the last 24 hours' : `the last ${hours} hours`;
  const ratio = r.fee_gross_ratio === null ? null : Math.round(r.fee_gross_ratio * 100);
  const trades = `${r.trades} trade${r.trades === 1 ? '' : 's'}`;
  const won = r.win_rate === null ? '' : `, ${Math.round(r.win_rate * 100)}% won`;
  return [
    {
      key: 'realised_net_usdt',
      label: `Made in ${span}`,
      value: signedMoney(r.realised_net_usdt),
      hint: `${trades}${won}. ${r.definitions.realised_net_usdt ?? ''}`,
      positive: r.realised_net_usdt === null ? null : r.realised_net_usdt >= 0,
    },
    {
      key: 'fees_usdt',
      label: 'Paid in fees',
      value: money(r.fees_usdt),
      hint: `${ratio === null ? 'No gross profit to take a share of' : `${ratio}% of gross profit`}. ${
        r.definitions.fees_usdt ?? ''
      }`,
      positive: r.fees_usdt === null ? null : r.fees_usdt === 0,
    },
    {
      key: 'btc_hold_usdt',
      label: 'Holding BTC instead would have made',
      value:
        r.benchmark.btc_hold_usdt === null ? 'no price on record' : signedMoney(r.benchmark.btc_hold_usdt),
      hint: `${pct(r.benchmark.btc_hold_pct)} on the money put in. ${r.definitions.btc_hold_usdt ?? ''}`,
      positive: r.benchmark.btc_hold_usdt === null ? null : r.benchmark.btc_hold_usdt >= 0,
    },
  ];
}

/** The same three numbers since the test began, as one line. */
export function sinceStartLine(ledger: ProfitGapsLedger | undefined): string | null {
  if (!ledger) return null;
  const r = ledger.realised;
  const days = ledger.window.hours / 24;
  const span = days >= 2 ? `${days.toFixed(1)} days` : `${Math.round(ledger.window.hours)} hours`;
  const btc =
    r.benchmark.btc_hold_usdt === null
      ? 'no BTC price on record for the comparison'
      : `holding BTC would have made ${signedMoney(r.benchmark.btc_hold_usdt)}`;
  return (
    `Since the test began (${span}): made ${signedMoney(r.realised_net_usdt)} net on ${r.trades} ` +
    `trade${r.trades === 1 ? '' : 's'}, paid ${money(r.fees_usdt)} in fees, ${btc}.`
  );
}

/** D: the three sentences, or the one that says nothing was lost. */
export function topThree(ledger: ProfitGapsLedger): string[] {
  const sentences = ledger.top_three.map((t) => t.sentence).filter((s) => s.length > 0);
  return sentences.length > 0 ? sentences : [NOTHING_LOST];
}

export function lineValue(line: ProfitGapsLine): string {
  if (line.value === null || line.value === undefined) return 'n/a';
  const v = line.value;
  const text = Number.isInteger(v) ? v.toLocaleString('en-US') : v.toLocaleString('en-US', { maximumFractionDigits: 2 });
  return `${text} ${line.unit}`;
}

export function gapSize(gap: ProfitGapsGap): string {
  const v = gap.size;
  const text = Number.isInteger(v) ? v.toLocaleString('en-US') : v.toLocaleString('en-US', { maximumFractionDigits: 2 });
  return `${text} ${gap.unit}`;
}

/* ---------------------------------------------------------------------------- the card */

export function ProfitGapsCard() {
  const [open, setOpen] = useState(false);
  const [windowKey, setWindowKey] = useState<ProfitGapsWindowKey>(FRONT_WINDOW);
  const query = useQuery({
    queryKey: profitGapsKeys.ledger,
    queryFn: () => profitGapsApi.get(),
    refetchInterval: 300_000,
    retry: false,
  });

  const shell = (children: React.ReactNode) => (
    <Card
      withBorder
      padding="md"
      radius="md"
      data-testid="profit-gaps-card"
      style={{ flexBasis: '100%', maxWidth: '100%' }}
    >
      <Stack gap="xs">
        <Text fw={600}>What it should earn, and what it is losing</Text>
        {children}
      </Stack>
    </Card>
  );

  if (query.isLoading) {
    return shell(
      <Group justify="center" p="sm">
        <Loader size="sm" />
      </Group>,
    );
  }
  if (query.isError) {
    return shell(
      <Text size="sm" c="dimmed" data-testid="profit-gaps-unavailable">
        The profit and gap ledger could not be read: {errorMessage(query.error)}
      </Text>,
    );
  }
  const data = query.data;
  if (!isProfitGapsResponse(data)) {
    return shell(
      <Text size="sm" c="dimmed" data-testid="profit-gaps-empty">
        {NOT_COMPUTED}
      </Text>,
    );
  }
  if (data.error) {
    return shell(
      <Text size="sm" c="dimmed" data-testid="profit-gaps-unavailable">
        The profit and gap ledger could not be computed: {data.error}
      </Text>,
    );
  }
  const front = data.windows[FRONT_WINDOW];
  if (!front) {
    return shell(
      <Text size="sm" c="dimmed" data-testid="profit-gaps-empty">
        {NOT_COMPUTED}
      </Text>,
    );
  }
  const details = data.windows[windowKey] ?? front;
  const since = sinceStartLine(data.windows.since_start);

  return shell(
    <>
      {/* A: the declared expectation, and that days of results cannot test it. */}
      <Text size="sm" data-testid="profit-gaps-expected">
        {expectationLine(front)}
      </Text>

      {/* B: three numbers, each with its definition. */}
      <Group align="stretch" gap="md" grow wrap="wrap" data-testid="profit-gaps-realised">
        {realisedTiles(front).map((tile) => (
          <StatCard
            key={tile.key}
            label={tile.label}
            value={tile.value}
            hint={tile.hint}
            {...(tile.positive === null
              ? {}
              : { delta: { value: tile.positive ? 'up' : 'down', positive: tile.positive } })}
          />
        ))}
      </Group>
      {since ? (
        <Text size="xs" c="dimmed" data-testid="profit-gaps-since-start">
          {since}
        </Text>
      ) : null}

      {/* D: the top three, as sentences. */}
      <Stack gap={4} data-testid="profit-gaps-top-three">
        <Text size="sm" fw={600}>
          Where the money went that the strategy never chose
        </Text>
        {topThree(front).map((sentence, index) => (
          <Text key={sentence} size="sm">
            {index + 1}. {sentence}
          </Text>
        ))}
      </Stack>

      <Group justify="space-between">
        <Button size="xs" variant="subtle" onClick={() => setOpen((v) => !v)} data-testid="profit-gaps-toggle">
          {open ? 'Hide the details' : 'Show the details'}
        </Button>
        {open ? (
          <SegmentedControl
            size="xs"
            value={windowKey}
            onChange={(value) => setWindowKey(value as ProfitGapsWindowKey)}
            data={[
              { label: 'Last 24 hours', value: 'last_24h' },
              { label: 'Since the test began', value: 'since_start' },
            ]}
          />
        ) : null}
      </Group>

      {/* C: every gap with every line and the query behind it — rendered only while open. */}
      {open ? (
        <Stack gap="sm" data-testid="profit-gaps-details">
          <Text size="xs" c="dimmed">
            {details.window.since_utc} to {details.window.until_utc} ({details.window.hours.toFixed(1)} hours).
            Hover a line for the query that produced it.
          </Text>
          <Table striped highlightOnHover withTableBorder>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Gap</Table.Th>
                <Table.Th style={{ textAlign: 'right' }}>Size</Table.Th>
                <Table.Th style={{ textAlign: 'right' }}>Score</Table.Th>
                <Table.Th>Cause</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {details.gaps.map((gap) => (
                <Table.Tr key={gap.key} data-testid={`profit-gap-${gap.key}`}>
                  <Table.Td>
                    <Stack gap={2}>
                      <Text size="sm" fw={500}>
                        {gap.title}
                      </Text>
                      {gap.error ? (
                        <Text size="xs" c="red">
                          Could not be computed: {gap.error}
                        </Text>
                      ) : null}
                      {gap.lines.map((line) => (
                        <Text key={line.key} size="xs" c="dimmed" title={line.query} data-testid={`profit-gap-line-${gap.key}-${line.key}`}>
                          {line.label}: {lineValue(line)}
                          {line.note ? ` (${line.note})` : ''}
                        </Text>
                      ))}
                    </Stack>
                  </Table.Td>
                  <Table.Td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                    <Text size="sm">{gapSize(gap)}</Text>
                  </Table.Td>
                  <Table.Td style={{ textAlign: 'right' }}>
                    <Text size="sm" title={`severity ${Math.round(gap.severity)} of 100 within its scope, × weight ${gap.weight} for how much of the system it touches`}>
                      {Math.round(gap.score)}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size="xs">{gap.sentence || gap.cause}</Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
          {details.errors.length > 0 ? (
            <Text size="xs" c="dimmed">
              Notes: {details.errors.join('; ')}
            </Text>
          ) : null}
        </Stack>
      ) : null}
    </>,
  );
}
