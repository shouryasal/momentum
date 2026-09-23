import { Alert, Button, Group, Modal, Stack, Text, TextInput, Textarea } from '@mantine/core';
import { useState } from 'react';

const NAME_RE = /^[a-z][a-z0-9-]{1,47}$/;
const MIN_DESCRIPTION = 40;

export interface NewSkillModalProps {
  opened: boolean;
  onClose: () => void;
  onCreate: (body: { name: string; description: string; title?: string }) => void;
  busy?: boolean;
  error?: string | null;
}

/**
 * The new-skill wizard.
 *
 * It scaffolds from `.claude/skills/_template` and the skill lands **incubating**: linted,
 * tested, and loaded by nothing until it is bound to a task. That is deliberate — a new
 * skill changes how every future run thinks, so existing has to be cheaper than being used.
 */
export function NewSkillModal({ opened, onClose, onCreate, busy, error }: NewSkillModalProps) {
  const [name, setName] = useState('');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');

  const nameOk = NAME_RE.test(name);
  const descriptionOk = description.trim().length >= MIN_DESCRIPTION;

  return (
    <Modal opened={opened} onClose={onClose} title="New skill" size="lg">
      <Stack gap="sm">
        <Text size="sm" c="dimmed">
          Two things earn a new skill: a root cause that recurred for two review weeks with{' '}
          <code>fix_path=skill</code>, or a procedure repeated three times. Anything else is a
          note in the weekly report.
        </Text>

        <TextInput
          label="Name"
          description="Lowercase letters, digits and hyphens. It becomes the folder name."
          value={name}
          onChange={(event) => setName(event.currentTarget.value)}
          error={name && !nameOk ? 'lowercase letters, digits and hyphens, 2–48 chars' : null}
        />
        <TextInput
          label="Title"
          description="Optional heading for SKILL.md."
          value={title}
          onChange={(event) => setTitle(event.currentTarget.value)}
        />
        <Textarea
          label="Description"
          description={
            `What it does, what it refuses, and the words that trigger it — at least ` +
            `${MIN_DESCRIPTION} characters, third person.`
          }
          autosize
          minRows={3}
          value={description}
          onChange={(event) => setDescription(event.currentTarget.value)}
          error={
            description && !descriptionOk
              ? `${description.trim().length}/${MIN_DESCRIPTION} characters`
              : null
          }
        />

        <Alert color="blue" title="What you get">
          The template&apos;s SKILL.md, checklist, contract tests and eval cases — with the
          name placeholders filled in. Write the tests before the body; the change gate runs
          them.
        </Alert>

        {error ? (
          <Alert color="red" title="Not created">
            {error}
          </Alert>
        ) : null}

        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!nameOk || !descriptionOk}
            loading={busy}
            onClick={() =>
              onCreate({ name, description, ...(title ? { title } : {}) })
            }
          >
            Create (incubating)
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
