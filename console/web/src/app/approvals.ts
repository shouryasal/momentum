import type { ApprovalItem, ApprovalsPending } from '../api/contracts';

/**
 * How many proposals are actually waiting on the operator.
 *
 * `GET /api/approvals/pending` returns every proposal whose approval status is pending
 * *or* approved-not-yet-applied, and it keeps a row after its TTL has run out so the page
 * can show that it expired.  The header badge is a call to action, so it counts only rows
 * that a human can still act on: undecided, and not yet expired.  When neither sleeve
 * requires approval the queue is informational and the badge stays at zero.
 */
export function isAwaitingHuman(item: ApprovalItem): boolean {
  return !item.decision && item.seconds_left > 0;
}

export function pendingApprovalCount(data: ApprovalsPending | null | undefined): number {
  if (!data || !data.requires_approval) return 0;
  return (data.items ?? []).filter(isAwaitingHuman).length;
}
