/**
 * The one control: is it running, whose money, and how much it does by itself.
 *
 * The owner asked "is there a button on the UI that starts the autonomous running?" and
 * there was not. This is the seat for the answer, and it sits at the top of Home because it
 * is now the first thing a person needs to know — above what the money is worth, because a
 * number is meaningless if you do not know whether anything is producing it.
 *
 * Three things are kept deliberately apart on this card:
 *
 *   1. **Is it alive** — one honest line from the engine. When nothing is installed on the
 *      machine's timer it says so in those words. It is never a green tick by default.
 *   2. **Whose money** — the signed mode machine, per bot. Untouched by anything here.
 *   3. **How much it does by itself** — the new per-bot level, per bot. This is the switch.
 *
 * Conflating 1 and 3 is the failure this card exists to prevent: the bots being up is not
 * the system deciding, and before today nothing on screen said which was which.
 *
 * The friction is uneven on purpose. Pause is one press and a confirm; flatten makes you
 * type a phrase; letting real money trade unattended makes you type a different phrase and
 * hand over a fresh passing preflight. The emergency stop is untouched and stays exactly
 * where it was, in the header, at the same size.
 */
import {
  Alert,
  Badge,
  Box,
  Button,
  Card,
  Divider,
  Group,
  Loader,
  Progress,
  SegmentedControl,
  Stack,
  Text,
  Textarea,
  Tooltip,
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconCircleCheck,
  IconPlayerPause,
  IconPlayerPlay,
  IconPlayerStop,
  IconShoppingCartOff,
} from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { errorMessage } from '@/api';
import { useTopicEvents } from '@/app/EventStreamContext';
import { ConfirmDialog } from '@/components';
import { sleeveName } from '@/lib/plain';

import { Preview } from './Preview';
import {
  CONTROL_BOTS,
  LEVEL_ORDER,
  controlApi,
  controlKeys,
  isControlPayload,
  type BotView,
  type ControlLevel,
  type ControlPayload,
} from './control.api';
import {
  BY_ITSELF_QUESTION,
  LEVEL_COLOR,
  LEVEL_LABEL,
  MONEY_COLOR,
  MONEY_LABEL,
  MONEY_MEANING,
  VERDICT_COLOR,
  actingSentence,
  blockedBanner,
  botSentence,
  clampSentence,

  levelMeaning,
  livenessHeadline,
  moneyKind,
  moveWarning,
  needsLivePhrase,
  nextRunSentence,
  spendSentence,
  spendTone,
  supervisorGoodNews,
  supervisorWarning,
  troubleSummary,
  whenText,
} from './controlWords';

/**
 * Who moved this bot, said the way a person would.
 *
 * The audit string is `human:console:<session id>`, which is the right thing to keep in the
 * record and the wrong thing to print: a sixteen-character session id on the dashboard is
 * noise that reads like an error. The record keeps it; the screen says "by you".
 */
export function actorText(setBy: string | null | undefined): string {
  if (!setBy) return '';
  if (setBy.startsWith('human:console:')) return ' by you';
  if (setBy.startsWith('human:')) return ` by ${setBy.slice('human:'.length).split(':')[0]}`;
  return ` by ${setBy}`;
}

/** A move the operator has asked for and not yet confirmed. */
interface PendingMove {
  bot: string;
  level: ControlLevel;
  /** `start` also installs the schedule and reads it back; `level` only moves the level. */
  kind: 'start' | 'level';
}

