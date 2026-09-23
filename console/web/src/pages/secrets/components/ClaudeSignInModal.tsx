/**
 * Signing in to Claude without opening a terminal.
 *
 * The whole flow the owner used to do by hand — open Ubuntu, run `claude setup-token`,
 * copy the URL out of the terminal into Windows, approve it, copy the code back into the
 * waiting CLI, then copy the token into `.env` — happens on the server. This file is the
 * three things the operator still has to do: say "yes, start it" (step-up, because it ends
 * in a credential being written), open the link, and paste back the code the browser shows.
 *
 * That last one is not optional and not cosmetic. The CLI's `redirect_uri` is the
 * *platform.claude.com* callback page, not a loopback port, so approving in the browser
 * ends with a code on screen and a CLI still waiting for it. A modal without a box for it
 * is a modal that spins forever.
 *
 * Four rules shape it.
 *
 * 1. **The token is never here.** Nothing in `SignInStatus` can carry one; the modal shows
 *    a phase, a link, a sentence and `…last4`. If a value ever appeared in this component
 *    it would mean the server contract broke, and `secrets.test.tsx` asserts it does not.
 * 2. **The link is the point.** The CLI waits inside WSL, which has no browser; the one
 *    thing that can go wrong for a first-time operator is not realising the link must be
 *    opened on Windows. So it is a button, a copyable URL and a sentence saying exactly
 *    that — not a line of log output.
 * 3. **The phase is always named.** The page polls once a second and renders `phase`
 *    verbatim, so a flow that stops moving says which step it stopped on.
 * 4. **Every ending is explained.** `reason` is a closed set on the server, and each value
 *    maps here to what happened, whether anything was written (never) and the one command
 *    that fixes it. A spinner that stops spinning is not an answer.
 */
import {
  Alert, Anchor, Badge, Button, Code, CopyButton, Group, List, Loader, Modal, Stack, Text,
  TextInput,
} from '@mantine/core';
import {
  IconAlertTriangle, IconBrandWindows, IconCheck, IconCopy, IconExternalLink, IconKey,
} from '@tabler/icons-react';
import { useMutation } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';

import { errorMessage } from '@/api';
import { useSession } from '@/app/SessionContext';
import { ConfirmDialog } from '@/components';

import type { SecretsApi, SignInReason, SignInStatus } from '../api';

export interface ClaudeSignInModalProps {
  opened: boolean;
  onClose: () => void;
  api: SecretsApi;
  /** The latest `GET /api/llm/claude/signin`; the page polls it while a session is live. */
  status: SignInStatus | undefined;
  /** Refetch the sign-in status and the secret table after anything changes. */
  onChanged: () => void;
}

/** What each terminal `reason` means, and the one command that answers it. */
export const ENDINGS: Record<SignInReason, { title: string; what: string; command?: string }> = {
  cli_missing: {
    title: 'The Claude CLI is not installed in WSL',
    what: 'Nothing ran and nothing was written. Install the CLI inside Ubuntu, then try again.',
    command: 'npm i -g @anthropic-ai/claude-code',
  },
  unsupported: {
    title: 'This host cannot give the CLI a terminal',
    what:
      '`claude setup-token` only runs under a pseudo-terminal. Start the console inside ' +
      'WSL (Ubuntu) rather than from Windows, and sign in from there.',
  },
  no_link: {
    title: 'The CLI never printed a sign-in link',
    what:
      'Nothing was written. Run the command once in a WSL terminal to see what it is ' +
      'waiting for — usually a CLI update.',
    command: 'claude setup-token',
  },
  not_approved: {
    title: 'The link was never approved',
    what:
      'Nothing was written. The link has to be opened in your Windows browser and ' +
      'approved while the CLI waits; start again when the browser is ready.',
  },
  no_code: {
    title: 'The code from the browser was never pasted back',
    what:
      'Nothing was written. Approving the link ends on a page showing a code, and the ' +
      'CLI waits for it — start again with that page open and paste the code here.',
  },
  bad_code: {
    title: 'The CLI would not accept that code',
    what:
      'Nothing was written. Codes are single-use and expire quickly: start again, and ' +
      'copy the whole code from the browser page in one go rather than retyping it.',
  },
  cli_failed: {
    title: 'The Claude CLI stopped with an error',
    what:
      'Nothing was written. Run it once in a WSL terminal to see the message it printed.',
    command: 'claude setup-token',
  },
  no_token: {
    title: 'The CLI finished without printing a token',
    what: 'Nothing was written. This usually means the approval was for a different account.',
    command: 'claude setup-token',
  },
  cancelled: {
    title: 'Sign-in cancelled',
    what: 'The CLI was stopped and nothing was written.',
  },
  store_failed: {
    title: 'The credential could not be saved',
    what:
      'The sign-in worked but the .env writer refused it, so no credential is stored. ' +
      'Check that the file is writable, then try again.',
  },
};

