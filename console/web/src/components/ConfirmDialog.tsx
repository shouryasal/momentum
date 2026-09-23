import { Alert, Button, Code, Group, Modal, PasswordInput, Stack, Text, TextInput } from '@mantine/core';
import { IconAlertTriangle, IconLock } from '@tabler/icons-react';
import { useEffect, useState, type ReactNode } from 'react';

import { errorMessage } from '../api/errors';

export interface ConfirmDialogProps {
  opened: boolean;
  onClose?: () => void;
  /** Alias for {@link ConfirmDialogProps.onClose}. */
  onCancel?: () => void;
  title: string;
  description?: ReactNode;
  /** When set the operator must type this phrase exactly (spec 5.2 `confirm_phrase`). */
  confirmPhrase?: string;
  /** Dangerous action: needs a fresh token unless the session is already stepped up. */
  requireStepUp?: boolean;
  stepUpSatisfied?: boolean;
  confirmLabel?: string;
  danger?: boolean;
  /** Set by the caller when its own fields (rendered as children) are not valid yet. */
  confirmDisabled?: boolean;
  /** Extra controls (e.g. the KILL "flatten" checkbox) rendered above the confirmation. */
  children?: ReactNode;
  onConfirm: (args: { stepUpToken?: string }) => Promise<void> | void;
}

/**
 * Typed-confirmation + step-up modal shared by every destructive action
 * (go live, clear KILL, test-run reset, force exit, protected config write).
 */
export function ConfirmDialog({
  opened,
  onClose: onCloseProp,
  onCancel,
  title,
  description,
  confirmPhrase,
  requireStepUp = false,
  stepUpSatisfied = false,
  confirmLabel = 'Confirm',
  danger = false,
  confirmDisabled = false,
  children,
  onConfirm,
}: ConfirmDialogProps) {
  const onClose = onCloseProp ?? onCancel ?? (() => undefined);
  const [phrase, setPhrase] = useState('');
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!opened) {
      setPhrase('');
      setToken('');
      setBusy(false);
      setError(null);
    }
  }, [opened]);

  const needsToken = requireStepUp && !stepUpSatisfied;
  const phraseOk = !confirmPhrase || phrase === confirmPhrase;
  const tokenOk = !needsToken || token.trim().length > 0;
  const canConfirm = phraseOk && tokenOk && !busy && !confirmDisabled;

  const submit = async () => {
    if (!canConfirm) return;
    setBusy(true);
    setError(null);
    try {
      await onConfirm(needsToken ? { stepUpToken: token } : {});
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal opened={opened} onClose={onClose} title={title} centered data-testid="confirm-dialog">
      <Stack gap="sm">
        {description ? <Text size="sm">{description}</Text> : null}
        {children}
        {confirmPhrase ? (
          <TextInput
            label={
              <span>
                Type <Code>{confirmPhrase}</Code> to confirm
              </span>
            }
            value={phrase}
            onChange={(event) => setPhrase(event.currentTarget.value)}
            data-testid="confirm-phrase"
            autoComplete="off"
            spellCheck={false}
          />
        ) : null}
        {needsToken ? (
          <PasswordInput
            label="Console token (step-up)"
            description="Dangerous actions re-check the token."
            leftSection={<IconLock size={14} />}
            value={token}
            onChange={(event) => setToken(event.currentTarget.value)}
            data-testid="confirm-stepup"
            autoComplete="off"
          />
        ) : null}
        {error ? (
          <Alert color="red" icon={<IconAlertTriangle size={16} />} data-testid="confirm-error">
            {error}
          </Alert>
        ) : null}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            color={danger ? 'red' : undefined}
            disabled={!canConfirm}
            loading={busy}
            onClick={() => void submit()}
            data-testid="confirm-submit"
          >
            {confirmLabel}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
