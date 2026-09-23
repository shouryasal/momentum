import { ActionIcon, Button, Fieldset, Group, Paper, Stack, Table, Text, TextInput } from '@mantine/core';
import { IconPlus, IconTrash } from '@tabler/icons-react';
import { useState } from 'react';

import { pathToString, type ObjectPath } from '../../lib/objectPath';
import { FieldLabel, LeafWidget } from './widgets';
import type { JsonSchema, SchemaFormOptions, ValidationIssue } from './types';
import { defaultForSchema, issuesAt, issuesUnder, normaliseSchema, sortedProperties, titleFor, widgetFor, withInheritedPresentation } from './util';

export interface SchemaFieldProps {
  name: string;
  schema: JsonSchema;
  root: JsonSchema;
  path: ObjectPath;
  value: unknown;
  onChange: (path: ObjectPath, value: unknown) => void;
  issues: ValidationIssue[];
  options?: SchemaFormOptions;
  readOnly?: boolean;
  required?: boolean;
  depth?: number;
}

/** One schema node: a container (object / array / additionalProperties map) or a leaf. */
export function SchemaField(props: SchemaFieldProps) {
  const { name, root, path, value, onChange, issues, options, readOnly, required, depth = 0 } = props;
  const { schema } = normaliseSchema(props.schema, root);
  const widget = widgetFor(schema);
  const label = titleFor(name, schema);
  const pathKey = pathToString(path);

  if (widget === 'object') {
    const record = (value ?? {}) as Record<string, unknown>;
    const nested = issuesUnder(issues, pathKey);
    return (
      <Fieldset
        legend={<FieldLabel label={label} schema={schema} />}
        variant={depth === 0 ? 'default' : 'filled'}
        data-testid={`object-${pathKey || 'root'}`}
      >
        <Stack gap="sm">
          {schema.description ? (
            <Text size="xs" c="dimmed">
              {schema.description}
            </Text>
          ) : null}
          {nested.length > 0 && depth > 0 ? (
            <Text size="xs" c="red">
              {nested.length} validation issue(s) below
            </Text>
          ) : null}
          {sortedProperties(schema).map(([key, child]) => (
            <SchemaField
              key={key}
              name={key}
              schema={child}
              root={root}
              path={[...path, key]}
              value={record[key]}
              onChange={onChange}
              issues={issues}
              {...(options ? { options } : {})}
              {...(readOnly ? { readOnly } : {})}
              required={(schema.required ?? []).includes(key)}
              depth={depth + 1}
            />
          ))}
        </Stack>
      </Fieldset>
    );
  }

  if (widget === 'map') {
    return (
      <MapField
        label={label}
        schema={schema}
        root={root}
        path={path}
        value={value}
        onChange={onChange}
        issues={issues}
        {...(options ? { options } : {})}
        {...(readOnly ? { readOnly } : {})}
        depth={depth}
      />
    );
  }

  if (widget === 'array' && schema.items) {
    const items = Array.isArray(value) ? value : [];
    // `x-widget`/`x-unit` sit on the container (pydantic never annotates `list[str]` items),
    // so `research.slots` gets a clock picker per entry rather than a bare text box.
    const itemSchema = withInheritedPresentation(schema, schema.items);
    return (
      <Fieldset
        legend={<FieldLabel label={label} schema={schema} />}
        variant="filled"
        data-testid={`array-${pathKey}`}
      >
        <Stack gap="xs">
          {schema.description ? (
            <Text size="xs" c="dimmed">
              {schema.description}
            </Text>
          ) : null}
          {items.length === 0 ? (
            <Text size="xs" c="dimmed">
              empty
            </Text>
          ) : null}
          {items.map((item, index) => (
            <Paper key={`${pathKey}-${index}`} p="xs" radius="sm">
              <Group align="flex-start" wrap="nowrap" gap="xs">
                <div style={{ flex: 1 }}>
                  <SchemaField
                    name={`${label} ${index + 1}`}
                    schema={itemSchema}
                    root={root}
                    path={[...path, index]}
                    value={item}
                    onChange={onChange}
                    issues={issues}
                    {...(options ? { options } : {})}
                    {...(readOnly ? { readOnly } : {})}
                    depth={depth + 1}
                  />
                </div>
                <ActionIcon
                  variant="subtle"
                  color="red"
                  aria-label={`remove item ${index + 1}`}
                  data-testid={`array-remove-${pathKey}-${index}`}
                  disabled={readOnly}
                  onClick={() => {
                    const next = [...items];
                    next.splice(index, 1);
                    onChange(path, next);
                  }}
                >
                  <IconTrash size={16} />
                </ActionIcon>
              </Group>
            </Paper>
          ))}
          <Group>
            <Button
              size="xs"
              variant="light"
              leftSection={<IconPlus size={14} />}
              disabled={readOnly}
              data-testid={`array-add-${pathKey}`}
              onClick={() => onChange(path, [...items, defaultForSchema(itemSchema, root)])}
            >
              Add
            </Button>
          </Group>
        </Stack>
      </Fieldset>
    );
  }

  const error = issuesAt(issues, pathKey)
    .map((issue) => issue.message)
    .join('; ');

  return (
    <LeafWidget
      widget={widget}
      schema={schema}
      path={pathKey}
      label={label}
      value={value}
      onChange={(next) => onChange(path, next)}
      {...(error ? { error } : {})}
      {...(readOnly ? { readOnly } : {})}
      {...(required ? { required } : {})}
      {...(options ? { options } : {})}
    />
  );
}

