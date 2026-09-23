import { ApiError, normaliseErrorDetail } from './errors';

export const API_BASE = '/api';
export const CSRF_HEADER = 'X-Earn-CSRF';

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';

export interface RequestOptions {
  method?: HttpMethod;
  /** JSON body; serialised with `JSON.stringify`. */
  body?: unknown;
  /** Appended as a query string; `undefined`/`null` values are dropped. */
  query?: Record<string, string | number | boolean | null | undefined>;
  signal?: AbortSignal;
  /** Skip the global 401 handler (used by the login form itself). */
  skipAuthRedirect?: boolean;
}

type UnauthorizedHandler = () => void;

/**
 * Typed fetch client for the console API.
 *
 * - always sends the session cookie (`credentials: 'same-origin'`),
 * - adds `X-Earn-CSRF` and `Content-Type: application/json` to every non-GET (spec 5.4),
 * - normalises errors to {@link ApiError},
 * - reports 401 to a single handler so the shell can show the login screen.
 */
export class ApiClient {
  private csrf: string | null = null;
  private onUnauthorized: UnauthorizedHandler | null = null;
  private readonly fetchImpl: typeof fetch;
  private readonly base: string;

  constructor(options: { fetchImpl?: typeof fetch; base?: string } = {}) {
    this.fetchImpl = options.fetchImpl ?? ((...args) => globalThis.fetch(...args));
    this.base = options.base ?? API_BASE;
  }

  setCsrf(token: string | null): void {
    this.csrf = token;
  }

  getCsrf(): string | null {
    return this.csrf;
  }

  setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
    this.onUnauthorized = handler;
  }

  url(path: string, query?: RequestOptions['query']): string {
    const joined = path.startsWith('/') ? `${this.base}${path}` : `${this.base}/${path}`;
    if (!query) return joined;
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null) continue;
      params.set(key, String(value));
    }
    const qs = params.toString();
    return qs ? `${joined}?${qs}` : joined;
  }

  async request<T>(path: string, options: RequestOptions = {}): Promise<T> {
    const method = options.method ?? 'GET';
    const headers = new Headers({ Accept: 'application/json' });
    const init: RequestInit = {
      method,
      credentials: 'same-origin',
      headers,
      ...(options.signal ? { signal: options.signal } : {}),
    };

    if (method !== 'GET') {
      headers.set('Content-Type', 'application/json');
      if (this.csrf) headers.set(CSRF_HEADER, this.csrf);
      init.body = JSON.stringify(options.body ?? {});
    }

    const response = await this.fetchImpl(this.url(path, options.query), init);
    const payload = await readBody(response);

    if (!response.ok) {
      const error = new ApiError(response.status, normaliseErrorDetail(response.status, payload));
      if (error.isUnauthorized && !options.skipAuthRedirect) this.onUnauthorized?.();
      throw error;
    }
    return payload as T;
  }

  get<T>(path: string, query?: RequestOptions['query'], signal?: AbortSignal): Promise<T> {
    return this.request<T>(path, {
      method: 'GET',
      ...(query ? { query } : {}),
      ...(signal ? { signal } : {}),
    });
  }

  post<T>(path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'POST', body });
  }

  put<T>(path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'PUT', body });
  }

  del<T>(path: string, body?: unknown, options: RequestOptions = {}): Promise<T> {
    return this.request<T>(path, { ...options, method: 'DELETE', body });
  }
}

async function readBody(response: Response): Promise<unknown> {
  if (response.status === 204) return null;
  const contentType = response.headers.get('content-type') ?? '';
  const text = await response.text();
  if (text === '') return null;
  if (contentType.includes('json')) {
    try {
      return JSON.parse(text) as unknown;
    } catch {
      return text;
    }
  }
  return text;
}

/** The shell's singleton; tests build their own {@link ApiClient}. */
export const api = new ApiClient();
