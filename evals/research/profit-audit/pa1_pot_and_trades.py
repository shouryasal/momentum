"""pa1 (a)(b)(d)(f): every trade ever made, the three pot definitions, the daily series,
and what the sample can support.

Measurement only. Reads a SANDBOX state root built by copying the live databases with
sqlite's backup API. Writes nothing under ~/earn-run.

Usage: python evals/research/profit-audit/pa1_pot_and_trades.py <state_root>
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()

from console.services import pot_service  # noqa: E402
from ops.config import load_config  # noqa: E402

cfg = load_config(ROOT / "config" / "earn.yaml", root=ROOT)
print("profile applied:", getattr(getattr(cfg, "profiles", None), "active", None))

FEE = 0.001  # taker/maker fee both bots book (fee_open == fee_close == 0.001)

# ---------------------------------------------------------------- read every trade

DBS = [
    ("a", "legacy-4h", ROOT / "ft_userdata" / "a" / "tradesv3.sqlite"),
    ("b", "legacy-4h", ROOT / "ft_userdata" / "b" / "tradesv3.sqlite"),
    ("a", "fast-1h", ROOT / "ft_userdata" / "a" / "runs" / "test-a-000.sqlite"),
    ("b", "fast-1h", ROOT / "ft_userdata" / "b" / "runs" / "test-b-000.sqlite"),
]

ONE_OFF = {"force_exit", "target_zero"}


def ro(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def parse(s):
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00").split("+")[0]).replace(
        tzinfo=UTC)


trades = []
for sleeve, era, path in DBS:
    if not path.exists():
        continue
    c = ro(path)
    fee_by_trade = defaultdict(float)
    buy_cost = defaultdict(float)
    sell_cost = defaultdict(float)
    for o in c.execute("select ft_trade_id tid, ft_order_side side, status, filled, "
                       "cost from orders"):
        if (o["status"] or "") != "closed" or not (o["filled"] or 0):
            continue
        cost = float(o["cost"] or 0.0)
        fee_by_trade[o["tid"]] += cost * FEE
        (buy_cost if o["side"] == "buy" else sell_cost)[o["tid"]] += cost
    for t in c.execute("select id, pair, is_open, open_date, close_date, open_rate, "
                       "close_rate, close_profit_abs, exit_reason, enter_tag, strategy, "
                       "amount, stake_amount, max_rate, min_rate from trades order by id"):
        od, cd = parse(t["open_date"]), parse(t["close_date"])
        trades.append({
            "sleeve": sleeve, "era": era, "db": path.name, "id": t["id"],
            "pair": t["pair"], "open": od, "close": cd,
            "is_open": bool(t["is_open"]),
            "net": float(t["close_profit_abs"] or 0.0),
            "fees": fee_by_trade[t["id"]],
            "buy_cost": buy_cost[t["id"]], "sell_cost": sell_cost[t["id"]],
            "exit_reason": t["exit_reason"] or ("(open)" if t["is_open"] else "?"),
            "enter_tag": t["enter_tag"], "strategy": t["strategy"],
            "open_rate": t["open_rate"], "close_rate": t["close_rate"],
            "max_rate": t["max_rate"], "min_rate": t["min_rate"],
            "hold_h": None if not cd else (cd - od).total_seconds() / 3600.0,
        })
    c.close()

closed = [t for t in trades if not t["is_open"]]
openn = [t for t in trades if t["is_open"]]
oneoff = [t for t in closed if t["exit_reason"] in ONE_OFF]
strat = [t for t in closed if t["exit_reason"] not in ONE_OFF]


def block(label, rows):
    n = len(rows)
    if not n:
        print(f"{label:<34} n=0")
        return
    net = sum(r["net"] for r in rows)
    fees = sum(r["fees"] for r in rows)
    wins = [r for r in rows if r["net"] > 0]
    holds = sorted(r["hold_h"] for r in rows if r["hold_h"] is not None)
    print(f"{label:<34} n={n:<3} net={net:+9.2f} fees={fees:7.2f} gross={net + fees:+9.2f} "
          f"win={100 * len(wins) / n:5.1f}%  median_hold_h="
          f"{statistics.median(holds) if holds else float('nan'):6.2f} "
          f"min={holds[0]:.2f} max={holds[-1]:.2f}")


print("\n==================== (a) EVERY TRADE EVER MADE ====================")
print(f"rows in the four bot databases: {len(trades)} "
      f"({len(closed)} closed, {len(openn)} still open)")
block("ALL closed", closed)
block("  strategy's own decisions", strat)
block("  the two day-one one-offs", oneoff)
for era in ("legacy-4h", "fast-1h"):
    block(f"era {era}", [t for t in closed if t["era"] == era])
for s in ("a", "b"):
    block(f"sleeve {s} closed", [t for t in closed if t["sleeve"] == s])
    block(f"sleeve {s} strategy only", [t for t in strat if t["sleeve"] == s])

print("\n-- exit-reason mix (closed) --")
print(f"{'reason':<22}{'n':>4}{'net':>11}{'fees':>9}{'won':>6}{'med_hold_h':>12}")
for reason, n in Counter(t["exit_reason"] for t in closed).most_common():
    rows = [t for t in closed if t["exit_reason"] == reason]
    hs = sorted(r["hold_h"] for r in rows)
    print(f"{reason:<22}{n:>4}{sum(r['net'] for r in rows):>11.2f}"
          f"{sum(r['fees'] for r in rows):>9.2f}"
          f"{sum(1 for r in rows if r['net'] > 0):>6}{statistics.median(hs):>12.2f}")

print("\n-- hold-time distribution, strategy trades only (hours) --")
hs = sorted(t["hold_h"] for t in strat)
if hs:
    qs = [0, 10, 25, 50, 75, 90, 100]
    print("  " + "  ".join(
        f"p{q}={hs[min(len(hs) - 1, int(round(q / 100 * (len(hs) - 1))))]:.2f}" for q in qs))
    print(f"  mean={statistics.fmean(hs):.2f}  under 1h: "
          f"{sum(1 for h in hs if h < 1)}/{len(hs)}  under 24h: "
          f"{sum(1 for h in hs if h < 24)}/{len(hs)}")

print("\n-- the two day-one one-off events, itemised --")
for t in sorted(oneoff, key=lambda r: (r["sleeve"], r["id"])):
    print(f"  sleeve {t['sleeve']} {t['pair']:<9} {t['exit_reason']:<12} "
          f"open={t['open']:%Y-%m-%dT%H:%MZ} close={t['close']:%Y-%m-%dT%H:%MZ} "
          f"hold={t['hold_h']:5.2f}h net={t['net']:+8.2f} fees={t['fees']:5.2f} "
          f"tag={t['enter_tag']}")
for s in ("a", "b"):
    rows = [t for t in oneoff if t["sleeve"] == s]
    if rows:
        print(f"  sleeve {s} one-off total: {sum(r['net'] for r in rows):+.2f} USDT")

print("\n-- every closed strategy trade, in order --")
print(f"{'sleeve':<7}{'pair':<10}{'opened (UTC)':<19}{'hold_h':>8}{'exit':<20}"
      f"{'net':>9}{'fees':>7}")
for t in sorted(strat, key=lambda r: (r["open"], r["sleeve"])):
    print(f"{t['sleeve']:<7}{t['pair']:<10}{t['open']:%Y-%m-%d %H:%M}   "
          f"{t['hold_h']:>8.2f}{t['exit_reason']:<20}{t['net']:>9.2f}{t['fees']:>7.2f}")
for t in openn:
    print(f"{t['sleeve']:<7}{t['pair']:<10}{t['open']:%Y-%m-%d %H:%M}   "
          f"{'OPEN':>8}{'(open)':<20}{'-':>9}{t['fees']:>7.2f}")

# ---------------------------------------------------------------- (b) the three pots

print("\n==================== (b) THE POT, THREE DEFINITIONS ====================")
jdb = ro(ROOT / "journal" / "journal.db")
kdb = ro(ROOT / "knowledge" / "earn.db")
marks = {}
for pair in {t["pair"] for t in openn}:
    row = kdb.execute("select close from candles where pair=? and tf='1h' and is_closed=1 "
                      "order by open_time desc limit 1", (pair,)).fetchone()
    if row:
        marks[pair] = float(row[0])
print("marks used for open positions:", marks)

tot = defaultdict(float)
for s in ("a", "b"):
    p = pot_service.sleeve_pot(cfg, s, conn=jdb, root=ROOT, marks=marks)
    print(f"\nsleeve {s}: seed={p['seed_usdt']} ({p['seed_source']})")
    for k in ("cumulative_net_usdt", "gain_usdt", "realised_all_runs_usdt",
              "realised_current_run_usdt", "realised_earlier_runs_usdt",
              "open_mark_usdt", "open_value_usdt", "fees_usdt", "gross_usdt",
              "ledger_nav_usdt", "ledger_as_of_utc", "ledger_gap_usdt",
              "current_run", "restarts", "fully_priced"):
        print(f"    {k:<30} {p[k]}")
    for r in p["runs"]:
        print(f"    run {r.get('key')}: realised_net={r.get('realised_net')} "
              f"trades={r.get('closed')} open={len(r.get('open') or [])}")
    for k in ("seed_usdt", "cumulative_net_usdt", "realised_all_runs_usdt",
              "realised_current_run_usdt", "open_mark_usdt", "fees_usdt"):
        if p[k] is not None:
            tot[k] += float(p[k])
    if p["ledger_nav_usdt"] is not None:
        tot["ledger_nav_usdt"] += float(p["ledger_nav_usdt"])

print("\n-- both sleeves together --")
print(f"  D1 cumulative pot   (seed 20,000 + every realised trade in every run DB "
      f"+ open mark-to-market) = {tot['cumulative_net_usdt']:,.2f}  "
      f"({tot['cumulative_net_usdt'] - tot['seed_usdt']:+,.2f})")
print(f"  D2 realised since the CURRENT run began (fast-1h databases only, open position "
      f"excluded) = {tot['realised_current_run_usdt']:+,.2f}")
print(f"  D3 console 15-minute ledger (nav_points, restarts from the seed on a fresh DB) "
      f"= {tot['ledger_nav_usdt']:,.2f} ({tot['ledger_nav_usdt'] - tot['seed_usdt']:+,.2f})")
print(f"  realised across ALL run DBs = {tot['realised_all_runs_usdt']:+,.2f}; "
      f"open mark = {tot['open_mark_usdt']:+,.2f}; fees = {tot['fees_usdt']:,.2f}")
print(f"  cross-check, Σ close_profit_abs over the four DBs = "
      f"{sum(t['net'] for t in closed):+,.2f}")

# ---------------------------------------------------------------- (d) daily series

print("\n==================== (d) PER DAY AND PER AWAKE HOUR ====================")


def heartbeats(sleeve):
    log = ROOT / "ft_userdata" / sleeve / "logs" / "freqtrade.log"
    out = []
    if not log.exists():
        return out
    with log.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if "heartbeat" not in line:
                continue
            try:
                out.append(datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                           .replace(tzinfo=UTC))
            except ValueError:
                continue
    return out


beats = sorted(set(heartbeats("a")) | set(heartbeats("b")))
print(f"heartbeat stamps merged over both bots: {len(beats)}  "
      f"first={beats[0]:%Y-%m-%dT%H:%MZ} last={beats[-1]:%Y-%m-%dT%H:%MZ}")
GAP = timedelta(minutes=10)
awake = []  # (start, end) spans where a bot was alive
cur_s, cur_e = beats[0], beats[0]
for b in beats[1:]:
    if b - cur_e <= GAP:
        cur_e = b
    else:
        awake.append((cur_s, cur_e))
        cur_s = cur_e = b
awake.append((cur_s, cur_e))
awake_h = sum((e - s).total_seconds() / 3600.0 for s, e in awake)
span_h = (beats[-1] - beats[0]).total_seconds() / 3600.0
print(f"awake spans: {len(awake)}  awake hours={awake_h:.2f}  calendar span={span_h:.2f}h  "
      f"uptime={100 * awake_h / span_h:.1f}%")
print("dark windows over 1h:")
for (_s0, e0), (s1, _) in zip(awake, awake[1:], strict=False):
    d = (s1 - e0).total_seconds() / 3600.0
    if d >= 1.0:
        print(f"   dark {d:6.2f}h  {e0:%m-%d %H:%MZ} -> {s1:%m-%d %H:%MZ}")

per_day_awake = defaultdict(float)
for s, e in awake:
    t = s
    while t < e:
        nxt = min(e, (t + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                     microsecond=0))
        per_day_awake[t.date().isoformat()] += (nxt - t).total_seconds() / 3600.0
        t = nxt

per_day = defaultdict(lambda: {"net": 0.0, "fees": 0.0, "n": 0, "oneoff": 0.0})
for t in closed:
    d = per_day[t["close"].date().isoformat()]
    d["net"] += t["net"]
    d["fees"] += t["fees"]
    d["n"] += 1
    if t["exit_reason"] in ONE_OFF:
        d["oneoff"] += t["net"]
days = sorted(set(per_day) | set(per_day_awake))
print(f"\n{'day':<12}{'awake_h':>9}{'closed':>8}{'net':>10}{'one-off':>10}"
      f"{'strategy':>10}{'fees':>8}{'net/awake_h':>13}")
cum = 0.0
for d in days:
    r = per_day.get(d, {"net": 0.0, "fees": 0.0, "n": 0, "oneoff": 0.0})
    ah = per_day_awake.get(d, 0.0)
    cum += r["net"]
    rate = r["net"] / ah if ah > 0.05 else float("nan")
    print(f"{d:<12}{ah:>9.2f}{r['n']:>8}{r['net']:>10.2f}{r['oneoff']:>10.2f}"
          f"{r['net'] - r['oneoff']:>10.2f}{r['fees']:>8.2f}{rate:>13.3f}")
print(f"{'TOTAL':<12}{awake_h:>9.2f}{len(closed):>8}{cum:>10.2f}"
      f"{sum(t['net'] for t in oneoff):>10.2f}"
      f"{sum(t['net'] for t in strat):>10.2f}"
      f"{sum(t['fees'] for t in closed):>8.2f}"
      f"{cum / awake_h:>13.3f}")
print(f"\nstrategy-only net per awake hour (both sleeves) = "
      f"{sum(t['net'] for t in strat) / awake_h:+.3f} USDT/h")
print(f"strategy trades opened per awake hour = {len(strat + openn) / awake_h:.3f} "
      f"(i.e. {24 * len(strat + openn) / awake_h:.2f} a day while awake, both sleeves)")

# ------------------------------------------------- (f) what the sample can support

print("\n==================== (f) WHAT THIS SAMPLE SUPPORTS ====================")
# A trade taken by BOTH sleeves on the same pair within the hour is ONE market bet:
# the two bots run identical rules under this profile, so they are not independent.
groups = defaultdict(list)
for t in strat:
    groups[(t["pair"], t["open"].strftime("%Y-%m-%dT%H"))].append(t)
print(f"closed strategy trade rows: {len(strat)}")
print(f"independent market bets (same pair, same hour, both bots = one bet): "
      f"{len(groups)}")
ind = [statistics.fmean(v["net"] for v in g) for g in groups.values()]
seed_total = tot["seed_usdt"] or 20000.0
per_bet_pct = [100.0 * x / (seed_total / 2 * 0.05) for x in ind]  # vs a 5% seat
m = statistics.fmean(ind)
sd = statistics.stdev(ind) if len(ind) > 1 else float("nan")
se = sd / math.sqrt(len(ind))
print(f"mean net per independent bet = {m:+.3f} USDT; sd = {sd:.3f}; "
      f"standard error = {se:.3f}")
print(f"95% interval on the mean bet (t, {len(ind) - 1} df) = "
      f"[{m - 2.365 * se:+.3f}, {m + 2.365 * se:+.3f}] USDT  "
      f"-> {'includes zero' if (m - 2.365 * se) * (m + 2.365 * se) < 0 else 'excludes zero'}")
print(f"t statistic = {m / se:.3f} (needs about 2.0 for 95% confidence)")
need = (1.96 * sd / m) ** 2 if m else float("inf")
print(f"bets needed for the mean to clear zero at 95%, at THIS mean and sd: "
      f"{need:.0f}")
rate_per_day = len(groups) / awake_h * 24
print(f"independent bets per awake day = {rate_per_day:.2f}; at that rate "
      f"{need:.0f} bets take {need / rate_per_day:.0f} awake days "
      f"({need / rate_per_day / 365:.1f} awake years)")
print(f"per-bet net as a share of the 5% seat it used: mean "
      f"{statistics.fmean(per_bet_pct):+.3f}%, cost floor 0.30% a round trip")
print(json.dumps({"independent_bets": len(groups), "mean_usdt": round(m, 4),
                  "sd_usdt": round(sd, 4), "t": round(m / se, 3),
                  "bets_needed": round(need), "awake_hours": round(awake_h, 2)}, indent=2))
jdb.close()
kdb.close()
