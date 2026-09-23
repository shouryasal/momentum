/**
 * The whole of what Home used to show, kept reachable and off the screen.
 *
 * Home is the money now: what went in, what it is worth, what it holds and what it has
 * traded. Everything the owner called "errors and tech" —
 * the health internals, the freshness numbers, the job schedule, the funnel counts, the
 * gate tallies, the open incidents, the settings diff, the model bill — is still here,
 * still here, behind a URL. Nothing was deleted; it stopped being the first thing a
 * person sees.
 *
 * It reads only from the bundle the screen already loaded, so a panel that is empty means
 * nothing happened, not that something failed. Raw ids, file paths and timestamps belong
 * here and nowhere else.
 */
import { Anchor, Badge, Divider, Group, Progress, Stack, Table, Text } from '@mantine/core';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';

import { sleeveFullName } from '@/lib/plain';

import type { OverviewPayload } from './api';
import { clock, modePhrase, systemStatus } from './story';

/**
 * The kinds of row this pane still answers for.
 *
 * None of them is reachable by clicking on Home any more — Home is the money, and the two
 * kinds it opens are `holding` and `transaction`.  These stay registered because nothing
 * was deleted: a link someone saved, or a page that deep-links into `?detail=status:now`,
 * still opens the block it always did.
 */
export const LEGACY_DETAIL_KINDS = ['status', 'check', 'incident', 'change', 'decision', 'limits'] as const;

export const HOME_DETAIL_TITLE: Record<string, string> = {
  status: 'How the system is doing, in detail',
  check: 'An order the safety check turned down',
  incident: 'Something that went wrong',
  change: 'A change to how the system works',
  decision: 'The last time the system thought about the market',
  limits: 'The limits it stops at',
};

export const HOME_DETAIL_SUBTITLE: Record<string, string> = {
  status:
    'Everything behind the status line: what is open, what is due to run, what the safety checks did, and what the thinking cost.',
  check:
    'The order that was refused, which rule refused it, and how serious it was. No order can skip these checks.',
  incident: 'What broke, how serious it is, and when it was first seen. It is still open.',
  change: 'What was changed, by whom, and what it touched.',
  decision: 'Which model answered, how long it took, and whether it had to escalate.',
  limits: 'How much of the pot may be in the market, and where trading stops on its own.',
};

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `${(value * 100).toFixed(1)}%`;
}

function Fact({ label, value, meaning }: { label: string; value: string; meaning?: string }) {
  return (
    <Stack gap={0}>
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

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Stack gap={4}>
      <Text fw={600} size="sm">
        {title}
      </Text>
      {children}
    </Stack>
  );
}

function Nothing({ children }: { children: ReactNode }) {
  return (
    <Text size="xs" c="dimmed">
      {children}
    </Text>
  );
}

/* ------------------------------------------------------------------ the technical block */

/**
 * What the health block, the incident list, the schedule, the funnel and the bill used to
 * say on the front page.
 *
 * One pane, in the order an operator would actually ask: what is wrong, what is open,
 * what did the checks do, what is due, what did the thinking cost, and what changed.
 */
