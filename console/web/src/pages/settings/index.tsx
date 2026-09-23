import {
  Alert,
  Badge,
  Button,
  Card,
  Code,
  Grid,
  Group,
  Loader,
  ScrollArea,
  Select,
  Stack,
  Table,
  Tabs,
  Text,
  Title,
  Tooltip,
} from '@mantine/core';
import {
  IconAlertTriangle,
  IconArrowBackUp,
  IconFileCode,
  IconHistory,
  IconListCheck,
  IconLock,
  IconRefresh,
} from '@tabler/icons-react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';

import { errorMessage } from '@/api';
import { usePageCommands } from '@/app/commandRegistry';
import { useSession } from '@/app/SessionContext';
import { CodeEditor, ConfirmDialog, DataTable, DiffView, EmptyState, SchemaForm } from '@/components';
import type { JsonSchema, ValidationIssue } from '@/components';

import { routeBlurb } from '@/routes';

import { configApi, configKeys, type HistoryEntry, type PatchOp, type PreviewResult } from './api';
import { PendingEffectsBanner } from './components/PendingEffectsBanner';
import { SetupPanel } from './components/SetupPanel';
import { SavePreview, type SaveIntent } from './components/SavePreview';
import { SectionTree, sectionsOf } from './components/SectionTree';
import { changeBodyFor, getAtDotted, patchFrom, setAtDotted } from './patch';

type SaveBody = Parameters<typeof configApi.save>[1];

/**
 * Settings (spec 12 page 16).
 *
 * Driven entirely by the JSON Schema the pydantic models emit: the section tree, the form
 * widgets, the lock icons, the effect badges and the field help all come from `x-*`
 * annotations. Adding a config key in `ops/config.py` adds a field here with no change to
 * this file — that property is what the tests on both sides pin.
 */