export function ControlCard() {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<PendingMove | null>(null);
  const [stopping, setStopping] = useState<string | null>(null);
  const [pausing, setPausing] = useState<string | null>(null);
  const [flattening, setFlattening] = useState<string | null>(null);
  const [supervising, setSupervising] = useState(false);
  const [reason, setReason] = useState('');
  const [failure, setFailure] = useState<string | null>(null);

  const query = useQuery({
    queryKey: controlKeys.control,
    queryFn: () => controlApi.get(),
    refetchInterval: 30_000,
    retry: false,
  });

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: controlKeys.control });
  };
  // `mode`, `kill` and `job` are the topics that exist today; the engine also publishes a
  // `control` topic, which the server's fixed vocabulary does not carry yet, so subscribing
  // to it would break the whole stream with a 400 rather than deliver anything. Until it is
  // added, the 30-second poll above and the invalidate after every mutation are what keep
  // this honest — and a control that is a few seconds stale is still a control that tells
  // the truth, which is the property that matters here.
  useTopicEvents(['mode', 'kill', 'job'], refresh);

  const run = <T,>(work: Promise<T>) =>
    work
      .then((value) => {
        setFailure(null);
        refresh();
        return value;
      })
      .catch((error: unknown) => {
        setFailure(errorMessage(error));
        refresh();
        throw error;
      });

  const startMutation = useMutation({
    mutationFn: (args: { bot: string; level: ControlLevel; phrase?: string }) =>
      run(
        controlApi.start({
          bot: args.bot,
          level: args.level,
          ...(reason.trim() ? { reason: reason.trim() } : {}),
          ...(args.phrase ? { confirm_phrase: args.phrase } : {}),
        }),
      ),
  });
  const levelMutation = useMutation({
    mutationFn: (args: { bot: string; level: ControlLevel; phrase?: string }) =>
      run(
        controlApi.setLevel({
          bot: args.bot,
          level: args.level,
          ...(reason.trim() ? { reason: reason.trim() } : {}),
          ...(args.phrase ? { confirm_phrase: args.phrase } : {}),
        }),
      ),
  });
  const pauseMutation = useMutation({
    mutationFn: (bot: string) => run(controlApi.pause(bot, reason.trim() || undefined)),
  });
  const stopMutation = useMutation({
    mutationFn: (bot: string) => run(controlApi.stop(bot, reason.trim() || undefined)),
  });
  const flattenMutation = useMutation({
    mutationFn: (args: { bot: string; phrase: string }) =>
      run(controlApi.flatten(args.bot, args.phrase, reason.trim() || undefined)),
  });
  const unitsMutation = useMutation({ mutationFn: () => run(controlApi.installUnits()) });

  if (query.isLoading) {
    return (
      <Card withBorder padding="md" radius="md" data-testid="control-card">
        <Group justify="center" p="sm">
          <Loader size="sm" />
        </Group>
      </Card>
    );
  }

  const data = isControlPayload(query.data) ? query.data : null;
  if (!data) {
    return (
      <Card withBorder padding="md" radius="md" data-testid="control-card">
        <Stack gap={4}>
          <Text fw={600}>Is it running by itself?</Text>
          {/*
            Deliberately not "everything is fine". A control that cannot read its own state
            has to say so: silence here would read as health, which is the mistake the whole
            card exists to prevent.
          */}
          <Text size="sm" c="dimmed" data-testid="control-unavailable">
            Cannot tell — the control could not be read. Nothing here proves anything has run
            by itself.
          </Text>
          <Text size="xs" c="dimmed">
            {query.isError
              ? errorMessage(query.error)
              : 'The answer did not look like a control, so none of it is being shown.'}
          </Text>
        </Stack>
      </Card>
    );
  }

  const trouble = troubleSummary(data.jobs);
  const closePending = () => {
    setPending(null);
    setReason('');
  };

  return (
    <Card withBorder padding="md" radius="md" data-testid="control-card">
      <Stack gap="sm">
        {/*
          Above everything, including the liveness line, because it outranks everything.
          "The loop is alive and on schedule" was on this screen for fourteen hours while
          the system refused 655 consecutive buys. The state that matters is not whether
          the machinery turns, it is whether the machinery is allowed to do anything, and
          that state gets the top of the card and the loudest colour available.
        */}
        <BlockedAlert data={data} />

        <LivenessLine data={data} />

        {failure ? (
          <Alert color="red" icon={<IconAlertTriangle size={16} />} data-testid="control-error">
            {failure}
          </Alert>
        ) : null}

        {data.state.mirror_ahead ? (
          <Alert color="yellow" icon={<IconAlertTriangle size={16} />}>
            Something changed the unsigned copy of this setting after the signed one. The
            scheduled jobs ignore the unsigned copy, so the machine is still doing what the
            signed setting says.
          </Alert>
        ) : null}

        <Divider />

        {CONTROL_BOTS.map((bot) => (
          <BotRow
            key={bot}
            data={data}
            bot={bot}
            busy={
              startMutation.isPending ||
              levelMutation.isPending ||
              pauseMutation.isPending ||
              stopMutation.isPending
            }
            onMove={(level, kind) => {
              setReason('');
              setPending({ bot, level, kind });
            }}
            onPause={() => {
              setReason('');
              setPausing(bot);
            }}
            onStop={() => {
              setReason('');
              setStopping(bot);
            }}
            onFlatten={() => {
              setReason('');
              setFlattening(bot);
            }}
          />
        ))}

        <SupervisorLine
          data={data}
          busy={unitsMutation.isPending}
          onInstall={() => setSupervising(true)}
        />

        {trouble ? (
          <Alert color="yellow" icon={<IconAlertTriangle size={16} />} data-testid="control-trouble">
            <Stack gap={2}>
              {trouble.lines.map((line) => (
                <Text size="sm" key={line}>
                  {line}
                </Text>
              ))}
              {trouble.more ? (
                <Text size="sm" c="dimmed">
                  and {trouble.more} more — see Operations for the full list.
                </Text>
              ) : null}
            </Stack>
          </Alert>
        ) : null}

        <Divider />
        <Preview />
      </Stack>

      {/* ------------------------------------------------------------------ the dialogs */}

      <ConfirmDialog
        opened={pending !== null}
        onClose={closePending}
        title={
          pending
            ? `${sleeveName(pending.bot)}: ${LEVEL_LABEL[pending.level]}`
            : 'Change what it does by itself'
        }
        confirmLabel={pending?.kind === 'start' ? 'Start it' : 'Change it'}
        requireStepUp
        danger={pending?.level === 'trading'}
        {...(pending && needsLivePhrase(pending.level, data.modes[pending.bot])
          ? { confirmPhrase: data.phrases.arm_live_trading }
          : {})}
        description={
          pending ? moveWarning(pending.level, data.modes[pending.bot]) : ''
        }
        onConfirm={async ({ phrase }) => {
          if (!pending) return;
          const args = {
            bot: pending.bot,
            level: pending.level,
            ...(phrase ? { phrase } : {}),
          };
          if (pending.kind === 'start') await startMutation.mutateAsync(args);
          else await levelMutation.mutateAsync(args);
          closePending();
        }}
      >
        <Stack gap="xs">
          {pending?.kind === 'start' ? (
            <Text size="xs" c="dimmed">
              Starting also installs the schedule on this machine and reads it back to prove
              it took. Without that, nothing runs by itself however this is set.
            </Text>
          ) : null}
          <ReasonBox value={reason} onChange={setReason} />
        </Stack>
      </ConfirmDialog>

      <ConfirmDialog
        opened={pausing !== null}
        onClose={() => setPausing(null)}
        title={pausing ? `Pause ${sleeveName(pausing)}` : 'Pause'}
        confirmLabel="Pause it"
        requireStepUp
        description="It stops making plans and stops opening anything new. What it already holds is left exactly as it is, stops and all, and it keeps watching. Start puts it back where it was."
        onConfirm={async () => {
          if (pausing) await pauseMutation.mutateAsync(pausing);
          setPausing(null);
        }}
      >
        <ReasonBox value={reason} onChange={setReason} />
      </ConfirmDialog>

      <ConfirmDialog
        opened={stopping !== null}
        onClose={() => setStopping(null)}
        title={stopping ? `Turn off ${sleeveName(stopping)}` : 'Turn it off'}
        confirmLabel="Turn it off"
        requireStepUp
        description="Nothing at all runs for this bot after this — not even the watching. What it holds is left alone; this does not sell anything."
        onConfirm={async () => {
          if (stopping) await stopMutation.mutateAsync(stopping);
          setStopping(null);
        }}
      >
        <ReasonBox value={reason} onChange={setReason} />
      </ConfirmDialog>

      <ConfirmDialog
        opened={supervising}
        onClose={() => setSupervising(false)}
        title="Make this console restart itself"
        confirmLabel="Set it up"
        requireStepUp
        description="This installs a small system service that starts this console again, within seconds, every time it stops — including after a reboot. It needs no password. Nothing about trading changes."
        onConfirm={async () => {
          await unitsMutation.mutateAsync();
          setSupervising(false);
        }}
      >
        <Text size="xs" c="dimmed">
          It has stopped on its own before, and while it was down nothing could tell you the
          system had stopped trading. The result below is read back from the machine, so it
          says what is actually running rather than what was attempted.
        </Text>
      </ConfirmDialog>

      <ConfirmDialog
        opened={flattening !== null}
        onClose={() => setFlattening(null)}
        title={flattening ? `Sell everything ${sleeveName(flattening)} holds` : 'Sell everything'}
        confirmLabel="Sell it all"
        danger
        requireStepUp
        confirmPhrase={data.phrases.flatten}
        description="This sells every position this bot holds, at market, now. It is not the emergency stop: the emergency stop is still in the header and still works separately."
        onConfirm={async ({ phrase }) => {
          if (flattening) {
            await flattenMutation.mutateAsync({
              bot: flattening,
              phrase: phrase ?? data.phrases.flatten,
            });
          }
          setFlattening(null);
        }}
      >
        <ReasonBox value={reason} onChange={setReason} />
      </ConfirmDialog>
    </Card>
  );
}