function StatusDetail({ data, open }: { data: OverviewPayload; open: (kind: string, id: string) => void }) {
  const status = systemStatus(data);
  const funnel = data.funnel?.stages ?? [];
  const top = funnel[0]?.count ?? 0;

  return (
    <Stack gap="md">
      <Group gap="xs">
        <Badge color={status.ok ? 'teal' : 'orange'} variant="light" data-testid="status-detail-verdict">
          {status.headline}
        </Badge>
        {(data.flags?.active ?? []).map((flag) => (
          <Badge key={flag} size="xs" color="yellow" variant="light">
            {flag}
          </Badge>
        ))}
      </Group>

      <Section title="What needs attention">
        {status.reasons.length === 0 ? (
          <Nothing>Nothing. Every check the console can make came back fine.</Nothing>
        ) : (
          <Stack gap={4}>
            {status.reasons.map((reason) => (
              <Text key={reason} size="sm">
                {reason}
              </Text>
            ))}
          </Stack>
        )}
      </Section>

      <Section title="Which bot is on what money">
        <Stack gap={2}>
          {Object.entries(data.mode?.sleeves ?? {}).map(([sleeve, mode]) => (
            <Text key={sleeve} size="sm">
              {sleeveFullName(sleeve)}: {modePhrase(mode.state, mode.submode)}
              {mode.run_id ? (
                <Text span size="xs" c="dimmed" ff="monospace">
                  {' '}
                  · {mode.run_id}
                </Text>
              ) : null}
            </Text>
          ))}
          {data.mode?.verified === false ? (
            <Nothing>Could not be verified: {data.mode.reason}</Nothing>
          ) : null}
        </Stack>
      </Section>

      <Section title="Things that went wrong and are still open">
        {(data.incidents ?? []).length === 0 ? (
          <Nothing>Nothing is broken.</Nothing>
        ) : (
          <Stack gap={4}>
            {(data.incidents ?? []).map((row) => (
              <Group key={row.id} gap="xs" wrap="nowrap">
                <Badge size="xs" color={row.severity === 'critical' ? 'red' : 'yellow'}>
                  {row.severity}
                </Badge>
                <Anchor size="sm" onClick={() => open('incident', String(row.id))}>
                  {row.kind}: {row.detail}
                </Anchor>
              </Group>
            ))}
          </Stack>
        )}
      </Section>

      <Section title="What the safety checks did today">
        <Group gap={6}>
          <Badge variant="light">{data.gate?.allow ?? 0} let through</Badge>
          <Badge color="orange">{data.gate?.reject ?? 0} turned down</Badge>
          <Badge color="red">{data.gate?.breach ?? 0} hit a hard limit</Badge>
        </Group>
        {(data.gate?.recent ?? []).length === 0 ? (
          <Nothing>Every order the safety checks saw was allowed through.</Nothing>
        ) : (
          <Stack gap={2}>
            {(data.gate?.recent ?? []).map((row) => (
              <Anchor key={row.id} size="xs" onClick={() => open('check', String(row.id))}>
                {clock(row.ts_utc)} · {row.pair} · {row.reason}
              </Anchor>
            ))}
          </Stack>
        )}
      </Section>

      <Section title="Ideas spotted in the last day, and how many survived">
        {funnel.length === 0 ? (
          <Nothing>The scanner has not run yet today.</Nothing>
        ) : (
          funnel.map((stage) => (
            <Group key={stage.stage} gap="xs" wrap="nowrap">
              <Text size="xs" w={120}>
                {stage.stage}
              </Text>
              <Progress value={top ? (stage.count / top) * 100 : 0} style={{ flex: 1 }} color="indigo" />
              <Text size="xs" w={28} ta="right">
                {stage.count}
              </Text>
            </Group>
          ))
        )}
      </Section>

      <Section title="What is due to run next">
        {(data.schedule?.jobs ?? []).length === 0 ? (
          <Nothing>Nothing is scheduled.</Nothing>
        ) : (
          <Table verticalSpacing={2} fz="xs">
            <Table.Tbody>
              {(data.schedule?.jobs ?? []).map((job) => (
                <Table.Tr key={job.job}>
                  <Table.Td>{job.job}</Table.Td>
                  <Table.Td>
                    <Text size="xs" ff="monospace">
                      {job.cron}
                    </Text>
                  </Table.Td>
                  <Table.Td>{job.next_fire_local}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        )}
      </Section>

      <Section title="Which AI answered in the last day, and what it cost">
        {(data.provider?.usage ?? []).length === 0 ? (
          <Nothing>No model calls in the last day.</Nothing>
        ) : (
          <Stack gap={2}>
            {(data.provider?.usage ?? []).map((row) => (
              <Text key={row.provider ?? 'unknown'} size="xs">
                {row.provider ?? 'unknown'}: {row.ok}/{row.calls} answered, ${(row.cost ?? 0).toFixed(2)}
              </Text>
            ))}
          </Stack>
        )}
      </Section>

      <Section title="What changed today">
        {(data.changed_today?.config ?? []).length === 0 &&
        (data.changed_today?.changes ?? []).length === 0 ? (
          <Nothing>No settings were changed today.</Nothing>
        ) : (
          <Stack gap={2}>
            {(data.changed_today?.config ?? []).map((row) => (
              <Anchor key={row.id} size="xs" onClick={() => open('change', String(row.id))}>
                {clock(row.ts_utc)} · {row.file} · {row.changed_paths.join(', ')}
              </Anchor>
            ))}
            {(data.changed_today?.changes ?? []).map((row) => (
              <Text key={row.change_id} size="xs" c="dimmed">
                {row.change_id} · {row.kind} · {row.target} · {row.status}
              </Text>
            ))}
          </Stack>
        )}
      </Section>

      {data.generated_utc ? (
        <Fact
          label="Read at"
          value={`${data.generated_utc} UTC`}
          meaning="Home re-reads this every minute and whenever something happens."
        />
      ) : null}

      <Divider />
      <Group gap="md">
        <Anchor component={Link} to="/operations" size="sm">
          The machine, the jobs and the logs
        </Anchor>
        <Anchor component={Link} to="/invariants" size="sm">
          The safety rules and the code behind them
        </Anchor>
        <Anchor component={Link} to="/audit" size="sm">
          Everything anyone or anything did
        </Anchor>
      </Group>
    </Stack>
  );
}

function LimitsDetail({ data }: { data: OverviewPayload }) {
  return (
    <Stack gap="md">
      {data.limits ? (
        <Stack gap="xs">
          <Fact
            label="Trading stops for the day"
            value={`${pct(data.limits.daily_loss_stop)} down`}
            meaning="Measured from the value at the start of the day."
          />
          <Fact label="Trading stops for the month" value={`${pct(data.limits.monthly_loss_stop)} down`} />
          <Fact
            label="Always kept as cash"
            value={pct(data.limits.usdt_floor)}
            meaning="The pot is never fully in the market."
          />
          <Fact label="Most trades in a day" value={String(data.limits.max_trades_per_day)} />
        </Stack>
      ) : (
        <Nothing>The limits could not be read.</Nothing>
      )}

      <Divider />
      <Section title="How much is in the market, against the most allowed">
        {(data.exposure?.sleeves ?? []).length === 0 ? (
          <Nothing>Nothing is held yet, so everything is still cash.</Nothing>
        ) : (
          (data.exposure?.sleeves ?? []).map((sleeve) => (
            <Stack key={sleeve.sleeve} gap={4}>
              <Group justify="space-between">
                <Text size="sm" fw={600}>
                  {sleeveFullName(sleeve.sleeve)}
                </Text>
                <Text size="xs" c="dimmed" data-testid={`gross-${sleeve.sleeve}`}>
                  {sleeve.gross === null ? 'unknown' : pct(sleeve.gross)} of the pot is in the market,{' '}
                  {pct(sleeve.gross_cap)} is the most allowed
                </Text>
              </Group>
              {/* A null util means the server could not compute it. Drawing a 0% blue bar
                  would claim the sleeve is flat; grey and "unknown" say nothing. */}
              <Progress
                value={Math.min(100, (sleeve.gross_util ?? 0) * 100)}
                color={sleeve.gross_util === null ? 'gray' : sleeve.gross_util > 0.9 ? 'red' : 'blue'}
              />
              {sleeve.assets.map((asset) => (
                <Group key={asset.asset} justify="space-between" gap="xs">
                  <Text size="xs">{asset.asset}</Text>
                  <Text size="xs" c="dimmed" data-testid={`weight-${asset.asset}`}>
                    {asset.weight === null ? `${asset.amount ?? '—'} unmarked` : pct(asset.weight)} of the
                    pot, {pct(asset.cap)} is the most allowed
                  </Text>
                </Group>
              ))}
            </Stack>
          ))
        )}
      </Section>

      <Divider />
      <Anchor component={Link} to="/risk" size="sm">
        See every limit and every order the checks turned down
      </Anchor>
    </Stack>
  );
}

function DecisionDetail({ data }: { data: OverviewPayload }) {
  const last = data.research?.last;
  if (!last) return <Missing what="decision" />;
  return (
    <Stack gap="md">
      <Group gap="xs">
        <Badge color={last.status === 'success' ? 'teal' : 'orange'}>{last.status}</Badge>
        {last.escalated ? <Badge color="grape">escalated</Badge> : null}
      </Group>
      <Fact label="Started" value={last.started_utc} meaning="Times are UTC everywhere." />
      {last.finished_utc ? <Fact label="Finished" value={last.finished_utc} /> : null}
      <Fact
        label="Which model answered"
        value={`${last.served_model ?? 'unknown'}${last.provider ? ` · ${last.provider}` : ''}`}
        meaning="What it was asked for, and what actually answered."
      />
      {last.escalation_reasons.length > 0 ? (
        <Fact label="Why it escalated" value={last.escalation_reasons.join(', ')} />
      ) : null}
      {last.cost_usd !== null && last.cost_usd !== undefined ? (
        <Fact label="What it cost" value={`$${last.cost_usd.toFixed(2)}`} />
      ) : null}
      <Fact label="Known to the system as" value={last.run_id} />
      <Divider />
      <Anchor component={Link} to="/decisions" size="sm">
        The whole story of every decision
      </Anchor>
    </Stack>
  );
}

export function HomeDetail({
  kind,
  id,
  data,
  open,
}: {
  kind: string;
  id: string;
  data: OverviewPayload;
  /** Lets a row inside the pane open another row, without leaving the screen. */
  open: (kind: string, id: string) => void;
}) {
  if (kind === 'status') return <StatusDetail data={data} open={open} />;
  if (kind === 'limits') return <LimitsDetail data={data} />;
  if (kind === 'decision') return <DecisionDetail data={data} />;

  if (kind === 'check') {
    const row = (data.gate?.recent ?? []).find((item) => String(item.id) === id);
    if (!row) return <Missing what="refused order" />;
    return (
      <Stack gap="md">
        <Group gap="xs">
          <Badge variant="light">{sleeveFullName(row.sleeve)}</Badge>
          <Badge color={row.severity === 'breach' ? 'red' : 'orange'}>{row.severity}</Badge>
        </Group>
        <Group gap="xl" wrap="wrap">
          <Fact label="Coin" value={row.pair} />
          <Fact label="When" value={row.ts_utc} meaning="Times are UTC everywhere." />
          <Fact
            label="Why it was refused"
            value={row.reason}
            meaning="A rule written in code, not a judgement call. Claude cannot change it or skip it."
          />
          <Fact
            label="Which check refused it"
            value={row.callback}
            meaning="The exact place in the trading code where the order was stopped."
          />
        </Group>
        <Divider />
        <Anchor component={Link} to="/risk" size="sm">
          See every limit and every order the checks turned down
        </Anchor>
      </Stack>
    );
  }

  if (kind === 'incident') {
    const row = (data.incidents ?? []).find((item) => String(item.id) === id);
    if (!row) return <Missing what="incident" />;
    return (
      <Stack gap="md">
        <Group gap="xs">
          <Badge color={row.severity === 'critical' ? 'red' : 'yellow'}>{row.severity}</Badge>
          <Badge variant="light">{row.kind}</Badge>
          {row.subkind ? <Badge variant="light">{row.subkind}</Badge> : null}
        </Group>
        <Fact label="What happened" value={row.detail} />
        <Fact label="First seen" value={row.opened_utc} meaning="It has not been closed off yet." />
        <Divider />
        <Anchor component={Link} to="/operations" size="sm">
          Open the machine and jobs screen to close it
        </Anchor>
      </Stack>
    );
  }

  const row = (data.changed_today?.config ?? []).find((item) => String(item.id) === id);
  if (!row) return <Missing what="change" />;
  return (
    <Stack gap="md">
      <Group gap="xs">
        <Badge variant="light">{row.file}</Badge>
        {row.protected_changed ? <Badge color="orange">protected setting</Badge> : null}
        {row.applied ? <Badge color="teal">in effect</Badge> : <Badge color="gray">queued</Badge>}
      </Group>
      <Fact label="When" value={row.ts_utc} />
      <Fact label="Who" value={row.actor} meaning="Settings only change through this console." />
      <Fact label="What was touched" value={row.changed_paths.join(', ') || '—'} />
      {row.reason ? <Fact label="Reason given" value={row.reason} /> : null}
      <Divider />
      <Anchor component={Link} to="/audit" size="sm">
        See every action anyone or anything took, in one timeline
      </Anchor>
    </Stack>
  );
}

function Missing({ what }: { what: string }) {
  return (
    <Text size="sm" c="dimmed">
      That {what} is no longer in today&apos;s list. It may have rolled over into yesterday.
    </Text>
  );
}
