import {
  Alert,
  Badge,
  Button,
  Card,
  Checkbox,
  Code,
  Group,
  List,
  Loader,
  NumberInput,
  Radio,
  ScrollArea,
  Stack,
  Stepper,
  Switch,
  Text,
  TextInput,
  Title,
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconCircleCheck,
  IconPlayerPlay,
  IconRocket,
} from '@tabler/icons-react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';

import { api, errorMessage } from '@/api';
import { usePageCommands } from '@/app/commandRegistry';
import { useSession } from '@/app/SessionContext';
import { ConfirmDialog, DataTable, EmptyState } from '@/components';
import { useLogFollow } from '@/pages/operations/components/LogViewer';

import { configApi, configKeys } from '../settings/api';
import { replaceOps, useConfigPatch } from './useConfigPatch';

const STEPS = 9;

/**
 * Setup wizard (spec 12 page 17).
 *
 * The first-run flow for the decisions only the owner can make. Every step writes through
 * the same audited config endpoint the Settings page uses — the wizard is a guided path
 * through `earn.yaml`, never a second writer.
 */
export default function SetupPage() {
  const session = useSession();
  const patch = useConfigPatch();
  const [step, setStep] = useState(0);
  const [log, setLog] = useState<string[]>([]);

  const earn = useQuery({ queryKey: configKeys.file('earn'), queryFn: () => configApi.get('earn') });
  const models = useQuery({
    queryKey: configKeys.file('models'),
    queryFn: () => configApi.get('models'),
  });

  const note = (message: string) => setLog((current) => [...current.slice(-40), message]);

  usePageCommands('setup', [
    {
      id: 'first-step',
      title: 'Setup: back to the first step',
      keywords: ['wizard', 'claude auth'],
      run: (ctx) => {
        setStep(0);
        ctx.close();
      },
    },
    {
      id: 'week-1-gate',
      title: 'Setup: jump to the week-1 gate',
      subtitle: 'The last step — run the healthcheck and read its verdict',
      run: (ctx) => {
        setStep(STEPS - 1);
        ctx.close();
      },
    },
    {
      id: 'clear-log',
      title: 'Clear the setup log',
      run: (ctx) => {
        setLog([]);
        ctx.close();
      },
    },
  ]);

  if (earn.isLoading) {
    return (
      <Group justify="center" p="xl">
        <Loader />
      </Group>
    );
  }
  if (earn.isError || !earn.data) {
    return <EmptyState title="Setup unavailable" description={errorMessage(earn.error)} />;
  }

  const values = earn.data.values as Record<string, any>;
  const modelValues = (models.data?.values ?? {}) as Record<string, any>;

  return (
    <Stack gap="md" data-testid="setup-page">
      <Group justify="space-between" align="flex-end">
        <Stack gap={2}>
          <Title order={3}>Setup</Title>
          <Text size="sm" c="dimmed">
            The decisions the owner makes once. Each step previews, validates and audits like
            any other config save.
          </Text>
        </Stack>
        <Badge variant="light">
          step {step + 1} / {STEPS}
        </Badge>
      </Group>

      {patch.error ? (
        <Alert color="red" icon={<IconAlertTriangle size={16} />} data-testid="setup-error">
          {patch.error}
        </Alert>
      ) : null}

      <Stepper active={step} onStepClick={setStep} allowNextStepsSelect orientation="vertical">
        <Stepper.Step label="Claude auth" description="Subscription, API key or auto">
          <AuthStep
            current={String(modelValues['auth']?.claude_mode ?? 'subscription')}
            cap={Number(modelValues['auth']?.api_key_monthly_cap_usd ?? 30)}
            busy={patch.busy}
            onSave={async (mode, cap) => {
              const out = await patch.apply(
                'models',
                replaceOps([
                  ['auth.claude_mode', mode],
                  ['auth.api_key_monthly_cap_usd', cap],
                ]),
                `setup: claude auth ${mode}`,
              );
              note(out.message);
              if (out.ok) setStep(1);
            }}
          />
        </Stepper.Step>

        <Stepper.Step label="Seeds" description="Starting balance per sleeve in TEST">
          <SeedStep
            a={Number(values['modes']?.test?.seed_usdt?.a ?? 10000)}
            b={Number(values['modes']?.test?.seed_usdt?.b ?? 10000)}
            liveCeilings={values['modes']?.live?.max_seed_usdt ?? {}}
            busy={patch.busy}
            onSave={async (a, b) => {
              const out = await patch.apply(
                'earn',
                replaceOps([
                  ['modes.test.seed_usdt.a', a],
                  ['modes.test.seed_usdt.b', b],
                ]),
                'setup: test seeds',
              );
              note(out.message);
              if (out.ok) setStep(2);
            }}
          />
        </Stepper.Step>

        <Stepper.Step label="Backups" description="Destination and optional mirror">
          <BackupStep
            dest={String(values['backup']?.dest ?? '~/earn-backups')}
            mirror={values['backup']?.mirror_dest ?? null}
            busy={patch.busy}
            onSave={async (dest, mirror) => {
              const out = await patch.apply(
                'earn',
                replaceOps([
                  ['backup.dest', dest],
                  ['backup.mirror_dest', mirror || null],
                ]),
                'setup: backup destination',
              );
              note(out.message);
              if (out.ok) setStep(3);
            }}
          />
        </Stepper.Step>

        <Stepper.Step label="Research slots" description="Gulf-time fire times for the decision run">
          <SlotStep
            slots={(values['research']?.slots ?? []) as string[]}
            busy={patch.busy}
            onSave={async (slots) => {
              const out = await patch.apply(
                'earn',
                replaceOps([['research.slots', slots]]),
                'setup: research slots',
              );
              note(out.message);
              if (out.ok) setStep(4);
            }}
          />
        </Stepper.Step>

        <Stepper.Step label="Paper start & live branch" description="Where the record begins">
          <BranchStep
            anchorDate={String(values['paper']?.anchor_date ?? '')}
            branch={String(values['git']?.live_branch ?? '')}
            confirmPhrase={earn.data.confirm_phrase}
            stepUpActive={session.stepUpActive}
            busy={patch.busy}
            onStepUp={session.stepUp}
            onSave={async (anchorDate, branch) => {
              const ops = replaceOps([
                ...(anchorDate ? ([['paper.anchor_date', anchorDate]] as Array<[string, unknown]>) : []),
                ['git.live_branch', branch],
              ]);
              const out = await patch.apply(
                'earn',
                ops,
                'setup: paper anchor and live branch',
                { confirmPhrase: earn.data.confirm_phrase },
              );
              note(out.message);
              if (out.ok) setStep(5);
            }}
          />
        </Stepper.Step>

        <Stepper.Step label="Telegram" description="Ops alerts and approvals">
          <TelegramStep
            enabled={Boolean(values['telegram']?.enabled ?? false)}
            busy={patch.busy}
            onSave={async (enabled) => {
              const out = await patch.apply(
                'earn',
                replaceOps([['telegram.enabled', enabled]]),
                'setup: telegram',
              );
              note(out.message);
              if (out.ok) setStep(6);
            }}
          />
        </Stepper.Step>

        <Stepper.Step label="Ollama" description="Detect a local model for cheap stages">
          <OllamaStep onDone={() => setStep(7)} />
        </Stepper.Step>

        <Stepper.Step label="Host readiness" description="ext4, systemd, sleep policy, docker">
          <HostStep onDone={() => setStep(8)} />
        </Stepper.Step>

        <Stepper.Step label="Week-1 gate" description="Run the checks with live output">
          <GateStep />
        </Stepper.Step>

        <Stepper.Completed>
          <Alert color="teal" icon={<IconCircleCheck size={16} />} title="Setup recorded">
            Every answer is in <Code>config/earn.yaml</Code> / <Code>config/models.yaml</Code> and
            in <Code>config_audit</Code>. Continue on <Link to="/settings">Settings</Link> or{' '}
            <Link to="/mode">Mode &amp; Live</Link>.
          </Alert>
        </Stepper.Completed>
      </Stepper>

      {log.length > 0 ? (
        <Card withBorder padding="sm">
          <Stack gap={2}>
            <Text size="sm" fw={600}>
              Setup log
            </Text>
            {log.map((line, index) => (
              <Text key={`${index}-${line}`} size="xs" ff="monospace">
                {line}
              </Text>
            ))}
          </Stack>
        </Card>
      ) : null}
    </Stack>
  );
}

