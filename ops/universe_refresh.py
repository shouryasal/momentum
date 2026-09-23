"""The weekly universe refresh: resolve, diff, guard, write, flag, top up.

Runs Sunday 18:00 Gulf, ahead of the candle top-up in ``ops/refresh_backtest_data.sh``
(which invokes it), because a new name needs a whitelist entry before it can get
history. Weekly matches the measured churn — the eligible set turns over a median
11.0% of names per week and the top 50 by ADV 6.0%, so a daily refresh would re-churn
1-2% of names for no informational gain while making the point-in-time record harder
to keep, and a monthly one would leave the tail ~40% stale.

The job is deliberately conservative in three places.

**It refuses to shrink.** A refresh that would cut the tradeable tier by more than
``universe.refresh.max_tradeable_shrink`` writes nothing and exits non-zero. Binance
returning a short or partial ``exchangeInfo`` must not flatten the book, and the
failure mode of "trade fewer things" looks harmless enough to go unnoticed for a
week.

**It never removes a held position by removing a pair.** Falling out of the universe
is a signal to exit in an orderly way, not an exit. A pair under a delisting notice
keeps its snapshot entry, gets ``exit_only: true`` and a cap of zero, and raises a
per-pair ``block_entries`` flag through ``ops.lib.flags`` — the same mechanism the
gate already reads. The exit ladder itself lives in the gate (package U2), because
the gate is the authority on orders.

**It fetches nothing it cannot verify.** Binance's delisting schedule is a ``sapi``
endpoint that needs an API key, and ``ops/envwrap.sh`` gives Binance keys to
``reconcile`` and ``preflight`` only. Notices therefore arrive as data in
``knowledge/universe/delistings.json`` (written by ``reg-watch`` or by a key-holding
job), never by this job going looking for a credential it should not have.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ops import universe as U
from ops.config import EarnConfig, load_config, resolver_args
from ops.lib import flags, locks
from ops.lib.paths import REPO_ROOT

#: Where ``reg-watch`` (and any key-holding job) drops delisting / halt notices:
#: ``{"<SYMBOL or PAIR>": "<ISO 8601 UTC>"}``. Absent means "no notices", never an error.
DELISTINGS_FILE = "delistings.json"

#: Flag name prefix for a pair the universe has put into exit-only.
FLAG_PREFIX = "universe_exit_only"

JOB = "universe_refresh"


class RefreshRefused(Exception):
    """A resolve that completed but must not be written without a human."""


def carry_forward(
    old: U.Snapshot | None, now: datetime, weeks: int
) -> dict[str, U.Retained]:
    """What last week's whitelist can trade that this week's resolve might drop.

    Everything the previous snapshot listed as tradeable is offered back to the resolver;
    a name that still qualifies is simply resolved normally and the offer is ignored. A
    name that no longer qualifies — depegged, halted, wrapped equity, or just thinned out
    below the satellite floor — comes back as ``exit_only`` instead of disappearing,
    which is the difference between winding a position down and orphaning it
    (wide-universe.md §1.5).

    The window is bounded because the resolver cannot see positions: after ``weeks``
    refreshes the name drops out for good. The sleeve trims it through the normal
    rebalance band long before then, and a delisting has its own hard deadline.
    """
    if old is None:
        return {}
    today = now.astimezone(UTC).date()
    out: dict[str, U.Retained] = {}
    for pair in old.tradeable_pairs:
        e = old.pairs[pair]
        since = e.exit_only_since or old.date
        if e.exit_only:
            try:
                started = date.fromisoformat(since)
            except ValueError:
                started = today
            if (today - started).days > weeks * 7:
                continue  # the grace window has closed
        out[pair] = U.Retained(tier=e.tier, exit_only_since=since,
                               reason=e.exit_reason)
    return out


def read_delistings(directory: Path) -> dict[str, str]:
    p = directory / DELISTINGS_FILE
    try:
        raw = json.loads(p.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}


def _active(snap: U.Snapshot) -> int:
    """Tradeable pairs that can still be **entered**.

    Exit-only names are excluded deliberately: carry-forward keeps them on the whitelist,
    so counting them would let a Binance hiccup turn the whole book exit-only without the
    shrink guard noticing a thing.
    """
    return sum(1 for p in snap.tradeable_pairs if not snap.pairs[p].exit_only)


def guard_shrink(old: U.Snapshot | None, new: U.Snapshot, max_shrink: float) -> None:
    """Refuse a refresh that collapses the tradeable tier."""
    if old is None:
        return
    before = _active(old)
    after = _active(new)
    if before == 0 or after >= before:
        return
    shrink = (before - after) / before
    if shrink > max_shrink + 1e-9:
        raise RefreshRefused(
            f"enterable tradeable tier would shrink {before} -> {after} ({shrink:.0%}), "
            f"over the {max_shrink:.0%} limit; re-run with --force once a human has "
            "looked at it"
        )


def guard_core(new: U.Snapshot) -> None:
    """A core asset that did not resolve into the core tier is an incident, not a refresh."""
    missing = U.missing_core(new)
    if missing:
        raise RefreshRefused(
            f"core assets {missing} did not resolve into the core tier — refusing to write a "
            "snapshot that would drop them from the whitelist"
        )


def sync_flags(
    new: U.Snapshot,
    flags_file: Path,
    *,
    now: datetime | None = None,
) -> dict[str, list[str]]:
    """Raise a per-pair entry block for every newly exit-only pair; clear the stale ones.

    Uses the existing flag mechanism rather than a parallel one: ``severity
    block_entries`` with ``scope`` set to the pair is exactly what
    ``flags.entries_blocked(path, pair)`` already checks, so the gate needs no new
    reader to honour a delisting.
    """
    raised: list[str] = []
    cleared: list[str] = []
    try:
        active = flags.active_flags(flags_file, now)
    except flags.FlagsError:
        active = {}
    for pair, entry in sorted(new.pairs.items()):
        name = f"{FLAG_PREFIX}:{pair}"
        if entry.exit_only and name not in active:
            flags.set_flag(
                flags_file, name,
                severity="block_entries", scope=pair,
                reason=(
                    f"universe: {pair} is exit_only ({entry.exit_reason or 'unknown'})"
                    + (f", delisting {entry.delisting_at}" if entry.delisting_at else "")
                ),
                set_by=f"system:{JOB}", now=now,
            )
            raised.append(pair)
    live_exit_only = {f"{FLAG_PREFIX}:{p}" for p, e in new.pairs.items() if e.exit_only}
    for name, f in sorted(active.items()):
        if name.startswith(f"{FLAG_PREFIX}:") and name not in live_exit_only:
            if f.get("set_by") == "human":
                continue  # only a human clears a human's flag
            if flags.clear_flag(flags_file, name, by=f"system:{JOB}", now=now):
                cleared.append(name.split(":", 1)[1])
    return {"raised": raised, "cleared": cleared}


def resolve_now(
    cfg: EarnConfig,
    *,
    root: Path | None = None,
    now: datetime | None = None,
    fetch: Any = None,
    old: U.Snapshot | None = None,
) -> tuple[U.Snapshot, dict[str, str]]:
    """Fetch and resolve. ``fetch`` is injectable so tests never touch the network."""
    rules, tiers, score = resolver_args(cfg)
    r = cfg.universe.refresh
    fetcher = fetch or U.fetch_inputs
    res = fetcher(
        quote=cfg.universe.quote,
        history_days=r.history_days,
        max_workers=r.max_workers,
    )
    directory = cfg.universe.snapshot_dir(root)
    at = now or datetime.now(UTC)
    snap = U.resolve(
        res.symbols,
        res.bars,
        at,
        rules=rules,
        tiers=tiers,
        score=score,
        delistings=read_delistings(directory),
        listed_at=getattr(res, "listed_at", None),
        retain=carry_forward(old, at, r.exit_only_weeks),
    )
    return snap, dict(res.errors)


def refresh(
    cfg: EarnConfig | None = None,
    *,
    root: Path | None = None,
    now: datetime | None = None,
    fetch: Any = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Resolve, guard, write, flag. Returns the report the CLI prints."""
    cfg = cfg or load_config()
    base = root or REPO_ROOT
    directory = cfg.universe.snapshot_dir(base)
    old = U.load_current(directory)

    snap, errors = resolve_now(cfg, root=base, now=now, fetch=fetch, old=old)
    guard_core(snap)
    if not force:
        guard_shrink(old, snap, cfg.universe.refresh.max_tradeable_shrink)

    delta = U.diff(old, snap)
    report: dict[str, Any] = {
        "date": snap.date,
        "generated_at": snap.generated_at,
        "sha256": snap.sha256,
        "counts": dict(snap.counts),
        "funnel": [dict(f) for f in snap.funnel],
        "diff": delta.as_dict(),
        "diff_text": delta.describe(),
        "tradeable": list(snap.tradeable_pairs),
        "previous": old.date if old else None,
        "fetch_errors": errors,
        "written": None,
        "flags": {"raised": [], "cleared": []},
        "dry_run": dry_run,
    }
    if dry_run:
        return report

    path = U.write_snapshot(snap, directory)
    U.clear_cache()
    report["written"] = str(path.relative_to(base)) if path.is_relative_to(base) else str(path)
    report["flags"] = sync_flags(snap, base / cfg.paths.flags_file, now=now)
    return report


