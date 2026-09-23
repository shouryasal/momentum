/**
 * The exchange keys, as two rows and a button.
 *
 * The owner's complaint about this page was *"too much content"*, and the exchange keys
 * were four rows of a fourteen-row table — key and secret, twice — each with a raw
 * variable name, a last-four, an updated stamp and a list of the jobs that receive it. A
 * key is one thing to a person: you either added it or you have not. So a pair is one
 * row, it says added or not added, and the button opens one dialog with both halves in it.
 *
 * What is deliberately NOT claimed here: that one slot is "the demo key" and the other is
 * "the live key". They are not. Each bot has one key slot, and whether that key points at
 * Binance's test network or at real money is set beside it (`BINANCE_VENUE_*`), not by
 * which row you typed into. Labelling a slot "live" that is actually pointed at the test
 * network — or worse, the reverse — is exactly the mistake that loses real money, so the
 * card says the rule in a sentence instead: while a bot is on play money, this is a test
 * network key, and you do not need a real-money key yet.
 */
import { Badge, Button, Card, Group, Stack, Text, Title } from '@mantine/core';
import { IconKey } from '@tabler/icons-react';

import { sleeveFullName } from '@/lib/plain';

import type { SecretRow } from '../api';

export interface KeyPair {
  id: string;
  /** What the operator is adding, in their words. */
  title: string;
  keyName: string;
  secretName: string;
}

/** One pair per bot: the key and the secret that go together. */
export const KEY_PAIRS: KeyPair[] = [
  {
    id: 'a',
    title: `Binance key for the ${sleeveFullName('a')}`,
    keyName: 'BINANCE_KEY_A',
    secretName: 'BINANCE_SECRET_A',
  },
  {
    id: 'b',
    title: `Binance key for the ${sleeveFullName('b')}`,
    keyName: 'BINANCE_KEY_B',
    secretName: 'BINANCE_SECRET_B',
  },
];

export interface ExchangeKeysCardProps {
  secrets: SecretRow[];
  onAdd: (pair: KeyPair) => void;
}

export function pairPresent(secrets: SecretRow[], pair: KeyPair): boolean {
  const key = secrets.find((row) => row.name === pair.keyName);
  const secret = secrets.find((row) => row.name === pair.secretName);
  return Boolean(key?.present && secret?.present);
}

export function ExchangeKeysCard({ secrets, onAdd }: ExchangeKeysCardProps) {
  return (
    <Card withBorder padding="md" radius="md" data-testid="exchange-keys">
      <Stack gap="sm">
        <Title order={5}>Exchange keys</Title>
        <Text size="sm" c="dimmed">
          While a bot is playing with play money, put a Binance <b>test network</b> key here —
          real order flow, fake funds. You do not need a real-money key yet, and only you can
          ever move a bot on to one.
        </Text>

        {KEY_PAIRS.map((pair) => {
          const present = pairPresent(secrets, pair);
          return (
            <Group key={pair.id} justify="space-between" wrap="nowrap" data-testid={`key-pair-${pair.id}`}>
              <Group gap="xs" wrap="nowrap">
                <Text size="sm">{pair.title}</Text>
                <Badge variant="light" color={present ? 'teal' : 'gray'}>
                  {present ? 'added' : 'not added yet'}
                </Badge>
              </Group>
              <Button
                size="compact-sm"
                variant={present ? 'subtle' : 'light'}
                leftSection={<IconKey size={14} />}
                onClick={() => onAdd(pair)}
              >
                {present ? 'Replace' : 'Add'}
              </Button>
            </Group>
          );
        })}
      </Stack>
    </Card>
  );
}

export default ExchangeKeysCard;