function StepShell({ children, onSave, busy, saveLabel = 'Save & continue', disabled }: {
  children: ReactNode;
  onSave: () => void;
  busy: boolean;
  saveLabel?: string;
  disabled?: boolean;
}) {
  return (
    <Card withBorder padding="md" mt="sm">
      <Stack gap="sm">
        {children}
        <Group justify="flex-end">
          <Button onClick={onSave} loading={busy} disabled={disabled}>
            {saveLabel}
          </Button>
        </Group>
      </Stack>
    </Card>
  );
}

function AuthStep({ current, cap, busy, onSave }: {
  current: string;
  cap: number;
  busy: boolean;
  onSave: (mode: string, cap: number) => void;
}) {
  const [mode, setMode] = useState(current);
  const [limit, setLimit] = useState(cap);
  return (
    <StepShell busy={busy} onSave={() => onSave(mode, limit)}>
      <Radio.Group value={mode} onChange={setMode} label="Which Claude credential do jobs get?">
        <Stack gap={4} mt={4}>
          <Radio
            value="subscription"
            label="Subscription only (Claude Max token) — no metered spend"
          />
          <Radio value="api_key" label="API key only — metered, capped below" />
          <Radio value="auto" label="Auto — prefer the subscription, fall back to the API key" />
        </Stack>
      </Radio.Group>
      {mode !== 'subscription' ? (
        <Alert color="yellow" icon={<IconAlertTriangle size={16} />} title="Terms and billing">
          Using an API key for automated runs means metered spend on every call, and the
          subscription's terms do not cover headless use of both at once. The monthly cap below
          is a hard stop, not a warning.
        </Alert>
      ) : null}
      <NumberInput
        label="API-key monthly cap (USD)"
        description="Hard cap whenever the key serves; budgets.mode is ignored."
        value={limit}
        onChange={(value) => setLimit(Number(value) || 0)}
        min={0}
        max={1000}
      />
      <Text size="xs" c="dimmed">
        The tokens themselves are written on the Secrets page; nothing secret is entered here.
      </Text>
    </StepShell>
  );
}

