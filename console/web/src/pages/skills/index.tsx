import {
  Alert, Badge, Button, Card, Code, Group, Progress, ScrollArea, Stack, Text, Textarea, Title,
} from '@mantine/core';
import { useState } from 'react';

import { usePageCommands } from '@/app/commandRegistry';
import { DataTable } from '@/components';

import {
  STATUS_COLOURS,
  useArchiveSkill,
  useCheckRun,
  useCreateSkill,
  useSkills,
  type CheckKind,
  type EvalResult,
  type JobStatus,
  type LintResult,
  type SkillRow,
  type TestResult,
  type TrialResult,
} from './api';
import { NewSkillModal } from './components/NewSkillModal';
import { SkillEditor } from './components/SkillEditor';

function CheckOutput({ kind, data }: { kind: CheckKind; data: unknown }) {
  if (!data) return null;
  if (kind === 'lint') {
    const result = data as LintResult;
    return (
      <Alert color={result.ok ? 'teal' : 'red'} title={result.ok ? 'Lint clean' : 'Lint findings'}>
        <ScrollArea.Autosize mah={220}>
          <Stack gap={2}>
            {result.findings.map((finding) => (
              <Text key={`${finding.code}-${finding.path}-${finding.message}`} size="xs">
                <Badge size="xs" color={finding.severity === 'error' ? 'red' : 'yellow'}>
                  {finding.severity}
                </Badge>{' '}
                <Code>{finding.code}</Code> {finding.path}: {finding.message}
              </Text>
            ))}
            {result.findings.length === 0 ? <Text size="sm">Nothing to report.</Text> : null}
          </Stack>
        </ScrollArea.Autosize>
      </Alert>
    );
  }
  if (kind === 'test') {
    const result = data as TestResult;
    return (
      <Alert color={result.ok ? 'teal' : 'red'} title={result.ok ? 'pytest passed' : 'pytest failed'}>
        <ScrollArea.Autosize mah={260}>
          <Code block style={{ whiteSpace: 'pre-wrap' }}>
            {result.log}
          </Code>
        </ScrollArea.Autosize>
      </Alert>
    );
  }
  if (kind === 'eval') {
    const result = data as EvalResult;
    return (
      <Alert
        color={result.ok ? 'teal' : 'red'}
        title={`Pass rate ${(result.pass_rate * 100).toFixed(0)}% (floor ${(
          result.min_pass_rate * 100
        ).toFixed(0)}%)`}
      >
        <Text size="xs" c="dimmed" mb="xs">
          A skipped case is not a pass: model-judgement cases need a session, and the change
          gate scores code-only.
        </Text>
        <Stack gap={2}>
          {result.cases.map((item) => (
            <Text key={item.id} size="xs">
              <Badge
                size="xs"
                color={
                  item.status === 'pass' ? 'teal' : item.status === 'skip' ? 'gray' : 'red'
                }
              >
                {item.status}
              </Badge>{' '}
              {item.id} {item.detail}
            </Text>
          ))}
        </Stack>
      </Alert>
    );
  }
  const result = data as TrialResult;
  return (
    <Alert color={result.ok ? 'teal' : 'red'} title={`Trial in ${result.branch}`}>
      <ScrollArea.Autosize mah={260}>
        <Code block style={{ whiteSpace: 'pre-wrap' }}>
          {result.text ?? result.error ?? '(no output)'}
        </Code>
      </ScrollArea.Autosize>
    </Alert>
  );
}

const JOB_COLOURS: Record<JobStatus, string> = {
  queued: 'gray',
  running: 'blue',
  ok: 'teal',
  failed: 'red',
  cancelled: 'orange',
};

/**
 * What a running check looks like: a bar, the line it is on, and its output as it lands.
 *
 * A job always ends somewhere — the runner guarantees a terminal status even when the
 * work dies — so this never sits on "running" with nothing behind it.
 */
function CheckProgress({ check }: { check: ReturnType<typeof useCheckRun> }) {
  if (!check.status) return null;
  return (
    <Stack gap={4} mb="sm" data-testid="check-progress">
      <Group gap="xs">
        <Badge color={JOB_COLOURS[check.status] ?? 'gray'}>{check.status}</Badge>
        {check.kind ? <Text size="xs">{check.kind}</Text> : null}
        {check.message ? (
          <Text size="xs" c="dimmed">
            {check.message}
          </Text>
        ) : null}
      </Group>
      {check.running ? <Progress value={Math.round(check.progress * 100)} size="sm" /> : null}
      {check.output.length > 0 ? (
        <ScrollArea.Autosize mah={200}>
          <Code block style={{ whiteSpace: 'pre-wrap', fontSize: 12 }} data-testid="check-output">
            {check.output.join('\n')}
          </Code>
        </ScrollArea.Autosize>
      ) : null}
      {check.status === 'failed' && check.error ? (
        <Alert color="red" title="The check did not finish">
          {check.error}
        </Alert>
      ) : null}
      {check.status === 'cancelled' ? (
        <Text size="xs" c="dimmed">
          Stopped before it finished; nothing was written.
        </Text>
      ) : null}
    </Stack>
  );
}

