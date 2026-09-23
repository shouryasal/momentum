import { Alert, Code, List, Stack, Text } from '@mantine/core';

import { ConfirmDialog } from '@/components';

import type { Anchors, ResumeResult, Sleeve } from '../api';

export interface ResumeMonthlyModalProps {
  opened: boolean;
  onClose: () => void;
  sleeve: Sleeve;
  anchors: Anchors;
  steppedUp?: boolean;
  onResume: (confirmPhrase: string) => Promise<ResumeResult>;
  onResumed?: (result: ResumeResult) => void;
}

/**
 * The resume wizard.
 *
 * It spells out what the action does because clearing the monthly stop is the one limit
 * code may never clear by itself: it re-anchors the month to today's NAV (without that
 * the next loop compares the reduced NAV with the pre-drawdown anchor and re-locks
 * within seconds) and deletes the bot's leftover pair locks.
 */
export function ResumeMonthlyModal({
  opened,
  onClose,
  sleeve,
  anchors,
  steppedUp,
  onResume,
  onResumed,
}: ResumeMonthlyModalProps) {
  const stopPct = (anchors.monthly_loss_stop * 100).toFixed(0);
  return (
    <ConfirmDialog
      opened={opened}
      onClose={onClose}
      title={`Resume sleeve ${sleeve.toUpperCase()} after the monthly stop`}
      confirmPhrase={anchors.confirm_phrase}
      requireStepUp
      {...(steppedUp === undefined ? {} : { stepUpSatisfied: steppedUp })}
      confirmLabel={`Resume sleeve ${sleeve.toUpperCase()}`}
      danger
      onConfirm={async () => {
        const result = await onResume(anchors.confirm_phrase);
        onResumed?.(result);
        onClose();
      }}
    >
      {/* The explanation lives in `children`, not `description`: the dialog renders the
          description inside a <p>, and this block is a list. */}
      <Stack gap="sm">
        <Alert color="orange" title="This restarts real entries">
          Sleeve {sleeve.toUpperCase()} stopped after a {stopPct}% monthly loss
          {anchors.monthly_locked_month ? ` in ${anchors.monthly_locked_month}` : ''}.
        </Alert>
        <Text size="sm">Resuming will:</Text>
        <List size="sm" spacing={4}>
          <List.Item>
            clear <Code>monthly_locked</Code> for this sleeve;
          </List.Item>
          <List.Item>
            re-anchor <Code>month_anchor_nav</Code> to today&apos;s NAV — without this the
            next loop compares the reduced NAV with the old anchor and locks again;
          </List.Item>
          <List.Item>delete this bot&apos;s leftover Freqtrade pair locks;</List.Item>
          <List.Item>write an audit row naming you as the actor.</List.Item>
        </List>
        <Text size="sm" c="dimmed">
          The stop re-arms on a fresh {stopPct}% fall measured from the new anchor. Every
          other limit stays exactly as it was.
        </Text>
      </Stack>
    </ConfirmDialog>
  );
}
