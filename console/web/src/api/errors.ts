import type { ErrorDetail } from './contracts';

/** Normalised error for every failed API call (spec 5.2 error shape). */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: unknown;

  constructor(status: number, body: ErrorDetail) {
    super(body.message || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.code = body.code;
    this.detail = body.detail;
  }

  /** 401: the session cookie is missing or expired -> back to login. */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  /** 403 + `step_up_required`: re-enter the console token (spec 5.4). */
  get needsStepUp(): boolean {
    return this.status === 403 && this.code === 'step_up_required';
  }

  /** 409: optimistic concurrency (etag / base_sha conflict). */
  get isConflict(): boolean {
    return this.status === 409;
  }

  /** 423: blocked by the ops lock or by KILL. */
  get isLocked(): boolean {
    return this.status === 423;
  }

  /** 421: the Host header was not 127.0.0.1/localhost (DNS-rebinding guard). */
  get isWrongHost(): boolean {
    return this.status === 421;
  }
}

const CODE_BY_STATUS: Record<number, string> = {
  400: 'bad_request',
  401: 'unauthorized',
  403: 'forbidden',
  404: 'not_found',
  409: 'conflict',
  421: 'misdirected_request',
  423: 'locked',
  429: 'rate_limited',
  500: 'server_error',
  502: 'bad_gateway',
  503: 'unavailable',
};

/** Coerce any response body into the documented `{error:{code,message,detail}}` shape. */
export function normaliseErrorDetail(status: number, raw: unknown): ErrorDetail {
  if (raw && typeof raw === 'object' && 'error' in raw) {
    const err = (raw as { error: unknown }).error;
    if (err && typeof err === 'object') {
      const e = err as Partial<ErrorDetail>;
      return {
        code: typeof e.code === 'string' ? e.code : (CODE_BY_STATUS[status] ?? 'error'),
        message: typeof e.message === 'string' ? e.message : `HTTP ${status}`,
        detail: (e.detail ?? null) as Record<string, unknown> | null,
      };
    }
  }
  if (typeof raw === 'string' && raw.trim() !== '') {
    return { code: CODE_BY_STATUS[status] ?? 'error', message: raw.slice(0, 500), detail: null };
  }
  return {
    code: CODE_BY_STATUS[status] ?? 'error',
    message: `HTTP ${status}`,
    detail: raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : null,
  };
}

/** True for anything the UI should surface as "the console is unreachable". */
export function isNetworkError(err: unknown): boolean {
  return err instanceof TypeError || (err instanceof Error && err.name === 'AbortError');
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

/**
 * Per-field messages out of a 422 body, as `"field: message"` lines.
 *
 * `console/app.py` turns a `RequestValidationError` into
 * `{code:'invalid', message:'request body failed validation', detail:{errors:[{loc,msg}]}}`.
 * `ApiError.detail` carried that and nothing read it, so a rejected field — a two-character
 * KILL reason, a seed below the minimum — surfaced as one unhelpful sentence that named
 * neither the field nor the rule. Anything that renders `errorMessage` should render these
 * beside it.
 */
export function errorFields(err: unknown): string[] {
  if (!(err instanceof ApiError)) return [];
  const detail = err.detail;
  if (!detail || typeof detail !== 'object') return [];
  const errors = (detail as { errors?: unknown }).errors;
  if (!Array.isArray(errors)) return [];
  const out: string[] = [];
  for (const entry of errors) {
    if (!entry || typeof entry !== 'object') continue;
    const { loc, msg } = entry as { loc?: unknown; msg?: unknown };
    if (typeof msg !== 'string' || msg === '') continue;
    const path = Array.isArray(loc)
      ? loc.filter((part) => part !== 'body').map(String).join('.')
      : '';
    out.push(path ? `${path}: ${msg}` : msg);
  }
  return out;
}
