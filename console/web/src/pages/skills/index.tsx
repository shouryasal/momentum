/**
 * Skills — what the system knows how to do, led by what each one actually does.
 *
 * The list answers the only question an operator has about a procedure they did not
 * write: what is this for?  So the first column is the skill's own sentence, taken from
 * its `SKILL.md`, and the machinery — bindings, policy, tests, origin, the file editor and
 * the four check buttons — lives in the detail pane that opens on the row you clicked.
 */
import {
  Alert, Badge, Button, Card, Code, Group, Progress, ScrollArea, Stack, Text, Textarea,
  Tooltip,
} from '@mantine/core';
import { useState } from 'react';

import { usePageCommands } from '@/app/commandRegistry';
import { useDetailSelection } from '@/app/detailParam';
import {
  ConfirmDialog,
  DataTable,
  DetailPane,
  MasterDetail,
  PageIntro,
} from '@/components';
import { routeBlurb } from '@/routes';

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

/**
 * The line that leads a procedure's row.
 *
 * Most skills have no `title:` in their frontmatter, only the `description:` their own
 * lint insists on — and that description opens with what the procedure does before it
 * says when it fires. So the headline is the title when there is one, the first sentence
 * of the description when there is not, and only the file name when the procedure has
 * written nothing down about itself.
 */
export function skillHeadline(row: { title?: string; description?: string; name: string }): string {
  const title = row.title?.trim();
  if (title) return title;
  const sentence = row.description?.trim().split(/(?<=\.)\s/)[0]?.trim();
  return sentence || row.name;
}

/** What is left of the description once the headline has taken its first sentence. */
export function skillSubline(row: { title?: string; description?: string }): string {
  const description = row.description?.trim() ?? '';
  if (!description) return 'No description written down yet.';
  if (row.title?.trim()) return description;
  const rest = description.split(/(?<=\.)\s/).slice(1).join(' ').trim();
  return rest || description;
}

/** What each status means, so a badge is never a word you have to already know. */
const STATUS_MEANING: Record<string, string> = {
  bound: 'In use: this procedure is loaded whenever the tasks listed run.',
  unbound: 'On disk but loaded by nothing. It does not run.',
  incubating: 'Being worked on. Nothing loads it yet.',
  archived: 'Retired. Nothing loads it, and nothing was deleted.',
  template: 'The blank a new procedure is copied from. It never runs.',
};

/** The Skills page: what exists, what loads it, and whether it still passes its own bar. */
export default function SkillsPage() {
  const skills = useSkills();
  const selection = useDetailSelection('skill');
  const selected = selection.id;
  const setSelected = selection.open;
  const [creating, setCreating] = useState(false);
  const [archiving, setArchiving] = useState(false);
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

  const detailPane =
    selected && current ? (
      <DetailPane
        title={skillHeadline(current)}
        subtitle={
          current.description?.trim() ||
          'This procedure has not written down what it does. Open SKILL.md below and say so in one sentence.'
        }
        rawId={selected}
        onClose={selection.close}
        actions={
          <Badge color={STATUS_COLOURS[current.status] ?? 'gray'}>{current.status}</Badge>
        }
      >
        <SkillDetail
          current={current}
          selected={selected}
          check={check}
          lastKind={lastKind}
          run={run}
          archive={archive}
          archiving={archiving}
          setArchiving={setArchiving}
          trialPrompt={trialPrompt}
          setTrialPrompt={setTrialPrompt}
        />
      </DetailPane>
    ) : null;

  return (
    <MasterDetail detail={detailPane}>
    <Stack gap="lg">
      <PageIntro
        title="Skills"
        blurb={routeBlurb('skills')}
        actions={<Button onClick={() => setCreating(true)}>Add a procedure</Button>}
      >
        <Text size="xs" c="dimmed">
          Each one is a written procedure the system follows. Click a row to see what loads
          it, who may change it, and the files themselves.
        </Text>
      </PageIntro>

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
          emptyTitle="No procedures on disk"
          emptyDescription="Nothing has been written down for the system to follow yet."
          columns={[
            {
              key: 'name',
              header: 'What it does',
              render: (row) => (
                <Stack gap={0}>
                  <Text size="sm" fw={row.name === selected ? 700 : 500}>
                    {skillHeadline(row)}
                  </Text>
                  <Text size="xs" c="dimmed" lineClamp={2}>
                    {skillSubline(row)}
                  </Text>
                </Stack>
              ),
              sortValue: (row) => skillHeadline(row),
            },
            {
              key: 'status',
              header: 'In use?',
              width: 130,
              render: (row) => (
                <Tooltip label={STATUS_MEANING[row.status] ?? row.status} multiline w={280}>
                  <Badge color={STATUS_COLOURS[row.status] ?? 'gray'}>{row.status}</Badge>
                </Tooltip>
              ),
              sortValue: (row) => row.status,
            },
            {
              key: 'bindings',
              header: 'Used when running',
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
              key: 'name-raw',
              header: 'Known as',
              width: 170,
              render: (row) => (
                <Text size="xs" ff="monospace" c="dimmed">
                  {row.name}
                </Text>
              ),
              sortValue: (row) => row.name,
            },
          ]}
        />
      </Card>

      <NewSkillModal
        opened={creating}
        onClose={() => setCreating(false)}
        busy={create.isPending}
        error={create.error ? String(create.error) : null}
        onCreate={async (body) => {
          await create.mutateAsync(body);
          setCreating(false);
          setSelected(body.name);
        }}
      />
    </Stack>
    </MasterDetail>
  );
}