/* --------------------------------------------------------------------------- pieces */

function ReasonBox({ value, onChange }: { value: string; onChange: (next: string) => void }) {
  return (
    <Textarea
      label="Why (optional)"
      placeholder="Kept with the record of this change"
      value={value}
      onChange={(event) => onChange(event.currentTarget.value)}
      autosize
      minRows={2}
      maxLength={500}
      data-testid="control-reason"
    />
  );
}

/**
 * "TRADING IS BLOCKED", in a place and a colour nobody can miss.
 *
 * This component is the whole point of the 2026-09-24 post-mortem. The chain was: PEPE/USDT
 * has no perpetual contract, so one 400 from Binance failed the entire funding phase, which
 * stopped ingest, which froze the freshness file, which made the watchdog raise a
 * `data_stale` flag with no expiry, which made the risk gate refuse every single entry for
 * fourteen hours while the strategy went on finding signals. The gate was right. What was
 * wrong is that this screen said "The loop is alive and on schedule" the entire time.
 *
 * So: red, filled, at the top, above the money and above the liveness line, with the four
 * facts an operator needs — what is stopped, why, since when, and what will clear it — and,
 * when the loop is still calling itself healthy, the sentence that explains why nothing
 * looked wrong. It renders nothing at all when nothing is blocked.
 */