/** The Skills page: what exists, what loads it, and whether it still passes its own bar. */
export default function SkillsPage() {
  const skills = useSkills();
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [trialPrompt, setTrialPrompt] = useState('');
  const [lastKind, setLastKind] = useState<CheckKind>('lint');
  const create = useCreateSkill();
  const archive = useArchiveSkill();
  const check = useCheckRun(selected ?? '');

  const run = (kind: CheckKind) => {
    setLastKind(kind);
    check.start(kind === 'trial' ? { kind, prompt: trialPrompt } : { kind });
  };

  const current = (skills.data?.items ?? []).find((row) => row.name === selected) ?? null;

  usePageCommands('skills', [
    {
      id: 'new-skill',
      title: 'New skill',
      subtitle: 'Copy the template and register it',
      run: (ctx) => {
        setCreating(true);
        ctx.close();
      },
    },
    {
      id: 'lint',
      title: selected ? `Lint ${selected}` : 'Lint the selected skill',
      subtitle: selected ? undefined : 'Pick a skill in the table first',
      run: (ctx) => {
        if (selected) run('lint');
        ctx.close();
      },
    },
    {
      id: 'test',
      title: selected ? `Run ${selected}'s tests` : 'Test the selected skill',
      subtitle: selected ? undefined : 'Pick a skill in the table first',
      run: (ctx) => {
        if (selected) run('test');
        ctx.close();
      },
    },
  ]);

  return (
    <Stack gap="lg">
      <Group justify="space-between">
        <Title order={2}>Skills</Title>
        <Button onClick={() => setCreating(true)}>New skill</Button>
      </Group>

      {skills.error ? (
        <Alert color="red" title="Could not load the skills">
          {String(skills.error)}
        </Alert>
      ) : null}

      <Card withBorder padding="md">
        <DataTable<SkillRow>
          rows={skills.data?.items ?? []}
          rowKey={(row) => row.name}
          loading={skills.isLoading}
          onRowClick={(row) => setSelected(row.name)}
          emptyTitle="No skills on disk"
          columns={[
            {
              key: 'name',
              header: 'Skill',
              render: (row) => (
                <Text size="sm" fw={row.name === selected ? 700 : 400}>
                  {row.name}
                </Text>
              ),
              sortValue: (row) => row.name,
            },
            {
              key: 'status',
              header: 'Status',
              render: (row) => (
                <Badge color={STATUS_COLOURS[row.status] ?? 'gray'}>{row.status}</Badge>
              ),
              sortValue: (row) => row.status,
            },
            {
              key: 'bindings',
              header: 'Loaded by',
              render: (row) =>
                row.bindings.length ? (
                  <Group gap={4}>
                    {row.bindings.map((task) => (
                      <Badge key={task} variant="light" size="sm">
                        {task}
                      </Badge>
                    ))}
                  </Group>
                ) : (
                  <Text size="xs" c="dimmed">
                    nothing
                  </Text>
                ),
            },
            {
              key: 'policy',
              header: 'Policy (body/scripts/tests)',
              render: (row) => (
                <Text size="xs" ff="monospace">
                  {row.policy.body}/{row.policy.scripts}/{row.policy.tests}
                </Text>
              ),
            },
            {
              key: 'tests',
              header: 'Tests',
              render: (row) =>
                row.has_tests ? (
                  <Text size="xs">{row.test_files} file(s)</Text>
                ) : (
                  <Badge color="red" variant="light" size="sm">
                    none
                  </Badge>
                ),
              sortValue: (row) => row.test_files,
            },
            {
              key: 'origin',
              header: 'Origin',
              render: (row) => <Text size="xs">{row.origin}</Text>,
            },
          ]}
        />
      </Card>

      {selected && current ? (
        <Card withBorder padding="md">
          <Group justify="space-between" mb="sm">
            <Group gap="xs">
              <Title order={4}>{selected}</Title>
              <Badge color={STATUS_COLOURS[current.status] ?? 'gray'}>{current.status}</Badge>
            </Group>
            <Group gap="xs">
              <Button
                size="xs"
                variant="light"
                disabled={check.running}
                onClick={() => run('lint')}
              >
                Lint
              </Button>
              <Button
                size="xs"
                variant="light"
                disabled={check.running}
                onClick={() => run('test')}
              >
                Test
              </Button>
              <Button
                size="xs"
                variant="light"
                disabled={check.running}
                onClick={() => run('eval')}
              >
                Eval
              </Button>
              <Button
                size="xs"
                variant="light"
                disabled={check.running || trialPrompt.trim().length < 10}
                onClick={() => run('trial')}
              >
                Trial
              </Button>
              {check.running ? (
                <Button
                  size="xs"
                  color="red"
                  variant="light"
                  loading={check.cancelling}
                  onClick={() => check.cancel()}
                >
                  Cancel
                </Button>
              ) : null}
              <Button
                size="xs"
                color="orange"
                variant="subtle"
                loading={archive.isPending}
                onClick={() =>
                  archive.mutate({
                    name: selected,
                    restore: current.status === 'archived',
                  })
                }
              >
                {current.status === 'archived' ? 'Restore' : 'Archive'}
              </Button>
            </Group>
          </Group>

          <Textarea
            label="Trial prompt"
            description="Runs one routed session with this skill in a throwaway worktree."
            placeholder="Grade the week of 2026-W39 and name the weakest step."
            autosize
            minRows={2}
            mb="sm"
            value={trialPrompt}
            onChange={(event) => setTrialPrompt(event.currentTarget.value)}
          />

          <CheckProgress check={check} />
          {check.status === 'ok' && check.result ? (
            <CheckOutput kind={lastKind} data={check.result} />
          ) : null}

          <Stack gap="sm" mt="md">
            <SkillEditor name={selected} />
          </Stack>
        </Card>
      ) : null}

      <NewSkillModal
        opened={creating}
        onClose={() => setCreating(false)}
        busy={create.isPending}
        error={create.error ? String(create.error) : null}
        onCreate={(body) =>
          create.mutate(body, {
            onSuccess: () => {
              setCreating(false);
              setSelected(body.name);
            },
          })
        }
      />
    </Stack>
  );
}
