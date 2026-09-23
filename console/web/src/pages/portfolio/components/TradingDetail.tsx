/**
 * The whole story behind one row on the Trading screen.
 *
 * A position, an order or a fill, opened in the shared detail pane beside the list it was
 * clicked in.  Everything here comes from the payload the screen already loaded, plus the
 * links out: the orders and fills that belong to a position, and the decision a row came
 * from, which opens the same pane on the Decisions screen.
 *
 * Every number says what it means beside it.  "+2.3%" on its own is a number an operator
 * has to interpret; "+12.40 USDT, 2.3% more than you paid" is an answer.
 */
import { Anchor, Badge, Divider, Group, Paper, Stack, Text, Tooltip } from '@mantine/core';
import { Link } from 'react-router-dom';

import { ExecutionBadge, Explain } from '@/components';
import { detailHref } from '@/app/detailParam';
import { sleeveFullName } from '@/lib/plain';

import type { FillRow, OrderRow, PortfolioPayload, Position } from '../api';
import { slippageBps } from './ActivityTables';

const num = (value: number | null | undefined, digits = 2): string =>
  value === null || value === undefined || !Number.isFinite(value) ? '—' : value.toFixed(digits);

const pct = (value: number | null | undefined, digits = 2): string =>
  value === null || value === undefined || !Number.isFinite(value)
    ? '—'
    : `${(value * 100).toFixed(digits)}%`;

/** One labelled fact: the number, and what it means, side by side. */
function Fact({
  label,
  value,
  meaning,
}: {
  label: string;
  value: React.ReactNode;
  meaning?: string;
}) {
  return (
    <Stack gap={0} data-testid={`fact-${label.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`}>
      <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
        {label}
      </Text>
      <Text size="sm">{value}</Text>
      {meaning ? (
        <Text size="xs" c="dimmed">
          {meaning}
        </Text>
      ) : null}
    </Stack>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Stack gap={6}>
      <Divider label={title} labelPosition="left" />
      {children}
    </Stack>
  );
}

/** A link into the Decisions screen's detail pane, opened on the run that led here. */
function DecisionLink({ runId }: { runId: string | null }) {
  if (!runId) {
    return (
      <Text size="sm" c="dimmed">
        No decision is recorded against this row — it came from the bot&apos;s own rules.
      </Text>
    );
  }
  return (
    <Anchor component={Link} to={detailHref('/decisions', 'run', runId)} size="sm">
      See the decision this came from
    </Anchor>
  );
}

export interface TradingDetailProps {
  kind: 'position' | 'order' | 'fill';
  id: string;
  payload: PortfolioPayload;
  /** Switch the pane to another row on this screen. */
  onOpen: (kind: string, id: string) => void;
  /** Offered only for an order that is still resting on the exchange. */
  onCancelOrder?: (row: OrderRow) => void;
}

export function TradingDetail({ kind, id, payload, onOpen, onCancelOrder }: TradingDetailProps) {
  if (kind === 'position') {
    const position = payload.positions.find((row) => row.pair === id);
    if (!position) return <NotHere what="position" />;
    return (
      <PositionStory
        position={position}
        payload={payload}
        onOpen={onOpen}
      />
    );
  }
  if (kind === 'order') {
    const order = payload.orders.find((row) => String(row.id) === id);
    if (!order) return <NotHere what="order" />;
    return (
      <OrderStory
        order={order}
        payload={payload}
        onOpen={onOpen}
        {...(onCancelOrder ? { onCancelOrder } : {})}
      />
    );
  }
  const fill = payload.fills.find((row) => String(row.id) === id);
  if (!fill) return <NotHere what="fill" />;
  return <FillStory fill={fill} onOpen={onOpen} />;
}

function NotHere({ what }: { what: string }) {
  return (
    <Text size="sm" c="dimmed">
      That {what} is not in the list any more. It may have closed, or you may be looking at a
      different bot — pick the bot it belonged to above.
    </Text>
  );
}

