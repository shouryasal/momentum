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
import { ConfirmDialog, DataTable, EmptyState } from '@/components';

import {
  allowedTargets,
  badgeFor,
  confirmPhraseFor,
  DEFAULT_CONFIRM_TEMPLATE,
  DEFAULT_DEMO_CONFIRM_TEMPLATE,
  isDemo,
  isLive,
  isTransient,
  isVenueBound,
  MODE_MEANING,
  modeApi,
  STEP_LABELS,
  stateColour,
  statusColour,
  TRANSITION_STEPS,
  type ModeBadge,
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
  const [recovering, setRecovering] = useState(false);

  /**
   * `POST /api/mode/recover` — step-up guarded, and until now called by nothing.
   *
   * An operator staring at a pulsing TRANSITIONING badge after a crash had no console
   * path to clear it; the endpoint existed only for start-up recovery.
   */
  const recover = useMutation({
    mutationFn: () => modeApi.recover(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['mode'] });
      void history.refetch();
    },
  });

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
  const stuck = sleeves.filter((s) => isTransient(s.state));
  const demoSleeves = sleeves.filter((s) => s.is_demo ?? isDemo(s.state));

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

      {demoSleeves.length > 0 ? (
        <Alert
          color="violet"
          variant="light"
          title={`Sleeve ${demoSleeves
            .map((s) => s.sleeve.toUpperCase())
            .join(', ')} is on DEMO — real orders, fake money`}
          data-testid="demo-page-banner"
        >
          Demo Mode places genuine orders against{' '}
          <b>{demoSleeves[0].venue_host ?? 'demo-api.binance.com'}</b>, with the live
          venue&apos;s prices, order book, filters and rate limits — and balances that are
          not real. Treat everything it produces as a <b>rehearsal</b>: demo P&amp;L is
          never live performance, and a balance reset is a run boundary, so drop to TEST
          before asking Binance for one.
        </Alert>
      ) : null}

      {stuck.length > 0 ? (
        <Alert
          color="yellow"
          variant="light"
          title={`Sleeve ${stuck.map((s) => s.sleeve.toUpperCase()).join(', ')} is mid-transition`}
        >
          <Group justify="space-between" align="center">
            <Text size="sm">
              A crashed transition leaves a sleeve ARMING or DISARMING. Recovery forces it
              back to TEST with entries stopped and the kill switch engaged.
            </Text>
            <Button
              size="compact-sm"
              color="yellow"
              onClick={() => setRecovering(true)}
              loading={recover.isPending}
              data-testid="recover-transition"
            >
              Recover…
            </Button>
          </Group>
        </Alert>
      ) : null}

      <ConfirmDialog
        opened={recovering}
        onClose={() => setRecovering(false)}
        title="Recover the interrupted transition?"
        confirmLabel="Recover"
        requireStepUp
        danger
        description="Every stuck sleeve is forced back to TEST, entries are stopped and the kill switch is engaged. Clear KILL yourself once you have checked the exchange."
        onConfirm={async () => {
          await recover.mutateAsync();
          setRecovering(false);
        }}
      />

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
  // The server's own `ops.modes.ALLOWED` decides *which* targets exist — offering one the
  // machine will refuse is a puzzle, not a gate — while the client mirror decides the
  // ORDER, so the safest option (propose before execute, demo before live) is the default.
  const targets = useMemo(() => {
    const mirror = allowedTargets(sleeve.state);
    const server = sleeve.allowed_targets;
    if (!server?.length) return mirror;
    const allowed = new Set(server.filter((t) => t !== sleeve.state));
    const ordered = mirror.filter((t) => allowed.has(t));
    const extra = server.filter(
      (t) => t !== sleeve.state && !ordered.includes(t as ModeTarget),
    ) as ModeTarget[];
    return [...ordered, ...extra];
  }, [sleeve.state, sleeve.allowed_targets]);
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
  const goingDemo = isDemo(target);
  // Demo and live both arm a real exchange: both need a seed, a preflight and a phrase.
  // What differs is what is at stake, and the page says so in words, not only in colour.
  const arming = goingLive || goingDemo;
  const targetCeiling = sleeve.seed_ceilings?.[target] ?? sleeve.max_seed_usdt;
  const expected = useMemo(
    // The server's echo wins once a preflight has run for this exact seed; before that the
    // mirror computes it locally, because the seed is still being typed.
    () =>
      preflight?.confirm_phrase ??
      confirmPhraseFor({
        sleeve: sleeve.sleeve,
        from: sleeve.state,
        target,
        seed,
        flatten,
        template: DEFAULT_CONFIRM_TEMPLATE,
        demoTemplate: DEFAULT_DEMO_CONFIRM_TEMPLATE,
      }),
    [preflight, sleeve.sleeve, sleeve.state, target, seed, flatten],
  );

  const submodeOf = (value: ModeTarget): 'propose' | 'execute' | null =>
    value === 'LIVE_EXECUTE' || value === 'DEMO_EXECUTE'
      ? 'execute'
      : value === 'LIVE_PROPOSE' || value === 'DEMO_PROPOSE'
        ? 'propose'
        : null;

  const runPreflight = useMutation({
    mutationFn: () =>
      modeApi.preflight({
        sleeve: sleeve.sleeve as SleeveId,
        target,
        submode: submodeOf(target),
        seed_usdt: arming ? seed : null,
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
        submode: submodeOf(target),
        seed_usdt: arming ? seed : null,
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

  const seedTooBig = arming && seed > targetCeiling;
  const ready =
    stepUpActive &&
    phrase.trim() === expected &&
    (!arming || (preflight?.ok === true && (expiresIn ?? 0) > 0)) &&
    !seedTooBig;

  const steps = liveSteps.length > 0 ? liveSteps : transition.data?.steps ?? [];

  return (
    <Card withBorder radius="md" padding="lg">
      <Stack gap="md">
        <Group justify="space-between" align="flex-start">
          <div>
            <Group gap="xs">
              <Title order={4}>Sleeve {sleeve.sleeve.toUpperCase()}</Title>
              <ModeBadgeChip sleeve={sleeve} />
              <Badge color={stateColour(sleeve.state)} variant="light">
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
            <VenueLine sleeve={sleeve} />
          </div>
          <StateDiagram current={sleeve.state} />
        </Group>

        {sleeve.is_demo ? (
          <Alert
            color="violet"
            variant="light"
            title="DEMO — real orders, fake money"
            data-testid="demo-banner"
          >
            This sleeve is trading on <b>{sleeve.venue_host ?? 'demo-api.binance.com'}</b>.
            The orders, the book, the filters and the rate limits are the live venue&apos;s;
            the balances are not. <b>Its P&amp;L is a rehearsal and is never reported as live
            performance</b> — runs are filed under the <code>demo</code> mode word and stay
            out of every live performance surface.
          </Alert>
        ) : null}

        <Radio.Group
          label="Target state"
          value={target}
          onChange={(value) => setTarget(value as ModeTarget)}
        >
          <Group gap="lg" mt="xs">
            {targets.map((option) => (
              <Radio
                key={option}
                value={option}
                color={stateColour(option)}
                label={
                  <Group gap={6} wrap="nowrap">
                    <Text size="sm">{option}</Text>
                    {isVenueBound(option) ? (
                      <Badge size="xs" color={stateColour(option)} variant="light">
                        {badgeFor(option)}
                      </Badge>
                    ) : null}
                  </Group>
                }
              />
            ))}
          </Group>
        </Radio.Group>

        {arming ? (
          <Alert
            color={goingDemo ? 'violet' : 'red'}
            variant="light"
            icon={<IconAlertTriangle size={16} />}
            title={
              goingDemo
                ? 'Arming DEMO on demo-api.binance.com'
                : 'Arming LIVE on api.binance.com'
            }
            data-testid={`arming-notice-${goingDemo ? 'demo' : 'live'}`}
          >
            {MODE_MEANING[goingDemo ? 'DEMO' : 'LIVE']}
          </Alert>
        ) : null}

        {arming ? (
          <NumberInput
            label={goingDemo ? 'Demo seed (USDT, not real money)' : 'Live seed (USDT)'}
            description={
              goingDemo
                ? `Hard ceiling ${targetCeiling.toLocaleString()} USDT. Size a demo run like the live run it rehearses — a token seed rehearses nothing.`
                : `Hard ceiling ${targetCeiling.toLocaleString()} USDT (modes.live.max_seed_usdt)`
            }
            value={seed}
            onChange={(value) => setSeed(Number(value) || 0)}
            min={0}
            max={targetCeiling}
            error={seedTooBig ? 'above the configured ceiling' : undefined}
            thousandSeparator
          />
        ) : null}

        {target === 'TEST' && isVenueBound(sleeve.state) ? (
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

        {arming ? (
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
              color={goingLive ? 'red' : goingDemo ? 'violet' : 'blue'}
              rightSection={<IconArrowRight size={16} />}
              disabled={!ready || transition.isPending}
              loading={transition.isPending}
              onClick={() => transition.mutate()}
              data-testid={`arm-${sleeve.sleeve}`}
            >
              {goingLive
                ? `Arm sleeve ${sleeve.sleeve.toUpperCase()} — LIVE`
                : goingDemo
                  ? `Arm sleeve ${sleeve.sleeve.toUpperCase()} — DEMO`
                  : 'Back to TEST'}
            </Button>
          </Tooltip>
        </Group>

        {steps.length > 0 || transition.isPending ? <StepProgress steps={steps} /> : null}
      </Stack>
    </Card>
  );
}

// --------------------------------------------------------------------------- pieces

const BADGE_COLOUR: Record<ModeBadge, string> = {
  TEST: 'blue',
  DEMO: 'violet',
  LIVE: 'red',
  TRANSITIONING: 'yellow',
};

/**
 * The one thing an operator must be able to read across the room.
 *
 * Three different words, three different colours, and — because colour alone is a promise
 * no screen keeps — the venue host printed underneath by {@link VenueLine}. DEMO is never
 * styled as LIVE and never styled as TEST.
 */
function ModeBadgeChip({ sleeve }: { sleeve: SleeveModeState }) {
  const badge = sleeve.badge ?? badgeFor(sleeve.state, sleeve.transition_in_progress);
  return (
    <Tooltip label={MODE_MEANING[badge]} multiline maw={360}>
      <Badge
        color={BADGE_COLOUR[badge]}
        variant="filled"
        size="lg"
        data-testid={`mode-badge-${sleeve.sleeve}`}
        data-badge={badge}
      >
        {badge}
      </Badge>
    </Tooltip>
  );
}

/** Which Binance this sleeve reaches, in full, as text. Never inferred from a colour. */
function VenueLine({ sleeve }: { sleeve: SleeveModeState }) {
  if (!sleeve.venue_host) {
    return (
      <Text size="xs" c="dimmed" data-testid={`venue-${sleeve.sleeve}`}>
        venue: none — dry run, the container holds no exchange key
      </Text>
    );
  }
  return (
    <Text size="xs" c="dimmed" data-testid={`venue-${sleeve.sleeve}`}>
      venue: <b>{sleeve.venue}</b> ({sleeve.venue_host}) · P&amp;L basis:{' '}
      <b>{sleeve.pnl_basis}</b>
    </Text>
  );
}

/** Two ladders off TEST, because demo is not a rung on the way to live. */
const DIAGRAM_DEMO: string[] = ['DEMO_PROPOSE', 'DEMO_EXECUTE'];
const DIAGRAM_LIVE: string[] = ['LIVE_PROPOSE', 'LIVE_EXECUTE'];

function StateDiagram({ current }: { current: string }) {
  const chip = (state: string, label: string) => (
    <Badge
      key={state}
      size="xs"
      variant={state === current ? 'filled' : 'outline'}
      color={state === current ? stateColour(state) : 'gray'}
    >
      {label}
    </Badge>
  );
  return (
    <Stack gap={4} align="flex-end">
      <Group gap={4} wrap="nowrap">
        {chip('TEST', 'TEST')}
        {chip('ARMING', 'ARMING')}
      </Group>
      <Group gap={4} wrap="nowrap">
        <Text size="xs" c="dimmed">
          demo
        </Text>
        {DIAGRAM_DEMO.map((state) => chip(state, state.replace('DEMO_', '')))}
      </Group>
      <Group gap={4} wrap="nowrap">
        <Text size="xs" c="dimmed">
          live
        </Text>
        {DIAGRAM_LIVE.map((state) => chip(state, state.replace('LIVE_', '')))}
      </Group>
    </Stack>
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