export default function SettingsPage() {
  const [params, setParams] = useSearchParams();
  const queryClient = useQueryClient();
  const session = useSession();

  const fileId = params.get('file') ?? 'earn';
  const [section, setSection] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, unknown> | null>(null);
  const [rawDraft, setRawDraft] = useState<string | null>(null);
  // "Start here" first: the settings a demo run needs, before the whole generated form.
  const [tab, setTab] = useState<string | null>('setup');
  const [previewOpen, setPreviewOpen] = useState(false);
  const [preview, setPreview] = useState<PreviewResult | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const registry = useQuery({ queryKey: configKeys.list, queryFn: () => configApi.list() });
  const doc = useQuery({
    queryKey: configKeys.file(fileId),
    queryFn: () => configApi.get(fileId),
  });
  const history = useQuery({
    queryKey: configKeys.history(fileId),
    queryFn: () => configApi.history(fileId),
    enabled: tab === 'history',
  });

  // A fresh document resets the draft: the operator is looking at what is on disk.
  useEffect(() => {
    if (!doc.data) return;
    setDraft(doc.data.values);
    setRawDraft(doc.data.raw);
    setSection((current) => current ?? firstSection(doc.data.ui.map((m) => m.path)));
  }, [doc.data]);

  useEffect(() => {
    const wanted = params.get('path');
    if (wanted) setSection(wanted.split('.')[0] ?? null);
  }, [params]);

  const ops: PatchOp[] = useMemo(() => {
    if (!doc.data || !draft) return [];
    return patchFrom(doc.data.values, draft);
  }, [doc.data, draft]);

  const dirtyPaths = useMemo(
    () => (doc.data && draft ? changedDotted(doc.data.values, draft) : []),
    [doc.data, draft],
  );

  const rawDirty = Boolean(doc.data && rawDraft !== null && rawDraft !== doc.data.raw);
  const dirty = ops.length > 0 || rawDirty;

  /** The change, derived once, for both the preview and the save (see `changeBodyFor`). */
  const changeBody = useMemo(
    () => changeBodyFor({ rawDirty, rawDraft, ops }),
    [rawDirty, rawDraft, ops],
  );

  usePageCommands('settings', [
    {
      id: 'raw-yaml',
      title: 'Settings: raw YAML',
      subtitle: 'Edit the file directly, validated server-side on every change',
      run: (ctx) => {
        setTab('raw');
        ctx.close();
      },
    },
    {
      id: 'history',
      title: 'Settings: history and revert',
      run: (ctx) => {
        setTab('history');
        ctx.close();
      },
    },
    {
      id: 'discard',
      title: 'Discard the unsaved config changes',
      subtitle: dirty ? 'There are unsaved edits' : 'Nothing is unsaved',
      run: (ctx) => {
        if (doc.data) {
          setDraft(doc.data.values);
          setRawDraft(doc.data.raw);
        }
        ctx.close();
      },
    },
    {
      id: 'models-yaml',
      title: 'Settings: models.yaml',
      keywords: ['llm', 'routing'],
      run: (ctx) => {
        setParams({ file: 'models' });
        ctx.close();
      },
    },
  ]);

  const runPreview = useCallback(
    async (body: { patch?: PatchOp[]; raw?: string }) => {
      if (!doc.data) return;
      setPreviewError(null);
      setPreview(null);
      setPreviewOpen(true);
      try {
        setPreview(await configApi.preview(fileId, { ...body, base_sha: doc.data.sha }));
      } catch (err) {
        setPreviewError(errorMessage(err));
      }
    },
    [doc.data, fileId],
  );

  const save = useMutation({
    mutationFn: async (intent: SaveIntent) => {
      if (!doc.data) throw new Error('no document');
      // ConfirmDialog has already opened the step-up window; the token rides along only
      // because the server re-checks it on the protected-path branch.
      const body: SaveBody = {
        base_sha: doc.data.sha,
        reason: intent.reason,
        commit: intent.commit,
        apply_effects: intent.applyEffects,
        ...(intent.confirmPhrase ? { confirm_phrase: intent.confirmPhrase } : {}),
        ...changeBody,
      };
      return configApi.save(fileId, body);
    },
    onSuccess: (result) => {
      setNotice(
        `Saved ${result.rel} (${result.changed_paths.length} path(s))` +
          (result.git_commit ? ` · commit ${result.git_commit.slice(0, 8)}` : '') +
          (result.effects_applied ? ' · effects applied' : ''),
      );
      void queryClient.invalidateQueries({ queryKey: ['config'] });
    },
  });

  /**
   * Restoring an earlier revision of the whole file.
   *
   * `confirm_phrase` is the phrase the **operator typed**, not the one the server handed
   * the client in `GET /api/config/{id}`: forwarding `doc.data.confirm_phrase` made the
   * typed confirmation a no-op the page satisfied on the operator's behalf. `apply_effects`
   * is explicit for the same reason — the server's default is `apply_now`, which
   * regenerates `var/runtime` and restarts the bots.
   */
  const revert = useMutation({
    mutationFn: (vars: { entry: HistoryEntry; phrase: string }) =>
      configApi.revert(fileId, {
        audit_id: vars.entry.id,
        confirm_phrase: vars.phrase,
        apply_effects: false,
        commit: false,
      }),
    onSuccess: () => {
      setNotice('Reverted. The effects are queued — apply them from the banner.');
      void queryClient.invalidateQueries({ queryKey: ['config'] });
    },
  });

  const document = doc.data;
  const sections = useMemo(() => (document ? sectionsOf(document.ui) : []), [document]);
  const activeSection = section ?? sections[0]?.key ?? null;

  const subSchema: JsonSchema | null = useMemo(() => {
    if (!document?.schema || !activeSection) return null;
    const child = document.schema.properties?.[activeSection];
    if (!child) return null;
    return { ...child, ...(document.schema.$defs ? { $defs: document.schema.$defs } : {}) };
  }, [document, activeSection]);

  const issues: ValidationIssue[] = useMemo(() => {
    if (!preview || preview.valid) return [];
    const prefix = activeSection ? `${activeSection}.` : '';
    return preview.errors.map((err) => ({
      path: err.loc.startsWith(prefix) ? err.loc.slice(prefix.length) : err.loc,
      message: err.msg,
    }));
  }, [preview, activeSection]);

  if (doc.isLoading || registry.isLoading) {
    return (
      <Group justify="center" p="xl">
        <Loader />
      </Group>
    );
  }

  if (doc.isError || !document) {
    return (
      <EmptyState
        title="Config unavailable"
        description={errorMessage(doc.error) || `No config file named ${fileId}.`}
      />
    );
  }

  const files = registry.data?.files ?? [];

  return (
    <Stack gap="md" data-testid="settings-page">
      <Group justify="space-between" align="flex-end">
        <Stack gap={2}>
          <Title order={3}>Setup</Title>
          <Text size="sm" c="dimmed" data-testid="page-blurb">
            {routeBlurb('settings')}
          </Text>
          <Text size="xs" c="dimmed">
            {document.description}
          </Text>
        </Stack>
        <Group gap="xs">
          <Select
            data={files.map((file) => ({ value: file.id, label: `${file.title} — ${file.rel}` }))}
            value={fileId}
            onChange={(value) => {
              if (!value) return;
              setSection(null);
              setPreview(null);
              setParams({ file: value });
            }}
            w={320}
            data-testid="config-file-select"
          />
          <Button
            variant="default"
            leftSection={<IconRefresh size={14} />}
            onClick={() => void queryClient.invalidateQueries({ queryKey: ['config'] })}
          >
            Reload
          </Button>
        </Group>
      </Group>

      <PendingEffectsBanner initial={document.banner} />

      {document.bless.file_is_blessed && !document.bless.ok ? (
        <Alert color="orange" icon={<IconAlertTriangle size={16} />} title="Config is not blessed">
          {document.bless.reason}
          {document.bless.changed.length > 0 ? `: ${document.bless.changed.join(', ')}` : ''} —
          preflight refuses to go live until a save re-blesses it.
        </Alert>
      ) : null}

      {document.live_sleeves.length > 0 ? (
        <Alert color="yellow" icon={<IconLock size={16} />}>
          Sleeve {document.live_sleeves.join(', ')} is live: universe, strategy and live-mode keys
          are refused until it is back in TEST.
        </Alert>
      ) : null}

      {!document.editable ? (
        <Alert color="gray" icon={<IconLock size={16} />} title="Read-only">
          {document.read_only_reason}
        </Alert>
      ) : null}

      {notice ? (
        <Alert color="teal" withCloseButton onClose={() => setNotice(null)}>
          {notice}
        </Alert>
      ) : null}
      {save.isError ? (
        <Alert color="red" icon={<IconAlertTriangle size={16} />} data-testid="save-error">
          {errorMessage(save.error)}
        </Alert>
      ) : null}
      {revert.isError ? <Alert color="red">{errorMessage(revert.error)}</Alert> : null}

      <Tabs value={tab} onChange={setTab}>
        <Tabs.List>
          <Tabs.Tab value="setup" leftSection={<IconListCheck size={14} />}>
            Start here
          </Tabs.Tab>
          <Tabs.Tab value="form" disabled={!document.schema}>
            All settings
          </Tabs.Tab>
          <Tabs.Tab value="raw" leftSection={<IconFileCode size={14} />}>
            Raw {document.format.toUpperCase()}
          </Tabs.Tab>
          <Tabs.Tab value="history" leftSection={<IconHistory size={14} />}>
            What changed
          </Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="setup" pt="md">
          <SetupPanel
            onOpenSection={(next) => {
              setSection(next);
              setTab('form');
            }}
          />
        </Tabs.Panel>

        <Tabs.Panel value="form" pt="md">
          {document.schema ? (
            <Grid>
              <Grid.Col span={{ base: 12, md: 3 }}>
                <SectionTree
                  sections={sections}
                  active={activeSection}
                  onSelect={setSection}
                  dirtyPaths={dirtyPaths}
                />
              </Grid.Col>
              <Grid.Col span={{ base: 12, md: 9 }}>
                <Stack gap="md">
                  {subSchema && draft && activeSection ? (
                    <SchemaForm
                      schema={subSchema}
                      value={(draft[activeSection] ?? {}) as unknown}
                      onChange={(next) =>
                        setDraft((current) =>
                          current ? { ...current, [activeSection]: next } : current,
                        )
                      }
                      issues={issues}
                      readOnly={!document.editable}
                      title={activeSection}
                    />
                  ) : (
                    <EmptyState title="Pick a section" description="Choose a section on the left." />
                  )}
                  <FieldTools
                    fileId={fileId}
                    section={activeSection}
                    draft={draft}
                    onChange={setDraft}
                  />
                </Stack>
              </Grid.Col>
            </Grid>
          ) : (
            <EmptyState
              title="No schema"
              description="This file has no pydantic model; edit it on the Raw tab."
            />
          )}
        </Tabs.Panel>

        <Tabs.Panel value="raw" pt="md">
          <Stack gap="sm">
            <CodeEditor
              value={rawDraft ?? document.raw}
              onChange={setRawDraft}
              language={document.format === 'json' ? 'json' : 'yaml'}
              readOnly={!document.editable}
              height="520px"
            />
            {rawDirty ? (
              <DiffView before={document.raw} after={rawDraft ?? ''} context={3} maxHeight={260} />
            ) : null}
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value="history" pt="md">
          <HistoryTab
            entries={history.data?.entries ?? []}
            loading={history.isLoading}
            confirmPhrase={document.confirm_phrase}
            stepUpActive={session.stepUpActive}
            onRevert={(entry, phrase) => revert.mutateAsync({ entry, phrase })}
            reverting={revert.isPending}
          />
        </Tabs.Panel>
      </Tabs>

      {document.editable && tab !== 'history' ? (
        <Group justify="flex-end">
          <Text size="sm" c="dimmed">
            {dirty ? `${dirtyPaths.length || (rawDirty ? 1 : 0)} pending change(s)` : 'No changes'}
          </Text>
          <Button
            variant="default"
            onClick={() => {
              setDraft(document.values);
              setRawDraft(document.raw);
            }}
            disabled={!dirty}
          >
            Discard
          </Button>
          <Button
            disabled={!dirty}
            onClick={() =>
              void runPreview(changeBody)
            }
            data-testid="review-changes"
          >
            Review changes
          </Button>
        </Group>
      ) : null}

      <SavePreview
        opened={previewOpen}
        onClose={() => setPreviewOpen(false)}
        fileLabel={document.rel}
        before={document.raw}
        preview={preview}
        loading={preview === null && previewError === null}
        error={previewError ?? (save.isError ? errorMessage(save.error) : null)}
        stepUpActive={session.stepUpActive}
        defaultCommit={false}
        defaultApplyNow
        onSave={async (intent) => {
          await save.mutateAsync(intent);
        }}
      />
    </Stack>
  );
}

