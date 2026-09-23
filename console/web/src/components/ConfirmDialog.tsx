import { Alert, Button, Code, Group, List, Modal, PasswordInput, Stack, Text, TextInput } from '@mantine/core';
import { IconAlertTriangle, IconLock } from '@tabler/icons-react';
import { useEffect, useState, type ReactNode } from 'react';

import { errorFields, errorMessage } from '../api/errors';
import { useOptionalSession } from '../app/SessionContext';

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
  /**
   * Whether the step-up window is already open. Defaults to the session's own
   * `stepUpActive`, so a caller cannot leave the token box up while the session is
   * stepped up (or, worse, hide it while it is not).
   */
  stepUpSatisfied?: boolean;
  /** Override the step-up call. Defaults to `session.stepUp` (`POST /api/auth/step-up`). */
  onStepUp?: (token: string) => Promise<void>;
  confirmLabel?: string;
  danger?: boolean;
  /** Set by the caller when its own fields (rendered as children) are not valid yet. */
  confirmDisabled?: boolean;
  /** Extra controls (e.g. the KILL "flatten" checkbox) rendered above the confirmation. */
  children?: ReactNode;
  onConfirm: (args: { stepUpToken?: string; phrase?: string }) => Promise<void> | void;
}

/**
 * Typed-confirmation + step-up modal shared by every destructive action
 * (go live, clear KILL, test-run reset, force exit, protected config write).
 *
 * **The dialog performs the step-up itself.** It used to collect the token and hand it to
 * `onConfirm({stepUpToken})`, leaving each caller to remember to `await session.stepUp(...)`
 * before its mutation — and five of them did not, so the operator typed the right token,
 * clicked the button, and the guarded request went out with no step-up and came back 403
 * "re-enter the console token to continue". Doing it here means a caller cannot forget:
 * by the time `onConfirm` runs, the window is open. The token is still passed on for the
 * callers that legitimately re-send it (the secrets page re-checks it server-side).
 */
export function ConfirmDialog({
  opened,
  onClose: onCloseProp,
  onCancel,
  title,
  description,
  confirmPhrase,
  requireStepUp = false,
  stepUpSatisfied,
  onStepUp,
  confirmLabel = 'Confirm',
  danger = false,
  confirmDisabled = false,
  children,
  onConfirm,
}: ConfirmDialogProps) {
  const session = useOptionalSession();
  const onClose = onCloseProp ?? onCancel ?? (() => undefined);
  const [phrase, setPhrase] = useState('');
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fields, setFields] = useState<string[]>([]);

  useEffect(() => {
    if (!opened) {
      setPhrase('');
      setToken('');
      setBusy(false);
      setError(null);
      setFields([]);
    }
  }, [opened]);

  const stepUp = onStepUp ?? session?.stepUp;
  // Fail closed: with no way to prove the window is open, ask for the token.
  const satisfied = stepUpSatisfied ?? session?.stepUpActive ?? false;
  const needsToken = requireStepUp && !satisfied;
  const phraseOk = !confirmPhrase || phrase === confirmPhrase;
  const tokenOk = !needsToken || token.trim().length > 0;
  const canConfirm = phraseOk && tokenOk && !busy && !confirmDisabled;

  const submit = async () => {
    if (!canConfirm) return;
    setBusy(true);
    setError(null);
    setFields([]);
    try {
      if (needsToken && stepUp) await stepUp(token.trim());
      await onConfirm({
        ...(needsToken ? { stepUpToken: token.trim() } : {}),
        ...(confirmPhrase ? { phrase } : {}),
      });
      onClose();
    } catch (err) {
      setError(errorMessage(err));
      setFields(errorFields(err));
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
            {fields.length ? (
              <List size="sm" mt={4} data-testid="confirm-error-fields">
                {fields.map((line) => (
                  <List.Item key={line}>{line}</List.Item>
                ))}
              </List>
            ) : null}
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
