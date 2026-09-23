/**
 * The reasoning behind one row — the only place depth lives on Home.
 *
 * The owner's words were *"if i double click i should see reasoning"*, and this is what
 * that means in full: what the local model noticed, what Claude concluded and why, the
 * plan it produced, what the safety check decided, and the numbers each of those steps
 * used.  Home itself is four things and nothing else; everything a person might want to
 * ask about a holding or a trade is answered here instead of on the screen.
 *
 * It is deep-linkable because the pane holds no state: the open row lives in
 * `?detail=holding:<key>` / `?detail=transaction:<key>` (`app/detailParam.ts`), so a
 * refresh, the back button and a pasted link all land on the same reasoning.
 *
 * Raw identifiers — the run id, the model that answered, the exact safety callback — are
 * allowed *here*, at the foot and in the dimmed second line, and nowhere on the screen
 * behind it.
 */
import { Badge, Divider, Group, Loader, Progress, Stack, Text } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import type { ReactNode } from 'react';

import { formatUtcStamp } from '@/lib/format';

import type { GateDecision } from '../risk/api';
import { MoneyBadge } from './MoneyBadge';
import { overviewApi, overviewKeys } from './api';
import { money, signedPct } from './money';

/** What the pane is open on: enough to fetch the story, and the row's own numbers. */
export interface ReasoningTarget {
  kind: 'holding' | 'transaction';
  /** Which bot the row belongs to. */
  sleeve: string;
  /** `BTC/USDT` — used to find the safety decisions about this coin. */
  pair: string;
  asset: string;
  /** The decision this row came from, or `null` when it came from the fixed rules. */
  runId: string | null;
  /** The row's own numbers, already in money. */
  facts: Array<{ label: string; value: string; meaning?: string }>;
  mode: string | null;
}

/* ---------------------------------------------------------------------------- plumbing */

function text(source: Record<string, unknown> | null | undefined, key: string): string | null {
  const value = source?.[key];
  if (value === null || value === undefined) return null;
  const out = String(value).trim();
  return out === '' ? null : out;
}