export function BlockedAlert({ data }: { data: ControlPayload }) {
  const banner = blockedBanner(data);
  if (!banner) return null;
  return (
    <Alert
      color="red"
      variant="filled"
      icon={<IconAlertTriangle size={20} />}
      title={banner.title}
      data-testid="trading-blocked"
    >
      <Stack gap={4}>
        {banner.facts.map((fact) => (
          <Group key={fact.label} gap={6} wrap="nowrap" align="flex-start">
            <Text size="sm" fw={700} style={{ whiteSpace: 'nowrap' }}>
              {fact.label}:
            </Text>
            <Text size="sm">{fact.value}</Text>
          </Group>
        ))}
        {banner.contradiction ? (
          <Text size="sm" fs="italic" mt={4} data-testid="blocked-contradiction">
            {banner.contradiction}
          </Text>
        ) : null}
      </Stack>
    </Alert>
  );
}

/**
 * Will this console come back if it dies? It did not, once, and that was half the silence.
 *
 * The button installs a *user* systemd unit, which needs no password — which is exactly why
 * the system unit that has existed all along was never installed here. `unknown` renders
 * nothing: a warning that fires because a probe could not answer is a warning people learn
 * to ignore, and this one has to keep working.
 */
export function SupervisorLine({
  data,
  busy,
  onInstall,
}: {
  data: ControlPayload;
  busy: boolean;
  onInstall: () => void;
}) {
  const warning = supervisorWarning(data.supervisor);
  const good = supervisorGoodNews(data.supervisor);
  if (warning) {
    return (
      <Alert
        color={warning.tone === 'bad' ? 'red' : 'yellow'}
        icon={<IconAlertTriangle size={16} />}
        data-testid="supervisor-warning"
      >
        <Group justify="space-between" wrap="wrap" gap="xs">
          <Text size="sm">{warning.text}</Text>
          <Button size="xs" disabled={busy} onClick={onInstall} data-testid="install-supervisor">
            Make it restart itself
          </Button>
        </Group>
      </Alert>
    );
  }
  if (!good) return null;
  return (
    <Text size="xs" c="dimmed" data-testid="supervisor-ok">
      {good}
    </Text>
  );
}

