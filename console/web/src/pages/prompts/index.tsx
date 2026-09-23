import {
  Alert, Badge, Button, Card, Code, Group, Loader, NavLink, Paper, ScrollArea, Select, Stack,
  Switch, Table, Tabs, Text, Title,
} from '@mantine/core';
import { useEffect, useMemo, useState } from 'react';

import { usePageCommands } from '@/app/commandRegistry';
import { CodeEditor, ConfirmDialog, DiffView, EmptyState } from '@/components';

import {
  useActivatePrompt,
  usePrompt,
  usePromptDiff,
  usePrompts,
  useRenderPrompt,
  useSavePrompt,
  type PromptFamily,
} from './api';

function VersionBadges({ family }: { family: PromptFamily }) {
  return (
    <Group gap={4}>
      {family.versions.map((version) => (
        <Badge
          key={version.version_id}
          size="sm"
          variant={version.active ? 'filled' : 'light'}
          color={version.active ? 'teal' : version.immutable ? 'gray' : 'blue'}
        >
          v{version.version}
        </Badge>
      ))}
    </Group>
  );
}

/**
 * The Prompts page.
 *
 * A version that produced a recorded proposal is immutable, because the replay harness
 * rebuilds it to check for builder drift — so "Save" means "save the next version" unless
 * nothing has used this one yet. Activating a version is what actually changes production,
 * and it is step-up protected.
 */