function firstSection(paths: string[]): string | null {
  for (const path of paths) {
    const head = path.split('.')[0];
    if (head) return head;
  }
  return null;
}

function changedDotted(before: unknown, after: unknown, base = ''): string[] {
  return patchFrom(before, after).map((op) =>
    base +
    op.path
      .split('/')
      .slice(1)
      .map((part) => part.replace(/~1/g, '/').replace(/~0/g, '~'))
      .join('.'),
  );
}

/** Per-field blame and revert-to-default, addressed by dotted path (spec 12 page 16). */
function FieldTools({
  fileId,
  section,
  draft,
  onChange,
}: {
  fileId: string;
  section: string | null;
  draft: Record<string, unknown> | null;
  onChange: (next: Record<string, unknown>) => void;
}) {
  const doc = useQuery({ queryKey: configKeys.file(fileId), queryFn: () => configApi.get(fileId) });
  const [path, setPath] = useState<string | null>(null);

  const options = useMemo(() => {
    const ui = doc.data?.ui ?? [];
    return ui
      .filter((meta) => meta.path && (!section || meta.path.startsWith(`${section}.`)))
      .filter((meta) => !meta.path.endsWith('.*') && meta.type !== 'object')
      .map((meta) => ({ value: meta.path, label: meta.path }));
  }, [doc.data, section]);

  const meta = doc.data?.ui.find((entry) => entry.path === path);
  const blame = path ? doc.data?.blame[path] : undefined;
  const current = path && draft ? getAtDotted(draft, path) : undefined;

  const defaults = useMutation({
    mutationFn: (target: string) => configApi.defaults(fileId, [target]),
    onSuccess: (result) => {
      if (!path || !draft) return;
      onChange(setAtDotted(draft, path, result.defaults[path] ?? null));
    },
  });

  return (
    <Card withBorder padding="sm" data-testid="field-tools">
      <Stack gap="xs">
        <Group justify="space-between">
          <Text size="sm" fw={600}>
            Field tools
          </Text>
          <Text size="xs" c="dimmed">
            blame · revert to default
          </Text>
        </Group>
        <Select
          size="xs"
          searchable
          clearable
          placeholder="Pick a field path"
          data={options}
          value={path}
          onChange={setPath}
          data-testid="field-tools-path"
        />
        {meta ? (
          <Table withRowBorders={false} verticalSpacing={2} fz="xs">
            <Table.Tbody>
              <Table.Tr>
                <Table.Td w={140}>Current</Table.Td>
                <Table.Td>
                  <Text ff="monospace" size="xs">
                    {JSON.stringify(current)}
                  </Text>
                </Table.Td>
              </Table.Tr>
              <Table.Tr>
                <Table.Td>Default</Table.Td>
                <Table.Td>
                  <Text ff="monospace" size="xs">
                    {JSON.stringify(meta.default)}
                  </Text>
                </Table.Td>
              </Table.Tr>
              <Table.Tr>
                <Table.Td>Tier</Table.Td>
                <Table.Td>
                  <Group gap={4}>
                    <Badge size="xs" variant="light">
                      {meta.tier}
                    </Badge>
                    {meta.protected ? (
                      <Tooltip label="Needs step-up and a typed phrase">
                        <Badge size="xs" color="orange" leftSection={<IconLock size={10} />}>
                          protected
                        </Badge>
                      </Tooltip>
                    ) : null}
                    {meta.effects.map((effect) => (
                      <Badge key={effect} size="xs" color="grape" variant="light">
                        {effect}
                      </Badge>
                    ))}
                  </Group>
                </Table.Td>
              </Table.Tr>
              <Table.Tr>
                <Table.Td>Last changed</Table.Td>
                <Table.Td>
                  {blame ? (
                    <Text size="xs">
                      {blame.actor} at {blame.ts_utc}
                      {blame.reason ? ` — ${blame.reason}` : ''}
                    </Text>
                  ) : (
                    <Text size="xs" c="dimmed">
                      never changed through the console
                    </Text>
                  )}
                </Table.Td>
              </Table.Tr>
            </Table.Tbody>
          </Table>
        ) : null}
        <Group>
          <Button
            size="xs"
            variant="light"
            leftSection={<IconArrowBackUp size={14} />}
            disabled={!path || !draft}
            loading={defaults.isPending}
            onClick={() => path && defaults.mutate(path)}
          >
            Revert to default
          </Button>
        </Group>
      </Stack>
    </Card>
  );
}

