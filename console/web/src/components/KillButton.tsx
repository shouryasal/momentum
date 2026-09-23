import { Button, Checkbox, Stack, Text, Textarea, Tooltip } from '@mantine/core';
import { IconHandStop, IconPlayerPlay } from '@tabler/icons-react';
import { useState } from 'react';

import { ConfirmDialog } from './ConfirmDialog';

export const RESUME_TRADING_PHRASE = 'RESUME TRADING';

/** `console.contracts.KillRequest.reason`: `Field(min_length=3, max_length=500)`. */
export const KILL_REASON_MIN = 3;
export const KILL_REASON_MAX = 500;

export interface KillButtonProps {
  engaged: boolean;
  reason?: string | null;
  stepUpSatisfied?: boolean;
  /** `POST /api/kill` — no step-up: stopping must be frictionless (spec 12). */
  onKill: (args: { reason: string; flatten: boolean }) => Promise<void> | void;
  /** `DELETE /api/kill` — step-up **and** the typed phrase. */
  onResume: (args: { confirmPhrase: string; stepUpToken?: string }) => Promise<void> | void;
  compact?: boolean;
}

/**
 * The always-mounted KILL control (spec 12).  Engaging asks only for a reason and an
 * optional "flatten"; resuming needs step-up plus the typed phrase.
 */
export function KillButton({
  engaged,
  reason,
  stepUpSatisfied = false,
  onKill,
  onResume,
  compact,
}: KillButtonProps) {
  const [killOpen, setKillOpen] = useState(false);
  const [resumeOpen, setResumeOpen] = useState(false);
  const [killReason, setKillReason] = useState('');
  const [flatten, setFlatten] = useState(false);

  return (
    <>
      {engaged ? (
        <Tooltip label={reason ? `KILL engaged: ${reason}` : 'KILL engaged'}>
          <Button
            color="teal"
            variant="filled"
            size={compact ? 'xs' : 'sm'}
            leftSection={<IconPlayerPlay size={16} />}
            onClick={() => setResumeOpen(true)}
            data-testid="resume-button"
          >
            Resume
          </Button>
        </Tooltip>
      ) : (
        <Tooltip label="Stop all entries and cancel open entry orders on both sleeves">
          <Button
            color="red"
            variant="filled"
            size={compact ? 'xs' : 'sm'}
            leftSection={<IconHandStop size={16} />}
            onClick={() => setKillOpen(true)}
            data-testid="kill-button"
          >
            KILL
          </Button>
        </Tooltip>
      )}

      <ConfirmDialog
        opened={killOpen}
        onClose={() => {
          setKillOpen(false);
          setKillReason('');
          setFlatten(false);
        }}
        title="Engage the kill switch"
        danger
        confirmLabel="Engage KILL"
        confirmDisabled={killReason.trim().length < KILL_REASON_MIN}
        description="Writes ops/killdir/KILL, stops entries on both bots and cancels open entry orders. It never waits for the ops lock."
        onConfirm={() => onKill({ reason: killReason.trim(), flatten })}
      >
        <Stack gap="xs">
          {/* The server rejects a reason outside 3..500 characters with one opaque
              sentence, so the bound is enforced here: during an incident the operator
              must not lose seconds to a validation error they cannot read. */}
          <Textarea
            label="Reason"
            placeholder="Why are you stopping?"
            value={killReason}
            onChange={(event) => setKillReason(event.currentTarget.value)}
            autosize
            minRows={2}
            maxLength={KILL_REASON_MAX}
            error={
              killReason.length > 0 && killReason.trim().length < KILL_REASON_MIN
                ? `at least ${KILL_REASON_MIN} characters`
                : null
            }
            description={`${killReason.trim().length}/${KILL_REASON_MAX}`}
            data-testid="kill-reason"
          />
          <Checkbox
            label="Also flatten open positions"
            checked={flatten}
            onChange={(event) => setFlatten(event.currentTarget.checked)}
            data-testid="kill-flatten"
          />
          <Text size="xs" c="dimmed">
            The kill file is removed by a human — resuming asks for the token again.
          </Text>
        </Stack>
      </ConfirmDialog>

      <ConfirmDialog
        opened={resumeOpen}
        onClose={() => setResumeOpen(false)}
        title="Resume trading"
        confirmLabel="Remove KILL"
        confirmPhrase={RESUME_TRADING_PHRASE}
        requireStepUp
        stepUpSatisfied={stepUpSatisfied}
        description={reason ? `Engaged because: ${reason}` : 'Removes ops/killdir/KILL.'}
        onConfirm={({ stepUpToken }) =>
          onResume({
            confirmPhrase: RESUME_TRADING_PHRASE,
            ...(stepUpToken ? { stepUpToken } : {}),
          })
        }
      />
    </>
  );
}
