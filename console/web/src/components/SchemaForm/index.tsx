import { Alert, Stack } from '@mantine/core';
import { IconAlertTriangle } from '@tabler/icons-react';
import { useCallback } from 'react';

import { setAt, type ObjectPath } from '../../lib/objectPath';
import { SchemaField } from './SchemaField';
import type { JsonSchema, SchemaFormOptions, ValidationIssue } from './types';

export * from './types';
export { SchemaField } from './SchemaField';
export {
  DURATION_UNITS,
  FieldLabel,
  LeafWidget,
  convertDuration,
  durationBaseUnit,
  naturalUnit,
  type DurationUnit,
} from './widgets';
export {
  defaultForSchema,
  groupProperties,
  issuesAt,
  issuesUnder,
  normaliseSchema,
  resolveRef,
  schemaType,
  sortedProperties,
  titleFor,
  unitSuffix,
  widgetFor,
  withInheritedPresentation,
} from './util';

export interface SchemaFormProps<T = unknown> {
  /** JSON Schema of a pydantic model, `x-*` annotations included (spec 5.1 schema_meta). */
  schema: JsonSchema;
  value: T;
  onChange: (value: T) => void;
  /** Server validation output from `POST /api/config/{id}/preview`. */
  issues?: ValidationIssue[];
  options?: SchemaFormOptions;
  readOnly?: boolean;
  /** Render only this sub-tree of the schema, e.g. one `x-group` section. */
  rootPath?: ObjectPath;
  title?: string;
}

/**
 * JSON Schema -> Mantine form (spec 12).
 *
 * Adding an annotated pydantic field adds a form field with no frontend change: the
 * widget comes from the type plus `x-widget`/`x-unit`, protected fields show a lock, and
 * validation issues are addressed by dotted path.
 */
export function SchemaForm<T = unknown>({
  schema,
  value,
  onChange,
  issues = [],
  options,
  readOnly,
  title = 'Configuration',
}: SchemaFormProps<T>) {
  const handleChange = useCallback(
    (path: ObjectPath, next: unknown) => {
      onChange(setAt(value, path, next));
    },
    [onChange, value],
  );

  const rootIssues = issues.filter((issue) => issue.path === '');

  return (
    <Stack gap="md" data-testid="schema-form">
      {rootIssues.length > 0 ? (
        <Alert color="red" icon={<IconAlertTriangle size={16} />} data-testid="schema-form-errors">
          {rootIssues.map((issue) => issue.message).join('; ')}
        </Alert>
      ) : null}
      <SchemaField
        name={title}
        schema={schema}
        root={schema}
        path={[]}
        value={value}
        onChange={handleChange}
        issues={issues}
        {...(options ? { options } : {})}
        {...(readOnly ? { readOnly } : {})}
        depth={0}
      />
    </Stack>
  );
}