/**
 * The honest liveness line.
 *
 * It prints the engine's own headline rather than deriving one, so the sentence a person
 * reads is the sentence the machine computed. When nothing is installed the words are
 * "NOTHING IS SCHEDULED", and there is no arrangement of this component that turns that
 * into a tick.
 */
export function LivenessLine({ data }: { data: ControlPayload }) {
  const color = VERDICT_COLOR[data.verdict] ?? 'gray';
  const good = data.verdict === 'alive';
  return (
    <Stack gap={4} data-testid="liveness">
      <Group gap="xs" wrap="nowrap" align="flex-start">
        {good ? (
          <IconCircleCheck size={20} color="var(--mantine-color-teal-6)" />
        ) : (
          <IconAlertTriangle size={20} color={`var(--mantine-color-${color}-6)`} />
        )}
        <Stack gap={2}>
          <Text fw={600} data-testid="liveness-headline">
            {livenessHeadline(data)}
          </Text>
          <Text size="sm" c="dimmed" data-testid="liveness-next">
            {nextRunSentence(data)}
          </Text>
          {/*
            "On schedule" and "achieving something" are different claims, and until this
            line existed only the first one was ever on screen. When trading is blocked this
            is deliberately empty — the red banner above says it, louder.
          */}
          {actingSentence(data) ? (
            <Text size="sm" c="dimmed" data-testid="liveness-outcomes">
              {actingSentence(data)}
            </Text>
          ) : null}
        </Stack>
      </Group>
      {data.kill_engaged ? (
        <Badge color="red" variant="filled" data-testid="liveness-kill">
          Emergency stop is on — nothing new is entered on either bot
        </Badge>
      ) : null}
      {data.schedule.installed && !data.schedule.matches ? (
        <Text size="xs" c="dimmed">
          The schedule on this machine is not the one these settings produce. Press Start on a
          bot to install the current one.
        </Text>
      ) : null}
    </Stack>
  );
}

