import { describe, expect, it, vi } from 'vitest';

import { ApiClient, CSRF_HEADER } from '../api/client';
import { ApiError, errorMessage, normaliseErrorDetail } from '../api/errors';
import { endpoints } from '../api/endpoints';

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

describe('ApiClient', () => {
  it('sends the session cookie and no CSRF header on GET', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ ok: true }));
    const client = new ApiClient({ fetchImpl: fetchImpl as unknown as typeof fetch });
    client.setCsrf('csrf-token');

    await client.get('/health');

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/health');
    expect(init.credentials).toBe('same-origin');
    const headers = init.headers as Headers;
    expect(headers.has(CSRF_HEADER)).toBe(false);
    expect(init.body).toBeUndefined();
  });

  it('adds CSRF and JSON content type to every non-GET', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ engaged: true, results: [] }));
    const client = new ApiClient({ fetchImpl: fetchImpl as unknown as typeof fetch });
    client.setCsrf('csrf-token');

    await endpoints(client).kill({ reason: 'manual stop', flatten: false });

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/kill');
    expect(init.method).toBe('POST');
    const headers = init.headers as Headers;
    expect(headers.get(CSRF_HEADER)).toBe('csrf-token');
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(JSON.parse(String(init.body))).toEqual({ reason: 'manual stop', flatten: false });
  });

  it('builds query strings and drops null/undefined values', async () => {
    const fetchImpl = vi.fn(async () => jsonResponse({ query: 'nav', hits: [] }));
    const client = new ApiClient({ fetchImpl: fetchImpl as unknown as typeof fetch });

    await client.get('/search', { q: 'nav', limit: 10, missing: null, gone: undefined });

    const [url] = fetchImpl.mock.calls[0] as unknown as [string];
    expect(url).toBe('/api/search?q=nav&limit=10');
  });

  it('normalises the documented error envelope into ApiError', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ error: { code: 'locked', message: 'ops lock held', detail: { by: 'apply' } } }, 423),
    );
    const client = new ApiClient({ fetchImpl: fetchImpl as unknown as typeof fetch });

    const error = await client.post('/config/effects/apply').catch((err: unknown) => err);

    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.status).toBe(423);
    expect(apiError.code).toBe('locked');
    expect(apiError.isLocked).toBe(true);
    expect(apiError.detail).toEqual({ by: 'apply' });
    expect(errorMessage(apiError)).toBe('ops lock held');
  });

  it('normalises a non-JSON error body', async () => {
    const fetchImpl = vi.fn(
      async () => new Response('upstream exploded', { status: 500, headers: { 'content-type': 'text/plain' } }),
    );
    const client = new ApiClient({ fetchImpl: fetchImpl as unknown as typeof fetch });

    const error = (await client.get('/meta').catch((err: unknown) => err)) as ApiError;

    expect(error.status).toBe(500);
    expect(error.code).toBe('server_error');
    expect(error.message).toBe('upstream exploded');
  });

  it('calls the unauthorized handler on 401 unless the caller opts out', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ error: { code: 'unauthorized', message: 'no session', detail: null } }, 401),
    );
    const client = new ApiClient({ fetchImpl: fetchImpl as unknown as typeof fetch });
    const onUnauthorized = vi.fn();
    client.setUnauthorizedHandler(onUnauthorized);

    await client.get('/meta').catch(() => undefined);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);

    await endpoints(client)
      .login('bad-token')
      .catch(() => undefined);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it('flags step-up refusals', () => {
    const error = new ApiError(403, normaliseErrorDetail(403, { error: { code: 'step_up_required', message: 'step up', detail: null } }));
    expect(error.needsStepUp).toBe(true);
    expect(new ApiError(409, normaliseErrorDetail(409, null)).isConflict).toBe(true);
    expect(new ApiError(421, normaliseErrorDetail(421, null)).isWrongHost).toBe(true);
  });

  it('returns null for 204 responses', async () => {
    const fetchImpl = vi.fn(async () => new Response(null, { status: 204 }));
    const client = new ApiClient({ fetchImpl: fetchImpl as unknown as typeof fetch });
    await expect(client.del('/flags/blackout')).resolves.toBeNull();
  });
});
