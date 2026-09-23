import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  Grid,
  Group,
  List,
  NumberInput,
  Radio,
  Stack,
  Text,
  TextInput,
  ThemeIcon,
  Title,
  Tooltip,
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconArrowRight,
  IconCheck,
  IconLock,
  IconMinus,
  IconX,
} from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useMemo, useState } from 'react';

import { errorMessage } from '@/api';
import { usePageCommands } from '@/app/commandRegistry';
import { useTopicEvents } from '@/app/EventStreamContext';
import { useSession } from '@/app/SessionContext';
import { DataTable, EmptyState } from '@/components';

import {
  allowedTargets,
  confirmPhraseFor,
  DEFAULT_CONFIRM_TEMPLATE,
  isLive,
  isTransient,
  modeApi,
  STEP_LABELS,
  stateColour,
  statusColour,
  TRANSITION_STEPS,
  type ModeTarget,
  type PreflightResponse,
  type SleeveId,
  type SleeveModeState,
  type TransitionRow,
  type TransitionStep,
} from './api';
import { PreflightPanel } from './PreflightPanel';

/**
 * Mode & Live (spec 12 page 18).
 *
 * The page is deliberately a one-way street with three gates: run the preflight, read the
 * evidence, then type the phrase. Nothing here can put a sleeve live without all three,
 * and the transition itself re-checks the preflight at the moment of the switch — so a
 * checklist that went green ten minutes ago is not authority, it is a starting point.
 */
export default function ModeLivePage() {
  const queryClient = useQueryClient();

  const mode = useQuery({ queryKey: ['mode'], queryFn: modeApi.read, refetchInterval: 15_000 });
  const history = useQuery({ queryKey: ['mode', 'transitions'], queryFn: () => modeApi.history() });

  const [live, setLive] = useState<TransitionStep[]>([]);

  useTopicEvents(['transition', 'mode'], (event) => {
    if (event.topic === 'mode') {
      void queryClient.invalidateQueries({ queryKey: ['mode'] });
      return;
    }
    const payload = event.payload as unknown as TransitionStep;
    if (!payload?.step) return;
    setLive((current) => [...current.filter((s) => s.step !== payload.step), payload]);
  });

  usePageCommands('mode-live', [
    {
      id: 'refresh',
      title: 'Re-read the mode file and transition history',
      run: (ctx) => {
        void mode.refetch();
        void history.refetch();
        ctx.close();
      },
    },
    {
      id: 'clear-progress',
      title: 'Clear the live transition progress',
      subtitle: 'Forget the steps streamed so far; the transition itself is untouched',
      run: (ctx) => {
        setLive([]);
        ctx.close();
      },
    },
  ]);

  const sleeves = mode.data?.sleeves ?? [];

  return (
    <Stack gap="lg">
      <Group justify="space-between" align="flex-start">
        <div>
          <Title order={3}>Mode &amp; Live</Title>
          <Text size="sm" c="dimmed">
            Per-sleeve state machine. The mode lives in a signed file, not in the config, so
            no config save can move it and no automated run can write it.
          </Text>
        </div>
        {mode.data && !mode.data.verified ? (
          <Alert color="orange" variant="light" title="Mode file unverified" maw={480}>
            Reason: <b>{mode.data.reason}</b>. Every sleeve therefore reads as TEST — the
            system fails closed.
          </Alert>
        ) : null}
      </Group>

      <Grid>
        {sleeves.map((sleeve) => (
          <Grid.Col key={sleeve.sleeve} span={{ base: 12, lg: 6 }}>
            <SleevePanel
              sleeve={sleeve}
              liveSteps={live}
              onDone={() => {
                setLive([]);
                void queryClient.invalidateQueries({ queryKey: ['mode'] });
                void history.refetch();
              }}
            />
          </Grid.Col>
        ))}
        {sleeves.length === 0 && !mode.isLoading ? (
          <Grid.Col span={12}>
            <EmptyState title="No sleeves" description="The mode file lists no sleeves." />
          </Grid.Col>
        ) : null}
      </Grid>

      <TransitionHistory rows={history.data ?? []} loading={history.isLoading} />
    </Stack>
  );
}

// --------------------------------------------------------------------------- one sleeve

