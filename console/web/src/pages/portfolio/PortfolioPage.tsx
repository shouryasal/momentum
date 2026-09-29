/**
 * Trading — every position, order and fill, and the story behind any of them.
 *
 * The list is on the left and the detail pane is on the right: clicking a position, an
 * order or a fill opens that one row's whole story beside the list rather than taking the
 * operator somewhere else.  The open row is in the URL (`?detail=order:41`), so a refresh
 * and a pasted link both land on it.
 *
 * The two bots are named, not lettered.  The owner had to ask what "sleeve A" was, so the
 * tabs read "Rules bot (no AI)" and "AI bot (Claude)" and keep the raw id in the tooltip.
 */
import { Alert, Badge, Card, Loader, Stack, Tabs, Text, Tooltip } from '@mantine/core';
import { useCallback, useEffect, useMemo, useState } from 'react';

import { errorMessage } from '@/api';
import { useApi } from '@/app/ApiContext';
import { usePageCommands } from '@/app/commandRegistry';
import { useDetailRoute } from '@/app/detailParam';
import {
  ConfirmDialog,
  DetailPane,
  EmptyState,
  MasterDetail,
  PageIntro,
} from '@/components';
import { sleeveFullName, sleeveHint, sleeveName } from '@/lib/plain';
import { routeBlurb } from '@/routes';

import { portfolioApi, type OrderRow, type PortfolioPayload, type Sleeve } from './api';
import { FillsTable, OrdersTable } from './components/ActivityTables';
import { PositionsTable } from './components/PositionsTable';
import { PotCard } from './components/PotCard';
import { TradingDetail } from './components/TradingDetail';
import { WalletPanel } from './components/WalletPanel';

/** What the pane calls each kind of row, and what that kind of row actually is. */
const DETAIL_KINDS = ['position', 'order', 'fill'] as const;

const DETAIL_SUBTITLE: Record<string, string> = {
  position: 'What this coin is worth now, where it sells out, and everything that built it.',
  order: 'What was asked of the exchange, what came back, and the decision behind it.',
  fill: 'What actually traded, what it cost, and how the price compared with the plan.',
};

const DETAIL_TITLE: Record<string, string> = {
  position: 'Position',
  order: 'Order',
  fill: 'Trade',
};