function SeedStep({ a, b, liveCeilings, busy, onSave }: {
  a: number;
  b: number;
  liveCeilings: Record<string, number>;
  busy: boolean;
  onSave: (a: number, b: number) => void;
}) {
  const [seedA, setSeedA] = useState(a);
  const [seedB, setSeedB] = useState(b);
  return (
    <StepShell busy={busy} onSave={() => onSave(seedA, seedB)}>
      <Text size="sm">
        The seed is the starting balance of the <strong>next</strong> test run, so changing it
        needs a Test Lab reset to take effect.
      </Text>
      <Group grow>
        <NumberInput
          label="Sleeve A (rules)"
          value={seedA}
          onChange={(value) => setSeedA(Number(value) || 0)}
          min={1}
        />
        <NumberInput
          label="Sleeve B (Claude)"
          value={seedB}
          onChange={(value) => setSeedB(Number(value) || 0)}
          min={1}
        />
      </Group>
      <Text size="xs" c="dimmed">
        Live ceilings (protected, changed on Settings): A ${liveCeilings['a'] ?? '—'} · B $
        {liveCeilings['b'] ?? '—'}
      </Text>
    </StepShell>
  );
}

function BackupStep({ dest, mirror, busy, onSave }: {
  dest: string;
  mirror: string | null;
  busy: boolean;
  onSave: (dest: string, mirror: string) => void;
}) {
  const [target, setTarget] = useState(dest);
  const [mirrorPath, setMirrorPath] = useState(mirror ?? '');
  const test = useMutation({
    mutationFn: () => api.post<{ ok: boolean; detail: string }>('/ops/backups/test-dest', {}),
  });
  return (
    <StepShell busy={busy} onSave={() => onSave(target, mirrorPath)}>
      <TextInput
        label="Backup destination"
        description="Must be on ext4 inside WSL; ~ and $VARS are expanded."
        value={target}
        onChange={(event) => setTarget(event.currentTarget.value)}
      />
      <TextInput
        label="Mirror (optional)"
        description="Best effort, e.g. a OneDrive folder. A failed mirror only warns."
        value={mirrorPath}
        onChange={(event) => setMirrorPath(event.currentTarget.value)}
      />
      <Group>
        <Button size="xs" variant="light" loading={test.isPending} onClick={() => test.mutate()}>
          Test destination
        </Button>
        {test.data ? (
          <Text size="xs" c={test.data.ok ? 'teal' : 'red'}>
            {test.data.detail}
          </Text>
        ) : null}
        {test.isError ? (
          <Text size="xs" c="dimmed">
            Backup probe not available yet ({errorMessage(test.error)}).
          </Text>
        ) : null}
      </Group>
    </StepShell>
  );
}

function SlotStep({ slots, busy, onSave }: {
  slots: string[];
  busy: boolean;
  onSave: (slots: string[]) => void;
}) {
  const [text, setText] = useState(slots.join(', '));
  const parsed = text
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean);
  const valid = parsed.length > 0 && parsed.every((slot) => /^\d{2}:\d{2}$/.test(slot));
  return (
    <StepShell busy={busy} disabled={!valid} onSave={() => onSave(parsed)}>
      <TextInput
        label="Research slots (HH:MM, Gulf time)"
        description="The single source for the cron lines, the nearest-slot check and the healthcheck."
        value={text}
        onChange={(event) => setText(event.currentTarget.value)}
        error={valid ? null : 'Each slot must look like 08:30, comma separated'}
      />
      <Text size="xs" c="dimmed">
        Saving queues a crontab reinstall; the pending-effects banner will say so.
      </Text>
    </StepShell>
  );
}

