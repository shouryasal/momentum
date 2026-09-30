"""MEASURE 1, part 3 — the benchmark pinned to prices that actually existed.

The automated ledger and my first pass disagreed by ~0.3pp on "what holding BTC did",
because one takes the close of the bar whose open_time <= t (a price ~23 min AFTER the
entry) and the other the close of the last bar to have FINISHED before t (~37 min before).
Neither is wrong; the entry happened mid-bar. This script reports all three conventions and
the one price we know for certain: the quote the bot was actually filled at.

Read-only. PYTHONPATH=. python evals/research/profit-audit/m1_bench_exact.py --root /tmp/pa1
"""

from __future__ import annotations

import argparse
import statistics
from datetime import UTC, datetime
from pathlib import Path

from m1_profit import COST_FLOOR_SIDE, awake_seconds, awake_windows, candles, heartbeats, z

SEED = 20_000.0
START = datetime(2026, 9, 23, 13, 37, 36, tzinfo=UTC)  # first fill ever
FILL_BTC = 85_748.84  # the ask the first BTC fill was struck at, journal fills id 2/3
FILL_ETH = 2_718.72  # the ask the first ETH fill was struck at, journal fills id 1


def bar_close_at_or_before_open(series, dt):
    """Close of the bar whose OPEN is at or before dt — the ledger's convention (a price
    up to an hour AFTER dt)."""
    ms = int(dt.timestamp() * 1000)
    best = None
    for ot, cl in series:
        if ot <= ms:
            best = cl
        else:
            break
    return best


def bar_close_finished_before(series, dt):
    """Close of the last bar to have FINISHED at or before dt (a price up to an hour
    BEFORE dt)."""
    ms = int(dt.timestamp() * 1000)
    best = None
    for ot, cl in series:
        if ot + 3_600_000 <= ms:
            best = cl
        else:
            break
    return best


def last_closed(series, dt):
    ms = int(dt.timestamp() * 1000)
    best = None
    for ot, cl in series:
        if ot + 3_600_000 <= ms:
            best = cl
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/pa1")
    args = ap.parse_args()
    root = Path(args.root)
    px = candles(root)
    beats = heartbeats(root)
    spans = awake_windows(beats)
    now = max(beats)
    basket = sorted(p for p in px if p != "BNB/USDT")
    rt = (1 - COST_FLOOR_SIDE) ** 2

    print(f"benchmark window: {z(START)} → {z(now)}  ({(now - START).total_seconds() / 3600:.1f} h)")
    print(f"awake inside it : {awake_seconds(spans, START, now) / 3600:.1f} h")
    end_btc = last_closed(px["BTC/USDT"], now)
    end_eth = last_closed(px["ETH/USDT"], now)
    print(f"\nlast CLOSED 1h bar used as the end price: BTC {end_btc:,.2f}  ETH {end_eth:,.2f}\n")

    print("BTC buy-and-hold, three entry conventions")
    print(f"{'convention':52} {'entry':>12} {'price-only':>11} {'costed 15bps/side':>19} {'on 20,000':>12}")
    rows = [
        ("the price the bot was actually filled at (journal fills)", FILL_BTC),
        ("close of the bar whose open <= start (the ledger)", bar_close_at_or_before_open(px["BTC/USDT"], START)),
        ("close of the last bar finished before start", bar_close_finished_before(px["BTC/USDT"], START)),
    ]
    for label, p0 in rows:
        raw = end_btc / p0 - 1
        print(f"{label:52} {p0:12,.2f} {raw * 100:10.3f}% {(( 1 + raw) * rt - 1) * 100:18.3f}% "
              f"{SEED * ((1 + raw) * rt):12,.2f}")

    print("\nETH buy-and-hold, filled price")
    raw = end_eth / FILL_ETH - 1
    print(f"  entry {FILL_ETH:,.2f} → {end_eth:,.2f}: {raw * 100:+.3f}% price-only, "
          f"{((1 + raw) * rt - 1) * 100:+.3f}% costed → {SEED * ((1 + raw) * rt):,.2f}")

    print("\nEqual-weight hold of the 31 whitelisted pairs (one entry convention at a time)")
    for name, fn in (("bar open <= start (the ledger)", bar_close_at_or_before_open),
                     ("last bar finished before start", bar_close_finished_before)):
        rs = []
        for p in basket:
            p0 = fn(px[p], START)
            p1 = last_closed(px[p], now)
            if p0 and p1:
                rs.append(p1 / p0 - 1)
        m = statistics.fmean(rs)
        print(f"  {name:34} n={len(rs)}  {m * 100:+.3f}% price-only, "
              f"{((1 + m) * rt - 1) * 100:+.3f}% costed → {SEED * ((1 + m) * rt):,.2f}")

    print("\nBest and worst of the 31 over the window (price-only, ledger convention)")
    each = []
    for p in basket:
        p0 = bar_close_at_or_before_open(px[p], START)
        p1 = last_closed(px[p], now)
        if p0 and p1:
            each.append((p1 / p0 - 1, p))
    each.sort(reverse=True)
    for r, p in each[:5]:
        print(f"  {p:11} {r * 100:+8.2f}%")
    print("  ...")
    for r, p in each[-5:]:
        print(f"  {p:11} {r * 100:+8.2f}%")
    up = sum(1 for r, _ in each if r > 0)
    print(f"  {up} of {len(each)} pairs rose; median {statistics.median(r for r, _ in each) * 100:+.2f}%")

    print("\nHeld ONLY during the hours the bots were awake (flat in USDT while asleep,")
    print("one entry cost, rebalanced at every wake-up):")
    for label, pairs in (("BTC/USDT", ["BTC/USDT"]), (f"equal-weight {len(basket)}", basket)):
        mults = []
        for p in pairs:
            m = 1.0
            for s, e in spans:
                if e <= START:
                    continue
                a = max(s, START)
                p0 = bar_close_at_or_before_open(px[p], a)
                p1 = last_closed(px[p], e) or bar_close_at_or_before_open(px[p], e)
                if p0 and p1:
                    m *= p1 / p0
            mults.append(m)
        m = statistics.fmean(mults) * rt
        print(f"  {label:20} {(m - 1) * 100:+.3f}% costed → {SEED * m:,.2f}")

    print("\nUSDT doing nothing: +0.000% → 20,000.00 (and it would have earned ~0.5pp of CAGR")
    print("per 1% APR if the cash yield in audit-and-research-2026-09-29.md were wired: over")
    print(f"{(now - START).total_seconds() / 86400:.2f} days at 4% APR that is about "
          f"{SEED * 0.04 * (now - START).total_seconds() / 86400 / 365:,.2f} USDT.")


if __name__ == "__main__":
    main()
