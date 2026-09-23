import { Alert, Button, Card, Center, PasswordInput, Stack, Text, Title } from '@mantine/core';
import { IconShieldLock } from '@tabler/icons-react';
import { useState } from 'react';

import { errorMessage } from '../api/errors';
import { useSession } from './SessionContext';

/**
 * Token login (spec 5.4).  The token lives in `~/.config/earn/console-token`; the server
 * stores only a salted hash and rate-limits this form.
 */
export function LoginPage() {
  const { login } = useSession();
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await login(token.trim());
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Center h="100vh" p="md">
      <Card w={420} padding="lg" radius="md" data-testid="login-page">
        <Stack gap="md">
          <Stack gap={4} align="center">
            <IconShieldLock size={32} />
            <Title order={3}>Earn Console</Title>
            <Text size="sm" c="dimmed" ta="center">
              Local only — this console binds to 127.0.0.1 and never accepts a remote client.
            </Text>
          </Stack>
          <PasswordInput
            label="Console token"
            description="cat ~/.config/earn/console-token"
            value={token}
            data-testid="login-token"
            autoFocus
            autoComplete="off"
            onChange={(event) => setToken(event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && token.trim() !== '') void submit();
            }}
          />
          {error ? (
            <Alert color="red" data-testid="login-error">
              {error}
            </Alert>
          ) : null}
          <Button loading={busy} disabled={token.trim() === ''} onClick={() => void submit()} fullWidth>
            Sign in
          </Button>
        </Stack>
      </Card>
    </Center>
  );
}