function BranchStep({ anchorDate, branch, confirmPhrase, stepUpActive, busy, onStepUp, onSave }: {
  anchorDate: string;
  branch: string;
  confirmPhrase: string;
  stepUpActive: boolean;
  busy: boolean;
  onStepUp: (token: string) => Promise<void>;
  onSave: (anchorDate: string, branch: string) => void;
}) {
  const [date, setDate] = useState(anchorDate);
  const [name, setName] = useState(branch);
  const [confirming, setConfirming] = useState(false);
  return (
    <>
      <StepShell busy={busy} saveLabel="Save (protected)…" onSave={() => setConfirming(true)}>
        <TextInput
          label="Paper record anchor date"
          description="Where the paper NAV record starts (YYYY-MM-DD)."
          value={date}
          onChange={(event) => setDate(event.currentTarget.value)}
        />
        <TextInput
          label="Live branch"
          description="The branch the merge gate is allowed to fast-forward."
          value={name}
          onChange={(event) => setName(event.currentTarget.value)}
        />
        <Alert color="orange" icon={<IconAlertTriangle size={16} />}>
          <Code>git.*</Code> is protected: saving needs step-up and the typed phrase.
        </Alert>
      </StepShell>
      <ConfirmDialog
        opened={confirming}
        onClose={() => setConfirming(false)}
        title="Save protected keys"
        description="This changes the branch the autonomy gate may merge into."
        confirmPhrase={confirmPhrase}
        requireStepUp
        stepUpSatisfied={stepUpActive}
        danger
        confirmLabel="Save"
        onConfirm={async ({ stepUpToken }) => {
          if (stepUpToken) await onStepUp(stepUpToken);
          onSave(date, name);
        }}
      />
    </>
  );
}

function TelegramStep({ enabled, busy, onSave }: {
  enabled: boolean;
  busy: boolean;
  onSave: (enabled: boolean) => void;
}) {
  const [on, setOn] = useState(enabled);
  const test = useMutation({
    mutationFn: () => api.post<{ ok: boolean; detail: string }>('/secrets/test/telegram', {}),
  });
  return (
    <StepShell busy={busy} onSave={() => onSave(on)}>
      <Switch
        label="Enable the ops Telegram bot"
        checked={on}
        onChange={(event) => setOn(event.currentTarget.checked)}
      />
      <Text size="sm">
        The bot token and chat id are secrets — write them on the Secrets page, never here.
      </Text>
      <Group>
        <Button size="xs" variant="light" loading={test.isPending} onClick={() => test.mutate()}>
          Send a test message
        </Button>
        {test.data ? (
          <Text size="xs" c={test.data.ok ? 'teal' : 'red'}>
            {test.data.detail}
          </Text>
        ) : null}
        {test.isError ? (
          <Text size="xs" c="dimmed">
            Probe unavailable ({errorMessage(test.error)}).
          </Text>
        ) : null}
      </Group>
    </StepShell>
  );
}

function OllamaStep({ onDone }: { onDone: () => void }) {
  const detect = useMutation({
    mutationFn: () =>
      api.get<{ base_url: string | null; probes: Array<{ url: string; ok: boolean }>;
        models: string[] }>('/llm/ollama/detect'),
  });
  return (
    <Card withBorder padding="md" mt="sm">
      <Stack gap="sm">
        <Text size="sm">
          A local model serves the cheap stages (screening, classification) and keeps the
          subscription's rate limit for decisions. It may never write a proposal — that is a code
          invariant, not a setting.
        </Text>
        <Group>
          <Button
            size="xs"
            leftSection={<IconPlayerPlay size={14} />}
            loading={detect.isPending}
            onClick={() => detect.mutate()}
          >
            Detect Ollama
          </Button>
          <Button size="xs" variant="subtle" onClick={onDone}>
            Skip
          </Button>
        </Group>
        {detect.data ? (
          <Stack gap={2}>
            <Text size="sm">
              Detected: <Code>{detect.data.base_url ?? 'none'}</Code>
            </Text>
            <List size="xs">
              {(detect.data.probes ?? []).map((probe) => (
                <List.Item key={probe.url}>
                  {probe.ok ? '✓' : '✗'} {probe.url}
                </List.Item>
              ))}
            </List>
            {detect.data.models?.length ? (
              <Text size="xs">Models: {detect.data.models.join(', ')}</Text>
            ) : null}
          </Stack>
        ) : null}
        {detect.isError ? (
          <Alert color="gray">
            The provider endpoints have not landed yet ({errorMessage(detect.error)}). Ollama can be
            configured later on AI &amp; Models.
          </Alert>
        ) : null}
      </Stack>
    </Card>
  );
}