function SleevePanel({
  sleeve,
  liveSteps,
  onDone,
}: {
  sleeve: SleeveModeState;
  liveSteps: TransitionStep[];
  onDone: () => void;
}) {
  const { stepUpActive } = useSession();
  const targets = allowedTargets(sleeve.state);
  const [target, setTarget] = useState<ModeTarget>(targets[0] ?? 'TEST');
  const [seed, setSeed] = useState<number>(Math.min(sleeve.seed_usdt, sleeve.max_seed_usdt));
  const [flatten, setFlatten] = useState(true);
  const [phrase, setPhrase] = useState('');
  const [overrideReason, setOverrideReason] = useState('');
  const [preflight, setPreflight] = useState<PreflightResponse | null>(null);
  const [expiresIn, setExpiresIn] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setPreflight(null);
    setPhrase('');
  }, [target]);

  useEffect(() => {
    if (!preflight) {
      setExpiresIn(null);
      return;
    }
    const tick = () => {
      const left = Math.floor((Date.parse(preflight.expires_utc) - Date.now()) / 1000);
      setExpiresIn(Math.max(0, left));
    };
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [preflight]);

  const goingLive = isLive(target);
  const expected = useMemo(
    () =>
      preflight?.confirm_phrase ??
      confirmPhraseFor({
        sleeve: sleeve.sleeve,
        from: sleeve.state,
        target,
        seed,
        flatten,
        template: DEFAULT_CONFIRM_TEMPLATE,
      }),
    [preflight, sleeve.sleeve, sleeve.state, target, seed, flatten],
  );

  const runPreflight = useMutation({
    mutationFn: () =>
      modeApi.preflight({
        sleeve: sleeve.sleeve as SleeveId,
        target,
        submode: target === 'LIVE_EXECUTE' ? 'execute' : target === 'LIVE_PROPOSE' ? 'propose' : null,
        seed_usdt: goingLive ? seed : null,
        override_reason: overrideReason || null,
      }),
    onSuccess: (result) => {
      setPreflight(result);
      setError(null);
    },
    onError: (e) => setError(errorMessage(e)),
  });

  const transition = useMutation({
    mutationFn: () =>
      modeApi.transition({
        sleeve: sleeve.sleeve as SleeveId,
        target,
        submode: target === 'LIVE_EXECUTE' ? 'execute' : target === 'LIVE_PROPOSE' ? 'propose' : null,
        seed_usdt: goingLive ? seed : null,
        preflight_id: preflight?.preflight_id ?? null,
        confirm_phrase: phrase,
        flatten: target === 'TEST' ? flatten : null,
        override_reason: overrideReason || null,
      }),
    onSuccess: () => {
      setError(null);
      setPhrase('');
      onDone();
    },
    onError: (e) => setError(errorMessage(e)),
  });

  const seedTooBig = goingLive && seed > sleeve.max_seed_usdt;
  const ready =
    stepUpActive &&
    phrase.trim() === expected &&
    (!goingLive || (preflight?.ok === true && (expiresIn ?? 0) > 0)) &&
    !seedTooBig;

  const steps = liveSteps.length > 0 ? liveSteps : transition.data?.steps ?? [];

  return (
    <Card withBorder radius="md" padding="lg">
      <Stack gap="md">
        <Group justify="space-between" align="flex-start">
          <div>
            <Group gap="xs">
              <Title order={4}>Sleeve {sleeve.sleeve.toUpperCase()}</Title>
              <Badge color={stateColour(sleeve.state)} variant="filled">
                {sleeve.state}
                {sleeve.submode ? `·${sleeve.submode}` : ''}
              </Badge>
              {sleeve.transition_in_progress || isTransient(sleeve.state) ? (
                <Badge color="yellow" variant="dot">
                  transitioning
                </Badge>
              ) : null}
            </Group>
            <Text size="sm" c="dimmed">
              run {sleeve.run_id ?? '—'} · seed {sleeve.seed_usdt.toLocaleString()} USDT
              {sleeve.days !== null ? ` · day ${Math.floor(sleeve.days)}` : ''}
              {sleeve.label ? ` · ${sleeve.label}` : ''}
            </Text>
          </div>
          <StateDiagram current={sleeve.state} />
        </Group>

        <Radio.Group
          label="Target state"
          value={target}
          onChange={(value) => setTarget(value as ModeTarget)}
        >
          <Group gap="lg" mt="xs">
            {targets.map((option) => (
              <Radio key={option} value={option} label={option} />
            ))}
          </Group>
        </Radio.Group>

        {goingLive ? (
          <NumberInput
            label="Live seed (USDT)"
            description={`Hard ceiling ${sleeve.max_seed_usdt.toLocaleString()} USDT (modes.live.max_seed_usdt)`}
            value={seed}
            onChange={(value) => setSeed(Number(value) || 0)}
            min={0}
            max={sleeve.max_seed_usdt}
            error={seedTooBig ? 'above the configured ceiling' : undefined}
            thousandSeparator
          />
        ) : null}

        {target === 'TEST' && isLive(sleeve.state) ? (
          <Checkbox
            checked={flatten}
            onChange={(event) => setFlatten(event.currentTarget.checked)}
            label="Flatten every position on the way out"
            description={
              flatten
                ? 'Force-exits all open trades and waits until the sleeve is flat.'
                : 'Positions are left on the exchange with no bot managing them — you must type LEAVE POSITIONS UNMANAGED.'
            }
          />
        ) : null}

        {goingLive ? (
          <PreflightPanel
            result={preflight}
            running={runPreflight.isPending}
            error={runPreflight.isError ? errorMessage(runPreflight.error) : null}
            overrideReason={overrideReason}
            onOverrideReason={setOverrideReason}
            onRun={() => runPreflight.mutate()}
            expiresIn={expiresIn}
          />
        ) : null}

        {expected ? (
          <TextInput
            label="Type the confirmation phrase"
            description={expected}
            value={phrase}
            onChange={(event) => setPhrase(event.currentTarget.value)}
            error={phrase && phrase.trim() !== expected ? 'does not match' : undefined}
          />
        ) : null}

        {!stepUpActive ? (
          <Alert color="gray" variant="light" icon={<IconLock size={16} />}>
            A mode transition needs step-up re-authentication. Use the lock in the header.
          </Alert>
        ) : null}

        {error ? (
          <Alert color="red" variant="light" title="Transition refused">
            {error}
          </Alert>
        ) : null}

        <Group justify="flex-end">
          <Tooltip
            label="Run the preflight, type the phrase and step up first"
            disabled={ready}
          >
            <Button
              color={goingLive ? 'red' : 'blue'}
              rightSection={<IconArrowRight size={16} />}
              disabled={!ready || transition.isPending}
              loading={transition.isPending}
              onClick={() => transition.mutate()}
            >
              {goingLive ? `Arm sleeve ${sleeve.sleeve.toUpperCase()}` : 'Back to TEST'}
            </Button>
          </Tooltip>
        </Group>

        {steps.length > 0 || transition.isPending ? <StepProgress steps={steps} /> : null}
      </Stack>
    </Card>
  );
}

