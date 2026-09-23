import { Alert, Badge, Card, Grid, Group, Loader, SimpleGrid, Stack, Table, Tabs, Text, Title }
  from '@mantine/core';
import { useCallback, useEffect, useMemo, useState } from 'react';

import { errorMessage } from '@/api';
import { useApi } from '@/app/ApiContext';
import { usePageCommands } from '@/app/commandRegistry';
import { EmptyState } from '@/components';

import { riskApi, type Anchors, type FlagsPayload, type GateDecision, type Meter,
  type RiskOverview, type Severity, type Sleeve, type UtilisationPayload } from './api';
import { GateLog } from './components/GateLog';
import { LimitsTable } from './components/LimitsTable';
import { MechanicsPanel } from './components/MechanicsPanel';
import { MeterBar, fmt } from './components/MeterBar';
import { ResumeMonthlyModal } from './components/ResumeMonthlyModal';

const METER_UNITS: Record<string, string> = {
  trades_per_day: 'count',
  orders_per_day: 'count',
  turnover_day: 'fraction',
  fee_budget: 'fraction',
  gross_cap: 'fraction',
  usdt_floor: 'fraction',
};

const METER_HELP: Record<string, string> = {
  trades_per_day: 'Discretionary entries today (Gulf day). Risk exits are exempt.',
  orders_per_day: 'Discretionary orders today. Stops and flattens never consume this.',
  turnover_day: 'Traded notional divided by NAV today.',
  fee_budget: 'Fees paid this month as a fraction of NAV.',
  gross_cap: 'Total non-USDT exposure.',
  usdt_floor: 'USDT held. This one is a FLOOR: healthy means staying above it.',
};

/**
 * How much of each allowed drawdown has been used, from the gate's own anchors.
 *
 * With no NAV both bars used to short-circuit to 0%, i.e. "nothing used", however far NAV
 * had actually fallen. They are marked invalid instead, so the bar reads "unknown".
 */
function StopProximity({ anchors, nav, navValid }: {
  anchors: Anchors;
  nav: number;
  navValid: boolean;
}) {
  const used = (anchor: number | null, stop: number) =>
    anchor && anchor > 0 && nav > 0 && stop > 0 ? Math.max(0, (1 - nav / anchor) / stop) : 0;
  const rows: Array<[string, number, number | null, number]> = [
    ['daily stop', used(anchors.day_anchor_nav, anchors.daily_loss_stop),
      anchors.day_anchor_nav, anchors.daily_loss_stop],
    ['monthly stop', used(anchors.month_anchor_nav, anchors.monthly_loss_stop),
      anchors.month_anchor_nav, anchors.monthly_loss_stop],
  ];
  return (
    <Stack gap="sm">
      {rows.map(([label, pct, anchor, stop]) => {
        const valid = navValid && nav > 0 && Boolean(anchor) && (anchor ?? 0) > 0;
        return (
          <MeterBar
            key={label}
            label={label}
            unit="fraction"
            meter={{ used: pct * stop, limit: stop, headroom: stop - pct * stop, pct, valid }}
            unknownReason={
              navValid ? 'unknown — no anchor stamped yet' : 'unknown — NAV could not be read'
            }
            help={`anchor ${anchor ? anchor.toFixed(2) : '—'} USDT · stop at ${
              (stop * 100).toFixed(0)}%`}
          />
        );
      })}
    </Stack>
  );
}