function number(source: Record<string, unknown> | null | undefined, key: string): number | null {
  const value = source?.[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** The steps a decision goes through, in words. */
export const STAGE_LABEL: Record<string, string> = {
  scan: 'The scanner looked for anything unusual',
  screen: 'The local model sifted what it found',
  validate: 'A second opinion checked the idea held up',
  decide: 'Claude decided what to hold',
  plan: 'The plan was turned into orders',
  review: 'The result was graded afterwards',
};

export function stageLabel(stage: string): string {
  return STAGE_LABEL[String(stage).toLowerCase()] ?? `Step: ${stage}`;
}

/** "the local model on this machine" / "Claude" — who answered, without a model id. */
export function whoAnswered(provider: string | null, model: string | null): string {
  const p = String(provider ?? '').toLowerCase();
  if (p.includes('ollama') || p.includes('local') || p.includes('llama')) {
    return 'the local model on this machine';
  }
  if (p.includes('anthropic') || p.includes('claude')) return 'Claude';
  if (p) return p;
  return model ? 'a model' : 'nothing recorded';
}

function Fact({ label, value, meaning }: { label: string; value: ReactNode; meaning?: string }) {
  return (
    <Stack gap={0} data-testid={`reason-fact-${label.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`}>
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
    <Stack gap={6}>
      <Divider label={title} labelPosition="left" />
      {children}
    </Stack>
  );
}

function Nothing({ children }: { children: ReactNode }) {
  return (
    <Text size="sm" c="dimmed">
      {children}
    </Text>
  );
}

/* ------------------------------------------------------------------------------ the pane */

export function Reasoning({ target }: { target: ReasoningTarget }) {
  const run = useQuery({
    queryKey: overviewKeys.run(target.runId ?? ''),
    queryFn: () => overviewApi.run(target.runId as string),
    enabled: Boolean(target.runId),
    retry: false,
  });

  const gate = useQuery({
    queryKey: overviewKeys.gate(target.sleeve, target.pair),
    queryFn: () => overviewApi.gateDecisions({ sleeve: target.sleeve, pair: target.pair, limit: 25 }),
    retry: false,
  });

  const detail = run.data ?? null;
  const signal = (detail?.signal ?? null) as Record<string, unknown> | null;
  const proposal = detail?.proposal ?? null;
  const gateRows: GateDecision[] = (gate.data?.rows ?? []).filter(
    (row) => !target.runId || !row.run_id || row.run_id === target.runId,
  );

  return (
    <Stack gap="md" data-testid="reasoning">
      <Group gap="xs">
        <Badge variant="light">{target.asset}</Badge>
        <MoneyBadge mode={target.mode} />
      </Group>

      <Section title="The numbers on this row">
        <Group gap="xl" wrap="wrap">
          {target.facts.map((fact) => (
            <Fact
              key={fact.label}
              label={fact.label}
              value={fact.value}
              {...(fact.meaning ? { meaning: fact.meaning } : {})}
            />
          ))}
        </Group>
      </Section>

      {!target.runId ? (
        <Section title="Why it happened">
          <Nothing>
            No thinking is recorded against this one. It came from the bot&apos;s own fixed rules —
            the price crossed a line and the rule fired — so there is nothing the AI decided here.
          </Nothing>
        </Section>
      ) : run.isLoading ? (
        <Group justify="center" p="md">
          <Loader size="sm" />
        </Group>
      ) : run.isError || !detail ? (
        <Section title="Why it happened">
          <Nothing>
            The thinking behind this one could not be read back. It may have rolled out of the
            window the console keeps.
          </Nothing>
        </Section>
      ) : (
        <>
          <Section title="What the local model noticed">
            {signal === null ? (
              <Nothing>
                Nothing was flagged first — this decision ran on its schedule rather than because
                something was spotted.
              </Nothing>
            ) : (
              <Stack gap="xs">
                <Text size="sm" data-testid="reason-noticed">
                  {text(signal, 'detector')
                    ? `The ${text(signal, 'detector')} check flagged ${text(signal, 'pair') ?? target.asset}`
                    : `Something was flagged on ${text(signal, 'pair') ?? target.asset}`}
                  {text(signal, 'direction') ? `, pointing ${text(signal, 'direction')}` : ''}.
                </Text>
                <Group gap="xl" wrap="wrap">
                  {number(signal, 'detector_score') !== null ? (
                    <Fact
                      label="How strong the signal was"
                      value={(number(signal, 'detector_score') as number).toFixed(2)}
                      meaning="Higher means the pattern stood out more against its own history."
                    />
                  ) : null}
                  {number(signal, 'screen_score') !== null ? (
                    <Fact
                      label="What the local model scored it"
                      value={(number(signal, 'screen_score') as number).toFixed(2)}
                      meaning="The first sift, run on this machine so it costs nothing."
                    />
                  ) : null}
                </Group>
                {text(signal, 'screen_rationale') ? (
                  <Text size="sm" data-testid="reason-local-rationale">
                    {text(signal, 'screen_rationale')}
                  </Text>
                ) : null}
                {text(signal, 'screen_model') ? (
                  <Text size="xs" c="dimmed">
                    Answered by {whoAnswered(text(signal, 'screen_provider'), text(signal, 'screen_model'))}{' '}
                    ({text(signal, 'screen_model')}).
                  </Text>
                ) : null}
              </Stack>
            )}
          </Section>

          <Section title="What Claude concluded, and why">
            {proposal === null ? (
              <Nothing>Claude did not get as far as a conclusion on this run.</Nothing>
            ) : proposal.abstain ? (
              <Stack gap="xs">
                <Text size="sm" data-testid="reason-conclusion">
                  Claude chose not to trade. Doing nothing is the safe default when the inputs are
                  stale or disagree with each other.
                </Text>
                {proposal.rationale.map((line, index) => (
                  <Text key={index} size="sm" c="dimmed">
                    {line}
                  </Text>
                ))}
              </Stack>
            ) : (
              <Stack gap="xs">
                <Text size="sm" data-testid="reason-conclusion">
                  Claude concluded the market was behaving like{' '}
                  {proposal.module ?? 'no named pattern'}
                  {proposal.confidence === null || proposal.confidence === undefined
                    ? ''
                    : `, and was ${Math.round(proposal.confidence * 100)}% sure of it`}
                  .
                </Text>
                {proposal.rationale.length === 0 ? (
                  <Nothing>No reasons were written down for this one.</Nothing>
                ) : (
                  proposal.rationale.map((line, index) => (
                    <Text key={index} size="sm">
                      {line}
                    </Text>
                  ))
                )}
              </Stack>
            )}
          </Section>

          <Section title="The plan it produced">
            {proposal === null || Object.keys(proposal.targets ?? {}).length === 0 ? (
              <Nothing>No plan came out of this run, so nothing was asked of the exchange.</Nothing>
            ) : (
              <Stack gap={6} data-testid="reason-plan">
                {Object.entries(proposal.targets).map(([pair, weight]) => (
                  <Group key={pair} gap="xs" wrap="nowrap">
                    <Text size="xs" w={90}>
                      {pair}
                    </Text>
                    <Progress value={Math.min(100, Math.abs(weight) * 100)} style={{ flex: 1 }} />
                    <Text size="xs" w={64} ta="right">
                      {(weight * 100).toFixed(1)}% of the pot
                    </Text>
                  </Group>
                ))}
                <Group gap="xl" wrap="wrap" pt={4}>
                  {proposal.exposure_scale !== null && proposal.exposure_scale !== undefined ? (
                    <Fact
                      label="How much of that to actually take"
                      value={`${Math.round(proposal.exposure_scale * 100)}%`}
                      meaning="The plan is scaled back when conditions are rough."
                    />
                  ) : null}
                  {proposal.horizon_days !== null && proposal.horizon_days !== undefined ? (
                    <Fact label="How long it expected to hold" value={`${proposal.horizon_days} days`} />
                  ) : null}
                </Group>
                {proposal.invalidation ? (
                  <Fact
                    label="What would prove it wrong"
                    value={proposal.invalidation}
                    meaning="The condition that ends the idea early."
                  />
                ) : null}
              </Stack>
            )}
          </Section>

          <Section title="The steps it went through">
            {detail.stages.length === 0 ? (
              <Nothing>No steps were recorded for this run.</Nothing>
            ) : (
              <Stack gap={4}>
                {detail.stages.map((stage) => (
                  <Stack key={`${stage.stage}-${stage.started_utc}`} gap={0}>
                    <Group gap="xs" wrap="nowrap">
                      <Badge size="xs" color={stage.status === 'success' ? 'teal' : 'orange'}>
                        {stage.status}
                      </Badge>
                      <Text size="sm">{stageLabel(stage.stage)}</Text>
                    </Group>
                    <Text size="xs" c="dimmed">
                      {whoAnswered(stage.provider, stage.served_model)} answered
                      {stage.served_model ? ` (${stage.served_model})` : ''}
                      {stage.cost_usd === null || stage.cost_usd === undefined
                        ? ''
                        : `, costing ${money(stage.cost_usd)}`}
                      {stage.escalated ? ', after being asked again by a stronger model' : ''}.
                    </Text>
                  </Stack>
                ))}
              </Stack>
            )}
          </Section>
        </>
      )}

      <Section title="What the safety check decided">
        {gate.isLoading ? (
          <Group justify="center" p="sm">
            <Loader size="xs" />
          </Group>
        ) : gateRows.length === 0 ? (
          <Nothing>
            No safety decision is on record for this coin. Every order still passes the checks —
            this one simply predates what the console keeps.
          </Nothing>
        ) : (
          <Stack gap={6} data-testid="reason-safety">
            {gateRows.slice(0, 8).map((row) => (
              <Stack key={row.id} gap={0}>
                <Group gap="xs" wrap="nowrap">
                  <Badge size="xs" color={row.allowed ? 'teal' : row.severity === 'breach' ? 'red' : 'orange'}>
                    {row.allowed ? 'let through' : 'turned down'}
                  </Badge>
                  <Text size="sm">{row.reason}</Text>
                </Group>
                <Text size="xs" c="dimmed">
                  {formatUtcStamp(row.ts_utc)}
                  {row.proposed_stake === null ? '' : ` · asked for ${money(row.proposed_stake)}`}
                  {row.nav === null ? '' : ` · out of ${money(row.nav)}`}
                  {row.gross_exposure === null
                    ? ''
                    : ` · ${signedPct(row.gross_exposure).replace('+', '')} already in the market`}
                  {` · checked by ${row.callback}`}
                </Text>
              </Stack>
            ))}
          </Stack>
        )}
      </Section>
    </Stack>
  );
}
