import {
  Alert,
  Badge,
  Card,
  Checkbox,
  Group,
  Loader,
  NumberInput,
  Select,
  SimpleGrid,
  Skeleton,
  Stack,
  Text,
  Title,
  Tooltip,
} from '@mantine/core';
import { IconInfoCircle } from '@tabler/icons-react';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';

import { useApi } from '@/app/ApiContext';
import { usePageCommands, type CommandContext } from '@/app/commandRegistry';
import { DataTable, EmptyState, ErrorAlert, type DataTableColumn } from '@/components';

import { marketApi, type ProposalMarker } from './api';
import { ChartLegend } from './components/ChartLegend';
import { PriceChart } from './components/PriceChart';
import {
  DEFAULT_MAX_BARS,
  DEFAULT_OVERLAYS,
  buildChartModel,
  emptyModel,
  type Overlays,
} from './model';

const OVERLAY_LABELS: Array<{ key: keyof Overlays; label: string }> = [
  { key: 'ma', label: 'MA' },
  { key: 'atr', label: 'ATR band' },
  { key: 'rsi', label: 'RSI pane' },
  { key: 'regime', label: 'Regime' },
  { key: 'blackout', label: 'Blackout' },
  { key: 'fills', label: 'Fills' },
  { key: 'exits', label: 'Stop / TP' },
  { key: 'signals', label: 'Signals' },
  { key: 'gateRejects', label: 'Gate rejects' },
  { key: 'proposals', label: 'Proposals' },
];

/**
 * Charts (spec 12 page 6).
 *
 * One pair, one timeframe, and everything the system did to it: fills with a SIM/LIVE
 * distinction, stop and take-profit exits, signals coloured by the *validator's* verdict,
 * gate rejects, the proposal target steps, blackout windows, regime shading, an MA overlay,
 * an ATR channel and an RSI pane.
 *
 * Candles come from the same feather store the containers read, so a chart and a backtest
 * see identical bars. Above the render budget the bars are aggregated into OHLC buckets
 * rather than thinned, and the page says so — a chart that quietly drops half its highs is
 * worse than a slow one.
 */