function HostStep({ onDone }: { onDone: () => void }) {
  const host = useQuery({
    queryKey: ['ops', 'host'],
    queryFn: () => api.get<Record<string, unknown>>('/ops/host'),
    retry: false,
  });
  const rows = Object.entries(host.data ?? {}).map(([key, value]) => ({ key, value }));
  return (
    <Card withBorder padding="md" mt="sm">
      <Stack gap="sm">
        {host.isError ? (
          <Alert color="gray">
            Host checks are not available yet ({errorMessage(host.error)}). The Operations page
            will carry them.
          </Alert>
        ) : (
          <DataTable
            dense
            rows={rows}
            rowKey={(row) => row.key}
            loading={host.isLoading}
            emptyTitle="No host facts"
            columns={[
              { key: 'k', header: 'Check', render: (row) => row.key },
              {
                key: 'v',
                header: 'Value',
                render: (row) => (
                  <Text size="xs" ff="monospace">
                    {JSON.stringify(row.value)}
                  </Text>
                ),
              },
            ]}
          />
        )}
        <Group justify="flex-end">
          <Button size="xs" onClick={onDone}>
            Continue
          </Button>
        </Group>
      </Stack>
    </Card>
  );
}

/**
 * The last step: run the healthcheck and watch it.
 *
 * The job is detached — it outlives this request and can take minutes — so the only
 * honest way to show it is to follow its log. `POST /api/ops/jobs/healthcheck/run` names
 * the file it writes to; `useLogFollow` reads that log's tail once and then streams what
 * the job appends on the `log:<name>` topic. Nothing here polls, and closing the page
 * ends the follower on the server.
 */
function GateStep() {
  const [notes, setNotes] = useState<string[]>([]);
  const [full, setFull] = useState(false);
  const [logName, setLogName] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: () =>
      api.post<{ id: number | null; job: string; status: string; log: string; pid: number | null }>(
        '/ops/jobs/healthcheck/run',
        { args: full ? ['--full'] : [] },
      ),
    onSuccess: (result) => {
      setNotes((current) => [
        ...current,
        `started ${result.job} (#${result.id ?? '?'}, pid ${result.pid ?? '?'}) → logs/${result.log}`,
      ]);
      setLogName(result.log);
    },
    onError: (error) => setNotes((current) => [...current, errorMessage(error)]),
  });
  const log = useLogFollow(logName, { tail: 200, stream: true });

  return (
    <Card withBorder padding="md" mt="sm">
      <Stack gap="sm">
        <Text size="sm">
          The week-1 gate is the healthcheck plus the preflight checklist: containers up, data
          fresh, no missed runs, config blessed, mode state verified. Its output streams here
          line by line, already redacted.
        </Text>
        <Checkbox
          label="Run the full check (slower: includes exchange reachability)"
          checked={full}
          onChange={(event) => setFull(event.currentTarget.checked)}
        />
        <Group>
          <Button
            leftSection={<IconRocket size={16} />}
            loading={run.isPending}
            onClick={() => run.mutate()}
          >
            Run the week-1 gate
          </Button>
          <Button component={Link} to="/invariants" variant="light">
            Open Invariants
          </Button>
          {logName ? (
            <Badge
              variant="light"
              color={log.following ? 'teal' : 'yellow'}
              data-testid="gate-stream-status"
            >
              {log.following ? `streaming logs/${logName}` : 'arming…'}
            </Badge>
          ) : null}
        </Group>
        {notes.map((line, index) => (
          <Text key={`${index}-${line}`} size="xs" ff="monospace">
            {line}
          </Text>
        ))}
        {log.error ? (
          <Text size="xs" c="red">
            {log.error}
          </Text>
        ) : null}
        {logName ? (
          <Card withBorder padding={0} radius="md">
            <ScrollArea h={260}>
              <Code block style={{ whiteSpace: 'pre', fontSize: 12 }} data-testid="gate-output">
                {log.lines.length > 0 ? log.lines.join('\n') : '(waiting for output)'}
              </Code>
            </ScrollArea>
          </Card>
        ) : null}
      </Stack>
    </Card>
  );
}
