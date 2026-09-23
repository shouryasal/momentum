/**
 * Models & Sign-in (spec 12, page 19 — still `/secrets`).
 *
 * The page is built around the same asymmetry as the API: a field can be written but
 * never read. Every input is empty on every render — there is no "current value" to
 * pre-fill, because the console does not have one — and a credential is only ever
 * reported as present or absent.
 *
 * What changed, and why: the owner said this page *"has too much content"*. Adding a key
 * meant reading a fourteen-row table with raw variable names, an auth-mode radio group, a
 * strip of probe buttons and two paragraphs about unattended runs and billing. So the
 * page is now three things — sign in to Claude, add your exchange keys, and one line about
 * the local model — and everything else lives under **Advanced**, one click down the same
 * page. Nothing became unreachable: the same table, the same auth-mode radio, the same
 * probes, the same warnings, all still here, all still behind `ConfirmDialog` with
 * `requireStepUp`.
 */
import {
  Alert, Badge, Button, Card, Code, Group, Loader, PasswordInput, Radio, Stack, Table,
  Text, Textarea, Title, Tooltip, UnstyledButton,
} from '@mantine/core';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  IconAlertTriangle, IconChevronDown, IconChevronRight, IconKey, IconPlugConnected, IconTrash,
} from '@tabler/icons-react';
import { useCallback, useMemo, useState } from 'react';

import { errorMessage } from '@/api';
import { useApi } from '@/app/ApiContext';
import { usePageCommands } from '@/app/commandRegistry';
import { useTopicEvents } from '@/app/EventStreamContext';
import { useSession } from '@/app/SessionContext';
import { ConfirmDialog, EmptyState, PageIntro } from '@/components';

import {
  secretKeys,
  secretsApi,
  TARGET_FOR_SECRET,
  type AuthMode,
  type SecretRow,
  type TestTarget,
  type TestVerdict,
} from './api';
import { ClaudeAccessCard, ClaudeSourcesTable } from './components/ClaudeAccessCard';
import { ClaudeSignInModal } from './components/ClaudeSignInModal';
import { ExchangeKeysCard, type KeyPair } from './components/ExchangeKeysCard';
import { LocalModelCard } from './components/LocalModelCard';

const AUTH_HELP: Record<AuthMode, string> = {
  subscription:
    'Every model job gets CLAUDE_CODE_OAUTH_TOKEN only. The API key is stripped, because ' +
    'a present key preempts subscription auth in headless runs. No metered spend.',
  api_key:
    'Every model job gets ANTHROPIC_API_KEY only. Usage is billed per token; the hard ' +
    'monthly cap in models.yaml (auth.api_key_monthly_cap_usd) still applies.',
  auto:
    'Jobs get the subscription token plus the API key under the name ' +
    'EARN_FALLBACK_ANTHROPIC_API_KEY, which the CLI can never pick up implicitly. Only an ' +
    'explicit fallback attempt spends money, and the monthly cap bounds it.',
};

function groupLabel(group: string): string {
  return group.charAt(0).toUpperCase() + group.slice(1);
}