const STEPS = [
  'Start the CLI', 'Approve in your browser', 'Paste the code', 'Store the credential',
] as const;

/**
 * Which step the flow is on — and, for one that ended badly, which step it died on.
 *
 * A failure that never got a link died on step 1; one that got a link and was never
 * approved died on step 2; one that was asked for a code and never got one died on step 3.
 * Reading that off the state alone would put every failure on step 1 and tell the operator
 * to look in the wrong place.
 */
function stepIndex(session: SignInStatus['session']): number {
  if (!session) return 0;
  if (session.state === 'done') return 4;
  if (session.state === 'exchanging') return 3;
  if (session.state === 'awaiting_code') return 2;
  if (session.state === 'url_ready') return 1;
  if (session.terminal) {
    if (session.reason === 'bad_code' || session.reason === 'no_token') return 3;
    if (session.reason === 'no_code' || session.attempts > 0) return 2;
    if (session.url) return 1;
  }
  return 0;
}

/** Seconds left on the server's own deadline, ticking once a second. */
function useCountdown(deadline: string | null | undefined): number | null {
  const [left, setLeft] = useState<number | null>(null);
  useEffect(() => {
    if (!deadline) {
      setLeft(null);
      return;
    }
    const tick = () => {
      const ms = new Date(deadline).getTime() - Date.now();
      setLeft(Number.isFinite(ms) ? Math.max(0, Math.round(ms / 1000)) : null);
    };
    tick();
    const timer = setInterval(tick, 1000);
    return () => clearInterval(timer);
  }, [deadline]);
  return left;
}