function PositionStory({
  position,
  payload,
  onOpen,
}: {
  position: Position;
  payload: PortfolioPayload;
  onOpen: (kind: string, id: string) => void;
}) {
  const orders = payload.orders.filter((row) => row.pair === position.pair);
  const fills = payload.fills.filter((row) => row.pair === position.pair);
  const capUse = position.weight_cap ? position.weight / position.weight_cap : 0;
  const up = position.upnl_usdt >= 0;

  return (
    <Stack gap="md">
      <Group gap="xs">
        <Badge variant="light">{sleeveFullName(payload.sleeve)}</Badge>
        <ExecutionBadge mode={position.mode} />
      </Group>

      <Group gap="xl" wrap="wrap">
        <Fact
          label="Worth now"
          value={`${num(position.value_usdt)} USDT`}
          meaning={`${num(position.amount, 6)} units at ${num(position.mark)} USDT each`}
        />
        <Fact
          label={up ? 'Up so far' : 'Down so far'}
          value={`${up ? '+' : ''}${num(position.upnl_usdt)} USDT (${pct(position.upnl_pct)})`}
          meaning={`against what you paid, ${num(position.avg_entry)} USDT on average`}
        />
        <Fact
          label="Share of the pot"
          value={`${pct(position.weight)} of ${pct(position.weight_cap)} allowed`}
          meaning={
            capUse >= 0.95
              ? 'At the cap: the safety check will refuse to add any more of this coin.'
              : `${pct(Math.max(0, position.weight_cap - position.weight))} of headroom left before the safety check refuses more.`
          }
        />
      </Group>

      <Section title="If it goes wrong">
        <Group gap="xl" wrap="wrap">
          <Fact
            label="Sells out at"
            value={`${num(position.stop_price)} USDT`}
            meaning={`${pct(position.stop_from_open)} below where it was opened${
              position.trailing_active ? ', and this level follows the price up' : ''
            }`}
          />
          <Fact
            label="Buys used"
            value={`${position.entries_used} of ${position.entries_max}`}
            meaning="The first buy plus every top-up. Once they are used up, no more can be added."
          />
          <Fact
            label="Next profit-taking step"
            value={
              position.next_tp_rung
                ? `sell ${pct(position.next_tp_rung.sell_fraction)} once it is ${pct(
                    position.next_tp_rung.at_profit_pct,
                  )} up`
                : position.tp_rungs_fired.length
                  ? 'all steps already taken'
                  : 'none set for this position'
            }
          />
        </Group>
      </Section>

      <Section title={`Orders for ${position.pair}`}>
        {orders.length === 0 ? (
          <Text size="sm" c="dimmed">
            No orders recorded for this coin in the window loaded.
          </Text>
        ) : (
          <Stack gap={4}>
            {orders.slice(0, 12).map((order) => (
              <RowLink
                key={order.id}
                onClick={() => onOpen('order', String(order.id))}
                title={`${order.side} ${num(order.amount, 6)} at ${num(order.price)} USDT`}
                subtitle={`${order.status} · ${order.ts_utc}`}
                mode={order.mode}
              />
            ))}
          </Stack>
        )}
      </Section>

      <Section title={`Fills for ${position.pair}`}>
        {fills.length === 0 ? (
          <Text size="sm" c="dimmed">
            Nothing has actually traded for this coin in the window loaded.
          </Text>
        ) : (
          <Stack gap={4}>
            {fills.slice(0, 12).map((fill) => (
              <RowLink
                key={fill.id}
                onClick={() => onOpen('fill', String(fill.id))}
                title={`${fill.side} ${num(fill.fill_amount, 6)} at ${num(fill.fill_price)} USDT`}
                subtitle={fill.ts_utc}
                mode={fill.mode}
              />
            ))}
          </Stack>
        )}
      </Section>
    </Stack>
  );
}

