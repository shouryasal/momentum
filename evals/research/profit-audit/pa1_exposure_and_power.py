"""pa1 (c)(f) finish: how much of the pot was actually in the market, the exposure-matched
benchmark, the per-trade edge against the cost floor, and an honest power calculation.

Measurement only, read-only, against the sandbox copy.

Usage: python evals/research/profit-audit/pa1_exposure_and_power.py <state_root>
"""

from __future__ import annotations

import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
SEED = 20000.0
COST_PER_SIDE = 0.0015


def ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def parse(s):
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00").split("+")[0]).replace(
        tzinfo=UTC)


DBS = [("a", ROOT / "ft_userdata" / "a" / "tradesv3.sqlite"),
       ("b", ROOT / "ft_userdata" / "b" / "tradesv3.sqlite"),
       ("a", ROOT / "ft_userdata" / "a" / "runs" / "test-a-000.sqlite"),
       ("b", ROOT / "ft_userdata" / "b" / "runs" / "test-b-000.sqlite")]

T0 = datetime(2026, 9, 23, 13, 37, 36, tzinfo=UTC)
T1 = datetime(2026, 9, 30, 5, 49, 45, tzinfo=UTC)
CAL_H = (T1 - T0).total_seconds() / 3600

trades = []
for sleeve, path in DBS:
    c = ro(path)
    buys = defaultdict(float)
    for o in c.execute("select ft_trade_id tid, cost from orders where status='closed' "
                       "and filled>0 and ft_order_side='buy'"):
        buys[o["tid"]] += float(o["cost"] or 0)
    for t in c.execute("select id, pair, is_open, open_date, close_date, "
                       "close_profit_abs, exit_reason from trades"):
        od, cd = parse(t["open_date"]), parse(t["close_date"])
        trades.append({"sleeve": sleeve, "pair": t["pair"], "open": od,
                       "close": cd or T1, "still_open": bool(t["is_open"]),
                       "notional": buys[t["id"]],
                       "net": float(t["close_profit_abs"] or 0.0),
                       "exit": t["exit_reason"] or "(open)"})
    c.close()

ONE_OFF = {"force_exit", "target_zero"}
strat = [t for t in trades if t["exit"] not in ONE_OFF]

print("================= HOW MUCH OF THE POT WAS EVER IN THE MARKET =================")
dollar_hours = sum(t["notional"] * (t["close"] - t["open"]).total_seconds() / 3600
                   for t in trades)
seed_hours = SEED * CAL_H
print(f"calendar hours of the record        : {CAL_H:.2f}")
print(f"dollar-hours the book held risk     : {dollar_hours:,.0f} USDT-hours")
print(f"seed-hours available                : {seed_hours:,.0f} USDT-hours")
print(f"AVERAGE EXPOSURE over the record    : "
      f"{100 * dollar_hours / seed_hours:.2f}% of the pot")
dh_strat = sum(t["notional"] * (t["close"] - t["open"]).total_seconds() / 3600
               for t in strat)
print(f"  strategy trades only              : "
      f"{100 * dh_strat / seed_hours:.2f}% of the pot")

# hours with at least one position open, and the peak simultaneous notional
edges = sorted({t["open"] for t in trades} | {t["close"] for t in trades})
in_market_h = 0.0
peak = 0.0
for a, b in zip(edges, edges[1:], strict=False):
    live = sum(t["notional"] for t in trades if t["open"] <= a and t["close"] >= b)
    if live > 0:
        in_market_h += (b - a).total_seconds() / 3600
    peak = max(peak, live)
print(f"hours with at least one seat open   : {in_market_h:.2f} "
      f"({100 * in_market_h / CAL_H:.1f}% of the record)")
print(f"peak money at risk at any one moment: {peak:,.2f} USDT "
      f"({100 * peak / SEED:.2f}% of the pot)")

print("\n================= EXPOSURE-MATCHED BENCHMARK =================")
kdb = ro(ROOT / "knowledge" / "earn.db")


def price(pair, at):
    r = kdb.execute("select close from candles where pair=? and tf='1h' and is_closed=1 "
                    "and open_time<=? order by open_time desc limit 1",
                    (pair, int(at.timestamp() * 1000))).fetchone()
    return None if r is None else float(r[0])


btc_gross = price("BTC/USDT", T1) / price("BTC/USDT", T0) - 1.0
btc_net = (1 + btc_gross) * (1 - COST_PER_SIDE) ** 2 - 1.0
expo = dollar_hours / seed_hours
print(f"BTC over the record, costed  : {100 * btc_net:+.2f}% "
      f"= {SEED * btc_net:+,.2f} USDT at full exposure")
print(f"the same at OUR exposure ({100 * expo:.2f}%): "
      f"{SEED * btc_net * expo:+,.2f} USDT")
