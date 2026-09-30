"""MEASURE 1, part 4 — what the coin did AFTER we sold it, and what we left on the table.

Not a claim about edge: 9 exits cannot establish that exits are early or late. It is the
arithmetic of this sample, which is what the owner is asking to see. For every closed
strategy trade: the exit price, the best 1h close in the 24 hours after the exit, and the
price now, so "we sold at +0.9% and it went to +17%" is a number rather than an impression.

Read-only. PYTHONPATH=. python evals/research/profit-audit/m1_after_exit.py --root /tmp/pa1
"""

from __future__ import annotations

import argparse
import statistics
from datetime import timedelta
from pathlib import Path

from m1_profit import candles, heartbeats, load_trades, price_at


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/pa1")
    args = ap.parse_args()
    root = Path(args.root)
    px = candles(root)
    now = max(heartbeats(root))
    tr = [t for t in load_trades(root) if not t["is_open"] and not t["one_off"]]

    # one row per independent event (both bots run identical rules)
    seen: set[tuple] = set()
    rows = []
    for t in sorted(tr, key=lambda r: r["open"]):
        key = (t["pair"], t["open"].strftime("%Y-%m-%d %H"))
        if key in seen:
            continue
        seen.add(key)
        rows.append(t)

    print(
        f"{'pair':10} {'exit (UTC)':17} {'reason':19} {'our ret':>8} "
        f"{'exit px':>10} {'best +24h':>10} {'+24h':>8} {'now':>10} {'to now':>8} {'left':>8}"
    )
    print("-" * 122)
    left_24 = []
    left_now = []
    for t in rows:
        ser = px.get(t["pair"])
        if not ser:
            continue
        ex = t["close"]
        exit_px = t["close_rate"]
        horizon = ex + timedelta(hours=24)
        after = [c for ot, c in ser if ex.timestamp() * 1000 < ot <= horizon.timestamp() * 1000]
        best24 = max(after) if after else None
        pnow = price_at(ser, now)
        ours = t["gross"] / t["buy_notional"]
        r24 = (best24 / exit_px - 1) if best24 else None
        rnow = (pnow / exit_px - 1) if pnow else None
        if r24 is not None:
            left_24.append(r24)
        if rnow is not None:
            left_now.append(rnow)
        print(
            f"{t['pair']:10} {ex.strftime('%Y-%m-%d %H:%M'):17} {t['exit_reason'][:19]:19} "
            f"{ours * 100:+7.2f}% {exit_px:10.4f} "
            f"{(best24 if best24 else 0):10.4f} {(r24 * 100 if r24 is not None else 0):+7.2f}% "
            f"{(pnow if pnow else 0):10.4f} {(rnow * 100 if rnow is not None else 0):+7.2f}% "
            f"{((r24 - ours) * 100 if r24 is not None else 0):+7.2f}%"
        )
    print("-" * 122)
    print(
        f"  median best move in the 24 h AFTER our exit : {statistics.median(left_24) * 100:+.2f}%"
        f"   (mean {statistics.fmean(left_24) * 100:+.2f}%)"
    )
    print(
        f"  median move from our exit price to NOW      : {statistics.median(left_now) * 100:+.2f}%"
        f"   (mean {statistics.fmean(left_now) * 100:+.2f}%)"
    )
    up24 = sum(1 for r in left_24 if r > 0)
    upnow = sum(1 for r in left_now if r > 0)
    print(
        f"  it kept rising after we sold in {up24} of {len(left_24)} cases within 24 h, "
        f"and is higher than our exit today in {upnow} of {len(left_now)}."
    )
    print(
        "\n  Read this as arithmetic on 9 exits, not as evidence that the exits are early:"
        "\n  a coin's next 24 hours is roughly a coin flip in this universe, and"
        "\n  growth-audit.md measured 77.5% of +100% run-ups given back within 60 days."
    )


if __name__ == "__main__":
    main()