function OrderStory({
  order,
  payload,
  onOpen,
  onCancelOrder,
}: {
  order: OrderRow;
  payload: PortfolioPayload;
  onOpen: (kind: string, id: string) => void;
  onCancelOrder?: (row: OrderRow) => void;
}) {
  const fills = payload.fills.filter(
    (row) => row.pair === order.pair && row.side === order.side && row.ts_utc >= order.ts_utc,
  );
  return (
    <Stack gap="md">
      <Group gap="xs">
        <Badge variant="light">{sleeveFullName(order.sleeve)}</Badge>
        <ExecutionBadge mode={order.mode} />
        <Badge color={order.side === 'buy' ? 'teal' : 'orange'}>{order.side}</Badge>
      </Group>

      <Group gap="xl" wrap="wrap">
        <Fact label="Coin" value={order.pair} />
        <Fact
          label="How much"
          value={num(order.amount, 6)}
          meaning={order.price ? `asking ${num(order.price)} USDT each` : 'at whatever the market gives'}
        />
        <Fact
          label="Kind of order"
          value={order.order_type}
          meaning={
            order.order_type === 'market'
              ? 'Takes the price on offer right now, so it is certain to go through.'
              : 'Waits for the price named above; it may never go through.'
          }
        />
        <Fact
          label="Where it stands"
          value={order.status}
          meaning={
            order.status === 'open'
              ? 'Still resting on the exchange.'
              : 'Finished — nothing further will happen to it.'
          }
        />
      </Group>

      {order.status === 'open' && onCancelOrder ? (
        <Paper withBorder p="xs" radius="sm">
          <Group justify="space-between">
            <Text size="sm">This order is still waiting on the exchange.</Text>
            <Anchor
              component="button"
              type="button"
              size="sm"
              c="orange"
              onClick={() => onCancelOrder(order)}
              data-testid="detail-cancel-order"
            >
              Cancel it
            </Anchor>
          </Group>
        </Paper>
      ) : null}

      <Section title="What actually traded">
        {fills.length === 0 ? (
          <Text size="sm" c="dimmed">
            Nothing has traded against this order yet.
          </Text>
        ) : (
          <Stack gap={4}>
            {fills.slice(0, 12).map((fill) => (
              <RowLink
                key={fill.id}
                onClick={() => onOpen('fill', String(fill.id))}
                title={`${num(fill.fill_amount, 6)} at ${num(fill.fill_price)} USDT`}
                subtitle={fill.ts_utc}
                mode={fill.mode}
              />
            ))}
          </Stack>
        )}
      </Section>

      <Section title="Why it was placed">
        <DecisionLink runId={order.run_id} />
      </Section>
    </Stack>
  );
}

function FillStory({
  fill,
  onOpen,
}: {
  fill: FillRow;
  onOpen: (kind: string, id: string) => void;
}) {
  const bps = slippageBps(fill);
  return (
    <Stack gap="md">
      <Group gap="xs">
        <Badge variant="light">{sleeveFullName(fill.sleeve)}</Badge>
        <ExecutionBadge mode={fill.mode} />
        <Badge color={fill.side === 'buy' ? 'teal' : 'orange'}>{fill.side}</Badge>
      </Group>

      <Group gap="xl" wrap="wrap">
        <Fact label="Coin" value={fill.pair} />
        <Fact
          label="Traded"
          value={`${num(fill.fill_amount, 6)} at ${num(fill.fill_price)} USDT`}
          meaning={`${num(fill.fill_amount * fill.fill_price)} USDT in total, at ${fill.ts_utc}`}
        />
        <Fact
          label="Fee"
          value={`${num(fill.fee_amount, 6)} ${fill.fee_currency ?? ''}`}
          meaning="What the exchange charged for this trade."
        />
        <Fact
          label="Price difference"
          value={
            bps === null ? (
              'not recorded'
            ) : (
              <Tooltip
                label="Basis points: one hundredth of a percent. 10 bps is 0.10%."
                withArrow
              >
                <span>
                  {bps.toFixed(1)} <Explain term="bps">bps</Explain>
                </span>
              </Tooltip>
            )
          }
          meaning={
            bps === null
              ? 'No decision-time price was captured for this trade.'
              : bps > 0
                ? `You paid ${(bps / 100).toFixed(3)}% more than the price the decision assumed.`
                : `You did ${Math.abs(bps / 100).toFixed(3)}% better than the price the decision assumed.`
          }
        />
      </Group>

      <Section title="Where it sits">
        <RowLink
          onClick={() => onOpen('position', fill.pair)}
          title={`Open the ${fill.pair} position`}
          subtitle="What this coin is worth now, and where it sells out"
          mode={fill.mode}
        />
      </Section>

      <Section title="Why it happened">
        <DecisionLink runId={fill.run_id} />
      </Section>
    </Stack>
  );
}

function RowLink({
  onClick,
  title,
  subtitle,
  mode,
}: {
  onClick: () => void;
  title: string;
  subtitle: string;
  mode: string | null | undefined;
}) {
  return (
    <Paper
      withBorder
      p="xs"
      radius="sm"
      onClick={onClick}
      style={{ cursor: 'pointer' }}
      data-testid="detail-row-link"
    >
      <Group justify="space-between" wrap="nowrap">
        <Stack gap={0}>
          <Text size="sm">{title}</Text>
          <Text size="xs" c="dimmed">
            {subtitle}
          </Text>
        </Stack>
        <ExecutionBadge mode={mode} />
      </Group>
    </Paper>
  );
}