export function SecretsPage() {
  const client = useApi();
  const api = useMemo(() => secretsApi(client), [client]);
  const queryClient = useQueryClient();
  const { stepUpActive } = useSession();

  /**
   * Do the write. `ConfirmDialog` has already opened the step-up window for us — it calls
   * `session.stepUp` itself before `onConfirm`, so re-sending the token here would only
   * double the `POST /api/auth/step-up`. The token stays in the signature because the
   * dialog still hands it over and a caller may need to re-present it.
   */
  const withStepUp = async (_token: string | undefined, run: () => Promise<unknown>) => {
    await run();
  };

  const [editing, setEditing] = useState<SecretRow | null>(null);
  const [draft, setDraft] = useState('');
  const [pair, setPair] = useState<KeyPair | null>(null);
  const [pairKey, setPairKey] = useState('');
  const [pairSecret, setPairSecret] = useState('');
  const [deleting, setDeleting] = useState<SecretRow | null>(null);
  const [pendingMode, setPendingMode] = useState<AuthMode | null>(null);
  const [modeReason, setModeReason] = useState('');
  const [confirmMode, setConfirmMode] = useState(false);
  const [verdicts, setVerdicts] = useState<Record<string, TestVerdict>>({});
  const [error, setError] = useState<string | null>(null);
  const [signInOpen, setSignInOpen] = useState(false);
  const [advanced, setAdvanced] = useState(false);

  const list = useQuery({ queryKey: secretKeys.list, queryFn: api.list });
  const invalidate = () => queryClient.invalidateQueries({ queryKey: secretKeys.list });

  /**
   * The sign-in status. Polled once a second *only* while a session is live — the flow is
   * a supervised process, not a row, so there is nothing else to watch it with; the SSE
   * topic below makes the poll a backstop rather than the mechanism.
   */
  const signIn = useQuery({
    queryKey: secretKeys.signIn,
    queryFn: api.signInStatus,
    refetchInterval: (query) => {
      const session = query.state.data?.session;
      return session && !session.terminal ? 1000 : false;
    },
  });

  const localModel = useQuery({
    queryKey: secretKeys.localModel,
    queryFn: api.localModel,
    refetchInterval: (query) => (query.state.data?.state === 'pulling' ? 2000 : 30_000),
  });

  const refreshSignIn = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: secretKeys.signIn });
    void queryClient.invalidateQueries({ queryKey: secretKeys.list });
  }, [queryClient]);

  useTopicEvents(['claude_auth'], refreshSignIn);

  const save = useMutation({
    mutationFn: ({ name, value }: { name: string; value: string }) =>
      api.setSecret(name, value),
    onSuccess: () => {
      setEditing(null);
      setDraft('');
      setError(null);
      invalidate();
    },
    onError: (e: unknown) => setError(errorMessage(e)),
  });

  /** A key and its secret are one thing to the operator, so they are saved together. */
  const savePair = useMutation({
    mutationFn: async ({ target, key, secret }: { target: KeyPair; key: string; secret: string }) => {
      await api.setSecret(target.keyName, key);
      await api.setSecret(target.secretName, secret);
    },
    onSuccess: () => {
      setPair(null);
      setPairKey('');
      setPairSecret('');
      setError(null);
      invalidate();
    },
    onError: (e: unknown) => setError(errorMessage(e)),
  });

  const remove = useMutation({
    mutationFn: (name: string) => api.deleteSecret(name),
    onSuccess: () => {
      setDeleting(null);
      invalidate();
    },
    onError: (e: unknown) => setError(errorMessage(e)),
  });

  const setMode = useMutation({
    mutationFn: ({ mode, reason }: { mode: AuthMode; reason: string }) =>
      api.setAuthMode(mode, reason || undefined),
    onSuccess: () => {
      setPendingMode(null);
      setModeReason('');
      setConfirmMode(false);
      setError(null);
      invalidate();
    },
    onError: (e: unknown) => setError(errorMessage(e)),
  });

  const test = useMutation({
    mutationFn: (target: TestTarget) => api.test(target),
    onSuccess: (verdict) =>
      setVerdicts((current) => ({ ...current, [verdict.target]: verdict })),
    onError: (e: unknown) => setError(errorMessage(e)),
  });

  usePageCommands('secrets', [
    {
      id: 'claude-signin',
      title: 'Sign in to Claude',
      subtitle: 'Runs `claude setup-token` for you and stores the result',
      keywords: ['oauth', 'token', 'login', 'subscription'],
      run: (ctx) => {
        setSignInOpen(true);
        ctx.close();
      },
    },
    {
      id: 'advanced',
      title: 'Show the advanced credentials',
      subtitle: 'The full credential table, the auth mode and the connection probes',
      run: (ctx) => {
        setAdvanced(true);
        ctx.close();
      },
    },
    {
      id: 'test-telegram',
      title: 'Test the Telegram credential',
      keywords: ['probe', 'alerts'],
      run: (ctx) => {
        test.mutate('telegram');
        ctx.close();
      },
    },
    {
      id: 'test-binance-a',
      title: 'Test the Binance key for the Rules bot',
      subtitle: 'Permission probe; the key itself never leaves the host',
      run: (ctx) => {
        test.mutate('binance_a');
        ctx.close();
      },
    },
    {
      id: 'refresh',
      title: 'Refresh the credential list',
      run: (ctx) => {
        void list.refetch();
        ctx.close();
      },
    },
  ]);

  const auth = list.data?.auth;
  const byGroup = useMemo(() => {
    const out = new Map<string, SecretRow[]>();
    for (const row of list.data?.secrets ?? []) {
      out.set(row.group, [...(out.get(row.group) ?? []), row]);
    }
    return out;
  }, [list.data]);

  return (
    <Stack gap="md">
      <PageIntro
        title="Models & Sign-in"
        blurb="Sign in to Claude, add your exchange keys, and check the local model is there."
      />

      {error ? <Alert color="red" onClose={() => setError(null)} withCloseButton>{error}</Alert> : null}

      {/* This one stays on the front page even though it is technical: the two files
          disagreeing about which credential to use is how a subscription run quietly
          becomes a metered one, and that is money. */}
      {auth && !auth.in_sync ? (
        <Alert color="orange" icon={<IconAlertTriangle size={18} />} title="Auth mode is out of step">
          <Code>models.yaml</Code> says <b>{auth.configured}</b> but <Code>.env</Code> says{' '}
          <b>{auth.env ?? 'nothing'}</b>. Jobs follow <Code>.env</Code> — re-save the mode under
          Advanced to bring them back together.
        </Alert>
      ) : null}

      {/* 1. Claude: signed in, what it costs, one button. */}
      <ClaudeAccessCard
        compact
        auth={auth}
        signIn={signIn.data}
        secrets={list.data?.secrets ?? []}
        onSignIn={() => setSignInOpen(true)}
        onTest={(target) => test.mutate(target)}
        testing={test.isPending ? (test.variables as TestTarget) : null}
        verdicts={verdicts}
      />

      {/* 2. The exchange keys: added, or a button to add them. */}
      <ExchangeKeysCard
        secrets={list.data?.secrets ?? []}
        onAdd={(target) => {
          setPair(target);
          setPairKey('');
          setPairSecret('');
        }}
      />

      {/* 3. The local model: one line, read-only. */}
      <LocalModelCard status={localModel.data} loading={localModel.isLoading} detailed={advanced} />

      <ClaudeSignInModal
        opened={signInOpen}
        onClose={() => setSignInOpen(false)}
        api={api}
        status={signIn.data}
        onChanged={refreshSignIn}
      />

      {list.isLoading ? <Loader size="sm" /> : null}

      {/* Everything else, one click down. */}
      <UnstyledButton onClick={() => setAdvanced((open) => !open)} data-testid="secrets-advanced-toggle">
        <Group gap={6}>
          {advanced ? <IconChevronDown size={16} /> : <IconChevronRight size={16} />}
          <Text size="sm" fw={600}>
            Advanced
          </Text>
          <Text size="xs" c="dimmed">
            every credential, which one is in effect, and the connection tests
          </Text>
        </Group>
      </UnstyledButton>

      {advanced ? (
        <Stack gap="md" data-testid="secrets-advanced">
          <Card withBorder padding="md" radius="md" data-testid="auth-mode-card">
            <Group justify="space-between" mb="sm">
              <Title order={5}>Claude auth mode</Title>
              <Badge variant="light">effective: {auth?.effective ?? '—'}</Badge>
            </Group>
            <Text size="xs" c="dimmed" mb="sm">
              Which credential <Code>ops/envwrap.sh</Code> hands to every model job. Changing
              it writes <Code>models.yaml</Code> and <Code>.env</Code> together.
            </Text>
            <Radio.Group
              value={pendingMode ?? auth?.effective ?? 'subscription'}
              onChange={(value) => setPendingMode(value as AuthMode)}
            >
              <Stack gap="sm">
                {(['subscription', 'api_key', 'auto'] as AuthMode[]).map((mode) => (
                  <Radio key={mode} value={mode} label={mode} description={AUTH_HELP[mode]} />
                ))}
              </Stack>
            </Radio.Group>

            {pendingMode && pendingMode !== auth?.effective ? (
              <Stack gap="xs" mt="md">
                {pendingMode !== 'subscription' ? (
                  <Alert color="yellow" variant="light" icon={<IconAlertTriangle size={16} />}>
                    This mode can spend money. Metered API usage is billed per token and is
                    bounded only by <Code>auth.api_key_monthly_cap_usd</Code>.
                  </Alert>
                ) : null}
                <Textarea
                  label="reason (goes into config_audit)"
                  autosize
                  minRows={2}
                  value={modeReason}
                  onChange={(event) => setModeReason(event.currentTarget.value)}
                />
                <Group>
                  <Button loading={setMode.isPending} onClick={() => setConfirmMode(true)}>
                    Save auth mode (step-up)
                  </Button>
                  <Button variant="subtle" onClick={() => setPendingMode(null)}>Cancel</Button>
                </Group>
              </Stack>
            ) : null}

            <ClaudeSourcesTable
              auth={auth}
              signIn={signIn.data}
              secrets={list.data?.secrets ?? []}
              onTest={(target) => test.mutate(target)}
              testing={test.isPending ? (test.variables as TestTarget) : null}
              verdicts={verdicts}
            />

            <Stack gap={4} mt="md">
              <Text size="xs" c="dimmed">
                Subscription source: <Code>{auth?.subscription_source ?? 'token'}</Code>.{' '}
                {auth?.subscription_source === 'token'
                  ? 'Model jobs are handed CLAUDE_CODE_OAUTH_TOKEN, which the sign-in above ' +
                    'writes for you.'
                  : 'The CLI uses its own ~/.claude/.credentials.json.'}
              </Text>
              <Group gap="xs" mt={4}>
                {(['claude_subscription', 'claude_login', 'claude_api_key', 'ollama'] as TestTarget[])
                  .map((target) => (
                    <Button
                      key={target}
                      size="compact-xs"
                      variant="light"
                      leftSection={<IconPlugConnected size={12} />}
                      loading={test.isPending && test.variables === target}
                      onClick={() => test.mutate(target)}
                    >
                      {target}
                    </Button>
                  ))}
              </Group>
              {(['claude_subscription', 'claude_login', 'claude_api_key', 'ollama'] as TestTarget[])
                .filter((target) => verdicts[target])
                .map((target) => (
                  <Text key={target} size="xs" c={verdicts[target]?.ok ? 'teal' : 'red'}>
                    {target}: {verdicts[target]?.detail}
                  </Text>
                ))}
            </Stack>
          </Card>

          {!list.isLoading && byGroup.size === 0 ? (
            <EmptyState
              title="No secrets declared"
              description="The rows come from the declared credential list; a missing one shows as absent, not as nothing."
            />
          ) : null}

          {[...byGroup.entries()].map(([group, rows]) => (
            <Card key={group} withBorder padding="md" radius="md">
              <Title order={5} mb="sm">{groupLabel(group)}</Title>
              <Table.ScrollContainer minWidth={780}>
                <Table data-testid={`secrets-${group}`}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th>secret</Table.Th>
                      <Table.Th>present</Table.Th>
                      <Table.Th>last4</Table.Th>
                      <Table.Th>updated</Table.Th>
                      <Table.Th>used by</Table.Th>
                      <Table.Th />
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {rows.map((row) => {
                      const target = TARGET_FOR_SECRET[row.name];
                      const verdict = target ? verdicts[target] : undefined;
                      return (
                        <Table.Tr key={row.name}>
                          <Table.Td>
                            <Tooltip label={row.help || row.label} multiline w={360} withArrow>
                              <div>
                                <Code>{row.name}</Code>
                                <Text size="xs" c="dimmed">{row.label}</Text>
                              </div>
                            </Tooltip>
                          </Table.Td>
                          <Table.Td>
                            {row.present ? (
                              <Badge color="teal" variant="light">set</Badge>
                            ) : (
                              <Badge color={row.required ? 'red' : 'gray'} variant="outline">
                                {row.required ? 'required' : 'empty'}
                              </Badge>
                            )}
                          </Table.Td>
                          <Table.Td><Code>{row.last4 ? `…${row.last4}` : '—'}</Code></Table.Td>
                          <Table.Td>
                            <Text size="xs" c="dimmed">{row.updated_at ?? '—'}</Text>
                          </Table.Td>
                          <Table.Td>
                            <Text size="xs" c="dimmed">{row.used_by.join(', ') || '—'}</Text>
                          </Table.Td>
                          <Table.Td>
                            <Group gap={4} justify="flex-end" wrap="nowrap">
                              <Button
                                size="compact-xs"
                                variant="light"
                                leftSection={<IconKey size={12} />}
                                onClick={() => {
                                  setEditing(row);
                                  setDraft('');
                                }}
                              >
                                Set
                              </Button>
                              {target ? (
                                <Button
                                  size="compact-xs"
                                  variant="subtle"
                                  loading={test.isPending && test.variables === target}
                                  onClick={() => test.mutate(target)}
                                >
                                  Test
                                </Button>
                              ) : null}
                              <Button
                                size="compact-xs"
                                variant="subtle"
                                color="red"
                                disabled={!row.present}
                                onClick={() => setDeleting(row)}
                              >
                                <IconTrash size={12} />
                              </Button>
                            </Group>
                            {verdict ? (
                              <Text size="xs" c={verdict.ok ? 'teal' : 'red'} ta="right">
                                {verdict.detail}
                              </Text>
                            ) : null}
                          </Table.Td>
                        </Table.Tr>
                      );
                    })}
                  </Table.Tbody>
                </Table>
              </Table.ScrollContainer>
            </Card>
          ))}
        </Stack>
      ) : null}

      <ConfirmDialog
        opened={confirmMode}
        onCancel={() => setConfirmMode(false)}
        title={`Switch Claude auth mode to ${pendingMode ?? ''}?`}
        description={
          'This writes models.yaml and .env together, so the next cron job uses the new ' +
          'credential. ' + (pendingMode ? AUTH_HELP[pendingMode] : '')
        }
        confirmLabel="Switch"
        danger={pendingMode !== 'subscription'}
        requireStepUp
        stepUpSatisfied={stepUpActive}
        onConfirm={({ stepUpToken }) =>
          withStepUp(stepUpToken, async () => {
            if (pendingMode) {
              await setMode.mutateAsync({ mode: pendingMode, reason: modeReason });
            }
          })
        }
      />

      <ConfirmDialog
        opened={pair != null}
        onCancel={() => {
          setPair(null);
          setPairKey('');
          setPairSecret('');
        }}
        title={pair ? pair.title : ''}
        description={
          'Give the key Reading and Spot trading permission only, with withdrawals disabled. ' +
          'The console can store it and prove it works; it can never show it to you again.'
        }
        confirmLabel="Save"
        requireStepUp
        stepUpSatisfied={stepUpActive}
        confirmDisabled={!pairKey.trim() || !pairSecret.trim()}
        onConfirm={({ stepUpToken }) =>
          withStepUp(stepUpToken, async () => {
            if (pair) await savePair.mutateAsync({ target: pair, key: pairKey, secret: pairSecret });
          })
        }
      >
        <Stack gap="sm">
          <PasswordInput
            label="API key"
            description="Write-only: this field is never pre-filled, and no route returns it."
            value={pairKey}
            onChange={(event) => setPairKey(event.currentTarget.value)}
            autoComplete="off"
            data-autofocus
          />
          <PasswordInput
            label="API secret"
            value={pairSecret}
            onChange={(event) => setPairSecret(event.currentTarget.value)}
            autoComplete="off"
          />
        </Stack>
      </ConfirmDialog>

      <ConfirmDialog
        opened={editing != null}
        onCancel={() => {
          setEditing(null);
          setDraft('');
        }}
        title={editing ? `Set ${editing.name}` : ''}
        description={editing?.help || editing?.label}
        confirmLabel="Save"
        requireStepUp
        stepUpSatisfied={stepUpActive}
        confirmDisabled={!draft.trim()}
        onConfirm={({ stepUpToken }) =>
          withStepUp(stepUpToken, async () => {
            if (editing) await save.mutateAsync({ name: editing.name, value: draft });
          })
        }
      >
        <PasswordInput
          label="new value"
          description="Write-only: this field is never pre-filled, and no route returns it."
          value={draft}
          onChange={(event) => setDraft(event.currentTarget.value)}
          autoComplete="off"
          data-autofocus
        />
      </ConfirmDialog>

      <ConfirmDialog
        opened={deleting != null}
        onCancel={() => setDeleting(null)}
        title={deleting ? `Remove ${deleting.name}?` : ''}
        description={
          `The jobs that use it (${deleting?.used_by.join(', ') || 'none'}) will fail ` +
          'their credential guard until it is set again.'
        }
        confirmLabel="Remove"
        danger
        requireStepUp
        stepUpSatisfied={stepUpActive}
        onConfirm={({ stepUpToken }) =>
          withStepUp(stepUpToken, async () => {
            if (deleting) await remove.mutateAsync(deleting.name);
          })
        }
      />
    </Stack>
  );
}

export default SecretsPage;