export function ClaudeSignInModal({
  opened, onClose, api, status, onChanged,
}: ClaudeSignInModalProps) {
  const { stepUpActive } = useSession();
  const session = status?.session ?? null;
  const live = Boolean(session && !session.terminal);
  const cli = status?.cli;
  const credential = status?.credential;

  /**
   * True once *this* operator started a run, so a run that finishes — or fails instantly,
   * like a missing CLI — still shows its ending instead of snapping back to the question.
   *
   * The ref exists because `ConfirmDialog` calls its `onClose` right after a successful
   * `onConfirm`, and that close handler must not tear down the flow it just started. The
   * state update from `onSuccess` has not been applied by then, so the handler reads the
   * ref, not the state.
   */
  const [armed, setArmed] = useState(false);
  const armedRef = useRef(false);
  const disarm = () => {
    armedRef.current = false;
    setArmed(false);
  };
  const [error, setError] = useState<string | null>(null);
  const [code, setCode] = useState('');
  useEffect(() => {
    if (!opened) {
      armedRef.current = false;
      setArmed(false);
      setError(null);
      setCode('');
    }
  }, [opened]);

  const start = useMutation({
    mutationFn: (replace: boolean) => api.startSignIn(replace),
    onSuccess: () => {
      armedRef.current = true;
      setArmed(true);
      setError(null);
      onChanged();
    },
    onError: (e: unknown) => setError(errorMessage(e)),
  });

  const cancel = useMutation({
    mutationFn: () => api.cancelSignIn(),
    onSuccess: () => onChanged(),
    onError: (e: unknown) => setError(errorMessage(e)),
  });

  /**
   * The step the old modal did not have. The code is typed here, posted, written to the
   * CLI's terminal server-side, and forgotten — it is cleared on success so a rejected
   * code cannot be resubmitted unchanged by a stray Enter.
   */
  const submitCode = useMutation({
    mutationFn: (value: string) => api.submitSignInCode(value),
    onSuccess: () => {
      setCode('');
      setError(null);
      onChanged();
    },
    onError: (e: unknown) => setError(errorMessage(e)),
  });

  const left = useCountdown(live ? session?.deadline_at : null);
  const ending = useMemo(
    () => (session?.terminal && session.reason ? ENDINGS[session.reason] : undefined),
    [session],
  );

  const blocked = cli && (!cli.present || !cli.pty);
  const replacing = Boolean(credential?.present);
  const showProgress = opened && (live || armed);

  return (
    <>
      <ConfirmDialog
        opened={opened && !showProgress}
        onCancel={() => { if (!armedRef.current) onClose(); }}
        title={replacing ? 'Replace the Claude credential?' : 'Sign in to Claude'}
        description={
          'The console runs `claude setup-token` for you in WSL and shows you the link to ' +
          'approve. The token it prints is written straight into .env at 0600 — it is ' +
          'never shown here, logged, or sent to your browser.'
        }
        confirmLabel={replacing ? 'Replace credential' : 'Start sign-in'}
        danger={replacing}
        requireStepUp
        stepUpSatisfied={stepUpActive}
        confirmDisabled={Boolean(blocked)}
        onConfirm={async () => {
          await start.mutateAsync(replacing);
        }}
      >
        <Stack gap="xs">
          {cli && !cli.present ? (
            <Alert color="red" icon={<IconAlertTriangle size={16} />} title={ENDINGS.cli_missing.title}>
              <Text size="sm">{ENDINGS.cli_missing.what}</Text>
              <CommandLine command={ENDINGS.cli_missing.command ?? ''} shell="WSL (Ubuntu)" />
            </Alert>
          ) : null}
          {cli?.present && !cli.pty ? (
            <Alert color="red" icon={<IconAlertTriangle size={16} />} title={ENDINGS.unsupported.title}>
              <Text size="sm">{ENDINGS.unsupported.what}</Text>
            </Alert>
          ) : null}
          {replacing ? (
            <Alert color="yellow" variant="light" icon={<IconAlertTriangle size={16} />}>
              <Code>{credential?.name}</Code> is already set (…{credential?.last4}). A new
              sign-in overwrites it. Jobs keep using the old one until this finishes.
            </Alert>
          ) : null}
          <List size="sm" spacing={4} data-testid="signin-plan">
            <List.Item>The CLI starts in WSL and prints a claude.com link.</List.Item>
            <List.Item>You open that link in your <b>Windows</b> browser and approve it.</List.Item>
            <List.Item>
              The browser shows a short code — paste it back here, and the token it returns
              is stored for you.
            </List.Item>
          </List>
          {cli?.version ? (
            <Text size="xs" c="dimmed">CLI: {cli.version}</Text>
          ) : null}
        </Stack>
      </ConfirmDialog>

      <Modal
        opened={showProgress}
        onClose={onClose}
        title="Signing in to Claude"
        centered
        size="lg"
        closeOnClickOutside={!live}
        data-testid="signin-progress"
      >
        <Stack gap="md">
          <Group gap="xs" data-testid="signin-steps">
            {STEPS.map((label, index) => {
              const at = stepIndex(session);
              const failed = session?.terminal && session.state !== 'done' && index === at;
              return (
                <Badge
                  key={label}
                  variant={index === at && !session?.terminal ? 'filled' : 'light'}
                  color={failed ? 'red' : index < at ? 'teal' : 'gray'}
                  leftSection={
                    index < at ? <IconCheck size={12} />
                      : index === at && live ? <Loader size={10} color="white" /> : undefined
                  }
                >
                  {index + 1}. {label}
                </Badge>
              );
            })}
          </Group>

          {session && session.url
            && (session.state === 'url_ready' || session.state === 'awaiting_code') ? (
            <Alert
              color="blue"
              icon={<IconBrandWindows size={18} />}
              title="Open this link in your Windows browser"
              data-testid="signin-link"
            >
              <Stack gap="xs">
                <Text size="sm">
                  The CLI is waiting inside WSL, which has no browser of its own. Open the
                  link on Windows, sign in with the account your Claude subscription is on,
                  and approve it. This window updates itself — leave it open.
                </Text>
                <Group gap="xs" wrap="nowrap">
                  <Button
                    component="a"
                    href={session.url}
                    target="_blank"
                    rel="noreferrer noopener"
                    leftSection={<IconExternalLink size={14} />}
                  >
                    Open the approval page
                  </Button>
                  <CopyButton value={session.url}>
                    {({ copied, copy }) => (
                      <Button variant="light" leftSection={<IconCopy size={14} />} onClick={copy}>
                        {copied ? 'Copied' : 'Copy link'}
                      </Button>
                    )}
                  </CopyButton>
                </Group>
                <Anchor
                  href={session.url}
                  target="_blank"
                  rel="noreferrer noopener"
                  size="xs"
                  style={{ wordBreak: 'break-all' }}
                >
                  {session.url}
                </Anchor>
                {/* The countdown belongs to whichever wait is actually running: once the
                    CLI asks for the code, it is the code box that is counting down. */}
                {left != null && session.state === 'url_ready' ? (
                  <Text size="xs" c={left < 60 ? 'orange' : 'dimmed'}>
                    Waiting for approval — {Math.floor(left / 60)}m {left % 60}s left before
                    the console gives up. Nothing is written if it does.
                  </Text>
                ) : null}
              </Stack>
            </Alert>
          ) : null}

          {session?.state === 'awaiting_code' ? (
            <Alert
              color="indigo"
              icon={<IconKey size={18} />}
              title="Paste the code from the browser"
              data-testid="signin-code"
            >
              <Stack gap="xs">
                <Text size="sm">
                  After you approve, the page ends by showing an <b>authorization code</b>.
                  The CLI is sitting at its prompt waiting for it — copy it from the browser
                  and paste it here. It is single-use and expires in a minute or two.
                </Text>
                <Group gap="xs" align="flex-end" wrap="nowrap">
                  <TextInput
                    label="Authorization code"
                    placeholder="Paste the code the browser showed"
                    value={code}
                    autoFocus
                    style={{ flex: 1 }}
                    onChange={(event) => setCode(event.currentTarget.value)}
                    onKeyDown={(event) => {
                      if (event.key === 'Enter' && code.trim()) {
                        submitCode.mutate(code.trim());
                      }
                    }}
                  />
                  <Button
                    loading={submitCode.isPending}
                    disabled={!code.trim()}
                    onClick={() => submitCode.mutate(code.trim())}
                  >
                    Submit code
                  </Button>
                </Group>
                {session.attempts > 0 ? (
                  <Text size="xs" c="orange" data-testid="signin-code-retry">
                    {session.message} ({session.attempts_left} attempt
                    {session.attempts_left === 1 ? '' : 's'} left)
                  </Text>
                ) : null}
                {left != null ? (
                  <Text size="xs" c={left < 60 ? 'orange' : 'dimmed'}>
                    {Math.floor(left / 60)}m {left % 60}s left before the console gives up.
                    Nothing is written if it does.
                  </Text>
                ) : null}
              </Stack>
            </Alert>
          ) : null}

          {live && session?.state !== 'url_ready' && session?.state !== 'awaiting_code' ? (
            <Group gap="xs">
              <Loader size="sm" />
              <Text size="sm">{session?.message || 'Starting…'}</Text>
            </Group>
          ) : null}

          {session?.state === 'done' ? (
            <Alert color="teal" icon={<IconCheck size={18} />} title="Signed in" data-testid="signin-done">
              <Text size="sm">
                <Code>{status?.credential.name}</Code> is stored (…{session.last4}). Every
                model job picks it up on its next run — no restart, no terminal.
              </Text>
            </Alert>
          ) : null}

          {ending && session?.state !== 'done' ? (
            <Alert
              color={session?.state === 'cancelled' ? 'gray' : 'red'}
              icon={<IconAlertTriangle size={18} />}
              title={ending.title}
              data-testid="signin-failed"
            >
              <Stack gap="xs">
                <Text size="sm">{ending.what}</Text>
                {session?.exit_code != null && session.reason === 'cli_failed' ? (
                  <Text size="xs" c="dimmed">The CLI exited with code {session.exit_code}.</Text>
                ) : null}
                {ending.command ? (
                  <CommandLine command={ending.command} shell="WSL (Ubuntu)" />
                ) : null}
              </Stack>
            </Alert>
          ) : null}

          {error ? <Alert color="red">{error}</Alert> : null}

          {/* The phase verbatim from the server. It is the one line that distinguishes
              "still working" from "stuck", which a spinner alone cannot. */}
          {session ? (
            <Text size="xs" c="dimmed" data-testid="signin-phase">
              phase: {session.phase || session.state}
            </Text>
          ) : null}

          <Group justify="flex-end">
            {live ? (
              <Button
                variant="light"
                color="red"
                loading={cancel.isPending}
                onClick={() => cancel.mutate()}
              >
                Cancel sign-in
              </Button>
            ) : (
              <>
                {session?.state !== 'done' ? (
                  <Button variant="light" onClick={disarm}>
                    Try again
                  </Button>
                ) : null}
                <Button onClick={onClose}>
                  {session?.state === 'done' ? 'Done' : 'Close'}
                </Button>
              </>
            )}
          </Group>
        </Stack>
      </Modal>
    </>
  );
}

/** One copyable command, with the shell it belongs in — never a wall of instructions. */
export function CommandLine({ command, shell }: { command: string; shell: string }) {
  if (!command) return null;
  return (
    <Group gap="xs" wrap="nowrap" align="flex-start" data-testid="command-line">
      <Code style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all', flex: 1 }}>{command}</Code>
      <CopyButton value={command}>
        {({ copied, copy }) => (
          <Button size="compact-xs" variant="subtle" onClick={copy}
                  leftSection={<IconCopy size={12} />}>
            {copied ? 'Copied' : `Copy for ${shell}`}
          </Button>
        )}
      </CopyButton>
    </Group>
  );
}

export default ClaudeSignInModal;
