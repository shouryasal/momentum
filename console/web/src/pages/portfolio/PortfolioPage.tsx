import { Alert, Badge, Card, Group, Loader, Stack, Tabs, Text, Title } from '@mantine/core';
import { useCallback, useEffect, useMemo, useState } from 'react';

import { errorMessage } from '@/api';
import { useApi } from '@/app/ApiContext';
import { usePageCommands } from '@/app/commandRegistry';
import { EmptyState } from '@/components';

import { portfolioApi, type OrderRow, type PortfolioPayload, type Sleeve } from './api';
import { FillsTable, OrdersTable } from './components/ActivityTables';
import { PositionsTable } from './components/PositionsTable';
import { WalletPanel } from './components/WalletPanel';

export default function PortfolioPage() {
  const client = useApi();
  const pf = useMemo(() => portfolioApi(client), [client]);
  const [sleeve, setSleeve] = useState<Sleeve>('a');
  const [data, setData] = useState<PortfolioPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

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

  const cancel = useCallback(
    async (row: OrderRow) => {
      try {
        await pf.cancelOrder(sleeve, row.ft_trade_id ?? row.id);
        await load();
      } catch (e) {
        setError(errorMessage(e));
      }
    },
    [pf, sleeve, load],
  );

  usePageCommands('portfolio', [
    {
      id: 'sleeve-a',
      title: 'Portfolio: sleeve A',
      run: (ctx) => {
        setSleeve('a');
        ctx.close();
      },
    },
    {
      id: 'sleeve-b',
      title: 'Portfolio: sleeve B',
      run: (ctx) => {
        setSleeve('b');
        ctx.close();
      },
    },
    {
      id: 'reload',
      title: 'Reload positions, orders and fills',
      run: (ctx) => {
        void load();
        ctx.close();
      },
    },
  ]);

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>Portfolio</Title>
        <Group gap="xs">
          {data ? (
            <Badge color={data.bot_up ? 'teal' : 'red'} variant="light">
              bot {data.bot_up ? 'up' : 'down'}
            </Badge>
          ) : null}
          {loading ? <Loader size="xs" /> : null}
        </Group>
      </Group>

      <Tabs value={sleeve} onChange={(v) => setSleeve((v as Sleeve) ?? 'a')}>
        <Tabs.List>
          <Tabs.Tab value="a">Sleeve A</Tabs.Tab>
          <Tabs.Tab value="b">Sleeve B</Tabs.Tab>
        </Tabs.List>
      </Tabs>

      {error ? <Alert color="red" title="Could not load portfolio">{error}</Alert> : null}

      {data ? (
        <Stack>
          <WalletPanel wallet={data.wallet} />
          <Card withBorder padding="md">
            <Text fw={600} mb="sm">Positions</Text>
            <PositionsTable positions={data.positions} loading={loading} />
          </Card>
          <Card withBorder padding="md">
            <Text fw={600} mb="sm">Orders</Text>
            <OrdersTable rows={data.orders} onCancel={cancel} loading={loading} />
          </Card>
          <Card withBorder padding="md">
            <Text fw={600} mb="sm">Fills</Text>
            <FillsTable rows={data.fills} loading={loading} />
          </Card>
        </Stack>
      ) : error ? (
        <EmptyState
          title="Nothing to show"
          description="The portfolio could not be loaded."
          action={{ label: 'Retry', onClick: () => void load() }}
        />
      ) : (
        <Loader />
      )}
    </Stack>
  );
}