interface MapFieldProps {
  label: string;
  schema: JsonSchema;
  root: JsonSchema;
  path: ObjectPath;
  value: unknown;
  onChange: (path: ObjectPath, value: unknown) => void;
  issues: ValidationIssue[];
  options?: SchemaFormOptions;
  readOnly?: boolean;
  depth: number;
}

/** `additionalProperties` -> key/value table with add & remove (spec 12). */
function MapField({ label, schema, root, path, value, onChange, issues, options, readOnly, depth }: MapFieldProps) {
  const pathKey = pathToString(path);
  const record = (value ?? {}) as Record<string, unknown>;
  const entries = Object.entries(record);
  const valueSchema: JsonSchema = withInheritedPresentation(
    schema,
    typeof schema.additionalProperties === 'object' ? schema.additionalProperties : {},
  );
  const [newKey, setNewKey] = useState('');

  const addEntry = () => {
    const key = newKey.trim();
    if (key === '' || key in record) return;
    onChange(path, { ...record, [key]: defaultForSchema(valueSchema, root) });
    setNewKey('');
  };

  return (
    <Fieldset
      legend={<FieldLabel label={label} schema={schema} />}
      variant="filled"
      data-testid={`map-${pathKey}`}
    >
      <Stack gap="xs">
        {schema.description ? (
          <Text size="xs" c="dimmed">
            {schema.description}
          </Text>
        ) : null}
        <Table withRowBorders={false}>
          <Table.Tbody>
            {entries.map(([key, entryValue]) => (
              <Table.Tr key={key}>
                <Table.Td style={{ width: '32%', verticalAlign: 'top' }}>
                  <Text size="sm" ff="monospace" pt={6}>
                    {key}
                  </Text>
                </Table.Td>
                <Table.Td>
                  <SchemaField
                    name={key}
                    schema={valueSchema}
                    root={root}
                    path={[...path, key]}
                    value={entryValue}
                    onChange={onChange}
                    issues={issues}
                    {...(options ? { options } : {})}
                    {...(readOnly ? { readOnly } : {})}
                    depth={depth + 1}
                  />
                </Table.Td>
                <Table.Td style={{ width: 40, verticalAlign: 'top' }}>
                  <ActionIcon
                    variant="subtle"
                    color="red"
                    aria-label={`remove ${key}`}
                    data-testid={`map-remove-${pathKey}-${key}`}
                    disabled={readOnly}
                    onClick={() => {
                      const next = { ...record };
                      delete next[key];
                      onChange(path, next);
                    }}
                  >
                    <IconTrash size={16} />
                  </ActionIcon>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
        <Group gap="xs">
          <TextInput
            size="xs"
            placeholder="new key"
            value={newKey}
            disabled={readOnly}
            onChange={(event) => setNewKey(event.currentTarget.value)}
            data-testid={`map-newkey-${pathKey}`}
          />
          <Button
            size="xs"
            variant="light"
            leftSection={<IconPlus size={14} />}
            disabled={readOnly || newKey.trim() === ''}
            onClick={addEntry}
            data-testid={`map-add-${pathKey}`}
          >
            Add entry
          </Button>
        </Group>
      </Stack>
    </Fieldset>
  );
}
