/** Public API surface for feature packages. */
export { ApiClient, api, API_BASE, CSRF_HEADER, type RequestOptions } from './client';
export { ApiError, errorMessage, isNetworkError, normaliseErrorDetail } from './errors';
export { createQueryClient, shellKeys } from './queryClient';
export { endpoints, type Endpoints } from './endpoints';
/**
 * `useEventStream` is deliberately NOT re-exported: the shell owns the one connection and
 * pages subscribe to it with `useTopicEvents` from `@/app/EventStreamContext`.
 */
export {
  EventStream,
  backoffDelay,
  DEFAULT_BACKOFF,
  type BackoffOptions,
  type EventSourceFactory,
  type EventSourceLike,
  type StreamStatus,
} from './sse';
export * from './contracts';
