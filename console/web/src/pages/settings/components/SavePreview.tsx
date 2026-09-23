import {
  Alert,
  Badge,
  Button,
  Checkbox,
  Divider,
  Drawer,
  Group,
  List,
  Radio,
  Stack,
  Text,
  TextInput,
} from '@mantine/core';
import { IconAlertTriangle, IconInfoCircle, IconLock } from '@tabler/icons-react';
import { useEffect, useState } from 'react';

import { ConfirmDialog, DiffView } from '@/components';

import type { PreviewResult } from '../api';

export interface SaveIntent {
  reason: string;
  commit: boolean;
  applyEffects: boolean;
  confirmPhrase?: string;
  stepUpToken?: string;
}

export interface SavePreviewProps {
  opened: boolean;
  onClose: () => void;
  fileLabel: string;
  before: string;
  preview: PreviewResult | null;
  loading: boolean;
  error: string | null;
  stepUpActive: boolean;
  defaultCommit: boolean;
  defaultApplyNow: boolean;
  onSave: (intent: SaveIntent) => Promise<void>;
}

/**
 * The preview drawer, then the save dialog (spec 12 page 16).
 *
 * Nothing is written until the operator has seen the diff, the validation result, the
 * effects and the restart list. A protected change adds step-up plus a typed phrase on top.
 */
export function SavePreview({
  opened,
  onClose,
  fileLabel,
  before,
  preview,
  loading,
  error,
  stepUpActive,
  defaultCommit,
  defaultApplyNow,
  onSave,
}: SavePreviewProps) {
  const [reason, setReason] = useState('');
  const [commit, setCommit] = useState(defaultCommit);
  const [applyNow, setApplyNow] = useState(defaultApplyNow);
  const [confirming, setConfirming] = useState(false);

  useEffect(() => {
    if (opened) {
      setReason('');
      setCommit(defaultCommit);
      setApplyNow(defaultApplyNow);
    }
  }, [opened, defaultCommit, defaultApplyNow]);

  const blocked = Boolean(preview?.locked_paths.length);
  const canSave = Boolean(preview?.valid) && !blocked && reason.trim().length > 0;

  return (
    <Drawer
      opened={opened}
      onClose={onClose}
      position="right"
      size="xl"
      title={`Review changes — ${fileLabel}`}
      data-testid="save-preview"
    >
      <Stack gap="md">
        {error ? (
          <Alert color="red" icon={<IconAlertTriangle size={16} />} data-testid="preview-error">
            {error}
          </Alert>
        ) : null}

        {preview && !preview.valid ? (
          <Alert color="red" icon={<IconAlertTriangle size={16} />} title="Invalid configuration">
            <List size="sm">
              {preview.errors.map((issue) => (
                <List.Item key={`${issue.loc}:${issue.msg}`}>
                  <Text size="sm" component="span" fw={600}>
                    {issue.loc || 'document'}
                  </Text>
                  {': '}
                  {issue.msg}
                </List.Item>
              ))}
            </List>
          </Alert>
        ) : null}

        {blocked && preview ? (
          <Alert color="red" icon={<IconLock size={16} />} title="Refused while a sleeve is live">
            {preview.locked_paths.join(', ')} cannot change while real money is at risk. Take the
            sleeve back to TEST first.
          </Alert>
        ) : null}

        {preview?.reflowed ? (
          <Alert color="yellow" icon={<IconInfoCircle size={16} />}>
            This edit changes the document structure, so the file is re-emitted. Comments are
            kept, but flow style and alignment may normalise.
          </Alert>
        ) : null}

        {preview ? (
          <>
            <Group gap="xs" wrap="wrap">
              <Badge variant="light">{preview.changed_paths.length} path(s)</Badge>
              {preview.protected_changed.length > 0 ? (
                <Badge color="orange" leftSection={<IconLock size={11} />}>
                  {preview.protected_changed.length} protected
                </Badge>
              ) : null}
              {preview.effect_details.map((effect) => (
                <Badge key={effect.effect} color="grape" variant="light" title={effect.detail}>
                  {effect.title}
                </Badge>
              ))}
            </Group>

            {preview.changed_paths.length > 0 ? (
              <Text size="xs" c="dimmed" ff="monospace">
                {preview.changed_paths.join(' · ')}
              </Text>
            ) : null}

            {preview.restarts.length > 0 ? (
              <Text size="sm">
                Will restart: <strong>{preview.restarts.join(', ')}</strong>
              </Text>
            ) : null}

            <Divider label="Diff" />
            <DiffView before={before} after={preview.new_text} context={3} maxHeight={320} />
          </>
        ) : null}

        <Divider label="Save" />
        <TextInput
          label="Reason"
          description="Recorded in config_audit and in the commit message."
          value={reason}
          onChange={(event) => setReason(event.currentTarget.value)}
          data-testid="save-reason"
        />
        <Checkbox
          label="Commit this file to git"
          checked={commit}
          onChange={(event) => setCommit(event.currentTarget.checked)}
        />
        <Radio.Group
          value={applyNow ? 'apply_now' : 'save_only'}
          onChange={(value) => setApplyNow(value === 'apply_now')}
          label="Effects"
        >
          <Stack gap={4} mt={4}>
            <Radio value="apply_now" label="Apply now — regenerate and restart under the ops lock" />
            <Radio value="save_only" label="Save only — queue the effects and show the banner" />
          </Stack>
        </Radio.Group>

        <Group justify="flex-end">
          <Badge variant="default">{loading ? 'validating…' : `sha ${preview?.base_sha.slice(0, 12) ?? ''}`}</Badge>
          <Button
            disabled={!canSave}
            onClick={() => setConfirming(true)}
            data-testid="open-save-dialog"
          >
            Save…
          </Button>
        </Group>

        <ConfirmDialog
          opened={confirming}
          onClose={() => setConfirming(false)}
          title={`Save ${fileLabel}`}
          description={
            preview?.protected_changed.length
              ? `This moves protected keys: ${preview.protected_changed.join(', ')}.`
              : 'The file is written atomically, blessed and audited.'
          }
          {...(preview?.requires_confirm ? { confirmPhrase: preview.confirm_phrase } : {})}
          requireStepUp={Boolean(preview?.requires_stepup)}
          stepUpSatisfied={stepUpActive}
          confirmLabel="Save"
          danger={Boolean(preview?.protected_changed.length)}
          onConfirm={async ({ stepUpToken }) => {
            await onSave({
              reason: reason.trim(),
              commit,
              applyEffects: applyNow,
              ...(preview?.requires_confirm ? { confirmPhrase: preview.confirm_phrase } : {}),
              ...(stepUpToken ? { stepUpToken } : {}),
            });
            setConfirming(false);
            onClose();
          }}
        />
      </Stack>
    </Drawer>
  );
}