// --------------------------------------------------------------------------- pieces

const DIAGRAM: string[] = ['TEST', 'ARMING', 'LIVE_PROPOSE', 'LIVE_EXECUTE'];

function StateDiagram({ current }: { current: string }) {
  return (
    <Group gap={4} wrap="nowrap">
      {DIAGRAM.map((state) => (
        <Badge
          key={state}
          size="xs"
          variant={state === current ? 'filled' : 'outline'}
          color={state === current ? stateColour(state) : 'gray'}
        >
          {state.replace('LIVE_', '')}
        </Badge>
      ))}
    </Group>
  );
}

function StepProgress({ steps }: { steps: TransitionStep[] }) {
  const byName = new Map(steps.map((step) => [step.step, step]));
  const extra = steps.filter((step) => !TRANSITION_STEPS.includes(step.step as never));
  return (
    <Card withBorder radius="sm" padding="sm">
      <Text size="xs" c="dimmed" tt="uppercase" fw={600} mb="xs">
        Transition progress
      </Text>
      <List spacing={4} size="sm" center>
        {[...TRANSITION_STEPS, ...extra.map((step) => step.step)].map((name) => {
          const step = byName.get(name);
          const status = step?.status ?? 'pending';
          return (
            <List.Item
              key={name}
              icon={
                <ThemeIcon size={18} radius="xl" color={statusColour(status)} variant="light">
                  {status === 'ok' ? (
                    <IconCheck size={12} />
                  ) : status === 'failed' ? (
                    <IconX size={12} />
                  ) : status === 'warn' ? (
                    <IconAlertTriangle size={12} />
                  ) : (
                    <IconMinus size={12} />
                  )}
                </ThemeIcon>
              }
            >
              <Group gap={6}>
                <Text size="sm">{STEP_LABELS[name] ?? name}</Text>
                {step?.detail ? (
                  <Text size="xs" c="dimmed">
                    {step.detail}
                  </Text>
                ) : null}
              </Group>
            </List.Item>
          );
        })}
      </List>
    </Card>
  );
}

function TransitionHistory({ rows, loading }: { rows: TransitionRow[]; loading: boolean }) {
  return (
    <Card withBorder radius="md" padding="lg">
      <Title order={5} mb="sm">
        Transition history
      </Title>
      <DataTable
        rows={rows}
        loading={loading}
        rowKey={(row) => String(row.id)}
        emptyTitle="No transitions yet"
        emptyDescription="Every arm, disarm and rollback is journalled here with its twelve steps."
        columns={[
          {
            key: 'started',
            header: 'Started',
            render: (row) => <Text size="sm">{row.started_utc}</Text>,
            sortValue: (row) => row.started_utc,
          },
          { key: 'sleeve', header: 'Sleeve', render: (row) => row.sleeve.toUpperCase() },
          {
            key: 'move',
            header: 'Move',
            render: (row) => (
              <Group gap={4}>
                <Badge size="xs" variant="outline">
                  {row.from_state}
                </Badge>
                <IconArrowRight size={12} />
                <Badge size="xs" color={stateColour(row.to_state)} variant="light">
                  {row.to_state}
                </Badge>
              </Group>
            ),
          },
          {
            key: 'status',
            header: 'Status',
            render: (row) => (
              <Badge
                size="xs"
                color={
                  row.status === 'completed'
                    ? 'teal'
                    : row.status === 'running'
                      ? 'yellow'
                      : 'red'
                }
                variant="light"
              >
                {row.status}
              </Badge>
            ),
          },
          { key: 'actor', header: 'Actor', render: (row) => <Text size="xs">{row.actor}</Text> },
          {
            key: 'steps',
            header: 'Steps',
            render: (row) => (
              <Text size="xs" c="dimmed">
                {row.steps.length} · {row.error ?? 'no error'}
              </Text>
            ),
          },
        ]}
      />
    </Card>
  );
}
