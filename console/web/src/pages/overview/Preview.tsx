/**
 * "What would it do right now" — the single best thing to look at before arming anything.
 *
 * This is not a simulation of the decision. `console/services/preview_service.py` runs the
 * real one: the same inputs, the same model chain including the escalation, the same
 * read-only tool set, the same schema check, and then the *real* safety checks loaded from
 * the real settings, evaluated against the book the bot last reported. Four writes are
 * stubbed and nothing else — no plan file, no record, no snapshot, and the safety checks'
 * own counters are read but never written back.
 *
 * So the panel's job is to show that honestly:
 *
 *   - the plan it would make, including a plain "it chose not to trade" when it declines;
 *   - what the safety checks would do with each target, and which check refused it;
 *   - who answered, what it cost, and that nothing was saved.
 *
 * It is marked as a preview everywhere, including on the row that says nothing was written,
 * because an operator who mistakes this for a decision would be making the opposite mistake
 * to the one the whole control exists to prevent.
 */
import { Alert, Badge, Button, Group, Loader, Stack, Table, Text, Tooltip } from '@mantine/core';
import { IconAlertTriangle, IconEye } from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { errorMessage } from '@/api';
import { sleeveName } from '@/lib/plain';

import { controlApi, controlKeys, type PreviewResult } from './control.api';
import { money } from './controlWords';

/** Which bot the preview is run for. The AI bot is the one that has a plan to preview. */
const PREVIEW_BOT = 'b';

export function Preview() {
  const queryClient = useQueryClient();
  const [result, setResult] = useState<PreviewResult | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const state = useQuery({
    queryKey: controlKeys.preview,
    queryFn: () => controlApi.preview(),
    refetchInterval: 60_000,
    retry: false,
  });

  const runIt = useMutation({
    mutationFn: () => controlApi.runPreview(PREVIEW_BOT),
    onSuccess: (value) => {
      setFailure(null);
      setResult(value);
      void queryClient.invalidateQueries({ queryKey: controlKeys.preview });
    },
    onError: (error: unknown) => {
      setFailure(errorMessage(error));
      void queryClient.invalidateQueries({ queryKey: controlKeys.preview });
    },
  });

  const budget = state.data?.budget ?? null;
  const waiting = budget ? !budget.ready : false;
  const waitMinutes = budget ? Math.ceil(budget.wait_s / 60) : 0;

  return (
    <Stack gap="xs" data-testid="preview-panel">
      <Group justify="space-between" wrap="wrap" gap="xs">
        <Stack gap={0}>
          <Text fw={600}>What would it do right now?</Text>
          <Text size="xs" c="dimmed">
            Runs the real thinking and the real safety checks, and buys nothing. Nothing it
            produces is saved or acted on.
          </Text>
        </Stack>
        <Tooltip
          label={
            waiting
              ? `Just ran one. ${waitMinutes} minute(s) to wait.`
              : budget
                ? `Costs at most ${money(budget.max_usd)}`
                : 'Runs the real decision, read-only'
          }
        >
          <Button
            size="xs"
            variant="light"
            leftSection={<IconEye size={14} />}
            loading={runIt.isPending}
            disabled={waiting || runIt.isPending || state.isError}
            onClick={() => runIt.mutate()}
            data-testid="preview-run"
          >
            Show me
          </Button>
        </Tooltip>
      </Group>

      {state.isError ? (
        <Text size="xs" c="dimmed" data-testid="preview-unavailable">
          The preview could not be reached.
        </Text>
      ) : null}

      {failure ? (
        <Alert color="yellow" icon={<IconAlertTriangle size={16} />} data-testid="preview-error">
          {failure}
        </Alert>
      ) : null}

      {runIt.isPending ? (
        <Group gap="xs">
          <Loader size="xs" />
          <Text size="sm" c="dimmed">
            Thinking — this takes a few minutes, the same as a real run.
          </Text>
        </Group>
      ) : null}

      {result ? <PreviewBody result={result} /> : null}

      {!result && budget?.last_started_utc ? (
        <Text size="xs" c="dimmed">
          Last looked at {new Date(budget.last_started_utc).toLocaleString()}
          {budget.last_cost_usd !== null ? ` · cost ${money(budget.last_cost_usd)}` : ''}
        </Text>
      ) : null}
    </Stack>
  );
}