export default function PromptsPage() {
  const prompts = usePrompts();
  const [path, setPath] = useState<string | null>(null);
  const [compareWith, setCompareWith] = useState<string | null>(null);
  const [asNewVersion, setAsNewVersion] = useState(true);
  const [draft, setDraft] = useState('');
  const [activating, setActivating] = useState<{ family: string; version: string } | null>(null);

  const detail = usePrompt(path);
  const other = usePrompt(compareWith);
  const diff = usePromptDiff(compareWith, path);
  const save = useSavePrompt();
  const activate = useActivatePrompt();
  const render = useRenderPrompt();

  const families = prompts.data?.families ?? [];

  useEffect(() => {
    if (path) return;
    const first = families[0]?.versions.at(-1)?.path ?? null;
    if (first) setPath(first);
  }, [families, path]);

  useEffect(() => {
    if (detail.data) setDraft(detail.data.content);
  }, [detail.data]);

  const currentFamily = useMemo(
    () => families.find((family) => family.versions.some((v) => v.path === path)) ?? null,
    [families, path],
  );

  const dirty = Boolean(detail.data) && draft !== detail.data?.content;
  const immutable = detail.data?.immutable ?? false;

  usePageCommands('prompts', [
    {
      id: 'render',
      title: 'Render the selected prompt',
      subtitle: 'Token estimate against the context budget',
      run: (ctx) => {
        if (path) render.mutate({ path, context: {} });
        ctx.close();
      },
    },
    {
      id: 'discard',
      title: 'Discard the unsaved prompt edits',
      subtitle: dirty ? 'There are unsaved edits' : 'Nothing is unsaved',
      run: (ctx) => {
        if (detail.data) setDraft(detail.data.content);
        ctx.close();
      },
    },
    {
      id: 'activate',
      title: 'Activate the selected prompt version',
      subtitle: 'Step-up protected; the next unattended run sends it',
      run: (ctx) => {
        if (currentFamily && detail.data) {
          setActivating({ family: currentFamily.family, version: detail.data.version_id });
        }
        ctx.close();
      },
    },
  ]);

  return (
    <Stack gap="lg">
      <Title order={2}>Prompts</Title>

      {prompts.error ? (
        <Alert color="red" title="Could not load the prompts">
          {String(prompts.error)}
        </Alert>
      ) : null}

      <Group align="flex-start" gap="md" wrap="nowrap">
        <Paper withBorder p="xs" w={300}>
          <ScrollArea.Autosize mah={560}>
            {prompts.isLoading ? <Loader size="sm" m="md" /> : null}
            {!prompts.isLoading && families.length === 0 ? (
              <EmptyState
                compact
                title="No prompt families"
                description="Families are the `<name>.v<N>.md` files at the top of prompts/."
              />
            ) : null}
            {families.map((family) => (
              <div key={family.family}>
                <Group justify="space-between" px="xs" pt="xs">
                  <Text size="sm" fw={600}>
                    {family.family}
                  </Text>
                  <VersionBadges family={family} />
                </Group>
                {family.versions.map((version) => (
                  <NavLink
                    key={version.path}
                    label={version.version_id}
                    description={`${version.tokens} tokens · ${version.snapshots} snapshot(s)`}
                    active={version.path === path}
                    onClick={() => setPath(version.path)}
                    rightSection={
                      version.active ? (
                        <Badge size="xs" color="teal">
                          active
                        </Badge>
                      ) : null
                    }
                  />
                ))}
              </div>
            ))}
          </ScrollArea.Autosize>
        </Paper>

        <Stack gap="sm" style={{ flex: 1, minWidth: 0 }}>
          {immutable ? (
            <Alert color="yellow" title="This version is immutable">
              It produced {detail.data?.snapshots} recorded proposal(s). The replay harness
              rebuilds it to check for builder drift, so editing it in place would invalidate
              that evidence. Save the next version instead.
            </Alert>
          ) : null}

          <Tabs defaultValue="edit" keepMounted={false}>
            <Tabs.List>
              <Tabs.Tab value="edit">Edit</Tabs.Tab>
              <Tabs.Tab value="diff">Diff</Tabs.Tab>
              <Tabs.Tab value="render">Render</Tabs.Tab>
              <Tabs.Tab value="usage">Used by</Tabs.Tab>
            </Tabs.List>

            <Tabs.Panel value="edit" pt="sm">
              <CodeEditor
                value={draft}
                onChange={setDraft}
                language="markdown"
                height="440px"
                data-testid="prompt-editor"
              />
              <Group justify="space-between" mt="sm">
                <Group gap="sm">
                  <Switch
                    label="Save as the next version"
                    checked={asNewVersion || immutable}
                    disabled={immutable}
                    onChange={(event) => setAsNewVersion(event.currentTarget.checked)}
                  />
                  <Text size="xs" c="dimmed">
                    placeholders: {(detail.data?.placeholders ?? []).join(', ') || 'none'}
                  </Text>
                </Group>
                <Button
                  disabled={!dirty || !path}
                  loading={save.isPending}
                  onClick={() =>
                    path &&
                    save.mutate({
                      path,
                      content: draft,
                      as_new_version: asNewVersion || immutable,
                      base_sha: detail.data?.sha ?? null,
                    })
                  }
                >
                  Save
                </Button>
              </Group>
              {save.error ? (
                <Alert color="red" title="Not saved" mt="sm">
                  {String(save.error)}
                </Alert>
              ) : null}
              {save.isSuccess ? (
                <Alert color="teal" title="Saved" mt="sm">
                  {save.data?.version_id}
                </Alert>
              ) : null}
            </Tabs.Panel>

            <Tabs.Panel value="diff" pt="sm">
              <Select
                label="Compare with"
                data={(currentFamily?.versions ?? []).map((v) => ({
                  value: v.path,
                  label: v.version_id,
                }))}
                value={compareWith}
                onChange={setCompareWith}
                mb="sm"
              />
              {other.data && detail.data ? (
                <DiffView
                  before={other.data.content}
                  after={detail.data.content}
                  beforeLabel={other.data.version_id}
                  afterLabel={detail.data.version_id}
                />
              ) : (
                <Text size="sm" c="dimmed">
                  Pick a version to compare against.
                </Text>
              )}
              {diff.data?.diff ? (
                <ScrollArea.Autosize mah={280} mt="sm">
                  <Code block style={{ whiteSpace: 'pre' }}>
                    {diff.data.diff}
                  </Code>
                </ScrollArea.Autosize>
              ) : null}
            </Tabs.Panel>

            <Tabs.Panel value="render" pt="sm">
              <Button
                mb="sm"
                loading={render.isPending}
                onClick={() => path && render.mutate({ path, context: {} })}
              >
                Render preview
              </Button>
              {render.data ? (
                <Stack gap="xs">
                  <Group gap="xs">
                    <Badge color={render.data.over_budget ? 'red' : 'teal'}>
                      {render.data.tokens} tokens
                      {render.data.budget_tokens
                        ? ` / budget ${render.data.budget_tokens}`
                        : ''}
                    </Badge>
                    {render.data.unresolved_placeholders.length ? (
                      <Badge color="yellow">
                        unresolved: {render.data.unresolved_placeholders.join(', ')}
                      </Badge>
                    ) : null}
                  </Group>
                  <ScrollArea.Autosize mah={400}>
                    <Code block style={{ whiteSpace: 'pre-wrap' }}>
                      {render.data.text}
                    </Code>
                  </ScrollArea.Autosize>
                </Stack>
              ) : null}
            </Tabs.Panel>

            <Tabs.Panel value="usage" pt="sm">
              <Table>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Run</Table.Th>
                    <Table.Th>When</Table.Th>
                    <Table.Th>Model</Table.Th>
                    <Table.Th>Valid</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {(detail.data?.snapshots_using ?? []).map((row) => (
                    <Table.Tr key={row.run_id}>
                      <Table.Td>{row.run_id}</Table.Td>
                      <Table.Td>{row.ts_utc}</Table.Td>
                      <Table.Td>{row.model}</Table.Td>
                      <Table.Td>{row.valid ? 'yes' : 'no'}</Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Tabs.Panel>
          </Tabs>

          {currentFamily && detail.data ? (
            <Card withBorder padding="sm">
              <Group justify="space-between">
                <Text size="sm">
                  Active for <Code>{currentFamily.family}</Code>:{' '}
                  {currentFamily.active ?? 'nothing yet'}
                </Text>
                <Button
                  variant="light"
                  disabled={currentFamily.active === detail.data.version_id}
                  onClick={() =>
                    setActivating({
                      family: currentFamily.family,
                      version: detail.data.version_id,
                    })
                  }
                >
                  Activate {detail.data.version_id}
                </Button>
              </Group>
              {activate.error ? (
                <Alert color="red" title="Not activated" mt="sm">
                  {String(activate.error)}
                </Alert>
              ) : null}
            </Card>
          ) : null}
        </Stack>
      </Group>

      <ConfirmDialog
        opened={Boolean(activating)}
        onClose={() => setActivating(null)}
        title="Activate this prompt version?"
        confirmLabel="Activate"
        requireStepUp
        description={
          <Text size="sm">
            The next unattended run will send this version. It is written to{' '}
            <Code>config/prompts-auto.yaml</Code>, the tier-1 overlay, and takes effect
            without a restart.
          </Text>
        }
        onConfirm={async () => {
          // `await` matters: the dialog reports the failure and stays open, and — since
          // it performs the step-up itself — the PUT now goes out inside the window.
          if (activating) await activate.mutateAsync(activating);
          setActivating(null);
        }}
      />
    </Stack>
  );
}