def pair_list(cfg: EarnConfig, which: str = "tradeable") -> list[str]:
    """The pair list the shell scripts and the config generator ask for.

    ``download`` is watchlist + ``data_only_symbols``: candles are cheap (0.219 MB per
    pair-year at 1h) and a watchlist name with no history is a name the scanner cannot
    see, so the store is filled for everything we look at, not only what we trade.
    """
    if which == "tradeable":
        return list(cfg.universe.pairs)
    watch = list(cfg.universe.watchlist_pairs)
    if which == "watchlist":
        return watch
    if which == "download":
        seen = dict.fromkeys([*watch, *cfg.universe.data_only_symbols])
        return list(seen)
    raise ValueError(f"unknown pair list {which!r}")


def _describe(report: dict[str, Any]) -> str:
    c = report["counts"]
    lines = [
        f"universe {report['date']}  sha {report['sha256'][:12]}"
        + (f"  (previous {report['previous']})" if report["previous"] else "  (first refresh)"),
        f"  watchlist {c['watchlist']}  tradeable {c['tradeable']} "
        f"= core {c['core']} + major {c['major']} + satellite {c['satellite']}"
        f"  | watch-only {c['watchlist_only']}  excluded {c['excluded']}",
    ]
    for step in report["funnel"]:
        if step["removed"]:
            lines.append(
                f"    {step['step']:>2}. {step['filter']:<17} -{step['removed']:<4} "
                f"-> {step['remaining']}"
            )
    lines.append(f"  changes: {report['diff_text']}")
    if report["fetch_errors"]:
        lines.append(f"  fetch errors: {len(report['fetch_errors'])} symbol(s)")
    if report["flags"]["raised"]:
        lines.append("  flags raised: " + ", ".join(report["flags"]["raised"]))
    if report["flags"]["cleared"]:
        lines.append("  flags cleared: " + ", ".join(report["flags"]["cleared"]))
    lines.append(
        f"  wrote {report['written']}" if report["written"] else "  DRY RUN, nothing written"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Refresh the point-in-time universe snapshot.")
    ap.add_argument("--dry-run", action="store_true", help="resolve and report, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="write even if the tradeable tier shrinks past the limit")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    ap.add_argument(
        "--pairs", nargs="?", const="tradeable",
        choices=["tradeable", "watchlist", "download"],
        help="print a pair list, one per line, and exit. tradeable = the freqtrade "
             "whitelist; watchlist = everything we carry features for; download = the "
             "watchlist plus universe.data_only_symbols (what the candle store needs).",
    )
    args = ap.parse_args(argv)

    cfg = load_config()

    if args.pairs:
        print("\n".join(pair_list(cfg, args.pairs)))
        return 0

    try:
        with locks.acquire(JOB):
            report = refresh(cfg, force=args.force, dry_run=args.dry_run)
    except locks.LockBusy:
        print(f"{JOB}: another refresh is running", file=sys.stderr)
        return 0
    except RefreshRefused as e:
        print(f"{JOB} REFUSED: {e}", file=sys.stderr)
        return 2
    except U.UniverseError as e:
        print(f"{JOB} FAILED: {e}", file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2, sort_keys=True) if args.json else _describe(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