function PreviewBody({ result }: { result: PreviewResult }) {
  if (!result.ok) {
    return (
      <Alert color="yellow" icon={<IconAlertTriangle size={16} />} data-testid="preview-failed">
        It could not produce a plan: {result.error}. Nothing was saved and nothing was bought.
      </Alert>
    );
  }
  const plan = result.plan;
  const gate = result.gate;
  const targets = Object.entries(plan?.targets ?? {}).filter(([, weight]) => weight > 0);

  return (
    <Stack gap="xs" data-testid="preview-result">
      <Group gap="xs">
        <Badge color="grape" variant="light" data-testid="preview-badge">
          Preview — nothing was placed or saved
        </Badge>
        <Badge color="gray" variant="light">
          for {sleeveName(PREVIEW_BOT)}
        </Badge>
      </Group>

      {plan?.abstain ? (
        <Alert color="blue" data-testid="preview-abstain">
          It chose not to trade. That is the safe answer when the numbers it reads are stale
          or point in different directions.
          {plan.rationale?.length ? ` It said: ${plan.rationale.join(' ')}` : ''}
        </Alert>
      ) : (
        <>
          <Text size="sm" fw={500}>
            It would aim to hold:
          </Text>
          <Table withTableBorder withColumnBorders data-testid="preview-targets">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Coin</Table.Th>
                <Table.Th>Share of the money</Table.Th>
                <Table.Th>What the safety checks say</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {targets.map(([asset, weight]) => {
                const verdict = gate?.verdicts.find((entry) => entry.pair.startsWith(`${asset}/`));
                return (
                  <Table.Tr key={asset}>
                    <Table.Td>{asset}</Table.Td>
                    <Table.Td>{(weight * 100).toFixed(1)}%</Table.Td>
                    <Table.Td>
                      {!verdict ? (
                        <Text size="sm" c="dimmed">
                          not checked — nothing would be bought
                        </Text>
                      ) : verdict.allowed ? (
                        <Text size="sm" c="teal">
                          would be allowed ({money(verdict.stake_usdt)})
                        </Text>
                      ) : (
                        <Text size="sm" c="orange">
                          would be refused — {refusalText(verdict.reason)}
                        </Text>
                      )}
                    </Table.Td>
                  </Table.Tr>
                );
              })}
            </Table.Tbody>
          </Table>
        </>
      )}

      {plan && !plan.abstain && plan.rationale?.length ? (
        <Stack gap={2} data-testid="preview-reasoning">
          <Text size="sm" fw={500}>
            Why:
          </Text>
          {plan.rationale.map((line, index) => (
            <Text size="sm" key={index}>
              {line}
            </Text>
          ))}
          {plan.invalidation ? (
            <Text size="sm" c="dimmed">
              It would change its mind if: {plan.invalidation}
            </Text>
          ) : null}
        </Stack>
      ) : null}

      {gate && !gate.available ? (
        <Text size="xs" c="orange">
          {gate.reason}
        </Text>
      ) : null}
      {gate?.available && gate.nav_valid === false ? (
        <Text size="xs" c="orange" data-testid="preview-no-book">
          The safety checks refused everything because this bot has no recorded value yet
          ({gate.nav_reason}). That is what would really happen.
        </Text>
      ) : null}

      <Text size="xs" c="dimmed" data-testid="preview-footer">
        Answered by {result.served_model ?? result.requested_model}
        {result.effort ? ` at ${result.effort} effort` : ''} · cost {money(result.cost_usd)} ·
        nothing was written
      </Text>
    </Stack>
  );
}

/**
 * The safety check's own reason, said in words.
 *
 * The raw reason is kept on the end because it is what the logs and the records say, and
 * hiding it would make this screen impossible to reconcile with them.
 */
export function refusalText(reason: string): string {
  const [check] = reason.split(':');
  const said: Record<string, string> = {
    nav_valid: 'it has no recorded value to size against',
    kill: 'the emergency stop is on',
    monthly_lock: 'it is stopped for the month after a loss',
    daily_lock: 'it is stopped for the day after a loss',
    blackout: 'this coin is blocked right now',
    staleness: 'the price data is too old',
    reconcile: 'its books and the exchange disagree',
    exit_only: 'this coin may only be sold, not bought',
    tier: 'this coin is not one it is allowed to buy',
    trades_per_day: 'it has already traded enough today',
    orders_per_day: 'it has already placed enough orders today',
    turnover_day: 'it has already moved enough money today',
    fee_budget: 'the fees this month are at their ceiling',
    min_notional: 'the order would be too small to place',
    step_size: 'the order would be smaller than the exchange allows',
    order_notional: 'the order would be too big a single bite',
    entries_per_trade: 'it has already added to this position enough times',
    min_position: 'the position would be too small to manage',
    max_positions: 'it already holds as many coins as it may',
    satellite_count: 'it already holds as many smaller coins as it may',
    satellite_gross: 'the smaller coins already take up their whole share',
    weight_cap: 'that would be too much of one coin',
    beta_cap: 'the book would move too much with BTC',
    corr_cap: 'the coins would be too alike',
    gross_cap: 'too much of the money would be in the market',
    usdt_floor: 'it would leave too little cash',
  };
  const plain = said[check ?? ''];
  return plain ? `${plain} (${reason})` : reason;
}
