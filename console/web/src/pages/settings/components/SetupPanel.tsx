/**
 * Start here — the handful of decisions a demo run on the testnet actually needs.
 *
 * The generated settings form is every key in `config/earn.yaml` and `config/models.yaml`,
 * which is the right shape for whoever built the system and the wrong shape for someone
 * about to paper-trade for the first time.  This panel is the short version: sign in,
 * choose play money or real money, and set the money, the limits and the schedule for this
 * run.  Nothing is hidden — each card says which settings it covers and opens the full
 * form at exactly that section, and the sign-in page is the same page it has always been.
 */
import { Anchor, Badge, Card, Group, SimpleGrid, Stack, Text } from '@mantine/core';
import {
  IconAdjustmentsHorizontal,
  IconCalendarClock,
  IconKey,
  IconRobot,
  IconShieldCheck,
  IconToggleRight,
} from '@tabler/icons-react';
import type { ComponentType } from 'react';
import { Link } from 'react-router-dom';

export interface SetupCard {
  id: string;
  title: string;
  /** What this decides, in plain words. Required — a card with no sentence is the bug. */
  blurb: string;
  icon: ComponentType<{ size?: number | string }>;
  /** Either a route to send the operator to… */
  to?: string;
  /** …or a section of the generated settings form to open. */
  section?: string;
  /** What the settings form calls it, for anyone who wants to look it up. */
  covers: string;
}

export const SETUP_CARDS: SetupCard[] = [
  {
    id: 'sign-in',
    title: 'Sign in to Claude',
    blurb:
      'Let the system talk to Claude. Done here, in the browser — you never need a terminal for it.',
    icon: IconKey,
    to: '/secrets',
    covers: 'Claude sign-in, exchange keys, and a check that each one works',
  },
  {
    id: 'mode',
    title: 'Play money or real money',
    blurb:
      'Which bot is trading with real funds, and which is only pretending. Both start on play money, and only you can change that.',
    icon: IconToggleRight,
    to: '/mode',
    covers: 'the signed mode file, the pre-flight checks and the starting money',
  },
  {
    id: 'limits',
    title: 'How much it may risk',
    blurb:
      'The most it may hold in one coin, how far down it may go before it stops for the day, and how much cash it always keeps.',
    icon: IconShieldCheck,
    section: 'risk',
    covers: 'risk',
  },
  {
    id: 'trading',
    title: 'What it may trade, and how',
    blurb:
      'Which coins are allowed, the size of each trade, and the order types it may use.',
    icon: IconAdjustmentsHorizontal,
    section: 'trading',
    covers: 'trading and universe',
  },
  {
    id: 'schedule',
    title: 'When it thinks and when it trades',
    blurb: 'The times of day the system looks at the market and makes a decision.',
    icon: IconCalendarClock,
    section: 'ops',
    covers: 'ops.schedules and research.slots',
  },
  {
    id: 'autonomy',
    title: 'What it may change about itself',
    blurb:
      'How much the system may improve itself without asking you first. It cannot touch the safety checks either way.',
    icon: IconRobot,
    section: 'autonomy',
    covers: 'autonomy',
  },
];

export function SetupPanel({ onOpenSection }: { onOpenSection: (section: string) => void }) {
  return (
    <Stack gap="md" data-testid="setup-panel">
      <Text size="sm" c="dimmed">
        These are the decisions a first run on the exchange test network needs. Everything
        else the system can be told is under <strong>All settings</strong>, and nothing has
        been taken away.
      </Text>
      <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }}>
        {SETUP_CARDS.map((card) => {
          const Icon = card.icon;
          const body = (
            <Card
              withBorder
              padding="md"
              h="100%"
              data-testid={`setup-card-${card.id}`}
              style={{ cursor: 'pointer' }}
              {...(card.section ? { onClick: () => onOpenSection(card.section as string) } : {})}
            >
              <Stack gap={6}>
                <Group gap={8}>
                  <Icon size={18} />
                  <Text fw={600}>{card.title}</Text>
                </Group>
                <Text size="sm" c="dimmed">
                  {card.blurb}
                </Text>
                <Badge size="xs" variant="light" style={{ alignSelf: 'flex-start' }}>
                  covers {card.covers}
                </Badge>
              </Stack>
            </Card>
          );
          return card.to ? (
            <Anchor key={card.id} component={Link} to={card.to} underline="never">
              {body}
            </Anchor>
          ) : (
            <div key={card.id}>{body}</div>
          );
        })}
      </SimpleGrid>
    </Stack>
  );
}