function BotRow({
  data,
  bot,
  busy,
  onMove,
  onPause,
  onStop,
  onFlatten,
}: {
  data: ControlPayload;
  bot: string;
  busy: boolean;
  onMove: (level: ControlLevel, kind: 'start' | 'level') => void;
  onPause: () => void;
  onStop: () => void;
  onFlatten: () => void;
}) {
  const view: BotView | undefined = data.bots[bot];
  const mode = data.modes[bot];
  const spend = data.spend[bot];
  if (!view) return null;

  const kind = moneyKind(mode);
  const clamp = clampSentence(view);
  const tone = spendTone(spend);
  const isOff = view.level === 'off';
  const lastMoved = whenText(view.since, data.timezone);

  return (
    <Stack gap={6} data-testid={`control-bot-${bot}`}>
      <Group justify="space-between" wrap="wrap" gap="xs">
        <Group gap="xs" wrap="nowrap">
          <Text fw={600}>{sleeveName(bot)}</Text>
          <Tooltip label={MONEY_MEANING[kind]} multiline w={260}>
            <Badge color={MONEY_COLOR[kind]} variant="light" data-testid={`money-${bot}`}>
              {MONEY_LABEL[kind]}
            </Badge>
          </Tooltip>
          <Badge color={LEVEL_COLOR[view.level]} variant="filled" data-testid={`level-${bot}`}>
            {LEVEL_LABEL[view.level]}
          </Badge>
        </Group>
        <Group gap="xs" wrap="nowrap">
          {isOff ? (
            <Button
              size="xs"
              leftSection={<IconPlayerPlay size={14} />}
              disabled={busy}
              onClick={() => onMove(view.resume_level ?? 'watching', 'start')}
              data-testid={`start-${bot}`}
            >
              Start
            </Button>
          ) : (
            <Button
              size="xs"
              variant="light"
              leftSection={<IconPlayerPause size={14} />}
              disabled={busy}
              onClick={onPause}
              data-testid={`pause-${bot}`}
            >
              Pause
            </Button>
          )}
          <Button
            size="xs"
            variant="subtle"
            color="gray"
            leftSection={<IconPlayerStop size={14} />}
            disabled={busy || isOff}
            onClick={onStop}
            data-testid={`stop-${bot}`}
          >
            Turn off
          </Button>
          <Button
            size="xs"
            variant="subtle"
            color="red"
            leftSection={<IconShoppingCartOff size={14} />}
            disabled={busy}
            onClick={onFlatten}
            data-testid={`flatten-${bot}`}
          >
            Sell everything
          </Button>
        </Group>
      </Group>

      <Text size="sm" data-testid={`bot-sentence-${bot}`}>
        {botSentence(view, mode)}
      </Text>

      <Box>
        <Text size="xs" c="dimmed" mb={4}>
          {BY_ITSELF_QUESTION}
        </Text>
        <SegmentedControl
          size="xs"
          fullWidth
          value={view.level}
          disabled={busy}
          onChange={(next) => onMove(next as ControlLevel, isOff ? 'start' : 'level')}
          data={LEVEL_ORDER.map((level) => ({
            value: level,
            label: LEVEL_LABEL[level],
          }))}
          data-testid={`level-picker-${bot}`}
        />
        <Text size="xs" c="dimmed" mt={4} data-testid={`level-meaning-${bot}`}>
          {levelMeaning(view.level, data.level_meaning)}
        </Text>
      </Box>

      {clamp ? (
        <Text size="xs" c="orange" data-testid={`clamped-${bot}`}>
          {clamp}
        </Text>
      ) : null}

      <Group gap="xs" wrap="nowrap" align="center">
        <Text size="xs" c={tone === 'over' ? 'red' : tone === 'warn' ? 'orange' : 'dimmed'}
              data-testid={`spend-${bot}`}>
          Model spend: {spendSentence(spend)}
        </Text>
        {spend?.month_pct === null || spend?.month_pct === undefined ? null : (
          <Progress
            value={Math.min(100, spend.month_pct)}
            w={80}
            size="sm"
            color={tone === 'over' ? 'red' : tone === 'warn' ? 'orange' : 'teal'}
          />
        )}
      </Group>

      {lastMoved ? (
        <Text size="xs" c="dimmed" data-testid={`last-changed-${bot}`}>
          Last changed {lastMoved}
          {actorText(view.set_by)}
          {view.reason ? ` — ${view.reason}` : ''}
        </Text>
      ) : null}
    </Stack>
  );
}
