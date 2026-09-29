"""H-B — 2026-09-29-min-hold-vs-min-edge.

A vectorised-then-sequential replay of the fast-test profile (``strategies/SleeveFast.py``
+ ``config/profiles/fast-test.yaml``) on the 1h feathers, with the portfolio-level gate
limits that shape its cadence (4 new trades per Gulf day, 300-minute entry spacing, 2h
per-pair re-entry cooldown, 1%-of-NAV monthly fee budget), so that two churn controls can
be compared on the same signals:

* ``min_edge`` — the shipped plan-shape check (risk.min_edge = 3 x 0.30%): refuses every
  entry of a plan whose smallest booked target is below 0.90%. Under the shipped L3R3 plan
  the smallest target IS 0.90%, so it refuses nothing; under the old L0/R0 plan (ROI floor
  0.5%) it refuses everything. It is evaluated here as arithmetic, not simulated.
* the analogue-timing.md §4.5/§5.1 R3 minimum-holding gate — ``H_min = max(10,
  ceil((k*c/(lambda*sd20))^2))`` days, ``lambda = 0.20`` fixed, ``c = 0.30%``, applied to the
  discretionary exit(s) only; the fixed stop is never blocked. Two variants: block the
  trend-loss exit only (R3 as written), or block every non-stop exit (trend loss, ROI,
  trailing) — the second is what "a horizon floor" means on a profile whose exits are
  mostly ROI.

Why not ``evals/backtest_api.py``: that runs freqtrade in docker (~50 CPU-min per 31-pair
cell under load, and this host hibernated on critical battery this morning) and its patch
namespace has no minimum-holding key — adding one is ``strategies/**``, tier 2, and four
other agents are editing that tree. The replay is calibrated below against the freqtrade
numbers in ``docs/design/risk-and-ladder-2026-09-29.md`` §2.3 (cell L3R3) on the same
window and its deviations are reported, not hidden.

Not modelled (stated, not hidden): the take-profit ladder (48 rungs in 2,199 trades in the
freqtrade replay; the no-ladder cell was within 0.2pp), the StoplossGuard 24h lock, tier
caps other than the 5%-of-NAV stake, intrabar ordering finer than stop > trailing > ROI.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import COST_SIDE, REGIMES, cagr_vol_mdd, utc, write_json  # noqa: E402

from runs.features import load_candles  # noqa: E402

# ---- the profile, verbatim from config/profiles/fast-test.yaml
EMA_FAST, EMA_SLOW, BREAKOUT, ATR_N = 6, 18, 3, 14
MIN_ATR_PCT, MAX_ATR_PCT = 0.0015, 0.070
TARGET_PCT_NAV = 0.05
STOP = 0.06
TRAIL_ACTIVATE, TRAIL_DIST = 0.015, 0.008
ROI_TABLE = {0: 0.020, 90: 0.015, 300: 0.009}       # minutes -> profit
REENTRY_COOLDOWN_H = 2
MIN_ENTRY_SPACING_MIN = 300
MAX_TRADES_PER_DAY = 4                                # risk.max_trades_per_day
MAX_FEE_PCT_PER_MONTH = 0.01                          # risk.max_fee_pct_per_month
START_NAV = 10_000.0
GULF = pd.Timedelta(hours=4)

# ---- the R3 gate
R3_C, R3_LAMBDA, R3_FLOOR_DAYS = 0.0030, 0.20, 10

# ---- min_edge arithmetic (risk.min_edge: 3 x 0.30%)
MIN_EDGE_FLOOR = 3.0 * 0.0030
PLAN_L3R3 = {"ladder": [0.009, 0.015], "roi": [0.020, 0.015, 0.009]}
PLAN_L0R0 = {"ladder": [0.006, 0.012], "roi": [0.020, 0.010, 0.005]}


def min_edge_refuses(plan: dict) -> bool:
    smallest = min(plan["ladder"] + plan["roi"])
    return smallest + 1e-9 < MIN_EDGE_FLOOR


def whitelist(snapshot: Path) -> list[str]:
    """The 31 tradeable pairs of the point-in-time snapshot (core + major + satellite)."""
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    pairs: set[str] = set()

    def walk(node, key=None):
        if isinstance(node, dict):
            tier = node.get("tier")
            if tier in ("core", "major", "satellite"):
                sym = node.get("symbol") or node.get("pair") or key
                if sym:
                    pairs.add(str(sym))
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)

    walk(data)
    out = []
    for p in sorted(pairs):
        p = p.replace("_", "/")
        if "/" not in p and p.endswith("USDT"):
            p = p[:-4] + "/USDT"
        out.append(p)
    return out


@dataclass
class PairData:
    pair: str
    idx: pd.DatetimeIndex
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    enter: np.ndarray      # signal on candle i (acts on candle i+1)
    trend_down: np.ndarray
    sd20: np.ndarray       # 20-day std of daily log returns, as of the day before candle i


def prepare(pair: str, start: str) -> PairData | None:
    df = load_candles(pair, "1h")
    if df is None or len(df) < 600:
        return None
    df = df.sort_values("date").drop_duplicates("date")
    close = df["close"].astype(float)
    ema_f = close.ewm(span=EMA_FAST, adjust=False, min_periods=EMA_FAST).mean()
    ema_s = close.ewm(span=EMA_SLOW, adjust=False, min_periods=EMA_SLOW).mean()
    prev = close.shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()],
                   axis=1).max(axis=1)
    atr = tr.rolling(ATR_N, min_periods=ATR_N).mean()
    atr_pct = atr / close.where(close > 0)
    breakout_high = df["high"].rolling(BREAKOUT, min_periods=BREAKOUT).max().shift(1)
    trend_up = (ema_f > ema_s)
    enter = (trend_up.fillna(False) & (close > breakout_high) & (atr_pct >= MIN_ATR_PCT)
             & (atr_pct <= MAX_ATR_PCT)).to_numpy()
    trend_down = (~trend_up.fillna(True)).to_numpy()
    # sd20 from daily closes, known at the start of each UTC day (yesterday's window)
    daily = close.set_axis(pd.to_datetime(df["date"], utc=True)).resample("1D").last().dropna()
    lr = np.log(daily / daily.shift(1))
    sd20_daily = lr.rolling(20, min_periods=20).std().shift(1)
    dates = pd.to_datetime(df["date"], utc=True)
    sd20 = sd20_daily.reindex(dates.dt.floor("D")).to_numpy()
    idx = pd.DatetimeIndex(dates)
    keep = idx >= utc(start)
    if keep.sum() == 0:
        return None
    return PairData(pair, idx[keep], df["open"].to_numpy(float)[keep], df["high"].to_numpy(float)[keep],
                    df["low"].to_numpy(float)[keep], close.to_numpy(float)[keep], enter[keep],
                    trend_down[keep], sd20[keep])


@dataclass
class Trade:
    pair: str
    open_time: pd.Timestamp
    open_price: float
    stake: float
    amount: float
    max_rate: float
    h_min_days: float


def roi_level(minutes: float) -> float:
    lvl = None
    for m in sorted(ROI_TABLE):
        if minutes >= m:
            lvl = ROI_TABLE[m]
    return lvl if lvl is not None else 10.0


def simulate(pairs: dict[str, PairData], *, gate: str, k: float, start: str, end: str,
             cost: float = COST_SIDE) -> dict:
    """gate: 'none' | 'minhold_trend' | 'minhold_all'."""
    hours = pd.DatetimeIndex(sorted(set().union(*[set(p.idx) for p in pairs.values()])))
    hours = hours[(hours >= utc(start)) & (hours <= utc(end))]
    pos = {p: {h: i for i, h in enumerate(pd.idx)} for p, pd in pairs.items()}
    cash = START_NAV
    open_trades: dict[str, Trade] = {}
    last_exit: dict[str, pd.Timestamp] = {}
    last_entry: pd.Timestamp | None = None
    trades_today: dict[pd.Timestamp, int] = {}
    fees_month: dict[pd.Period, float] = {}
    log: list[dict] = []
    equity: list[tuple[pd.Timestamp, float]] = []
    refused = {"day_cap": 0, "spacing": 0, "cooldown": 0, "fee_budget": 0}
    blocked_exits = 0

    def nav_at(t_i: dict[str, int]) -> float:
        v = cash
        for p, tr in open_trades.items():
            i = t_i.get(p)
            v += tr.amount * (pairs[p].c[i] if i is not None else tr.open_price)
        return v

    for t in hours:
        t_i = {p: pos[p].get(t) for p in pairs}
        gulf_day = (t + GULF).normalize()
        gulf_month = (t + GULF).to_period("M")
        nav = nav_at(t_i)
        # ---- exits first (freqtrade evaluates open trades before new entries)
        for p in list(open_trades):
            i = t_i.get(p)
            if i is None or i == 0:
                continue
            d, tr = pairs[p], open_trades[p]
            held_days = (t - tr.open_time).total_seconds() / 86400.0
            minutes = held_days * 1440.0
            before_hmin = held_days < tr.h_min_days
            reason, price = None, None
            stop_price = tr.open_price * (1 - STOP)
            trail_armed = tr.max_rate >= tr.open_price * (1 + TRAIL_ACTIVATE)
            trail_price = tr.max_rate * (1 - TRAIL_DIST) if trail_armed else None
            sig_exit = d.trend_down[i - 1]
            # 1. trend-loss signal from the previous candle fills at this open
            if sig_exit and not (gate in ("minhold_trend", "minhold_all") and before_hmin):
                reason, price = "exit_signal", d.o[i]
            elif sig_exit:
                blocked_exits += 1
            if reason is None:
                # 2. hard stop, never blocked
                if d.lo[i] <= stop_price:
                    reason, price = "stop_loss", min(stop_price, d.o[i])
                # 3. trailing stop
                elif trail_price is not None and d.lo[i] <= trail_price and not (
                        gate == "minhold_all" and before_hmin):
                    reason, price = "trailing_stop_loss", min(trail_price, d.o[i])
                else:
                    # 4. ROI at the level applicable at this candle
                    lvl = roi_level(minutes)
                    roi_price = tr.open_price * (1 + lvl)
                    if d.h[i] >= roi_price and not (gate == "minhold_all" and before_hmin):
                        reason, price = "roi", max(roi_price, d.o[i])
            # update the high-water mark after this candle
            tr.max_rate = max(tr.max_rate, d.h[i])
            if reason is not None:
                proceeds = tr.amount * price
                fee = proceeds * cost
                cash += proceeds - fee
                fees_month[gulf_month] = fees_month.get(gulf_month, 0.0) + fee
                entry_fee = tr.stake * cost
                pnl = proceeds - fee - tr.stake - entry_fee
                log.append({"pair": p, "open_time": tr.open_time, "close_time": t,
                            "hold_days": held_days, "stake": tr.stake, "pnl": pnl,
                            "fees": fee + entry_fee, "reason": reason, "h_min_days": tr.h_min_days,
                            "ret": pnl / tr.stake})
                last_exit[p] = t
                del open_trades[p]
        # ---- entries: signal on the previous candle fills at this open
        cands = [p for p in pairs if t_i.get(p) not in (None, 0) and pairs[p].enter[t_i[p] - 1]
                 and p not in open_trades]
        for p in cands:
            i = t_i[p]
            d = pairs[p]
            if trades_today.get(gulf_day, 0) >= MAX_TRADES_PER_DAY:
                refused["day_cap"] += 1
                continue
            if last_entry is not None and (t - last_entry) < pd.Timedelta(minutes=MIN_ENTRY_SPACING_MIN):
                refused["spacing"] += 1
                continue
            if p in last_exit and (t - last_exit[p]) < pd.Timedelta(hours=REENTRY_COOLDOWN_H):
                refused["cooldown"] += 1
                continue
            if fees_month.get(gulf_month, 0.0) >= MAX_FEE_PCT_PER_MONTH * nav:
                refused["fee_budget"] += 1
                continue
            stake = TARGET_PCT_NAV * nav
            if stake > cash or stake < 25:
                continue
            price = d.o[i]
            fee = stake * cost
            amount = stake / price
            cash -= stake + fee
            fees_month[gulf_month] = fees_month.get(gulf_month, 0.0) + fee
            sd = d.sd20[i]
            if gate == "none" or not np.isfinite(sd) or sd <= 0:
                h_min = 0.0 if gate == "none" else float(R3_FLOOR_DAYS)
            else:
                h_min = max(float(R3_FLOOR_DAYS), math.ceil((k * R3_C / (R3_LAMBDA * sd)) ** 2))
            open_trades[p] = Trade(p, t, price, stake, amount, d.h[i], h_min)
            trades_today[gulf_day] = trades_today.get(gulf_day, 0) + 1
            last_entry = t
        equity.append((t, nav_at(t_i)))
    eq = pd.Series([v for _, v in equity], index=pd.DatetimeIndex([t for t, _ in equity]))
    # mark open trades at the end
    tl = pd.DataFrame(log)
    return {"equity": eq, "trades": tl, "refused": refused, "blocked_exits": blocked_exits,
            "open_at_end": len(open_trades)}


def summarise(res: dict, start: str, end: str, btc_hold: pd.Series) -> dict:
    eq = res["equity"]
    eq = eq[(eq.index >= utc(start)) & (eq.index <= utc(end))]
    daily = eq.resample("1D").last().dropna()
    if len(daily) < 2:
        return {}
    r = daily.pct_change().dropna()
    s = cagr_vol_mdd(r, periods_per_year=365.0)
    tl = res["trades"]
    if len(tl):
        tl = tl[(tl["open_time"] >= utc(start)) & (tl["open_time"] <= utc(end))]
    years = max((daily.index[-1] - daily.index[0]).days / 365.0, 1e-9)
    start_nav = float(daily.iloc[0])
    fees = float(tl["fees"].sum()) if len(tl) else 0.0
    hold_notional_days = float((tl["stake"] * tl["hold_days"]).sum()) if len(tl) else 0.0
    mix = (tl.groupby("reason").agg(n=("pnl", "size"), pnl=("pnl", "sum")).to_dict("index")
           if len(tl) else {})
    bh = btc_hold[(btc_hold.index >= utc(start)) & (btc_hold.index <= utc(end))]
    hs = cagr_vol_mdd(bh.pct_change().dropna(), periods_per_year=365.0)
    return {
        "window": [start, end], "days": int(len(daily)), "years": years,
        "net_pct": (float(daily.iloc[-1]) / start_nav - 1.0) * 100,
        "cagr_pct": s["cagr"] * 100, "vol_pct": s["vol"] * 100, "sharpe": s["sharpe"],
        "mdd_pct": s["mdd"] * 100,
        "trades": int(len(tl)), "trades_per_year": len(tl) / years,
        "mean_hold_h": float(tl["hold_days"].mean() * 24) if len(tl) else np.nan,
        "median_hold_h": float(tl["hold_days"].median() * 24) if len(tl) else np.nan,
        "win_rate": float((tl["pnl"] > 0).mean()) if len(tl) else np.nan,
        "avg_ret_per_trade_pct": float(tl["ret"].mean() * 100) if len(tl) else np.nan,
        "fees_pct_start_per_year": fees / start_nav / years * 100,
        "bps_per_holding_day": (fees / hold_notional_days * 1e4) if hold_notional_days else np.nan,
        "exit_mix": mix,
        "btc_hold_cagr_pct": hs["cagr"] * 100, "btc_hold_mdd_pct": hs["mdd"] * 100,
        "btc_hold_sharpe": hs["sharpe"],
        "refused": res["refused"], "blocked_exits": res["blocked_exits"],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=Path, default=REPO / "knowledge/universe/2026-09-23.json")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--end", default="2026-09-23")
    ap.add_argument("--ks", default="3,5,10")
    ap.add_argument("--calib-start", default="2024-01-01")
    args = ap.parse_args(argv)
    pairs_list = whitelist(args.snapshot)
    print(f"whitelist: {len(pairs_list)} pairs: {pairs_list}")
    warm = (utc(args.start) - pd.Timedelta(days=400)).strftime("%Y-%m-%d")
    pairs = {}
    for p in pairs_list:
        try:
            d = prepare(p, warm)
        except FileNotFoundError:
            d = None
        if d is not None:
            pairs[p] = d
    print(f"loaded {len(pairs)} pairs with 1h data")
    btc = load_candles("BTC/USDT", "1d")
    btc_hold = pd.Series(btc["close"].to_numpy(float), index=pd.to_datetime(btc["date"], utc=True))
    ks = [float(x) for x in args.ks.split(",")]
    variants = [("none", 0.0)] + [(g, k) for k in ks for g in ("minhold_trend", "minhold_all")]
    out: dict = {"min_edge": {"floor": MIN_EDGE_FLOOR,
                              "L3R3_refuses_all_entries": min_edge_refuses(PLAN_L3R3),
                              "L0R0_refuses_all_entries": min_edge_refuses(PLAN_L0R0),
                              "L3R3_smallest_target": min(PLAN_L3R3["ladder"] + PLAN_L3R3["roi"]),
                              "L0R0_smallest_target": min(PLAN_L0R0["ladder"] + PLAN_L0R0["roi"])},
                 "variants": {}}
    windows = {"full": (args.start, args.end), "calib_2024on": (args.calib_start, args.end), **REGIMES}
    for gate, k in variants:
        name = f"{gate}_k{k:g}" if gate != "none" else "none"
        print(f"\n=== simulating {name}", flush=True)
        res = simulate(pairs, gate=gate, k=k, start=args.start, end=args.end)
        out["variants"][name] = {"gate": gate, "k": k,
                                 "windows": {w: summarise(res, s, e, btc_hold) for w, (s, e) in windows.items()}}
        for w, sm in out["variants"][name]["windows"].items():
            if sm:
                print(f"{name:18s} {w:12s} net {sm['net_pct']:8.2f}% CAGR {sm['cagr_pct']:7.2f}% "
                      f"MDD {sm['mdd_pct']:7.2f}% trades {sm['trades']:5d} hold {sm['mean_hold_h']:6.1f}h "
                      f"fees/yr {sm['fees_pct_start_per_year']:6.2f}% bps/day {sm['bps_per_holding_day']:6.1f} "
                      f"| BTC hold CAGR {sm['btc_hold_cagr_pct']:7.2f}% MDD {sm['btc_hold_mdd_pct']:7.2f}%")
    path = write_json("min_hold_gate.json", out)
    print(f"\nwritten {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
