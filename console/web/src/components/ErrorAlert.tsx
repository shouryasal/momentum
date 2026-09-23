import { Alert } from '@mantine/core';
import { IconAlertTriangle } from '@tabler/icons-react';
import type { ReactNode } from 'react';

import { errorMessage } from '../api/errors';

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
 */
export function ErrorAlert({ error, title = 'Could not load this', children }: ErrorAlertProps) {
  if (error === null || error === undefined) return null;
  return (
    <Alert color="red" title={title} icon={<IconAlertTriangle size={16} />} data-testid="error-alert">
      {errorMessage(error)}
      {children}
    </Alert>
  );
}
