"""MEASURE 1 — are we making profit?  Read-only audit of every paper fill ever made.

Reads COPIES of the live databases only (default /tmp/pa1).  Writes nothing anywhere but
stdout.  Run:  python evals/research/profit-audit/m1_profit.py [--root /tmp/pa1]

Sections follow the task: (a) every trade, (b) the pot in three definitions, (c) the
benchmarks over the same windows, (d) per-day / per-awake-hour P&L, (f) what the sample can
and cannot support.  (e) is runs/profit_gaps.py, run separately.
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

FEE_RATE_DRYRUN = 0.001  # 10 bps per side, what the bots actually charged themselves
COST_FLOOR_SIDE = 0.0015  # 15 bps per side = the repo's 0.30% round-trip cost floor
SEED_PER_SLEEVE = 10_000.0

BOOKS = [
    ("a", "legacy-4h", "ft_userdata/a/tradesv3.sqlite"),
    ("b", "legacy-4h", "ft_userdata/b/tradesv3.sqlite"),
    ("a", "fast-1h", "ft_userdata/a/runs/test-a-000.sqlite"),
    ("b", "fast-1h", "ft_userdata/b/runs/test-b-000.sqlite"),
]

# The two 2026-09-23 one-off events the paper-trading review identified, keyed by
# (sleeve, era, trade id).  Everything else is what the strategy did on its own.
ONE_OFFS = {
    ("a", "legacy-4h", 1): "operator: console SELL EVERYTHING 21:58Z",
    ("a", "legacy-4h", 2): "operator: console SELL EVERYTHING 21:58Z",
    ("b", "legacy-4h", 1): "code: double-buy + no-mandate flatten",
    ("b", "legacy-4h", 2): "code: double-buy + no-mandate flatten",
}


def ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def parse_ft(ts: str | None) -> datetime | None:
    if not ts:
        return None
    ts = ts.replace("T", " ").replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(ts[:26], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(ts)


def z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- trades


def load_trades(root: Path) -> list[dict]:
    """One row per trade, with gross/fees/net rebuilt from the FILLED ORDERS."""
    out: list[dict] = []
    for sleeve, era, rel in BOOKS:
        db = root / rel
        if not db.exists():
            continue
        con = ro(db)
        try:
            trades = con.execute("SELECT * FROM trades ORDER BY id").fetchall()
            orders = con.execute(
                "SELECT * FROM orders WHERE status='closed' AND filled > 0 ORDER BY id"
            ).fetchall()
        finally:
            con.close()
        by_trade: dict[int, list[sqlite3.Row]] = defaultdict(list)
        for o in orders:
            by_trade[o["ft_trade_id"]].append(o)
        for t in trades:
            legs = by_trade.get(t["id"], [])
            buy = sum(o["cost"] or 0.0 for o in legs if o["ft_order_side"] == "buy")
            sell = sum(o["cost"] or 0.0 for o in legs if o["ft_order_side"] == "sell")
            turnover = buy + sell
            gross = sell - buy
            fee_dry = turnover * FEE_RATE_DRYRUN
            fee_floor = turnover * COST_FLOOR_SIDE
            fill_ts = [
                parse_ft(o["order_filled_date"] or o["order_date"]) for o in legs
            ]
            out.append(
                {
                    "sleeve": sleeve,
                    "era": era,
                    "id": t["id"],
                    "pair": t["pair"],
                    "strategy": t["strategy"],
                    "is_open": bool(t["is_open"]),
                    "open": parse_ft(t["open_date"]),
                    "close": parse_ft(t["close_date"]),
                    "open_rate": t["open_rate"],
                    "close_rate": t["close_rate"],
                    "buy_notional": buy,
                    "sell_notional": sell,
                    "turnover": turnover,
                    "gross": gross,
                    "fee_dry": fee_dry,
                    "net": gross - fee_dry,
                    "net_at_cost_floor": gross - fee_floor,
                    "ft_net": t["close_profit_abs"] or 0.0,
                    "ret_pct": (t["close_profit"] or 0.0) * 100.0,
                    "exit_reason": t["exit_reason"] or ("(open)" if t["is_open"] else "?"),
                    "n_legs": len(legs),
                    "one_off": ONE_OFFS.get((sleeve, era, t["id"])),
                    "last_fill": max([x for x in fill_ts if x], default=None),
                }
            )
    return out


# ---------------------------------------------------------------- awake windows


HB = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*Bot heartbeat")


def heartbeats(root: Path) -> list[datetime]:
    seen: set[datetime] = set()
    for s in ("a", "b"):
        p = root / f"ft_userdata/{s}/logs/freqtrade.log"
        if not p.exists():
            continue
        with p.open(errors="replace") as fh:
            for line in fh:
                m = HB.match(line)
                if m:
                    seen.add(
                        datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
                            tzinfo=UTC
                        )
                    )
    return sorted(seen)


def awake_windows(beats: list[datetime], gap_min: int = 10) -> list[tuple]:
    """Merged [start, end] spans where at least one bot beat within `gap_min`."""
    if not beats:
        return []
    spans = []
    start = prev = beats[0]
    for b in beats[1:]:
        if (b - prev) > timedelta(minutes=gap_min):
            spans.append((start, prev))
            start = b
        prev = b
    spans.append((start, prev))
    return spans


def awake_seconds(spans, lo: datetime, hi: datetime) -> float:
    tot = 0.0
    for s, e in spans:
        a, b = max(s, lo), min(e, hi)
        if b > a:
            tot += (b - a).total_seconds()
    return tot


# ---------------------------------------------------------------- candles / benchmarks


def candles(root: Path, tf: str = "1h") -> dict[str, list[tuple[int, float]]]:
    con = ro(root / "knowledge/earn.db")
    try:
        rows = con.execute(
            "SELECT pair, open_time, close FROM candles WHERE tf=? ORDER BY pair, open_time",
            (tf,),
        ).fetchall()
    finally:
        con.close()
    out: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for r in rows:
        out[r["pair"]].append((r["open_time"], r["close"]))
    return out


def price_at(series: list[tuple[int, float]], dt: datetime) -> float | None:
    """Close of the last 1h bar that had closed at or before `dt`."""
    ms = int(dt.timestamp() * 1000)
    best = None
    for ot, cl in series:
        if ot + 3_600_000 <= ms:
            best = cl
        else:
            break
    return best


# ---------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/tmp/pa1")
    args = ap.parse_args()
    root = Path(args.root)

    tr = load_trades(root)
    beats = heartbeats(root)
    spans = awake_windows(beats)
    now = max(beats) if beats else datetime.now(UTC)

    px = candles(root)
    data_only = {"BNB/USDT"}
    basket = sorted(p for p in px if p not in data_only)

    closed = [t for t in tr if not t["is_open"]]
    open_tr = [t for t in tr if t["is_open"]]
    strat = [t for t in closed if not t["one_off"]]
    oneoff = [t for t in closed if t["one_off"]]

    print("=" * 96)
    print("MEASURE 1 — ARE WE MAKING PROFIT?   read-only, copies only")
    print(f"as-of (newest bot heartbeat): {z(now)}")
    print(f"books read: {len(BOOKS)}   trades: {len(tr)} ({len(open_tr)} still open)")
    print("=" * 96)

    # ------------------------------------------------ (a)
    print("\n### (a1) EVERY TRADE EVER MADE, both sleeves, both eras\n")
    hdr = (
        f"{'slv':3} {'era':9} {'id':>2} {'pair':10} {'opened (UTC)':19} {'held':>9} "
        f"{'exit_reason':19} {'buy$':>9} {'sell$':>9} {'gross':>8} {'fee':>7} "
        f"{'net':>8} {'ret%':>7}  kind"
    )
    print(hdr)
    print("-" * len(hdr))
    for t in sorted(tr, key=lambda r: (r["open"], r["sleeve"], r["era"])):
        if t["is_open"]:
            held = "OPEN"
        else:
            h = (t["close"] - t["open"]).total_seconds() / 3600.0
            held = f"{h:8.2f}h"
        kind = t["one_off"] or "strategy"
        print(
            f"{t['sleeve']:3} {t['era']:9} {t['id']:2d} {t['pair']:10} "
            f"{t['open'].strftime('%Y-%m-%d %H:%M:%S'):19} {held:>9} "
            f"{t['exit_reason'][:19]:19} {t['buy_notional']:9.2f} {t['sell_notional']:9.2f} "
            f"{t['gross']:8.2f} {t['fee_dry']:7.2f} {t['net']:8.2f} {t['ret_pct']:7.3f}  {kind}"
        )

    # reconciliation against freqtrade's own close_profit_abs
    worst = max((abs(t["net"] - t["ft_net"]) for t in closed), default=0.0)
    print(
        f"\nreconciliation: my net (rebuilt from filled orders) vs freqtrade's "
        f"close_profit_abs — worst absolute gap {worst:.6f} USDT over {len(closed)} closed trades."
    )

    def block(name: str, rows: list[dict]) -> None:
        if not rows:
            print(f"\n{name}: none")
            return
        g = sum(r["gross"] for r in rows)
        f = sum(r["fee_dry"] for r in rows)
        n = sum(r["net"] for r in rows)
        nf = sum(r["net_at_cost_floor"] for r in rows)
        w = sum(1 for r in rows if r["net"] > 0)
        to = sum(r["turnover"] for r in rows)
        print(f"\n{name}")
        print(f"  trades              {len(rows)}")
        print(f"  turnover            {to:,.2f} USDT")
        print(f"  gross P&L           {g:+.2f}")
        print(f"  fees charged (10bps){f:9.2f}   = {abs(f / g * 100) if g else 0:.0f}% of gross")
        print(f"  NET P&L             {n:+.2f}")
        print(f"  net at 0.30% floor  {nf:+.2f}   (what it would be at the repo cost floor)")
        print(f"  win rate            {w}/{len(rows)} = {w / len(rows) * 100:.0f}%")
        print(f"  mean net per trade  {n / len(rows):+.3f}")
        print(f"  mean gross per trade{g / len(rows):+.3f}  = {g / to * 100:+.4f}% of turnover")

    block("(a2) WHAT THE STRATEGY DID ON ITS OWN (closed)", strat)
    block("(a3) THE TWO 2026-09-23 ONE-OFF EVENTS (closed)", oneoff)
    for label in sorted({t["one_off"] for t in oneoff}):
        rows = [t for t in oneoff if t["one_off"] == label]
        print(
            f"    {label}: {len(rows)} trades, net {sum(r['net'] for r in rows):+.2f} "
            f"(price {sum(r['gross'] for r in rows):+.2f}, fees {sum(r['fee_dry'] for r in rows):.2f})"
        )
    block("(a4) ALL CLOSED TRADES TOGETHER", closed)

    print("\n(a5) exit-reason mix (closed trades)")
    for era in ("legacy-4h", "fast-1h"):
        rows = [t for t in closed if t["era"] == era]
        if not rows:
            continue
        print(f"  {era}:")
        cnt = Counter(t["exit_reason"] for t in rows)
        for reason, c in cnt.most_common():
            sub = [t for t in rows if t["exit_reason"] == reason]
            nn = sum(t["net"] for t in sub)
            ww = sum(1 for t in sub if t["net"] > 0)
            print(
                f"    {reason:20} {c:2d} trades  net {nn:+8.2f}  "
                f"won {ww}/{c}  mean {nn / c:+7.2f}"
            )

    print("\n(a6) hold-time distribution (closed trades)")
    for label, rows in (("strategy only", strat), ("all closed", closed)):
        hrs = sorted((t["close"] - t["open"]).total_seconds() / 3600.0 for t in rows)
        if not hrs:
            continue
        print(
            f"  {label:14} n={len(hrs):2d}  min {hrs[0]:.2f}h  p25 {hrs[len(hrs) // 4]:.2f}h  "
            f"median {statistics.median(hrs):.2f}h  p75 {hrs[3 * len(hrs) // 4]:.2f}h  "
            f"max {hrs[-1]:.2f}h  mean {statistics.fmean(hrs):.2f}h"
        )
    buckets = [(0, 0.25, "<15 min"), (0.25, 1, "15-60 min"), (1, 6, "1-6 h"),
               (6, 24, "6-24 h"), (24, 1e9, ">24 h")]
    for lo, hi, name in buckets:
        rows = [t for t in closed
                if lo <= (t["close"] - t["open"]).total_seconds() / 3600.0 < hi]
        if rows:
            print(
                f"    {name:10} {len(rows):2d} trades  net {sum(r['net'] for r in rows):+8.2f}"
            )

    # ------------------------------------------------ (b)
    print("\n\n### (b) THE POT — three definitions, each with its own starting line\n")
    open_mark = 0.0
    for t in open_tr:
        ser = px.get(t["pair"])
        p = price_at(ser, now) if ser else None
        if p is None:
            continue
        amt = t["buy_notional"] / t["open_rate"]
        mark = amt * p * (1 - FEE_RATE_DRYRUN) - t["buy_notional"]
        open_mark += mark
        print(
            f"  open position: {t['sleeve']} {t['pair']} entry {t['open_rate']:.4f} "
            f"mark {p:.4f} → {mark:+.2f} USDT unrealised (net of the exit fee)"
        )

    all_net = sum(t["net"] for t in closed)
    fast_net = sum(t["net"] for t in closed if t["era"] == "fast-1h")
    seed_total = SEED_PER_SLEEVE * 2
    d1 = seed_total + all_net + open_mark
    d2 = seed_total + fast_net + open_mark
    print()
    print(
        f"  D1  CUMULATIVE, everything ever: seed 20,000 + every realised trade in EVERY run\n"
        f"      database ({all_net:+.2f}) + open positions marked to market ({open_mark:+.2f})\n"
        f"      = {d1:,.2f} USDT   ({d1 - seed_total:+.2f}, {(d1 / seed_total - 1) * 100:+.3f}%)"
    )
    print(
        f"  D2  SINCE THE CURRENT RUN BEGAN (2026-09-23 23:37:58Z, fast profile only):\n"
        f"      seed 20,000 + fast-profile realised ({fast_net:+.2f}) + open mark ({open_mark:+.2f})\n"
        f"      = {d2:,.2f} USDT   ({d2 - seed_total:+.2f}, {(d2 / seed_total - 1) * 100:+.3f}%)"
    )
    print(
        f"  D3  THE CONSOLE LEDGER: closed fast-profile trades only, no open mark\n"
        f"      = {seed_total + fast_net:,.2f} USDT   ({fast_net:+.2f}, "
        f"{fast_net / seed_total * 100:+.3f}%)"
    )
    print(
        "\n  Which one answers 'am I making money': D1. It is the only definition whose\n"
        "  starting line is the 20,000 that went in. D2 and D3 start after the first\n"
        "  evening's two losses and therefore cannot show them."
    )

    # ------------------------------------------------ (c)
    print("\n\n### (c) THE BENCHMARK — same calendar span, same awake windows\n")
    first_open = min(t["open"] for t in tr)
    fast_start = min(t["open"] for t in tr if t["era"] == "fast-1h")
    entry_cost = (1 - COST_FLOOR_SIDE) ** 2  # buy now, mark net of an eventual exit

    def hold(pair: str, lo: datetime, hi: datetime) -> float | None:
        ser = px.get(pair)
        if not ser:
            return None
        p0, p1 = price_at(ser, lo), price_at(ser, hi)
        if not p0 or not p1:
            return None
        return (p1 / p0) * entry_cost - 1.0

    for label, lo in (
        ("whole test (first ever fill → now)", first_open),
        ("current run (fast profile → now)", fast_start),
    ):
        span_h = (now - lo).total_seconds() / 3600.0
        aw = awake_seconds(spans, lo, now) / 3600.0
        print(f"  {label}: {z(lo)} → {z(now)}  = {span_h:.1f} h, awake {aw:.1f} h ({aw / span_h * 100:.0f}%)")
        b = hold("BTC/USDT", lo, now)
        rets = [hold(p, lo, now) for p in basket]
        rets = [r for r in rets if r is not None]
        ew = statistics.fmean(rets)
        print(f"     BTC buy-and-hold, costed 15bps/side : {b * 100:+.3f}%  → {seed_total * (1 + b):,.2f} on 20,000")
        print(f"     equal-weight hold of {len(rets)} whitelisted pairs: {ew * 100:+.3f}%  → {seed_total * (1 + ew):,.2f}")
        print(f"     USDT doing nothing                  :  +0.000%  → {seed_total:,.2f}")
        print()
    print(
        f"  The book, same span, cumulative (D1): {(d1 / seed_total - 1) * 100:+.3f}%\n"
        f"  The book, current run (D2):            {(d2 / seed_total - 1) * 100:+.3f}%"
    )

    print("\n  Benchmark restricted to the hours the bots were actually AWAKE")
    print("  (hold only inside each awake span, flat in USDT while asleep; entry cost once):")
    for pair in ("BTC/USDT",):
        ser = px[pair]
        mult = 1.0
        for s, e in spans:
            if e <= first_open:
                continue
            s = max(s, first_open)
            p0, p1 = price_at(ser, s), price_at(ser, e)
            if p0 and p1:
                mult *= p1 / p0
        mult *= entry_cost
        print(f"     {pair} held only while awake: {(mult - 1) * 100:+.3f}%  → {seed_total * mult:,.2f}")
    mults = []
    for pair in basket:
        ser = px[pair]
        m = 1.0
        ok = True
        for s, e in spans:
            if e <= first_open:
                continue
            s = max(s, first_open)
            p0, p1 = price_at(ser, s), price_at(ser, e)
            if p0 and p1:
                m *= p1 / p0
            else:
                ok = False
        if ok:
            mults.append(m * entry_cost)
    if mults:
        print(
            f"     equal-weight {len(mults)} pairs held only while awake: "
            f"{(statistics.fmean(mults) - 1) * 100:+.3f}%  → {seed_total * statistics.fmean(mults):,.2f}"
        )

    # ------------------------------------------------ (d)
    print("\n\n### (d) P&L PER DAY AND PER AWAKE HOUR\n")
    per_day: dict[str, list[dict]] = defaultdict(list)
    for t in closed:
        per_day[t["close"].strftime("%Y-%m-%d")].append(t)
    d0 = first_open.date()
    dn = now.date()
    print(
        f"{'day (UTC)':11} {'awake h':>8} {'trades':>7} {'gross':>9} {'fees':>7} "
        f"{'net':>9} {'net/awake h':>12}  {'BTC that day':>13}"
    )
    print("-" * 86)
    cum = 0.0
    day = d0
    while day <= dn:
        key = day.isoformat()
        lo = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
        hi = min(lo + timedelta(days=1), now)
        aw = awake_seconds(spans, max(lo, first_open), hi) / 3600.0
        rows = per_day.get(key, [])
        n = sum(r["net"] for r in rows)
        g = sum(r["gross"] for r in rows)
        f = sum(r["fee_dry"] for r in rows)
        cum += n
        bd = hold("BTC/USDT", max(lo, first_open), hi)
        btc = f"{(bd + COST_FLOOR_SIDE * 2) * 100:+.2f}%" if bd is not None else "  n/a"
        print(
            f"{key:11} {aw:8.2f} {len(rows):7d} {g:+9.2f} {f:7.2f} {n:+9.2f} "
            f"{(n / aw if aw > 0.05 else float('nan')):12.2f}  {btc:>13}"
        )
        day += timedelta(days=1)
    tot_aw = awake_seconds(spans, first_open, now) / 3600.0
    span_h = (now - first_open).total_seconds() / 3600.0
    print("-" * 86)
    print(
        f"{'TOTAL':11} {tot_aw:8.2f} {len(closed):7d} "
        f"{sum(t['gross'] for t in closed):+9.2f} {sum(t['fee_dry'] for t in closed):7.2f} "
        f"{all_net:+9.2f} {all_net / tot_aw:12.2f}"
    )
    print(
        f"\n  calendar span {span_h:.1f} h ({span_h / 24:.2f} days); awake {tot_aw:.1f} h "
        f"= {tot_aw / span_h * 100:.0f}% of it."
    )
    print(f"  net per awake hour, all trades      : {all_net / tot_aw:+.3f} USDT/h")
    print(f"  net per awake hour, strategy only   : {sum(t['net'] for t in strat) / tot_aw:+.3f} USDT/h")
    print("\n  dark spans longer than 1 hour:")
    prev_end = None
    for s, e in spans:
        if prev_end is not None and (s - prev_end) > timedelta(hours=1):
            print(f"    dark {z(prev_end)} → {z(s)}  = {(s - prev_end).total_seconds() / 3600.0:.2f} h")
        prev_end = e
    print("  awake spans:")
    for s, e in spans:
        h = (e - s).total_seconds() / 3600.0
        if h >= 0.05:
            print(f"    awake {z(s)} → {z(e)}  = {h:.2f} h")

    # ------------------------------------------------ (f)
    print("\n\n### (f) WHAT THIS SAMPLE CAN AND CANNOT SUPPORT\n")
    # independent trades: both sleeves ran IDENTICAL rules under the fast profile, so a
    # pair+hour traded by both is ONE observation, not two.
    keys = {(t["pair"], t["open"].strftime("%Y-%m-%d %H")) for t in strat}
    print(f"  closed strategy trades in the databases : {len(strat)}")
    print("  INDEPENDENT observations (both sleeves run identical rules under this profile,")
    print(f"  so the same pair entered in the same hour is ONE event, not two): {len(keys)}")

    # per-independent-event returns, in % of notional, averaged across duplicate sleeves
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for t in strat:
        groups[(t["pair"], t["open"].strftime("%Y-%m-%d %H"))].append(t)
    rets = []
    for _, rows in groups.items():
        rets.append(statistics.fmean(r["net"] / r["buy_notional"] for r in rows))
    rets_floor = []
    for _, rows in groups.items():
        rets_floor.append(
            statistics.fmean(r["net_at_cost_floor"] / r["buy_notional"] for r in rows)
        )
    for label, xs in (("at the 10 bps the bots charged", rets),
                      ("at the 0.30% repo cost floor", rets_floor)):
        n = len(xs)
        m = statistics.fmean(xs)
        sd = statistics.stdev(xs) if n > 1 else float("nan")
        se = sd / math.sqrt(n)
        print(f"\n  mean return per independent trade, {label}:")
        print(f"    n = {n}, mean {m * 100:+.4f}%, sd {sd * 100:.4f}%, standard error {se * 100:.4f}%")
        print(
            f"    95% interval on the mean: {(m - 1.96 * se) * 100:+.4f}% .. "
            f"{(m + 1.96 * se) * 100:+.4f}%   → {'INCLUDES ZERO' if (m - 1.96 * se) * (m + 1.96 * se) < 0 else 'excludes zero'}"
        )
        t_stat = m / se if se else float("nan")
        print(f"    t = {t_stat:+.2f} (|t| > 2 would be the first hint of a real edge)")
        if m > 0 and sd > 0:
            need = math.ceil((1.96 * sd / m) ** 2)
            print(f"    trades needed for a 95% interval that excludes zero at this mean and spread: {need:,}")
            rate_per_awake_h = len(keys) / tot_aw
            print(
                f"    at the observed rate of {rate_per_awake_h:.3f} independent trades per AWAKE hour:"
            )
            for up in (0.24, 1.00):
                hrs = need / rate_per_awake_h
                print(
                    f"      {hrs:,.0f} awake hours = {hrs / 24 / up:,.0f} calendar days "
                    f"({hrs / 24 / up / 365:,.1f} years) at {up * 100:.0f}% uptime"
                )
        else:
            print("    mean is not positive, so no sample size makes it significantly positive.")

    print(
        "\n  What it cannot support: any statement about the strategy's edge. The profile's\n"
        "  own costed 30-day backtest is -1.29%; a handful of trades over a few days cannot\n"
        "  confirm or refute that, and the arithmetic above says how far off we are."
    )


if __name__ == "__main__":
    main()
