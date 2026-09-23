import type { CheckStatus, HealthSnapshot, HealthSummary } from '../api/contracts';

/**
 * Derive the header health dot from `GET /api/ops/health` (P1 owns that payload).
 *
 * The rules are deliberately conservative: stale data fails, an open incident warns,
 * and an absent number is `unknown` rather than `ok`.
 */
export function healthSummaryFrom(snapshot: HealthSnapshot | null | undefined): HealthSummary | null {
  if (!snapshot) return null;

  const age = snapshot.data_age_minutes;
  /**
   * A missing limit is not zero.
   *
   * Zero would make every feed stale and paint the header red on an install that simply
   * has not reported one yet, so an absent limit means the freshness checks read
   * `unknown` instead of inventing a verdict.
   */
  const limit =
    typeof snapshot.staleness_limit_min === 'number' && Number.isFinite(snapshot.staleness_limit_min)
      ? snapshot.staleness_limit_min
      : null;
  const freshness: CheckStatus =
    age === null || age === undefined || limit === null
      ? 'unknown'
      : age > limit
        ? 'fail'
        : age > limit * 0.75
          ? 'warn'
          : 'ok';

  const incidents: CheckStatus = (snapshot.open_incidents ?? 0) > 0 ? 'warn' : 'ok';
  const alerts: CheckStatus =
    snapshot.undelivered_alerts === null || snapshot.undelivered_alerts === undefined
      ? 'unknown'
      : snapshot.undelivered_alerts > 0
        ? 'warn'
        : 'ok';

  /**
   * A health payload that is missing `freshness_minutes` used to take the console down.
   *
   * `Object.entries(undefined)` throws, and this runs inside `AppLayout`'s render — above
   * every route's error boundary — so React unmounted the whole tree: no header, no mode
   * badges, no safety strip and, worst of all, **no kill switch**. From the outside that
   * is a blank page where no click does anything, which is exactly what the owner
   * reported. Nothing the server sends may be trusted to be there.
   */
  const feeds = snapshot.freshness_minutes;
  const stale =
    feeds && typeof feeds === 'object'
      ? Object.entries(feeds)
          .filter(([, minutes]) => minutes === null || (limit !== null && minutes > limit))
          .map(([name]) => name)
      : [];

  const checks = [
    {
      key: 'data_freshness',
      label: 'Data freshness',
      status: freshness,
      detail:
        age === null || age === undefined
          ? 'no candle age reported'
          : limit === null
            ? `${Math.round(age)} min old (no limit reported)`
            : `${Math.round(age)} min old (limit ${limit})`,
    },
    {
      key: 'feeds',
      label: 'Feeds',
      status: (stale.length > 0 ? 'warn' : 'ok') as CheckStatus,
      detail: stale.length > 0 ? `stale: ${stale.join(', ')}` : 'all feeds inside the limit',
    },
    {
      key: 'incidents',
      label: 'Open incidents',
      status: incidents,
      detail: `${snapshot.open_incidents ?? 0} open`,
    },
    {
      key: 'alerts',
      label: 'Undelivered alerts',
      status: alerts,
      detail:
        snapshot.undelivered_alerts === null || snapshot.undelivered_alerts === undefined
          ? 'not reported'
          : `${snapshot.undelivered_alerts} waiting`,
    },
  ];

  const status: CheckStatus = checks.some((check) => check.status === 'fail')
    ? 'fail'
    : checks.some((check) => check.status === 'warn')
      ? 'warn'
      : checks.some((check) => check.status === 'unknown')
        ? 'unknown'
        : 'ok';

  return { status, checks, ts: snapshot.as_of };
}
