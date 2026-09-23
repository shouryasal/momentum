/**
 * "Is Claude signed in, with what, and what does that cost?" — answered above the fold.
 *
 * There are three ways this system can authenticate to Claude and they are not
 * interchangeable: a subscription token written into `.env`, the CLI's own login session
 * in `~/.claude/.credentials.json`, and a metered API key. Before this card the console
 * showed them as three unrelated rows in a table of fourteen secrets, and the one question
 * an operator actually has — *which one will the 08:30 run use, and will it bill me?* —
 * had no answer anywhere.
 *
 * So: one verdict sentence, one button, and a three-row table where each row says what it
 * is, whether it is there, whether it is the one in effect, what it does to billing and
 * whether it survives a headless cron run. Nothing here is a value; presence and `…last4`
 * are all the console ever knows.
 */
import { Alert, Badge, Button, Card, Code, Group, Stack, Table, Text, Title } from '@mantine/core';
import { IconAlertTriangle, IconKey, IconPlugConnected } from '@tabler/icons-react';

import type { AuthState, SecretRow, SignInStatus, TestTarget, TestVerdict } from '../api';

import { ENDINGS } from './ClaudeSignInModal';

export interface ClaudeAccessCardProps {
  auth: AuthState | undefined;
  signIn: SignInStatus | undefined;
  secrets: SecretRow[];
  onSignIn: () => void;
  onTest: (target: TestTarget) => void;
  testing: TestTarget | null;
  verdicts: Record<string, TestVerdict>;
  /**
   * The short form: signed in or not, one line about what it costs, one button.
   *
   * The owner's words about this page were *"too much content"*, and the three-row
   * source table was most of it — three credentials, five columns, on a page whose whole
   * job is "am I signed in?". The table is not gone; it moved under Advanced on the same
   * page, where the person who needs to know which of the three is in effect still finds
   * it. See {@link ClaudeSourcesTable}.
   */
  compact?: boolean;
}

type Effect = 'in use' | 'fallback' | 'standby';

interface SourceRow {
  key: string;
  target: TestTarget;
  name: string;
  where: string;
  present: boolean;
  detail: string;
  effect: Effect;
  billing: string;
  headless: string;
}

const EFFECT_COLOR: Record<Effect, string> = {
  'in use': 'teal',
  fallback: 'yellow',
  standby: 'gray',
};

/**
 * Which of the three the model jobs will actually authenticate with.
 *
 * Mirrors `ops/envwrap.sh` and `ops.lib.claude_auth`: the auth mode decides which
 * credential a job is handed at all, and `auth.subscription_source` decides whether a
 * subscription job uses the token in `.env` or lets the CLI read its own login session.
 */
export function effectOf(auth: AuthState | undefined, key: string): Effect {
  const mode = auth?.effective ?? 'subscription';
  const source = auth?.subscription_source ?? 'token';
  if (mode === 'api_key') return key === 'api_key' ? 'in use' : 'standby';
  const subscription = source === 'login' ? 'login' : 'token';
  if (key === subscription) return 'in use';
  if (key === 'api_key') return mode === 'auto' ? 'fallback' : 'standby';
  return 'standby';
}

