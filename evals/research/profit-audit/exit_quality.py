"""MEASURE 4 (b) — EXIT QUALITY. Did each sell avoid a fall, or miss a rise? And what did
the LATE exit cost, in money?

MEASUREMENT ONLY. evals/research/profit-audit/exit_quality.py.

Pre-registered before any number was computed
---------------------------------------------
H4d: the shipped book's sells land before a fall — the mean forward asset return after an
     exit is NEGATIVE at +30 days.
     FALSIFIER: the mean forward 30d return after an exit is >= 0, i.e. the average sell
     gives up more than it saves.
H4e: the regime-flip exit is better timed than the 10% stop — its forward 30d return is
     more negative.
     FALSIFIER: the stop's forward 30d return is the more negative of the two.

Method
------
The shipped book is simulated day by day on the survivorship-free panel, reusing the
mechanics table of evals/research/exit-horizon/asym_books.py verbatim (that file names the
config line behind every constant). The book is BTC/ETH at base weights 0.40/0.30, gated by
each asset's own 200d-MA regime, vol-targeted at the book level to 30% annual, capped, with
the trend ensemble applied to the BUY side only — because strategies/earn_base.py:581-586
says in as many words that it "never touches an exit". The three sells in the shipped
configuration are therefore:

  exit_regime   SleeveA.py:92 — regime_1d == 0. The only routine sell.
  stop          trading.stoploss.fixed_pct = 10% from the volume-weighted entry, on the
                bar's own low, filled at the stop price.
  monthly_stop  riskgate.loop_tick — the sleeve-level -10%-month flatten.

The ladder is empty (earn.yaml:264), ROI is 10.0 = off (:263), trailing is false (:258),
ATR is false (:260) and daily_loss_response is hold (:189), so ladder / ROI / trailing /
trend-loss / force-exit sells DO NOT EXIST in the shipped 4h book and are reported as zero
rather than omitted. They exist only in the 1h fast-test profile, measured separately from
the live paper databases.

For every exit the forward return of the ASSET is measured at +1/+7/+30 days from the exit
price. That is deliberately GROSS: the question is the counterfactual "what would the money
still in the position have done", and the round trip has already been charged to the trade
that closed. The money figure is exposure_at_exit x forward_return x NAV, converted to USDT
on the console's 20,000 simulated pot.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pa4_common import (  # noqa: E402
    ANNUAL_DAYS,
    COST_PER_SIDE,
    arith_sharpe,
    cagr,
    describe,
    ensemble_weight,
    load_panel,
    mdd,
    realized_vol_annual,
    regime_series,
    series_for,
)

CORE = ["BTC", "ETH"]
BASE_WEIGHTS = {"BTC": 0.40, "ETH": 0.30}
CAPS = {"BTC": 0.40, "ETH": 0.30}
VOL_TARGET, VOL_LOOKBACK = 0.30, 20
STOP_PCT = 0.10
DCA_INTERVAL_DAYS, DCA_CHUNK_PCT_NAV = 7, 0.05
REBALANCE_BAND = 0.05
MONTHLY_LOSS_STOP = 0.10
REENTRY_COOLDOWN_DAYS = 1.0
POT_USDT = 20_000.0
HORIZONS = (1, 7, 30)


def sleeve_a_targets(regime: dict, vols: dict) -> dict:
    """sleeve_common.core_satellite_targets with no satellites — the shipped paper book."""
    raw = {a: (BASE_WEIGHTS[a] if regime.get(a, 0.0) > 0 else 0.0) for a in BASE_WEIGHTS}
    total = sum(w for w in raw.values() if w > 0)
    if total <= 0:
        return {a: 0.0 for a in raw}
    num = sum(w * float(vols.get(a, 0.0) or 0.0) for a, w in raw.items() if w > 0)
    book_vol = num / total
    if not np.isfinite(book_vol) or book_vol <= 0:
        return {a: 0.0 for a in raw}
    scale = min(VOL_TARGET / book_vol, 1.0)
    return {a: float(min(w * scale, CAPS[a])) for a, w in raw.items()}


def run_book(closes: dict, lows: dict, *, resize_down: bool = False,
             monthly_stop: bool = True) -> dict:
    """The deployed book on a NAV ledger. resize_down=True is the counterfactual TRIM."""
    assets = list(closes)
    index = closes[assets[0]].index
    n = len(index)
    ens = {a: ensemble_weight(closes[a]).to_numpy(float) for a in assets}
    reg = {a: regime_series(closes[a]).to_numpy(float) for a in assets}
    vol = {a: realized_vol_annual(closes[a]).to_numpy(float) for a in assets}
    px = {a: closes[a].to_numpy(float) for a in assets}
    lo = {a: lows[a].to_numpy(float) for a in assets}
    months = index.to_period("M").to_numpy()

    nav = cash = 1.0
    units = dict.fromkeys(assets, 0.0)
    entry = dict.fromkeys(assets, 0.0)
    last_dca = dict.fromkeys(assets, -10_000)
    stopped_at = dict.fromkeys(assets, -10_000)
    net = np.zeros(n)
    gross = np.zeros(n)
    turn = np.zeros(n)
    wts = {a: np.zeros(n) for a in assets}
    events: list[dict] = []
    month_anchor, current_month, locked = nav, months[0], False

    for t in range(1, n):
        if months[t] != current_month:
            current_month, month_anchor, locked = months[t], nav, False
        s = t - 1
        traded = 0.0
        nav_pre = cash + sum(units[a] * px[a][s] for a in assets)

        # 1. the one routine sell
        for a in assets:
            if units[a] <= 0 or reg[a][s] != 0.0:
                continue
            notional = units[a] * px[a][s]
            cash += notional * (1.0 - COST_PER_SIDE)
            traded += notional
            events.append({"bar": s, "date": str(index[s].date()), "asset": a,
                           "kind": "exit_regime", "px": float(px[a][s]),
                           "w": notional / nav_pre if nav_pre > 0 else 0.0,
                           "pnl_pct": px[a][s] / entry[a] - 1.0 if entry[a] else None})
            units[a], entry[a] = 0.0, 0.0

        # 2. the sleeve-level monthly flatten
        if monthly_stop and not locked and nav <= month_anchor * (1 - MONTHLY_LOSS_STOP):
            for a in assets:
                if units[a] <= 0:
                    continue
                notional = units[a] * px[a][s]
                cash += notional * (1.0 - COST_PER_SIDE)
                traded += notional
                events.append({"bar": s, "date": str(index[s].date()), "asset": a,
                               "kind": "monthly_stop", "px": float(px[a][s]),
                               "w": notional / nav_pre if nav_pre > 0 else 0.0,
                               "pnl_pct": px[a][s] / entry[a] - 1.0 if entry[a] else None})
                units[a], entry[a] = 0.0, 0.0
            locked = True

        nav_s = cash + sum(units[a] * px[a][s] for a in assets)
        tgt = sleeve_a_targets({a: reg[a][s] for a in assets},
                               {a: vol[a][s] for a in assets})

        # 3/4. the buy side (and, in the counterfactual, the trim)
        if not locked:
            for a in assets:
                held = units[a] * px[a][s]
                want = tgt[a] * nav_s
                scaled = ens[a][s] * tgt[a] * nav_s
                gap = want - held
                if units[a] <= 0:
                    if reg[a][s] <= 0 or ens[a][s] <= 0 or gap <= 0:
                        continue
                    if (s - stopped_at[a]) < REENTRY_COOLDOWN_DAYS:
                        continue
                    stake = min(gap, scaled, cash)
                    if stake <= 0:
                        continue
                    u = stake / px[a][s] * (1.0 - COST_PER_SIDE)
                    units[a], entry[a] = u, px[a][s]
                    last_dca[a] = s
                    cash -= stake
                    traded += stake
                    events.append({"bar": s, "date": str(index[s].date()), "asset": a,
                                   "kind": "entry", "px": float(px[a][s]),
                                   "w": stake / nav_s if nav_s else 0.0})
                    continue
                if resize_down and held > scaled:
                    sell = held - scaled
                    if sell > REBALANCE_BAND * nav_s:
                        units[a] = max(units[a] - sell / px[a][s], 0.0)
                        cash += sell * (1.0 - COST_PER_SIDE)
                        traded += sell
                        events.append({"bar": s, "date": str(index[s].date()), "asset": a,
                                       "kind": "trim", "px": float(px[a][s]),
                                       "w": sell / nav_s if nav_s else 0.0})
                        continue
                if tgt[a] <= 0 or gap <= 0:
                    continue
                if (s - last_dca[a]) < DCA_INTERVAL_DAYS:
                    continue
                if gap <= REBALANCE_BAND * nav_s:
                    continue
                chunk = min(gap, DCA_CHUNK_PCT_NAV * nav_s)
                stake = min(chunk, max(scaled - held, 0.0), cash)
                if stake <= 0:
                    continue
                u = stake / px[a][s] * (1.0 - COST_PER_SIDE)
                entry[a] = ((entry[a] * units[a]) + px[a][s] * u) / (units[a] + u)
                units[a] += u
                last_dca[a] = s
                cash -= stake
                traded += stake

        nav_open = cash + sum(units[a] * px[a][s] for a in assets)
        gross[t] = (sum(units[a] * px[a][s] for a in assets) / nav_open
                    if nav_open > 0 else 0.0)
        if nav_open > 0:
            for a in assets:
                wts[a][t] = units[a] * px[a][s] / nav_open

        # 6. the fixed stop is the only intraday event
        for a in assets:
            if units[a] <= 0:
                continue
            stop_px = entry[a] * (1.0 - STOP_PCT)
            if lo[a][t] <= stop_px:
                fill = min(stop_px, px[a][t])
                notional = units[a] * fill
                cash += notional * (1.0 - COST_PER_SIDE)
                traded += notional
                events.append({"bar": t, "date": str(index[t].date()), "asset": a,
                               "kind": "stop", "px": float(fill),
                               "w": notional / nav_open if nav_open > 0 else 0.0,
                               "pnl_pct": fill / entry[a] - 1.0 if entry[a] else None})
                units[a], entry[a] = 0.0, 0.0
                stopped_at[a] = t

        nav_close = cash + sum(units[a] * px[a][t] for a in assets)
        net[t] = nav_close / nav_open - 1.0 if nav_open > 0 else 0.0
        turn[t] = traded / nav_open if nav_open > 0 else 0.0
        nav = nav_close

    return {"index": index, "net": net, "gross": gross, "turnover": turn,
            "events": events, "weights": pd.DataFrame(wts, index=index)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", default="2017-08-17")
    args = ap.parse_args()

    panel = load_panel(args.panel)
    closes, lows = {}, {}
    for a in CORE:
        closes[a] = series_for(panel, a, "c")
        lows[a] = series_for(panel, a, "l")
    idx = closes["BTC"].index.intersection(closes["ETH"].index)
    idx = idx[idx >= pd.Timestamp(args.start, tz="UTC")]
    closes = {a: closes[a].reindex(idx).ffill() for a in CORE}
    lows = {a: lows[a].reindex(idx).ffill() for a in CORE}

    shipped = run_book(closes, lows, resize_down=False, monthly_stop=True)
    trimmed = run_book(closes, lows, resize_down=True, monthly_stop=True)

    out: dict = {
        "pre_registration": {
            "H4d": "mean forward 30d asset return after an exit is negative; falsifier: >= 0",
            "H4e": "regime flip is better timed than the 10% stop; falsifier: the stop's "
                   "forward 30d is the more negative",
        },
        "span": {"start": str(idx[0].date()), "end": str(idx[-1].date()),
                 "days": int(len(idx)), "years": round(len(idx) / ANNUAL_DAYS, 2)},
        "reasons_that_cannot_fire": {
            "ladder_rung": "take_profit.ladder = [] (earn.yaml:264)",
            "roi": "take_profit.roi_table = {'0': 10.0} = off (:263)",
            "trailing_stop": "stoploss.trailing.enabled = false (:258)",
            "atr_stop": "stoploss.atr.enabled = false (:260)",
            "trend_loss_trim": "not deployed — refused by the order-path reviewer",
            "daily_stop": "risk.daily_loss_response = hold (:189)",
            "force_exit": "human only; never fired in simulation",
        },
    }

    for name, bk in (("shipped", shipped), ("with_trim_counterfactual", trimmed)):
        ev = pd.DataFrame(bk["events"])
        sells = ev[ev["kind"].isin(["exit_regime", "stop", "monthly_stop", "trim"])].copy()
        out[name] = {
            "book": {"cagr": cagr(bk["net"][1:]), "sharpe_arith": arith_sharpe(bk["net"][1:]),
                     "mdd": mdd(bk["net"][1:]), "avg_gross": float(np.mean(bk["gross"][1:])),
                     "fee_drag_per_year": float(np.sum(bk["turnover"]) * COST_PER_SIDE
                                                / (len(idx) / ANNUAL_DAYS))},
            "counts": ev["kind"].value_counts().to_dict(),
            "sells_per_year": round(len(sells) / (len(idx) / ANNUAL_DAYS), 2),
            "by_reason": {},
        }
        for reason, grp in sells.groupby("kind"):
            rec: dict = {"n": int(len(grp)),
                         "mean_weight_sold": float(grp["w"].mean()),
                         "realised_pnl_pct": describe(
                             grp["pnl_pct"].dropna().to_numpy(float), "trade P&L at exit")
                         if "pnl_pct" in grp else {}}
            for h in HORIZONS:
                fwd, money = [], []
                for _, r in grp.iterrows():
                    c = closes[r["asset"]]
                    b = int(r["bar"])
                    if b + h >= len(c):
                        continue
                    fr = float(c.iloc[b + h] / r["px"] - 1.0)
                    fwd.append(fr)
                    money.append(-fr * float(r["w"]) * POT_USDT)
                fwd = np.asarray(fwd, float)
                rec[f"fwd_{h}d"] = describe(fwd, f"{reason} fwd {h}d")
                rec[f"fwd_{h}d_good_share"] = float(np.mean(fwd < 0)) if fwd.size else None
                rec[f"saved_usdt_{h}d_total"] = float(np.sum(money))
                rec[f"saved_usdt_{h}d_mean"] = float(np.mean(money)) if money else None
            out[name]["by_reason"][reason] = rec

    # ------------------------------------------------ what the LATE exit cost, in money
    # The five worst BTC falls of docs/design/exit-and-horizon-2026-09-29.md, re-measured
    # as the NAV each book lost peak-to-trough on the same dates.
    falls = [("2021-11-08", "2022-11-21"), ("2021-04-13", "2021-07-20"),
             ("2025-10-06", "2026-06-22"), ("2025-01-20", "2025-04-08"),
             ("2024-03-13", "2024-09-06")]
    out["late_exit_cost"] = []
    for p0, p1 in falls:
        t0, t1 = pd.Timestamp(p0, tz="UTC"), pd.Timestamp(p1, tz="UTC")
        m = (idx >= t0) & (idx <= t1)
        if m.sum() < 5:
            continue
        row = {"peak": p0, "trough": p1, "days": int(m.sum()),
               "btc_fall": float(closes["BTC"].loc[t1] / closes["BTC"].loc[t0] - 1.0)}
        for name, bk in (("shipped", shipped), ("with_trim", trimmed)):
            seg = bk["net"][m]
            eq = np.cumprod(1.0 + seg)
            row[f"{name}_nav_change"] = float(eq[-1] - 1.0)
            row[f"{name}_worst_dd"] = float((eq / np.maximum.accumulate(eq) - 1.0).min())
        row["late_exit_cost_pct_nav"] = row["shipped_nav_change"] - row["with_trim_nav_change"]
        row["late_exit_cost_usdt_on_20k"] = row["late_exit_cost_pct_nav"] * POT_USDT
        out["late_exit_cost"].append(row)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1, default=float))
    print(f"wrote {args.out}\n")
    for name in ("shipped", "with_trim_counterfactual"):
        b = out[name]
        print(f"=== {name}: CAGR {b['book']['cagr']*100:.1f}% Sharpe "
              f"{b['book']['sharpe_arith']:.3f} MDD {b['book']['mdd']*100:.1f}% "
              f"avg gross {b['book']['avg_gross']:.3f} fees {b['book']['fee_drag_per_year']*100:.2f}%/yr")
        print(f"    counts {b['counts']}  sells/yr {b['sells_per_year']}")
        for reason, r in b["by_reason"].items():
            print(f"  {reason:<14} n={r['n']:<4} avg weight sold {r['mean_weight_sold']*100:5.1f}% "
                  f"trade P&L med {r['realised_pnl_pct'].get('median', float('nan'))*100:+6.2f}%")
            for h in HORIZONS:
                d = r[f"fwd_{h}d"]
                print(f"      +{h:>2}d after the sell: mean {d['mean']*100:+7.2f}% "
                      f"med {d['median']*100:+7.2f}%  avoided-a-fall "
                      f"{(r[f'fwd_{h}d_good_share'] or 0)*100:5.1f}%  "
                      f"net saved {r[f'saved_usdt_{h}d_total']:+9.0f} USDT on a 20k pot "
                      f"({r[f'saved_usdt_{h}d_mean']:+7.0f}/sell)")
        print()
    print("=== what the late exit cost, in money (20,000 USDT pot)")
    for r in out["late_exit_cost"]:
        print(f"  {r['peak']} -> {r['trough']}  BTC {r['btc_fall']*100:+6.1f}%  "
              f"shipped {r['shipped_nav_change']*100:+6.2f}%  with-trim "
              f"{r['with_trim_nav_change']*100:+6.2f}%  cost "
              f"{r['late_exit_cost_pct_nav']*100:+6.2f}% = "
              f"{r['late_exit_cost_usdt_on_20k']:+8.0f} USDT")


if __name__ == "__main__":
    main()
