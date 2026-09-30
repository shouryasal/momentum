"""MEASURE 1, part 2 — the cross-checks and the statistics the answer has to survive.

(1) freqtrade orders vs journal `fills` vs `tca_fill_costs` — do the three records agree?
(2) the NAV path from `nav_points`, its peak-to-trough, and whether it agrees with the pot.
(3) per-trade economics against the 0.30% cost floor, the way exit-and-horizon-2026-09-29.md
    §"the 1h rule earns +0.022% per trade against the 0.300% it costs" states it.
(4) how fragile the positive number is: leave-one-out, and the sample size honestly needed.

Read-only. Run:  PYTHONPATH=. python evals/research/profit-audit/m1_crosscheck.py --root /tmp/pa1
"""

from __future__ import annotations

import argparse
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from m1_profit import (  # noqa: E402  (same directory)
    BOOKS,
    COST_FLOOR_SIDE,
    FEE_RATE_DRYRUN,
    awake_seconds,
    awake_windows,
    heartbeats,
    load_trades,
    parse_ft,
    ro,
    z,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/pa1")
    args = ap.parse_args()
    root = Path(args.root)
    tr = load_trades(root)
    closed = [t for t in tr if not t["is_open"]]
    strat = [t for t in closed if not t["one_off"]]
    beats = heartbeats(root)
    spans = awake_windows(beats)
    now = max(beats)

    # ---------------------------------------------------------------- (1)
    print("### CROSS-CHECK 1 — do the three records of the same fills agree?\n")
    ft_n = 0
    ft_fee = 0.0
    ft_notional = 0.0
    for _sleeve, _era, rel in BOOKS:
        db = root / rel
        if not db.exists():
            continue
        con = ro(db)
        try:
            rows = con.execute(
                "SELECT cost FROM orders WHERE status='closed' AND filled > 0"
            ).fetchall()
        finally:
            con.close()
        ft_n += len(rows)
        ft_notional += sum(r["cost"] or 0.0 for r in rows)
        ft_fee += sum((r["cost"] or 0.0) * FEE_RATE_DRYRUN for r in rows)

    con = ro(root / "journal/journal.db")
    try:
        jf = con.execute(
            "SELECT COUNT(*) n, SUM(fill_amount*fill_price) notional, SUM(fee_amount) fee"
            " FROM fills"
        ).fetchone()
        tf = con.execute(
            "SELECT COUNT(*) n, SUM(notional_usdt) notional, AVG(fee_bps) fee_bps,"
            " AVG(slippage_bps) slip FROM tca_fill_costs"
        ).fetchone()
        navs = con.execute(
            "SELECT ts_utc, sleeve, nav_usdt, realized_pnl, unrealized_pnl, open_trades"
            " FROM nav_points ORDER BY ts_utc"
        ).fetchall()
        roll = con.execute(
            "SELECT day, sleeve, window, n_fills, fee_bps_med, slip_bps_med, total_bps_med"
            " FROM tca_rolling WHERE day=(SELECT MAX(day) FROM tca_rolling)"
            " AND window='30d' ORDER BY sleeve"
        ).fetchall()
    finally:
        con.close()

    print(f"  freqtrade filled order legs   : {ft_n:3d}   notional {ft_notional:,.2f}   fees {ft_fee:,.2f}")
    print(f"  journal `fills` rows          : {jf['n']:3d}   notional {jf['notional']:,.2f}   fees {jf['fee']:,.2f}")
    print(f"  journal `tca_fill_costs` rows : {tf['n']:3d}   notional {tf['notional']:,.2f}   "
          f"fee {tf['fee_bps']:.1f} bps, slippage {tf['slip']:+.1f} bps mean")
    ok = ft_n == jf["n"] == tf["n"] and abs(ft_fee - (jf["fee"] or 0)) < 0.01
    print(f"  → {'AGREE on every leg and every fee' if ok else 'DISAGREE — investigate'}")
    print("\n  30-day TCA medians as they stand today (this is what the monthly cost")
    print("  calibration would write into the backtest cost model):")
    for r in roll:
        print(
            f"    sleeve {r['sleeve']}: {r['n_fills']} fills, fee {r['fee_bps_med']:.1f} bps, "
            f"slippage median {r['slip_bps_med']:+.2f} bps, total {r['total_bps_med']:.2f} bps"
        )
    print(
        f"    the repo's standing assumption is {COST_FLOOR_SIDE * 1e4:.1f} bps of slippage per side."
    )

    # ---------------------------------------------------------------- (2)
    print("\n\n### CROSS-CHECK 2 — the NAV path the 15-minute ledger recorded\n")
    per_sleeve: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for r in navs:
        per_sleeve[r["sleeve"]].append((parse_ft(r["ts_utc"]), float(r["nav_usdt"])))
    for s in sorted(per_sleeve):
        pts = per_sleeve[s]
        peak = -1e18
        mdd = 0.0
        mdd_at = None
        for t, v in pts:
            peak = max(peak, v)
            dd = v / peak - 1.0
            if dd < mdd:
                mdd, mdd_at = dd, t
        print(
            f"  sleeve {s}: {len(pts)} ticks {z(pts[0][0])} → {z(pts[-1][0])}; "
            f"first {pts[0][1]:,.2f}  last {pts[-1][1]:,.2f}  "
            f"min {min(v for _, v in pts):,.2f}  max {max(v for _, v in pts):,.2f}"
        )
        print(
            f"           worst peak-to-trough inside the ledger: {mdd * 100:+.3f}%"
            + (f" at {z(mdd_at)}" if mdd_at else "")
        )
    resets = 0
    for s in sorted(per_sleeve):
        pts = per_sleeve[s]
        for (_t0, v0), (t1, v1) in zip(pts, pts[1:], strict=False):
            if abs(v1 - 10_000.0) < 1e-6 and abs(v0 - 10_000.0) > 1e-6:
                resets += 1
                print(f"  ledger RESET to the seed: sleeve {s} at {z(t1)} (was {v0:,.2f})")
    print(f"  resets found: {resets}")

    # ---------------------------------------------------------------- (3)
    print("\n\n### CROSS-CHECK 3 — per-trade economics against the 0.30% cost floor\n")
    for label, rows in (("strategy only", strat), ("all closed", closed)):
        gross_ret = [t["gross"] / t["buy_notional"] for t in rows]
        mg = statistics.fmean(gross_ret)
        print(f"  {label}: n={len(rows)}")
        print(f"    mean GROSS return per trade (on money put in) : {mg * 100:+.4f}%")
        print(f"    what the round trip costs at the repo floor   : {COST_FLOOR_SIDE * 2 * 100:.3f}%")
        print(f"    what the bots actually charged themselves     : {FEE_RATE_DRYRUN * 2 * 100:.3f}%")
        print(f"    edge after the repo floor                     : {(mg - COST_FLOOR_SIDE * 2) * 100:+.4f}%")
        print(
            "    for reference, exit-and-horizon-2026-09-29.md measured the 1h rule at"
            " +0.022% gross per trade against the 0.300% it costs."
        )

    # ---------------------------------------------------------------- (4)
    print("\n\n### CROSS-CHECK 4 — how fragile is the positive number?\n")
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for t in strat:
        groups[(t["pair"], t["open"].strftime("%Y-%m-%d %H"))].append(t)
    events = []
    for key, rows in sorted(groups.items()):
        events.append(
            (
                key,
                statistics.fmean(r["net_at_cost_floor"] / r["buy_notional"] for r in rows),
                statistics.fmean(r["net_at_cost_floor"] for r in rows),
            )
        )
    xs = [e[1] for e in events]
    usd = [e[2] for e in events]
    print(f"  {len(events)} independent events (both bots run identical rules, so one entry = one event):")
    for (pair, hr), r, u in events:
        print(f"    {pair:10} {hr}  {r * 100:+7.3f}%  {u:+8.2f} USDT (at the 0.30% floor)")
    tot = sum(usd)
    print(f"\n  total at the 0.30% cost floor, per bot: {tot:+.2f} USDT; both bots: {tot * 2:+.2f}")
    biggest = max(events, key=lambda e: e[2])
    print(
        f"  the single largest contributor is {biggest[0][0]} on {biggest[0][1]}: "
        f"{biggest[2]:+.2f} USDT = {biggest[2] / tot * 100:.0f}% of the total."
    )
    print("\n  LEAVE-ONE-OUT: drop each event in turn and re-total.")
    for (pair, hr), _, u in events:
        rest = tot - u
        print(f"    without {pair:10} {hr}: {rest:+8.2f} USDT  {'(still positive)' if rest > 0 else '(NEGATIVE)'}")

    n = len(xs)
    m = statistics.fmean(xs)
    sd = statistics.stdev(xs)
    se = sd / math.sqrt(n)
    print(
        f"\n  mean per-event return at the 0.30% floor: {m * 100:+.4f}% "
        f"(sd {sd * 100:.4f}%, se {se * 100:.4f}%, t={m / se:+.2f})"
    )
    print(
        f"  95% interval: {(m - 1.96 * se) * 100:+.4f}% .. {(m + 1.96 * se) * 100:+.4f}% "
        f"— {'includes zero' if (m - 1.96 * se) < 0 < (m + 1.96 * se) else 'excludes zero'}"
    )
    tot_aw = awake_seconds(spans, min(t["open"] for t in tr), now) / 3600.0
    rate = n / tot_aw
    print(f"\n  observed rate: {n} independent events in {tot_aw:.1f} awake hours = {rate:.3f}/h")
    print("  trades needed for a 95% interval that excludes zero, for a range of TRUE means")
    print("  (the point estimate above is one of them, and it is the most optimistic):")
    for assumed in (m, m / 2, 0.0020, 0.0010, 0.00022):
        if assumed <= 0:
            continue
        need = math.ceil((1.96 * sd / assumed) ** 2)
        hrs = need / rate
        print(
            f"    true mean {assumed * 100:+.4f}% → {need:>7,} trades = {hrs:>9,.0f} awake hours "
            f"= {hrs / 24 / 0.31:>8,.0f} days at the observed 31% uptime "
            f"({hrs / 24 / 0.31 / 365:.1f} years), or {hrs / 24:>7,.0f} days at 100%"
        )
    print(
        "\n  For scale, dip-strategy.md §10 puts statistical separation of the shipped book"
        "\n  from holding BTC at 86 years of daily returns. Nothing measured in six days"
        "\n  changes that arithmetic."
    )


if __name__ == "__main__":
    main()
