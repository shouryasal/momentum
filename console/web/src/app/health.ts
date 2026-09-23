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
  const limit = snapshot.staleness_limit_min;
  const freshness: CheckStatus =
    age === null || age === undefined ? 'unknown' : age > limit ? 'fail' : age > limit * 0.75 ? 'warn' : 'ok';

  const incidents: CheckStatus = snapshot.open_incidents > 0 ? 'warn' : 'ok';
  const alerts: CheckStatus =
    snapshot.undelivered_alerts === null || snapshot.undelivered_alerts === undefined
      ? 'unknown'
      : snapshot.undelivered_alerts > 0
        ? 'warn'
        : 'ok';

  const stale = Object.entries(snapshot.freshness_minutes)
    .filter(([, minutes]) => minutes === null || minutes > limit)
    .map(([name]) => name);

  const checks = [
    {
      key: 'data_freshness',
      label: 'Data freshness',
      status: freshness,
      detail:
        age === null || age === undefined
          ? 'no candle age reported'
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
      detail: `${snapshot.open_incidents} open`,
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
