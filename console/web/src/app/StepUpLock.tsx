import { ActionIcon, Alert, Button, Group, Modal, PasswordInput, Stack, Text, Tooltip } from '@mantine/core';
import { IconLock, IconLockOpen2 } from '@tabler/icons-react';
import { useState } from 'react';

import { errorMessage } from '../api/errors';
import { formatRelative } from '../lib/format';
import { useSession } from './SessionContext';

/**
 * Header lock icon: open while the step-up window lasts, closed otherwise.
 * Clicking it re-enters the console token (spec 5.4).
 */
export function StepUpLock() {
  const { stepUpActive, session, stepUp } = useSession();
  const [opened, setOpened] = useState(false);
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await stepUp(token);
      setToken('');
      setOpened(false);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const label = stepUpActive
    ? `Step-up active, expires ${formatRelative(session?.step_up_until)}`
    : 'Step-up locked — dangerous actions will ask for the token';

  return (
    <>
      <Tooltip label={label}>
        <ActionIcon
          variant="subtle"
          color={stepUpActive ? 'teal' : 'gray'}
          aria-label={label}
          data-testid="stepup-lock"
          data-active={stepUpActive ? 'true' : 'false'}
          onClick={() => setOpened(true)}
        >
          {stepUpActive ? <IconLockOpen2 size={18} /> : <IconLock size={18} />}
        </ActionIcon>
      </Tooltip>
      <Modal opened={opened} onClose={() => setOpened(false)} title="Step-up authentication" centered>
        <Stack gap="sm">
          <Text size="sm">
            Re-enter the console token to unlock dangerous actions for the configured window.
          </Text>
          <PasswordInput
            label="Console token"
            value={token}
            onChange={(event) => setToken(event.currentTarget.value)}
            data-testid="stepup-token"
            autoComplete="off"
          />
          {error ? <Alert color="red">{error}</Alert> : null}
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setOpened(false)}>
              Cancel
            </Button>
            <Button loading={busy} disabled={token.trim() === ''} onClick={() => void submit()}>
              Unlock
            </Button>
          </Group>
        </Stack>
      </Modal>
    </>
  );
}
