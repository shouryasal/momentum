import { Alert, List } from '@mantine/core';
import { IconAlertTriangle } from '@tabler/icons-react';
import type { ReactNode } from 'react';

import { errorFields, errorMessage } from '../api/errors';

export interface ErrorAlertProps {
  /** A react-query `error`, an `ApiError`, or anything else that reached a catch. */
  error: unknown;
  title?: ReactNode;
  /** Extra context: what the operator can do about it. */
  children?: ReactNode;
}

/**
 * The one way a page says "this did not load".
 *
 * A failed panel must never render as an empty one — an empty table and a 503 look the
 * same to the eye and mean opposite things.  `error` being null or undefined renders
 * nothing, so this can sit unconditionally beside a query.
 *
 * A 422 also names its fields: `errorFields` decodes `detail.errors`, which the console's
 * validation handler always sends and which nothing used to read, so "request body failed
 * validation" was the whole message the operator got.
 */
export function ErrorAlert({ error, title = 'Could not load this', children }: ErrorAlertProps) {
  if (error === null || error === undefined) return null;
  const fields = errorFields(error);
  return (
    <Alert color="red" title={title} icon={<IconAlertTriangle size={16} />} data-testid="error-alert">
      {errorMessage(error)}
      {fields.length ? (
        <List size="sm" mt={4} data-testid="error-alert-fields">
          {fields.map((line) => (
            <List.Item key={line}>{line}</List.Item>
          ))}
        </List>
      ) : null}
      {children}
    </Alert>
  );
}