/** The three ways this system can authenticate to Claude, and which one is in effect. */
export function claudeSources(
  auth: AuthState | undefined,
  signIn: SignInStatus | undefined,
  secrets: SecretRow[],
): SourceRow[] {
  const token = secrets.find((row) => row.name === 'CLAUDE_CODE_OAUTH_TOKEN');
  const apiKey = secrets.find((row) => row.name === 'ANTHROPIC_API_KEY');
  const login = auth?.login_session;
  const credential = signIn?.credential;
  const tokenPresent = credential?.present ?? token?.present ?? false;
  const tokenLast4 = credential?.last4 ?? token?.last4 ?? null;

  const rows: SourceRow[] = [
    {
      key: 'token',
      target: 'claude_subscription',
      name: 'Subscription token',
      where: 'CLAUDE_CODE_OAUTH_TOKEN in .env',
      present: tokenPresent,
      detail: tokenPresent ? `set …${tokenLast4}` : 'not set',
      effect: effectOf(auth, 'token'),
      billing: 'Included in your Claude subscription. No per-token charge.',
      headless: 'Works unattended. This is what the scheduled runs are given.',
    },
    {
      key: 'login',
      target: 'claude_login',
      name: 'CLI login session',
      where: '~/.claude/.credentials.json',
      present: Boolean(login?.present),
      detail: login?.present
        ? `signed in${login.expired ? ' but EXPIRED' : ''}` +
          (login.expires_at ? ` until ${login.expires_at}` : '')
        : 'no session on this machine',
      effect: effectOf(auth, 'login'),
      billing: 'Included in your Claude subscription. No per-token charge.',
      headless:
        'Only while the session is valid; it expires and has to be renewed by hand. ' +
        'A token does not.',
    },
    {
      key: 'api_key',
      target: 'claude_api_key',
      name: 'Anthropic API key',
      where: 'ANTHROPIC_API_KEY in .env',
      present: Boolean(apiKey?.present),
      detail: apiKey?.present ? `set …${apiKey.last4}` : 'not set',
      effect: effectOf(auth, 'api_key'),
      billing: 'Metered — billed per token, bounded by auth.api_key_monthly_cap_usd.',
      headless: 'Works everywhere. Only used when the auth mode allows it.',
    },
  ];
  return rows;
}

export function ClaudeAccessCard({
  auth, signIn, secrets, onSignIn, onTest, testing, verdicts, compact = false,
}: ClaudeAccessCardProps) {
  const rows = claudeSources(auth, signIn, secrets);
  const login = auth?.login_session;

  const inUse = rows.find((row) => row.effect === 'in use');
  const ready = Boolean(inUse?.present);
  const running = Boolean(signIn?.session && !signIn.session.terminal);
  /**
   * The last attempt, when it ended badly. Without this the modal is the only place a
   * failure ever appears, and closing it would leave "not signed in" with no reason —
   * which is how an operator ends up running `claude setup-token` by hand anyway.
   */
  const last = signIn?.session;
  const failed =
    last?.terminal && last.state === 'failed' && last.reason ? ENDINGS[last.reason] : undefined;

  return (
    <Card withBorder padding="md" radius="md" data-testid="claude-access">
      <Group justify="space-between" align="flex-start" wrap="wrap">
        <Stack gap={2} style={{ flex: 1, minWidth: 260 }}>
          <Group gap="xs">
            <Title order={5}>Claude access</Title>
            <Badge color={ready ? 'teal' : 'red'} variant="light" data-testid="claude-verdict">
              {ready ? 'signed in' : 'not signed in'}
            </Badge>
            {running ? (
              <Badge
                color={last?.state === 'awaiting_code' ? 'indigo' : 'blue'}
                variant="light"
                data-testid="claude-signin-running"
              >
                {/* A run that is waiting on the operator has to say so here too: the modal
                    can be closed, and "sign-in running" would look like progress. */}
                {last?.state === 'awaiting_code' ? 'waiting for your code' : 'sign-in running'}
              </Badge>
            ) : null}
          </Group>
          <Text size="sm" c="dimmed">
            {ready
              ? `Model jobs authenticate with your ${inUse?.name.toLowerCase()}. ${inUse?.billing}`
              : 'No usable credential. Every job that needs a model — the brief, the ' +
                'decision, the review — will abstain until you sign in.'}
          </Text>
        </Stack>
        <Button
          leftSection={<IconKey size={16} />}
          onClick={onSignIn}
          variant={ready ? 'light' : 'filled'}
          data-testid="claude-signin-button"
        >
          {running ? 'Show sign-in' : ready ? 'Sign in again' : 'Sign in to Claude'}
        </Button>
      </Group>

      {signIn?.cli && !signIn.cli.present ? (
        <Alert color="orange" mt="sm" variant="light" icon={<IconAlertTriangle size={16} />}>
          The Claude CLI is not installed in WSL, so the console cannot sign in for you yet.
        </Alert>
      ) : null}
      {failed ? (
        <Alert
          color="orange"
          mt="sm"
          variant="light"
          icon={<IconAlertTriangle size={16} />}
          title={`Last sign-in attempt: ${failed.title.toLowerCase()}`}
          data-testid="claude-last-attempt"
        >
          <Text size="sm">{failed.what} Start again when you are ready.</Text>
        </Alert>
      ) : null}
      {login?.present && login.expired ? (
        <Alert color="orange" mt="sm" variant="light" icon={<IconAlertTriangle size={16} />}>
          The CLI login session on this machine has expired. A subscription token does not
          expire — signing in here writes one.
        </Alert>
      ) : null}

      {compact ? null : (
        <ClaudeSourcesTable
          auth={auth}
          signIn={signIn}
          secrets={secrets}
          onTest={onTest}
          testing={testing}
          verdicts={verdicts}
        />
      )}
    </Card>
  );
}