export default function PortfolioPage() {
  const client = useApi();
  const pf = useMemo(() => portfolioApi(client), [client]);
  const [sleeve, setSleeve] = useState<Sleeve>('a');
  const [data, setData] = useState<PortfolioPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [cancelling, setCancelling] = useState<OrderRow | null>(null);
  const detail = useDetailRoute(DETAIL_KINDS);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setData(await pf.load(sleeve));
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [pf, sleeve]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * `POST /api/portfolio/{sleeve}/orders/{trade_id}/cancel` is step-up guarded: it changes
   * what the exchange holds. It used to fire from the table's bare `onClick`, so outside
   * the step-up window every click was a 403 the operator could not clear from this page.
   */
  const cancel = useCallback(
    async (row: OrderRow) => {
      try {
        await pf.cancelOrder(sleeve, row.ft_trade_id ?? row.id);
        await load();
      } catch (e) {
        setError(errorMessage(e));
        throw e; // keep the dialog open with the reason on it
      }
    },
    [pf, sleeve, load],
  );

  usePageCommands('portfolio', [
    {
      id: 'sleeve-a',
      title: 'Trading: the Rules bot (no AI)',
      subtitle: 'sleeve a — trades from fixed rules; Claude never touches it',
      run: (ctx) => {
        setSleeve('a');
        ctx.close();
      },
    },
    {
      id: 'sleeve-b',
      title: 'Trading: the AI bot (Claude)',
      subtitle: 'sleeve b — Claude suggests how much to hold; the safety checks still decide',
      run: (ctx) => {
        setSleeve('b');
        ctx.close();
      },
    },
    {
      id: 'reload',
      title: 'Reload what is held, ordered and traded',
      run: (ctx) => {
        void load();
        ctx.close();
      },
    },
  ]);

  const detailPane =
    detail.opened && detail.kind && detail.id && data ? (
      <DetailPane
        title={`${DETAIL_TITLE[detail.kind] ?? 'Row'} · ${sleeveName(sleeve)}`}
        subtitle={DETAIL_SUBTITLE[detail.kind] ?? 'Everything behind this row.'}
        rawId={`${detail.kind} ${detail.id} · sleeve ${sleeve}`}
        onClose={detail.close}
      >
        <TradingDetail
          kind={detail.kind as (typeof DETAIL_KINDS)[number]}
          id={detail.id}
          payload={data}
          onOpen={detail.open}
          onCancelOrder={setCancelling}
        />
      </DetailPane>
    ) : null;

  return (
    <MasterDetail detail={detailPane}>
      <Stack>
        <PageIntro
          title="Trading"
          blurb={routeBlurb('portfolio')}
          actions={
            <>
              {data ? (
                <Tooltip
                  label={
                    data.bot_up
                      ? 'The trading bot is running and answering.'
                      : 'The trading bot is not answering. Nothing will trade until it is back.'
                  }
                  withArrow
                >
                  <Badge color={data.bot_up ? 'teal' : 'red'} variant="light">
                    {data.bot_up ? 'bot running' : 'bot not running'}
                  </Badge>
                </Tooltip>
              ) : null}
              {loading ? <Loader size="xs" /> : null}
            </>
          }
        />

        <Tabs value={sleeve} onChange={(v) => setSleeve((v as Sleeve) ?? 'a')}>
          <Tabs.List>
            {(['a', 'b'] as const).map((key) => (
              <Tooltip key={key} label={sleeveHint(key)} multiline w={300} withArrow>
                <Tabs.Tab value={key}>{sleeveFullName(key)}</Tabs.Tab>
              </Tooltip>
            ))}
          </Tabs.List>
        </Tabs>

        {error ? (
          <Alert color="red" title="Could not load what this bot holds">
            {error}
          </Alert>
        ) : null}

        {data ? (
          <Stack>
            {/* The pot across every restart leads; the ledger of the current run follows. */}
            <PotCard pot={data.pot} />
            <WalletPanel wallet={data.wallet} />
            <Card withBorder padding="md">
              <Text fw={600}>What it holds right now</Text>
              <Text size="xs" c="dimmed" mb="sm">
                Click a coin to see what it is worth, where it sells out, and everything that
                built the position.
              </Text>
              <PositionsTable
                positions={data.positions}
                loading={loading}
                onSelect={(row) => detail.open('position', row.pair)}
              />
            </Card>
            <Card withBorder padding="md">
              <Text fw={600}>What it asked the exchange to do</Text>
              <Text size="xs" c="dimmed" mb="sm">
                Click an order to see what came back and the decision behind it.
              </Text>
              <OrdersTable
                rows={data.orders}
                onCancel={setCancelling}
                loading={loading}
                onSelect={(row) => detail.open('order', String(row.id))}
              />
            </Card>
            <Card withBorder padding="md">
              <Text fw={600}>What actually traded</Text>
              <Text size="xs" c="dimmed" mb="sm">
                Click a trade to see the price it got, the fee, and how that compared with the
                price the decision assumed.
              </Text>
              <FillsTable
                rows={data.fills}
                loading={loading}
                onSelect={(row) => detail.open('fill', String(row.id))}
              />
            </Card>
          </Stack>
        ) : error ? (
          <EmptyState
            title="Nothing to show"
            description="What this bot holds could not be loaded."
            action={{ label: 'Try again', onClick: () => void load() }}
          />
        ) : (
          <Loader />
        )}

        <ConfirmDialog
          opened={cancelling !== null}
          onClose={() => setCancelling(null)}
          title="Cancel this waiting order?"
          confirmLabel="Cancel the order"
          requireStepUp
          danger
          description={
            cancelling
              ? `${cancelling.side ?? ''} ${cancelling.pair} — the bot cancels it on the exchange. ` +
                'Anything that already traded stays traded.'
              : ''
          }
          onConfirm={async () => {
            if (cancelling) await cancel(cancelling);
            setCancelling(null);
          }}
        />
      </Stack>
    </MasterDetail>
  );
}