function AnchorsCard({ anchors, onResume }: { anchors: Anchors; onResume: () => void }) {
  const rows: Array<[string, string]> = [
    ['month anchor', `${anchors.month_anchor_nav ?? '—'} (${anchors.month_anchor_month || '—'})`],
    ['day anchor', `${anchors.day_anchor_nav ?? '—'} (${anchors.day_anchor_date || '—'})`],
    ['entries locked until', anchors.locked_until || '—'],
    ['last resume', anchors.monthly_resumed_utc || 'never'],
  ];
  return (
    <Card withBorder padding="md">
      <Group justify="space-between" mb="sm">
        <Text fw={600}>Risk state</Text>
        {anchors.monthly_locked ? (
          <Badge color="red">MONTHLY STOP ENGAGED</Badge>
        ) : (
          <Badge color="teal" variant="light">running</Badge>
        )}
      </Group>
      <Table withRowBorders={false}>
        <Table.Tbody>
          {rows.map(([label, value]) => (
            <Table.Tr key={label}>
              <Table.Td>
                <Text size="sm" c="dimmed">{label}</Text>
              </Table.Td>
              <Table.Td>
                <Text size="sm" ff="monospace">{value}</Text>
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      {anchors.monthly_locked ? (
        <Badge
          mt="sm"
          color="orange"
          size="lg"
          style={{ cursor: 'pointer' }}
          onClick={onResume}
          data-testid="resume-monthly"
        >
          Resume after the monthly stop…
        </Badge>
      ) : null}
    </Card>
  );
}

function FlagsCard({ flags }: { flags: FlagsPayload }) {
  const entries = Object.entries(flags.flags ?? {});
  return (
    <Card withBorder padding="md">
      <Text fw={600} mb="sm">Flags</Text>
      {!flags.ok ? (
        <Alert color="red" title="Flags file unreadable">
          Entries are blocked fail-closed until this is fixed ({flags.error}).
        </Alert>
      ) : null}
      {entries.length ? (
        <Stack gap="xs">
          {entries.map(([name, flag]) => (
            <Group key={name} justify="space-between">
              <Text size="sm">{name}</Text>
              <Badge
                size="sm"
                color={flag.severity === 'block_entries' ? 'red' : 'gray'}
                variant={flag.active ? 'filled' : 'light'}
              >
                {String(flag.severity)}
              </Badge>
            </Group>
          ))}
        </Stack>
      ) : (
        <Text size="sm" c="dimmed">No flags set.</Text>
      )}
    </Card>
  );
}

export default function RiskPage() {
  const client = useApi();
  const risk = useMemo(() => riskApi(client), [client]);
  const [overview, setOverview] = useState<RiskOverview | null>(null);
  const [sleeve, setSleeve] = useState<Sleeve>('a');
  const [meters, setMeters] = useState<Record<string, Meter>>({});
  const [util, setUtil] = useState<UtilisationPayload | null>(null);
  const [decisions, setDecisions] = useState<GateDecision[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [severity, setSeverity] = useState('all');
  const [resumeOpen, setResumeOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await risk.overview();
      setOverview(data);
      // No nav/free_usdt arguments: the server reads the real portfolio. Passing 0 here
      // is what used to make every NAV-derived meter read 0% with a false floor breach.
      const result = await risk.utilisation(sleeve);
      setUtil(result);
      setMeters(result.meters ?? {});
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, [risk, sleeve]);

  const loadDecisions = useCallback(async () => {
    try {
      const data = await risk.gateDecisions({
        sleeve,
        ...(severity === 'all' ? {} : { severity: severity as Severity }),
        limit: 200,
      });
      setDecisions(data.rows);
      setCounts(data.counts);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [risk, sleeve, severity]);

  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    void loadDecisions();
  }, [loadDecisions]);

  const current = overview?.sleeves?.[sleeve];
  // The utilisation read is authoritative (it saw the bot); the overview's journal NAV is
  // the fallback for the header when the utilisation call has not landed yet.
  const nav = util?.nav || overview?.navs?.[sleeve] || 0;
  const navValid = util ? util.nav_valid : nav > 0;
  const navSource = util?.nav_source ?? 'unknown';
  // A payload without `meters` is an install that has not run yet, not a broken page.
  const meterRows = useMemo(() => Object.entries(meters ?? {}), [meters]);

  usePageCommands('risk', [
    {
      id: 'resume-monthly',
      title: 'Resume after the monthly stop',
      subtitle: 'Typed phrase plus step-up; re-arms entries for the sleeve',
      keywords: ['unlock', 'stop'],
      run: (ctx) => {
        setResumeOpen(true);
        ctx.close();
      },
    },
    {
      id: 'rejects-only',
      title: 'Gate log: rejections only',
      keywords: ['reject', 'blocked'],
      run: (ctx) => {
        setSeverity('reject');
        ctx.close();
      },
    },
    {
      id: 'reload',
      title: 'Reload the risk limits and gate log',
      run: (ctx) => {
        void load();
        void loadDecisions();
        ctx.close();
      },
    },
  ]);

  if (!overview) {
    if (error) {
      return (
        <EmptyState
          title="Could not load risk"
          description={error}
          action={{ label: 'Retry', onClick: () => void load() }}
        />
      );
    }
    return <Loader />;
  }

  return (
    <Stack>
      <Group justify="space-between">
        <Title order={2}>Risk</Title>
        <Group gap="xs">
          {overview.kill ? <Badge color="red" size="lg">KILL ENGAGED</Badge> : null}
          <Text size="sm" c={navValid ? 'dimmed' : 'orange'} data-testid="risk-nav">
            NAV {navValid ? `${fmt(nav, 'usdt')} (${navSource})` : 'unknown'}
          </Text>
          {loading ? <Loader size="xs" /> : null}
        </Group>
      </Group>

      {error ? <Alert color="red" title="Refresh failed">{error}</Alert> : null}

      <Tabs value={sleeve} onChange={(v) => setSleeve((v as Sleeve) ?? 'a')}>
        <Tabs.List>
          <Tabs.Tab value="a">Sleeve A</Tabs.Tab>
          <Tabs.Tab value="b">Sleeve B</Tabs.Tab>
        </Tabs.List>
      </Tabs>

      {current ? (
        <Grid>
          <Grid.Col span={{ base: 12, md: 4 }}>
            <Stack>
              <AnchorsCard anchors={current.anchors} onResume={() => setResumeOpen(true)} />
              <Card withBorder padding="md">
                <Text fw={600} mb="sm">Progress to stop</Text>
                <StopProximity anchors={current.anchors} nav={nav} navValid={navValid} />
              </Card>
              <FlagsCard flags={overview.flags} />
            </Stack>
          </Grid.Col>
          <Grid.Col span={{ base: 12, md: 8 }}>
            <Stack>
              <Card withBorder padding="md">
                <Group justify="space-between" mb="sm">
                  <Text fw={600}>Churn, turnover and fee budget</Text>
                  {!navValid ? (
                    <Badge color="orange" variant="light" data-testid="nav-unknown">
                      NAV unavailable — exposure meters unknown
                    </Badge>
                  ) : null}
                </Group>
                <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="md">
                  {meterRows.map(([name, meter]) => (
                    <MeterBar
                      key={name}
                      label={name}
                      meter={meter}
                      unit={METER_UNITS[name] ?? 'fraction'}
                      floor={name === 'usdt_floor'}
                      help={METER_HELP[name]}
                    />
                  ))}
                </SimpleGrid>
              </Card>
              <MechanicsPanel mechanics={current.mechanics} />
            </Stack>
          </Grid.Col>
          <Grid.Col span={12}>
            <Card withBorder padding="md">
              <Text fw={600} mb="sm">Limits</Text>
              <LimitsTable limits={current.limits.limits} />
            </Card>
          </Grid.Col>
          <Grid.Col span={12}>
            <Card withBorder padding="md">
              <Text fw={600} mb="sm">Gate decisions</Text>
              <GateLog
                rows={decisions}
                counts={counts}
                severity={severity}
                onSeverityChange={setSeverity}
              />
            </Card>
          </Grid.Col>
        </Grid>
      ) : null}

      {current ? (
        <ResumeMonthlyModal
          opened={resumeOpen}
          onClose={() => setResumeOpen(false)}
          sleeve={sleeve}
          anchors={current.anchors}
          onResume={(phrase) => risk.resumeMonthly(sleeve, phrase)}
          onResumed={() => {
            void load();
            void loadDecisions();
          }}
        />
      ) : null}
    </Stack>
  );
}