print(f"what the bots actually made  : {sum(t['net'] for t in trades):+,.2f} USDT "
      f"(all), {sum(t['net'] for t in strat):+,.2f} USDT (strategy only)")
print("Read: at the exposure the book actually ran, holding BTC instead would have lost "
      f"{abs(SEED * btc_net * expo):,.2f} USDT, so the strategy's "
      f"{sum(t['net'] for t in strat):+,.2f} is "
      f"{sum(t['net'] for t in strat) - SEED * btc_net * expo:+,.2f} against it.")

print("\n================= PER-TRADE EDGE AGAINST THE COST FLOOR =================")
closed_strat = [t for t in strat if not t["still_open"]]
pct = [100.0 * t["net"] / t["notional"] for t in closed_strat]
print(f"closed strategy trades         : {len(closed_strat)}")
print(f"mean notional a trade          : "
      f"{statistics.fmean(t['notional'] for t in closed_strat):,.2f} USDT")
print(f"mean net per trade             : "
      f"{statistics.fmean(t['net'] for t in closed_strat):+.3f} USDT "
      f"= {statistics.fmean(pct):+.3f}% of the money put in")
print(f"median net per trade           : {statistics.median(pct):+.3f}%")
print("fees the paper run charged     : 0.200% a round trip (0.10% a side)")
print("the repo cost floor            : 0.300% a round trip (0.15% a side)")
print(f"mean net per trade at the FLOOR: {statistics.fmean(pct) - 0.100:+.3f}% "
      f"(the extra 0.10% of slippage the dry run never charged)")
print(f"drop the single best trade (AVAX ROI, +24 in both bots) and the mean becomes "
      f"{statistics.fmean(sorted(pct)[:-2]):+.3f}%")

print("\n================= HONEST POWER =================")
groups = defaultdict(list)
for t in closed_strat:
    groups[(t["pair"], t["open"].strftime("%Y-%m-%dT%H"))].append(t)
ind = [statistics.fmean(v["net"] for v in g) for g in groups.values()]
n, m = len(ind), statistics.fmean(ind)
sd = statistics.stdev(ind)
se = sd / math.sqrt(n)
print(f"independent market bets        : {n} (both bots on the same pair in the same "
      f"hour is ONE bet - they run identical rules under this profile)")
print(f"mean per bet                   : {m:+.3f} USDT, sd {sd:.3f}, se {se:.3f}")
print(f"95% interval on the mean       : [{m - 2.365 * se:+.3f}, {m + 2.365 * se:+.3f}] "
      f"USDT (t, {n - 1} df) - straddles zero")
print(f"t                              : {m / se:.3f}")
n80 = 7.849 * (sd / m) ** 2
print(f"bets for 80% power at 95% IF the true mean equals today's estimate: {n80:.0f}")
lo = m - 2.365 * se
print(f"the same at the BOTTOM of today's interval ({lo:+.3f}): "
      f"{'no sample size shows a profit, the interval includes losses' if lo < 0 else n80}")
rate = n / (50.49 / 24)
print(f"independent bets per awake day : {rate:.2f}")
print(f"awake days to reach {n80:.0f} bets   : {n80 / rate:.0f} "
      f"({n80 / rate / 365:.2f} awake years)")
print(f"at the 31.5% uptime measured, that is {n80 / rate / 0.315:.0f} calendar days "
      f"({n80 / rate / 0.315 / 365:.2f} calendar years)")
print("\nThe prior that matters more than 9 bets: the SAME profile's own costed "
      "backtest over 2,199 trades is net -26.45% (config/profiles/fast-test.yaml), "
      "and the 9.11-year deployed book scores 0.83 against BTC-hold 0.83 "
      "(exit-and-horizon-2026-09-29.md).")

print("\n================= WORST POINT OF THE POT =================")
jdb = ro(ROOT / "journal" / "journal.db")
navs = defaultdict(list)
for r in jdb.execute("select ts_utc, sleeve, nav_usdt from nav_points order by ts_utc"):
    navs[r["ts_utc"]].append(float(r["nav_usdt"]))
series = [(ts, sum(v)) for ts, v in sorted(navs.items()) if len(v) == 2]
peak_v = -1e18
mdd = 0.0
mdd_at = None
for ts, v in series:
    peak_v = max(peak_v, v)
    dd = v / peak_v - 1
    if dd < mdd:
        mdd, mdd_at = dd, ts
print(f"15-minute ledger ticks with both sleeves: {len(series)}")
print(f"first {series[0]}  last {series[-1]}")
print(f"worst peak-to-trough on the ledger: {100 * mdd:.3f}% at {mdd_at}")
print("(the ledger restarted from the seed twice, so this understates the true dip; "
      "the cumulative pot low is the day-one -69.77 event)")
jdb.close()
kdb.close()
