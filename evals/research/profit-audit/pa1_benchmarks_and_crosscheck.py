"""pa1 (c) + the (a) cross-check: the benchmark over the same span AND over the same
awake windows, plus journal fills / tca / nav rows against the bot databases.

Measurement only, read-only, against the sandbox copy.

Usage: python evals/research/profit-audit/pa1_benchmarks_and_crosscheck.py <state_root>
"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()

from ops.config import load_config  # noqa: E402

cfg = load_config(ROOT / "config" / "earn.yaml", root=ROOT)
COST_PER_SIDE = 0.0015  # 15 bps, the repo cost floor per side
SEED = 20000.0


def ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


kdb = ro(ROOT / "knowledge" / "earn.db")
jdb = ro(ROOT / "journal" / "journal.db")

uni = getattr(cfg, "universe", None)
data_only = set(getattr(uni, "data_only_symbols", ()) or ())
pairs = [r[0] for r in kdb.execute(
    "select distinct pair from candles where tf='1h' order by pair")]
basket = [p for p in pairs if p.split("/")[0] not in data_only and p not in data_only]
print(f"1h-candle pairs: {len(pairs)}; data_only excluded: {sorted(data_only)}; "
      f"basket used: {len(basket)}")


def price(pair: str, at: datetime) -> float | None:
    row = kdb.execute(
        "select close from candles where pair=? and tf='1h' and is_closed=1 "
        "and open_time<=? order by open_time desc limit 1",
        (pair, int(at.timestamp() * 1000))).fetchone()
    return None if row is None else float(row[0])


# -------------------------------------------------------------- the awake windows

def heartbeats(sleeve: str) -> list[datetime]:
    log = ROOT / "ft_userdata" / sleeve / "logs" / "freqtrade.log"
    out = []
    if not log.exists():
        return out
    with log.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "heartbeat" in line:
                try:
                    out.append(datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                               .replace(tzinfo=UTC))
                except ValueError:
                    pass
    return out


beats = sorted(set(heartbeats("a")) | set(heartbeats("b")))
spans: list[tuple[datetime, datetime]] = []
s = e = beats[0]
for b in beats[1:]:
    if b - e <= timedelta(minutes=10):
        e = b
    else:
        spans.append((s, e))
        s = e = b
spans.append((s, e))
awake_h = sum((b - a).total_seconds() / 3600 for a, b in spans)

# The trading record starts at the first fill, not the first heartbeat.
T0 = datetime(2026, 9, 23, 13, 37, 36, tzinfo=UTC)
T1 = beats[-1]
trading_spans = [(max(a, T0), b) for a, b in spans if b > T0]
trading_awake_h = sum((b - a).total_seconds() / 3600 for a, b in trading_spans)
print(f"\ncalendar span of the record: {T0:%Y-%m-%dT%H:%MZ} -> {T1:%Y-%m-%dT%H:%MZ} "
      f"= {(T1 - T0).total_seconds() / 3600:.2f} h")
print(f"awake hours inside it: {trading_awake_h:.2f} "
      f"({100 * trading_awake_h / ((T1 - T0).total_seconds() / 3600):.1f}%)  "
      f"spans: {len(trading_spans)}")


def hold_over_span(pair: str, a: datetime, b: datetime) -> float | None:
    p0, p1 = price(pair, a), price(pair, b)
    if not p0 or not p1:
        return None
    return p1 / p0 - 1.0


def costed(gross: float) -> float:
    """Buy once at the start, sell once at the end, 15 bps each side."""
    return (1.0 + gross) * (1.0 - COST_PER_SIDE) ** 2 - 1.0


print("\n================= (c) THE BENCHMARK, TWO WAYS =================")
print("Definition 1 - CALENDAR: buy at the first fill, hold through the outages, sell now.")
print("Definition 2 - AWAKE ONLY: the same holding compounded across the 13 awake spans "
      "only, i.e. what the market offered while a bot was actually watching. Entry cost "
      "charged once.\n")

rows = []
for label, pr in [("BTC buy-and-hold", ["BTC/USDT"]),
                  (f"equal-weight hold, {len(basket)} whitelisted pairs", basket)]:
    cal = [hold_over_span(p, T0, T1) for p in pr]
    cal = [x for x in cal if x is not None]
    cal_gross = sum(cal) / len(cal)
    aw = []
    for p in pr:
        acc = 1.0
        ok = True
        for a, b in trading_spans:
            g = hold_over_span(p, a, b)
            if g is None:
                ok = False
                break
            acc *= (1.0 + g)
        if ok:
            aw.append(acc - 1.0)
    aw_gross = sum(aw) / len(aw)
    rows.append((label, cal_gross, costed(cal_gross), aw_gross, costed(aw_gross)))
rows.append(("USDT doing nothing (0% APR as configured)", 0.0, 0.0, 0.0, 0.0))

print(f"{'benchmark':<48}{'calendar %':>12}{'calendar USDT':>15}"
      f"{'awake-only %':>14}{'awake USDT':>13}")
for label, _cg, cc, _ag, ac in rows:
    print(f"{label:<48}{100 * cc:>12.2f}{SEED * cc:>15.2f}{100 * ac:>14.2f}"
          f"{SEED * ac:>13.2f}")
print("\n(all figures net: 15 bps each side charged once on the entry and the exit)")

print("\nper-pair calendar return of the basket, costed:")
per = sorted(((p, costed(hold_over_span(p, T0, T1) or 0.0)) for p in basket),
             key=lambda r: -r[1])
for p, r in per:
    print(f"   {p:<12}{100 * r:>8.2f}%")
print(f"   {'median':<12}{100 * per[len(per) // 2][1]:>8.2f}%   "
      f"pairs up: {sum(1 for _, r in per if r > 0)}/{len(per)}")

# Only the pairs the bots actually touched, over the same span.
touched = ["TRX/USDT", "NEAR/USDT", "BTC/USDT", "ONDO/USDT", "INJ/USDT", "SOL/USDT",
           "AVAX/USDT"]
tv = [costed(hold_over_span(p, T0, T1) or 0.0) for p in touched]
print(f"\nequal-weight hold of only the 7 pairs the bots actually traded: "
      f"{100 * sum(tv) / len(tv):.2f}% = {SEED * sum(tv) / len(tv):+.2f} USDT")

print("\n================= (a) CROSS-CHECK AGAINST THE JOURNAL =================")
fills = list(jdb.execute("select sleeve, side, pair, fill_amount, fill_price, fee_amount, "
                         "ts_utc from fills order by ts_utc"))
print(f"journal fills rows: {len(fills)}  "
      f"first={fills[0]['ts_utc']}  last={fills[-1]['ts_utc']}")
jf = defaultdict(lambda: {"n": 0, "notional": 0.0, "fee": 0.0})
for f in fills:
    k = (f["sleeve"], f["side"])
    jf[k]["n"] += 1
    jf[k]["notional"] += float(f["fill_amount"]) * float(f["fill_price"])
    jf[k]["fee"] += float(f["fee_amount"] or 0.0)

ft = defaultdict(lambda: {"n": 0, "notional": 0.0, "fee": 0.0})
for sleeve, path in [("a", ROOT / "ft_userdata" / "a" / "tradesv3.sqlite"),
                     ("b", ROOT / "ft_userdata" / "b" / "tradesv3.sqlite"),
                     ("a", ROOT / "ft_userdata" / "a" / "runs" / "test-a-000.sqlite"),
                     ("b", ROOT / "ft_userdata" / "b" / "runs" / "test-b-000.sqlite")]:
    c = ro(path)
    for o in c.execute("select ft_order_side side, cost, filled from orders "
                       "where status='closed' and filled>0"):
        k = (sleeve, o["side"])
        ft[k]["n"] += 1
        ft[k]["notional"] += float(o["cost"] or 0)
        ft[k]["fee"] += float(o["cost"] or 0) * 0.001
    c.close()

print(f"\n{'key':<12}{'journal n':>11}{'bot n':>8}{'journal notional':>19}"
      f"{'bot notional':>15}{'journal fee':>13}{'bot fee':>10}")
for k in sorted(set(jf) | set(ft)):
    a, b = jf[k], ft[k]
    print(f"{str(k):<12}{a['n']:>11}{b['n']:>8}{a['notional']:>19.2f}"
          f"{b['notional']:>15.2f}{a['fee']:>13.2f}{b['fee']:>10.2f}")
tot_j = sum(v["notional"] for v in jf.values())
tot_f = sum(v["notional"] for v in ft.values())
print(f"\ntotal notional traded: journal {tot_j:,.2f} USDT, bot databases {tot_f:,.2f} "
      f"USDT  (difference {tot_j - tot_f:+,.2f})")
print(f"fees actually charged in the paper run (0.10% a side): "
      f"{sum(v['fee'] for v in ft.values()):,.2f} USDT")
print(f"the repo cost floor is 0.15% a side; the SAME volume at that rate would cost "
      f"{tot_f * 0.0015:,.2f} USDT, i.e. {tot_f * 0.0005:,.2f} USDT of slippage the "
      f"paper run never charged")

print("\n-- tca_fill_costs --")
try:
    cols = [c[1] for c in jdb.execute("pragma table_info(tca_fill_costs)")]
    print("cols:", cols)
    n = jdb.execute("select count(*) from tca_fill_costs").fetchone()[0]
    print("rows:", n)
    for r in jdb.execute("select * from tca_fill_costs order by rowid limit 4"):
        print("   ", dict(r))
    for r in jdb.execute(
            "select sleeve, side, count(*) n, avg(slippage_bps) avg_slip, "
            "avg(fee_bps) avg_fee, avg(total_cost_bps) avg_tot from tca_fill_costs "
            "group by sleeve, side"):
        print("   agg:", dict(r))
except Exception as exc:  # noqa: BLE001
    print("  tca_fill_costs:", type(exc).__name__, exc)

print("\n-- nav_points, last tick per sleeve --")
for r in jdb.execute("select sleeve, max(ts_utc) ts, count(*) n from nav_points "
                     "group by sleeve"):
    print("   ", dict(r))
for r in jdb.execute(
        "select ts_utc, sleeve, nav_usdt, realized_pnl, unrealized_pnl, open_trades "
        "from nav_points where ts_utc=(select max(ts_utc) from nav_points) "
        "order by sleeve"):
    print("   ", dict(r))
print("\n-- nav_daily --")
for r in jdb.execute("select * from nav_daily order by 1 limit 20"):
    print("   ", dict(r))

kdb.close()
jdb.close()