/** Everything behind one procedure: who may change it, the checks, and the files. */
function SkillDetail({
  current,
  selected,
  check,
  lastKind,
  run,
  archive,
  archiving,
  setArchiving,
  trialPrompt,
  setTrialPrompt,
}: {
  current: SkillRow;
  selected: string;
  check: ReturnType<typeof useCheckRun>;
  lastKind: CheckKind;
  run: (kind: CheckKind) => void;
  archive: ReturnType<typeof useArchiveSkill>;
  archiving: boolean;
  setArchiving: (value: boolean) => void;
  trialPrompt: string;
  setTrialPrompt: (value: string) => void;
}) {
  return (
        <Stack gap="sm">
          <Group justify="space-between" mb="sm">
            <Text size="xs" c="dimmed">
              {STATUS_MEANING[current.status] ?? ''}
            </Text>
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
                onClick={() => setArchiving(true)}
                data-testid="archive-skill"
              >
                {current.status === 'archived' ? 'Restore' : 'Archive'}
              </Button>
            </Group>
          </Group>

          {/* `POST /api/skills/{name}/archive` is step-up guarded: archiving unbinds a
              skill from every task that loads it. It used to be a bare onClick. */}
          <ConfirmDialog
            opened={archiving}
            onClose={() => setArchiving(false)}
            title={
              current.status === 'archived'
                ? `Restore ${selected}?`
                : `Archive ${selected}?`
            }
            confirmLabel={current.status === 'archived' ? 'Restore' : 'Archive'}
            requireStepUp
            danger={current.status !== 'archived'}
            description={
              current.status === 'archived'
                ? 'The skill comes back as incubating; it is loaded by nothing until it is bound again.'
                : 'The skill stops being loaded by every task it is bound to. Nothing is deleted.'
            }
            onConfirm={async () => {
              await archive.mutateAsync({
                name: selected,
                restore: current.status === 'archived',
              });
              setArchiving(false);
            }}
          />

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

          <Group gap="xs">
            <Text size="xs" c="dimmed">
              Who may change which part (body / scripts / tests):
            </Text>
            <Text size="xs" ff="monospace">
              {current.policy.body}/{current.policy.scripts}/{current.policy.tests}
            </Text>
            <Tooltip
              multiline
              w={300}
              label="'human' means only you may change that part. 'gated' means Claude may propose a change, which is reviewed and merged before it takes effect."
            >
              <Badge size="xs" variant="light">
                what does this mean?
              </Badge>
            </Tooltip>
          </Group>
          <Text size="xs" c="dimmed">
            {current.has_tests
              ? `${current.test_files} test file(s) check this procedure still does what it says.`
              : 'No tests check this procedure. Nothing proves it still does what it says.'}
            {' '}Written by {current.origin}.
          </Text>

          <Stack gap="sm" mt="md">
            <SkillEditor name={selected} />
          </Stack>
        </Stack>
  );
}
