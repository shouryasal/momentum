/**
 * Inject a manual signal.
 *
 * A human may START the pipeline, never skip it: the signal enters at `screened`, so the
 * validator and `TriggerEngine.guards()` still stand between it and a research run. The
 * modal says so, because an operator who believes this is a "trade now" button will be
 * surprised in the worst possible way.
 */
import { Alert, Button, Group, Modal, Select, Stack, Text, TextInput, Textarea } from '@mantine/core';
import { useMutation } from '@tanstack/react-query';
import { useMemo, useState } from 'react';

import { errorMessage } from '@/api';
import { useApi } from '@/app/ApiContext';

import { signalsApi, type Direction } from '../api';

const DIRECTIONS: Array<{ value: Direction; label: string }> = [
  { value: 'up', label: 'up — I think this goes up' },
  { value: 'down', label: 'down — I think this goes down' },
  { value: 'risk', label: 'risk — something is wrong, look now' },
  { value: 'neutral', label: 'neutral — worth a look, no direction' },
];

export interface ManualSignalModalProps {
  opened: boolean;
  onClose: () => void;
  onCreated?: (signalId: string) => void;
}

export function ManualSignalModal({ opened, onClose, onCreated }: ManualSignalModalProps) {
  const client = useApi();
  const api = useMemo(() => signalsApi(client), [client]);
  const [pair, setPair] = useState('');
  const [direction, setDirection] = useState<Direction>('risk');
  const [note, setNote] = useState('');

  const create = useMutation({
    mutationFn: () =>
      api.manual({ pair: pair.trim() || null, direction, note: note.trim() }),
    onSuccess: (result) => {
      onCreated?.(result.signal_id);
      setPair('');
      setNote('');
      onClose();
    },
  });

  return (
    <Modal opened={opened} onClose={onClose} title="Inject a manual signal" size="lg">
      <Stack gap="sm">
        <Alert variant="light" color="blue">
          This enters the pipeline at <b>screened</b>. The validator still runs, the guards still
          apply, and the risk gate still decides whether anything may be traded. It is a request to
          look, not an instruction to act.
        </Alert>
        <TextInput
          label="Pair"
          description="Leave empty for a market-wide signal."
          placeholder="BTC/USDT"
          value={pair}
          onChange={(e) => setPair(e.currentTarget.value)}
        />
        <Select
          label="Direction"
          data={DIRECTIONS}
          value={direction}
          onChange={(v) => setDirection((v as Direction) ?? 'risk')}
          allowDeselect={false}
        />
        <Textarea
          label="Why"
          description="Recorded verbatim and shown to the validator as the detector's detail."
          minRows={3}
          autosize
          value={note}
          onChange={(e) => setNote(e.currentTarget.value)}
        />
        {create.isError ? (
          <Text size="sm" c="red">
            {errorMessage(create.error)}
          </Text>
        ) : null}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            onClick={() => create.mutate()}
            loading={create.isPending}
            disabled={note.trim().length === 0}
          >
            Inject
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