export default function ChartsPage() {
  const client = useApi();
  const market = useMemo(() => marketApi(client), [client]);

  const [pair, setPair] = useState('');
  const [timeframe, setTimeframe] = useState('4h');
  const [sleeve, setSleeve] = useState('a');
  const [limit, setLimit] = useState(1000);
  const [maPeriod, setMaPeriod] = useState(200);
  const [atrPeriod, setAtrPeriod] = useState(14);
  const [atrMult, setAtrMult] = useState(3);
  const [rsiPeriod, setRsiPeriod] = useState(14);
  const [maxBars, setMaxBars] = useState(DEFAULT_MAX_BARS);
  const [overlays, setOverlays] = useState<Overlays>(DEFAULT_OVERLAYS);

  const pairs = useQuery({ queryKey: ['charts', 'pairs'], queryFn: market.pairs });
  const universe = pairs.data?.pairs ?? [];
  const timeframes = pairs.data?.timeframes?.length ? pairs.data.timeframes : ['1h', '4h', '1d'];
  const activePair = pair || universe[0] || '';
  const activeTimeframe = timeframes.includes(timeframe) ? timeframe : (timeframes[0] ?? '4h');

  const candles = useQuery({
    queryKey: ['charts', 'candles', activePair, activeTimeframe, limit],
    queryFn: () => market.candles(activePair, activeTimeframe, limit),
    enabled: activePair !== '',
  });

  // Every context query is bounded by the window the candles actually cover, so switching
  // to a 400-bar 1h chart does not drag a year of journal rows across the wire.
  const firstCandle = candles.data?.candles?.[0]?.date ?? null;
  const sinceHours = useMemo(() => {
    if (!firstCandle) return null;
    const ms = Date.parse(firstCandle.includes('T') ? firstCandle : firstCandle.replace(' ', 'T'));
    if (!Number.isFinite(ms)) return null;
    return Math.max(1, Math.min(24 * 365, Math.ceil((Date.now() - ms) / 3_600_000)));
  }, [firstCandle]);

  const ready = candles.isSuccess && activePair !== '';

  const markers = useQuery({
    queryKey: ['charts', 'markers', activePair, sleeve, firstCandle],
    queryFn: () => market.markers(activePair, sleeve, firstCandle ?? undefined),
    enabled: ready,
  });

  const signals = useQuery({
    queryKey: ['charts', 'signals', activePair, sinceHours],
    queryFn: () => market.signals(activePair, sinceHours ?? undefined),
    enabled: ready,
  });

  const flags = useQuery({ queryKey: ['charts', 'flags'], queryFn: market.flags });
  const regime = useQuery({ queryKey: ['charts', 'regime'], queryFn: () => market.regime(200) });

  const model = useMemo(() => {
    if (!candles.data?.candles?.length) return emptyModel();
    return buildChartModel({
      candles: candles.data.candles,
      markers: markers.data?.markers ?? [],
      proposals: markers.data?.proposals ?? [],
      signals: signals.data?.signals ?? [],
      flags: flags.data?.flags ?? null,
      regimes: regime.data?.snapshots ?? [],
      maPeriod,
      atrPeriod,
      atrMult,
      rsiPeriod,
      maxBars,
      overlays,
    });
  }, [
    candles.data,
    markers.data,
    signals.data,
    flags.data,
    regime.data,
    maPeriod,
    atrPeriod,
    atrMult,
    rsiPeriod,
    maxBars,
    overlays,
  ]);

  usePageCommands('charts', [
    {
      id: 'reload',
      title: 'Charts: reload candles and markers',
      run: (ctx: CommandContext) => {
        void candles.refetch();
        void markers.refetch();
        void signals.refetch();
        ctx.close();
      },
    },
    ...timeframes.map((tf) => ({
      id: `tf-${tf}`,
      title: `Charts: ${tf} candles`,
      keywords: ['timeframe'],
      run: (ctx: CommandContext) => {
        setTimeframe(tf);
        ctx.close();
      },
    })),
    {
      id: 'markers-only',
      title: 'Charts: hide every overlay',
      keywords: ['overlay', 'clean'],
      run: (ctx: CommandContext) => {
        setOverlays((current) => ({ ...current, ma: false, atr: false, rsi: false,
          regime: false, blackout: false }));
        ctx.close();
      },
    },
    {
      id: 'overlays-all',
      title: 'Charts: show every overlay',
      keywords: ['overlay'],
      run: (ctx: CommandContext) => {
        setOverlays(DEFAULT_OVERLAYS);
        ctx.close();
      },
    },
  ]);

  const contextErrors = [
    signals.isError ? 'signals' : null,
    flags.isError ? 'blackout flags' : null,
    regime.isError ? 'regime history' : null,
    markers.isError ? 'markers' : null,
  ].filter((name): name is string => name !== null);

  const proposalColumns: Array<DataTableColumn<ProposalMarker>> = [
    {
      key: 'run',
      header: 'Run',
      sortValue: (r) => r.run_id,
      render: (r) => (
        <Text size="xs" ff="monospace">
          {r.run_id}
        </Text>
      ),
    },
    { key: 'module', header: 'Module', sortValue: (r) => r.module,
      render: (r) => <Text size="xs">{r.module}</Text> },
    {
      key: 'targets',
      header: 'Targets',
      render: (r) => (
        <Text size="xs" ff="monospace">
          {Object.entries(r.targets ?? {})
            .map(([k, v]) => `${k} ${(v * 100).toFixed(0)}%`)
            .join(' · ')}
        </Text>
      ),
    },
    {
      key: 'state',
      header: 'State',
      render: (r) => (
        <Badge size="xs" color={r.abstain ? 'gray' : 'teal'} variant="light">
          {r.abstain ? 'abstain' : 'active'}
        </Badge>
      ),
    },
  ];

  return (
    <Stack gap="lg">
      <Group justify="space-between" align="flex-start">
        <div>
          <Title order={3}>Charts</Title>
          <Text size="sm" c="dimmed">
            Candles from the shared feather store, with every decision the system made about
            them drawn on top.
          </Text>
        </div>
        {candles.isFetching ? <Loader size="xs" data-testid="charts-loading" /> : null}
      </Group>

      <Card withBorder padding="md">
        <Group align="end" gap="md" wrap="wrap">
          <Select
            label="Pair"
            data={universe}
            value={activePair || null}
            onChange={(v) => setPair(v ?? '')}
            w={150}
            allowDeselect={false}
            disabled={pairs.isLoading}
          />
          <Select
            label="Timeframe"
            data={timeframes}
            value={activeTimeframe}
            onChange={(v) => setTimeframe(v ?? '4h')}
            w={110}
            allowDeselect={false}
          />
          <Select
            label="Sleeve"
            data={[
              { value: 'a', label: 'Sleeve A' },
              { value: 'b', label: 'Sleeve B' },
            ]}
            value={sleeve}
            onChange={(v) => setSleeve(v ?? 'a')}
            w={130}
            allowDeselect={false}
          />
          <NumberInput
            label="Candles"
            value={limit}
            min={50}
            max={5000}
            step={100}
            onChange={(v) => setLimit(Number(v) || 1000)}
            w={110}
          />
          <Tooltip label="Above this, bars are aggregated into OHLC buckets to stay responsive">
            <NumberInput
              label="Render budget"
              value={maxBars}
              min={100}
              max={5000}
              step={100}
              onChange={(v) => setMaxBars(Number(v) || DEFAULT_MAX_BARS)}
              w={130}
            />
          </Tooltip>
          <NumberInput label="MA" value={maPeriod} min={2} max={400}
            onChange={(v) => setMaPeriod(Number(v) || 200)} w={90} />
          <NumberInput label="ATR" value={atrPeriod} min={2} max={100}
            onChange={(v) => setAtrPeriod(Number(v) || 14)} w={90} />
          <NumberInput label="ATR ×" value={atrMult} min={0.5} max={10} step={0.5} decimalScale={1}
            onChange={(v) => setAtrMult(Number(v) || 3)} w={90} />
          <NumberInput label="RSI" value={rsiPeriod} min={2} max={100}
            onChange={(v) => setRsiPeriod(Number(v) || 14)} w={90} />
        </Group>
        <SimpleGrid cols={{ base: 2, sm: 4, lg: 5 }} mt="md" spacing="xs">
          {OVERLAY_LABELS.map((entry) => (
            <Checkbox
              key={entry.key}
              size="xs"
              label={entry.label}
              checked={overlays[entry.key]}
              onChange={(event) =>
                setOverlays((current) => ({
                  ...current,
                  [entry.key]: event.currentTarget.checked,
                }))
              }
            />
          ))}
        </SimpleGrid>
      </Card>

      <ErrorAlert error={pairs.error} title="Could not load the universe">
        {' '}
        The console reads it from `config/earn.yaml`; a failure here means the config itself
        did not load.
      </ErrorAlert>
      <ErrorAlert error={candles.error} title="Could not load candles" />

      {contextErrors.length > 0 ? (
        <Alert
          color="yellow"
          variant="light"
          icon={<IconInfoCircle size={16} />}
          title="Drawn without some context"
          data-testid="context-warning"
        >
          {contextErrors.join(', ')} could not be read — the candles below are complete, the
          overlay is not. A fresh checkout has no journal until `ops/setup.sh` has run.
        </Alert>
      ) : null}

      <Card withBorder padding="md">
        <Group justify="space-between" mb="sm" wrap="wrap">
          <Group gap="xs">
            <Text fw={600}>
              {activePair || '—'} · {activeTimeframe}
            </Text>
            {model.factor > 1 ? (
              <Tooltip label="Open = first, high = max, low = min, close = last, volume = sum">
                <Badge size="xs" variant="light" color="blue" data-testid="downsample-badge">
                  {model.sourceBars} bars → {model.bars.length} ({model.factor}× buckets)
                </Badge>
              </Tooltip>
            ) : model.bars.length > 0 ? (
              <Badge size="xs" variant="light" color="gray">
                {model.bars.length} bars
              </Badge>
            ) : null}
            {model.droppedMarkers > 0 ? (
              <Tooltip label="Placing a mark on the wrong candle is worse than not drawing it">
                <Badge size="xs" variant="light" color="gray" data-testid="dropped-badge">
                  {model.droppedMarkers} outside this window
                </Badge>
              </Tooltip>
            ) : null}
          </Group>
          <ChartLegend model={model} />
        </Group>

        {pairs.isLoading || (activePair !== '' && candles.isLoading) ? (
          <Skeleton height={460} radius="sm" data-testid="chart-skeleton" />
        ) : candles.isError ? (
          <EmptyState
            compact
            title="No chart to draw"
            description="The candle request failed; see the message above."
          />
        ) : model.bars.length === 0 ? (
          <EmptyState
            title="No candles for this pair and timeframe"
            description="Run `ops/bootstrap_data.sh`, or wait for the next ingest — the feather store is written every 15 minutes."
            action={{ label: 'Reload', onClick: () => void candles.refetch() }}
          />
        ) : (
          <PriceChart
            model={model}
            overlays={overlays}
            maPeriod={maPeriod}
            atrPeriod={atrPeriod}
            atrMult={atrMult}
            rsiPeriod={rsiPeriod}
          />
        )}

        <Text size="xs" c="dimmed" mt="xs">
          Fills are drawn where they happened: a pale circle is a dry-run fill on live market
          data, a solid arrow is real money. Signals take the validator&apos;s verdict colour,
          never the detector&apos;s score. Shaded columns are the market regime; amber columns
          are blackout windows, where the gate refused every entry.
        </Text>
      </Card>

      <Card withBorder padding="md">
        <Group justify="space-between" mb="sm">
          <Text fw={600}>Proposals in this window</Text>
          {model.counts.proposal > 0 ? (
            <Badge size="xs" variant="light">
              {model.counts.proposal} on the chart
            </Badge>
          ) : null}
        </Group>
        <DataTable
          columns={proposalColumns}
          rows={markers.data?.proposals ?? []}
          rowKey={(r) => r.run_id}
          loading={markers.isLoading}
          dense
          emptyTitle="No proposals"
          emptyDescription="Sleeve B has not consumed a proposal in this window."
        />
      </Card>
    </Stack>
  );
}