export interface ClaudeSourcesTableProps {
  auth: AuthState | undefined;
  signIn: SignInStatus | undefined;
  secrets: SecretRow[];
  onTest: (target: TestTarget) => void;
  testing: TestTarget | null;
  verdicts: Record<string, TestVerdict>;
}

/**
 * Which of the three credentials a model job is handed, in full.
 *
 * This is the page's Advanced content, not its front door: three sources, what each does
 * to billing, and whether it survives an unattended run. An operator adding a key does
 * not need it; an operator debugging *why the 08:30 run billed me* needs nothing else.
 */
export function ClaudeSourcesTable({
  auth, signIn, secrets, onTest, testing, verdicts,
}: ClaudeSourcesTableProps) {
  const rows = claudeSources(auth, signIn, secrets);
  return (
    <>
      <Table.ScrollContainer minWidth={720} mt="md">
        <Table data-testid="claude-sources" verticalSpacing="xs">
          <Table.Thead>
            <Table.Tr>
              <Table.Th>source</Table.Th>
              <Table.Th>status</Table.Th>
              <Table.Th>in effect</Table.Th>
              <Table.Th>billing</Table.Th>
              <Table.Th>unattended runs</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {rows.map((row) => {
              const verdict = verdicts[row.target];
              return (
                <Table.Tr key={row.key} data-testid={`claude-source-${row.key}`}>
                  <Table.Td>
                    <Text size="sm" fw={500}>{row.name}</Text>
                    <Code>{row.where}</Code>
                  </Table.Td>
                  <Table.Td>
                    <Badge variant="light" color={row.present ? 'teal' : 'gray'}>
                      {row.detail}
                    </Badge>
                  </Table.Td>
                  <Table.Td>
                    <Badge variant={row.effect === 'in use' ? 'filled' : 'outline'}
                           color={EFFECT_COLOR[row.effect]}>
                      {row.effect}
                    </Badge>
                  </Table.Td>
                  <Table.Td><Text size="xs">{row.billing}</Text></Table.Td>
                  <Table.Td><Text size="xs">{row.headless}</Text></Table.Td>
                  <Table.Td>
                    <Button
                      size="compact-xs"
                      variant="subtle"
                      leftSection={<IconPlugConnected size={12} />}
                      loading={testing === row.target}
                      onClick={() => onTest(row.target)}
                    >
                      Test
                    </Button>
                    {verdict ? (
                      <Text size="xs" c={verdict.ok ? 'teal' : 'red'}>{verdict.detail}</Text>
                    ) : null}
                  </Table.Td>
                </Table.Tr>
              );
            })}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>

      <Text size="xs" c="dimmed" mt="xs">
        Auth mode <b>{auth?.effective ?? 'subscription'}</b> decides which of these a job is
        handed.
      </Text>
    </>
  );
}

export default ClaudeAccessCard;