/**
 * The audited history, with revert.
 *
 * Revert restores the *whole* file as it stood before one save — `risk.*` and `trading.*`
 * included — and by default regenerates `var/runtime` and restarts the bots. It used to be
 * a bare button whose only guard was `disabled={!row.revertable}`, so one misclick
 * rewrote protected config. It now goes through the same typed-phrase + step-up dialog a
 * protected save does, and shows the diff it is about to undo.
 */
export function HistoryTab({
  entries,
  loading,
  confirmPhrase,
  stepUpActive,
  onRevert,
  reverting,
}: {
  entries: HistoryEntry[];
  loading: boolean;
  confirmPhrase: string;
  stepUpActive: boolean;
  onRevert: (entry: HistoryEntry, phrase: string) => Promise<unknown>;
  reverting: boolean;
}) {
  const [selected, setSelected] = useState<HistoryEntry | null>(null);
  const [pendingRevert, setReverting] = useState<HistoryEntry | null>(null);

  return (
    <Stack gap="md">
      <DataTable<HistoryEntry>
        rows={entries}
        loading={loading}
        rowKey={(row) => String(row.id)}
        onRowClick={setSelected}
        emptyTitle="No saves yet"
        emptyDescription="Every console save lands in config_audit with its diff."
        columns={[
          { key: 'ts', header: 'When', render: (row) => row.ts_utc, sortValue: (row) => row.ts_utc },
          { key: 'actor', header: 'Actor', render: (row) => row.actor },
          { key: 'reason', header: 'Reason', render: (row) => row.reason ?? '' },
          {
            key: 'paths',
            header: 'Paths',
            render: (row) => (
              <Text size="xs" ff="monospace">
                {row.changed_paths.slice(0, 4).join(', ')}
                {row.changed_paths.length > 4 ? ` +${row.changed_paths.length - 4}` : ''}
              </Text>
            ),
          },
          {
            key: 'flags',
            header: '',
            render: (row) => (
              <Group gap={4}>
                {row.protected_changed ? (
                  <Badge size="xs" color="orange">
                    protected
                  </Badge>
                ) : null}
                {row.applied ? null : (
                  <Badge size="xs" color="yellow">
                    not applied
                  </Badge>
                )}
                {row.git_commit ? (
                  <Badge size="xs" variant="light">
                    {row.git_commit.slice(0, 8)}
                  </Badge>
                ) : null}
              </Group>
            ),
          },
          {
            key: 'revert',
            header: '',
            align: 'right',
            render: (row) => (
              <Button
                size="compact-xs"
                variant="light"
                disabled={!row.revertable || reverting}
                onClick={(event) => {
                  event.stopPropagation();
                  setReverting(row);
                }}
                data-testid={`revert-${row.id}`}
              >
                Revert
              </Button>
            ),
          },
        ]}
      />
      <ConfirmDialog
        opened={pendingRevert !== null}
        onClose={() => setReverting(null)}
        title={`Revert to before config_audit #${pendingRevert?.id ?? ''}?`}
        confirmLabel="Revert the file"
        confirmPhrase={confirmPhrase}
        requireStepUp
        stepUpSatisfied={stepUpActive}
        danger
        description={
          <>
            This restores the <strong>whole file</strong> as it stood before that save
            {pendingRevert?.changed_paths.length
              ? `, undoing ${pendingRevert.changed_paths.join(', ')}`
              : ''}
            . The effects are queued, not applied — use the banner when you are ready.
          </>
        }
        onConfirm={async ({ phrase }) => {
          if (pendingRevert) await onRevert(pendingRevert, phrase ?? '');
          setReverting(null);
        }}
      >
        {pendingRevert ? (
          <ScrollArea.Autosize mah={200}>
            <Code block style={{ whiteSpace: 'pre' }}>
              {pendingRevert.diff || 'no diff recorded'}
            </Code>
          </ScrollArea.Autosize>
        ) : null}
      </ConfirmDialog>
      {selected ? (
        <Card withBorder padding="sm">
          <Stack gap="xs">
            <Group justify="space-between">
              <Text size="sm" fw={600}>
                config_audit #{selected.id}
              </Text>
              <Button size="compact-xs" variant="subtle" onClick={() => setSelected(null)}>
                Close
              </Button>
            </Group>
            <CodeEditor value={selected.diff} language="text" readOnly height="280px" />
          </Stack>
        </Card>
      ) : null}
    </Stack>
  );
}
