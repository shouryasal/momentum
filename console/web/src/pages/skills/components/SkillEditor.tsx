import {
  Alert, Badge, Button, Code, Group, List, NavLink, Paper, ScrollArea, Stack, Text,
} from '@mantine/core';
import { useEffect, useState } from 'react';

import { CodeEditor, languageForPath } from '@/components';

import {
  useSaveSkillFile,
  useSkillFile,
  useSkillTree,
  type LintFinding,
} from '../api';

function FindingList({ findings }: { findings: LintFinding[] }) {
  if (findings.length === 0) return null;
  const errors = findings.filter((f) => f.severity === 'error');
  return (
    <Alert
      color={errors.length ? 'red' : 'yellow'}
      title={errors.length ? `${errors.length} lint error(s)` : 'Lint warnings'}
    >
      <List size="sm" spacing={2}>
        {findings.map((finding) => (
          <List.Item key={`${finding.code}-${finding.path}-${finding.message}`}>
            <Code>{finding.code}</Code> {finding.path}: {finding.message}
          </List.Item>
        ))}
      </List>
    </Alert>
  );
}

/**
 * File tree plus editor for one skill.
 *
 * A `scripts/**` file is tier 2: the editor still opens it, and the save asks the server,
 * which refuses without a fresh step-up. Every save is linted server side; errors roll the
 * file back and come back here as the reason.
 */
export function SkillEditor({ name }: { name: string }) {
  const tree = useSkillTree(name);
  const [path, setPath] = useState<string | null>(null);
  const file = useSkillFile(name, path);
  const save = useSaveSkillFile(name);
  const [draft, setDraft] = useState('');

  useEffect(() => {
    const files = tree.data?.files ?? [];
    if (!path && files.length > 0) setPath(files[0]?.path ?? null);
  }, [tree.data, path]);

  useEffect(() => {
    if (file.data) setDraft(file.data.content);
  }, [file.data]);

  const dirty = Boolean(file.data) && draft !== file.data?.content;
  const findings = (save.data?.findings ?? []) as LintFinding[];
  const errorDetail = save.error as { body?: { detail?: { findings?: LintFinding[] } } } | null;
  const refusedFindings = errorDetail?.body?.detail?.findings ?? [];

  return (
    <Group align="flex-start" gap="md" wrap="nowrap">
      <Paper withBorder p="xs" w={260}>
        <ScrollArea.Autosize mah={420}>
          {(tree.data?.files ?? []).map((node) => (
            <NavLink
              key={node.path}
              label={node.path}
              active={node.path === path}
              onClick={() => setPath(node.path)}
              disabled={!node.editable}
              rightSection={
                node.tier === 'tier2' ? (
                  <Badge size="xs" color="orange" variant="light">
                    tier 2
                  </Badge>
                ) : null
              }
            />
          ))}
        </ScrollArea.Autosize>
      </Paper>

      <Stack gap="sm" style={{ flex: 1, minWidth: 0 }}>
        {file.data?.requires_step_up ? (
          <Alert color="orange" title="This file is tier 2">
            <Code>scripts/**</Code> is human-only: an automated run cannot write it at all,
            and saving it here needs a fresh step-up.
          </Alert>
        ) : null}

        <CodeEditor
          value={draft}
          onChange={setDraft}
          language={languageForPath(path ?? '')}
          height="420px"
          data-testid="skill-editor"
        />

        {refusedFindings.length > 0 ? <FindingList findings={refusedFindings} /> : null}
        {save.isSuccess && findings.length > 0 ? <FindingList findings={findings} /> : null}
        {save.error && refusedFindings.length === 0 ? (
          <Alert color="red" title="The save was refused">
            {String(save.error)}
          </Alert>
        ) : null}

        <Group justify="space-between">
          <Text size="xs" c="dimmed">
            {path ?? 'no file selected'}
            {dirty ? ' · unsaved changes' : ''}
          </Text>
          <Button
            disabled={!dirty || !path}
            loading={save.isPending}
            onClick={() =>
              path && save.mutate({ path, content: draft, base_sha: file.data?.sha ?? null })
            }
          >
            Save
          </Button>
        </Group>
      </Stack>
    </Group>
  );
}
