"""MEASURE 4 (c) — RALLY CAPTURE, and the CEILING the mandate imposes.

MEASUREMENT ONLY. evals/research/profit-audit/rally_capture.py.

Pre-registered before any number was computed
---------------------------------------------
H4f: the shipped book captures at least half of the mechanical ceiling in a BTC rally,
     where the ceiling is the same rally taken by a perfect-foresight book pinned at the
     shipped caps (BTC 0.40 / ETH 0.30 / satellites 0.05 gross, gross 0.80).
     FALSIFIER: median capture / ceiling capture < 0.50 across the BTC rallies.
H4g: the binding constraint on rally capture is the trend ensemble's entry gate, not the
     caps and not the 30% volatility target.
     FALSIFIER: removing the ensemble from the buy side raises average rally exposure by
     less than removing the volatility target does.

The rally rule, stated
----------------------
A **rally** is an up-swing on daily closes found by a 25% zig-zag filter and worth at
least +50%:

  1. walk the closes forward holding a running extreme and a direction;
  2. in an up-swing, a new high extends it; a close 25% below the running high ENDS it and
     starts a down-swing at that high;
  3. in a down-swing, a new low extends it; a close 25% above the running low ends it;
  4. keep every up-swing trough->peak worth >= +50%.

25% is the reversal threshold, not a fitted parameter: it is the retracement the crypto
literature uses to separate a correction from a trend break, and the study reports the same
table at 20% and 30% so the reader can see it is not load-bearing. Swings still open at the
end of the sample are reported but excluded from the medians, because their peak is unknown.

Capture
-------
For each rally on each asset, over the rally's calendar window:

  asset_return   close[peak] / close[trough] - 1
  book_return    the shipped book's NAV change over the SAME dates, net of costs
  capture        book_return / asset_return

and the miss is decomposed on the rally's own days:

  not_invested    share of rally days with zero weight in that asset
  avg_weight      mean weight in that asset while the rally ran
  cap             that asset's shipped weight cap
  exited_inside   an exit fired inside the rally window (and on which day of it)

Four books, so the reader can see WHICH constraint costs the rally money. Each one removes
exactly one thing and changes nothing else:

  shipped     regime gate + ensemble on the buy side + 30% vol target + caps
  no_ensemble regime gate + 30% vol target + caps  (the ensemble entry gate removed)
  no_voltgt   regime gate + ensemble + caps        (the volatility target removed)
  oracle      pinned at the caps on every asset that RISES over the next day, perfect
              foresight, gross <= 0.80 — the mandate's own ceiling, unbeatable long-only
              spot at these caps
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
    CORE,
    COST_PER_SIDE,
    ENTERABLE_SATELLITES,
    MAJOR_TIER,
    SAT_GROSS,
    SATELLITE_TIER,
    WHITELIST,
    arith_sharpe,
    cagr,
    ensemble_weight,
    load_panel,
    mdd,
    realized_vol_annual,
    regime_series,
    series_for,
    wide,
)

BASE_WEIGHTS = {"BTC": 0.40, "ETH": 0.30}
CAPS = {"BTC": 0.40, "ETH": 0.30}
VOL_TARGET, VOL_LOOKBACK = 0.30, 20
GROSS_CAP = 0.80
STOP_PCT = 0.10
DCA_INTERVAL_DAYS, DCA_CHUNK_PCT_NAV, REBALANCE_BAND = 7, 0.05, 0.05
POT_USDT = 20_000.0
START, END = "2019-01-01", "2026-09-24"


# ------------------------------------------------------------------ the rally rule


def zigzag_rallies(close: pd.Series, reversal: float = 0.25,
                   min_gain: float = 0.50) -> list[dict]:
    c = close.dropna()
    if len(c) < 30:
        return []
    v = c.to_numpy(float)
    dates = c.index
    swings: list[tuple[int, int, str]] = []
    direction = "up"
    anchor = 0
    ext = 0
    for t in range(1, len(v)):
        if direction == "up":
            if v[t] >= v[ext]:
                ext = t
            elif v[t] <= v[ext] * (1 - reversal):
                swings.append((anchor, ext, "up"))
                direction, anchor, ext = "down", ext, t
        else:
            if v[t] <= v[ext]:
                ext = t
            elif v[t] >= v[ext] * (1 + reversal):
                swings.append((anchor, ext, "down"))
                direction, anchor, ext = "up", ext, t
    swings.append((anchor, ext, direction))
    out = []
    for i, (a, b, d) in enumerate(swings):
        if d != "up" or b <= a:
            continue
        gain = v[b] / v[a] - 1.0
        if gain < min_gain:
            continue
        out.append({"trough": dates[a], "peak": dates[b], "gain": float(gain),
                    "days": int(b - a),
                    "open_at_end": bool(i == len(swings) - 1 and direction == "up")})
    return out


# ------------------------------------------------------------------ the four books


def targets(regime: dict, vols: dict, *, vol_target: bool) -> dict:
    raw = {a: (BASE_WEIGHTS[a] if regime.get(a, 0.0) > 0 else 0.0) for a in BASE_WEIGHTS}
    total = sum(w for w in raw.values() if w > 0)
    if total <= 0:
        return {a: 0.0 for a in raw}
    if not vol_target:
        return {a: float(min(w, CAPS[a])) for a, w in raw.items()}
    num = sum(w * float(vols.get(a, 0.0) or 0.0) for a, w in raw.items() if w > 0)
    bv = num / total
    if not np.isfinite(bv) or bv <= 0:
        return {a: 0.0 for a in raw}
    scale = min(VOL_TARGET / bv, 1.0)
    return {a: float(min(w * scale, CAPS[a])) for a, w in raw.items()}


def run(closes: dict, lows: dict, *, use_ensemble: bool = True,
        use_vol_target: bool = True) -> dict:
    """The shipped mechanics; the two flags remove exactly one constraint each."""
    assets = list(closes)
    index = closes[assets[0]].index
    n = len(index)
    ens = {a: (ensemble_weight(closes[a]).to_numpy(float) if use_ensemble
               else np.ones(n)) for a in assets}
    reg = {a: regime_series(closes[a]).to_numpy(float) for a in assets}
    vol = {a: realized_vol_annual(closes[a]).to_numpy(float) for a in assets}
    px = {a: closes[a].to_numpy(float) for a in assets}
    lo = {a: lows[a].to_numpy(float) for a in assets}

    cash = 1.0
    units = dict.fromkeys(assets, 0.0)
    entry = dict.fromkeys(assets, 0.0)
    last_dca = dict.fromkeys(assets, -10_000)
    stopped = dict.fromkeys(assets, -10_000)
    net = np.zeros(n)
    wts = {a: np.zeros(n) for a in assets}
    turn = np.zeros(n)
    exits: list[dict] = []

    for t in range(1, n):
        s = t - 1
        traded = 0.0
        nav_pre = cash + sum(units[a] * px[a][s] for a in assets)
        for a in assets:
            if units[a] > 0 and reg[a][s] == 0.0:
                notional = units[a] * px[a][s]
                cash += notional * (1.0 - COST_PER_SIDE)
                traded += notional
                exits.append({"bar": s, "asset": a, "kind": "exit_regime",
                              "w": notional / nav_pre if nav_pre > 0 else 0.0})
                units[a], entry[a] = 0.0, 0.0
        nav_s = cash + sum(units[a] * px[a][s] for a in assets)
        tgt = targets({a: reg[a][s] for a in assets}, {a: vol[a][s] for a in assets},
                      vol_target=use_vol_target)
        for a in assets:
            held = units[a] * px[a][s]
            want = tgt[a] * nav_s
            scaled = ens[a][s] * tgt[a] * nav_s
            gap = want - held
            if units[a] <= 0:
                if reg[a][s] <= 0 or ens[a][s] <= 0 or gap <= 0 or (s - stopped[a]) < 1:
                    continue
                stake = min(gap, scaled, cash)
                if stake <= 0:
                    continue
                u = stake / px[a][s] * (1.0 - COST_PER_SIDE)
                units[a], entry[a], last_dca[a] = u, px[a][s], s
                cash -= stake
                traded += stake
                continue
            if tgt[a] <= 0 or gap <= 0 or (s - last_dca[a]) < DCA_INTERVAL_DAYS:
                continue
            if gap <= REBALANCE_BAND * nav_s:
                continue
            stake = min(min(gap, DCA_CHUNK_PCT_NAV * nav_s), max(scaled - held, 0.0), cash)
            if stake <= 0:
                continue
            u = stake / px[a][s] * (1.0 - COST_PER_SIDE)
            entry[a] = ((entry[a] * units[a]) + px[a][s] * u) / (units[a] + u)
            units[a] += u
            last_dca[a] = s
            cash -= stake
            traded += stake
        nav_open = cash + sum(units[a] * px[a][s] for a in assets)
        if nav_open > 0:
            for a in assets:
                wts[a][t] = units[a] * px[a][s] / nav_open
        for a in assets:
            if units[a] <= 0:
                continue
            stop_px = entry[a] * (1.0 - STOP_PCT)
            if lo[a][t] <= stop_px:
                fill = min(stop_px, px[a][t])
                notional = units[a] * fill
                cash += notional * (1.0 - COST_PER_SIDE)
                traded += notional
                exits.append({"bar": t, "asset": a, "kind": "stop",
                              "w": notional / nav_open if nav_open > 0 else 0.0})
                units[a], entry[a] = 0.0, 0.0
                stopped[a] = t
        nav_close = cash + sum(units[a] * px[a][t] for a in assets)
        net[t] = nav_close / nav_open - 1.0 if nav_open > 0 else 0.0
        turn[t] = traded / nav_open if nav_open > 0 else 0.0
    return {"index": index, "net": net, "weights": pd.DataFrame(wts, index=index),
            "exits": exits, "turnover": turn}


def oracle(closes: dict, *, caps: dict, gross_cap: float = GROSS_CAP) -> dict:
    """Perfect foresight at the shipped caps: tomorrow's risers held at their cap today,
    gross never above ``gross_cap``, cash at 0%, the same 15 bps per side on every change.

    This is the MANDATE'S CEILING for a long-only spot book. Nothing that obeys the caps
    can beat it, because it is right about direction every single day.
    """
    assets = list(closes)
    index = closes[assets[0]].index
    n = len(index)
    px = {a: closes[a].to_numpy(float) for a in assets}
    w = {a: np.zeros(n) for a in assets}
    for t in range(n - 1):
        want = {}
        for a in assets:
            up = np.isfinite(px[a][t]) and np.isfinite(px[a][t + 1]) and px[a][t + 1] > px[a][t]
            want[a] = caps.get(a, 0.0) if up else 0.0
        tot = sum(want.values())
        if tot > gross_cap:                       # rank by tomorrow's return, fill to gross
            order = sorted(assets, key=lambda a: -(px[a][t + 1] / px[a][t] - 1.0)
                           if np.isfinite(px[a][t]) and px[a][t] > 0 else 0.0)
            room, want = gross_cap, dict.fromkeys(assets, 0.0)
            for a in order:
                take = min(caps.get(a, 0.0), room)
                if take <= 0:
                    continue
                want[a] = take
                room -= take
        for a in assets:
            w[a][t + 1] = want[a]
    net = np.zeros(n)
    for a in assets:
        r = pd.Series(px[a], index=index).pct_change().fillna(0.0).to_numpy(float)
        wa = w[a]
        ch = np.abs(np.diff(np.concatenate([[0.0], wa])))
        net += wa * np.nan_to_num(r) - ch * COST_PER_SIDE
    return {"index": index, "net": net, "weights": pd.DataFrame(w, index=index)}


def seg_return(net: np.ndarray, index: pd.DatetimeIndex, t0, t1) -> float:
    m = (index > t0) & (index <= t1)
    return float(np.prod(1.0 + net[m]) - 1.0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    panel = load_panel(args.panel)
    cl = {a: series_for(panel, a, "c") for a in CORE}
    lw = {a: series_for(panel, a, "l") for a in CORE}
    idx = cl["BTC"].index.intersection(cl["ETH"].index)
    idx = idx[idx <= pd.Timestamp(END, tz="UTC")]
    cl = {a: cl[a].reindex(idx).ffill() for a in CORE}
    lw = {a: lw[a].reindex(idx).ffill() for a in CORE}

    books = {
        "shipped": run(cl, lw, use_ensemble=True, use_vol_target=True),
        "no_ensemble": run(cl, lw, use_ensemble=False, use_vol_target=True),
        "no_voltgt": run(cl, lw, use_ensemble=True, use_vol_target=False),
    }
    books["oracle"] = oracle(cl, caps=CAPS)
    hold_btc = cl["BTC"].pct_change().fillna(0.0).to_numpy(float)

    win = (idx >= pd.Timestamp(START, tz="UTC"))
    out: dict = {
        "pre_registration": {
            "H4f": "shipped capture >= 0.50 x the caps-ceiling capture in BTC rallies; "
                   "falsifier: median ratio < 0.50",
            "H4g": "the ensemble entry gate binds harder than the 30% vol target; "
                   "falsifier: removing the vol target raises rally exposure more",
        },
        "rally_rule": "25% zig-zag on daily closes, up-swings of >= +50%; 20%/30% reported",
        "span": {"start": str(idx[0].date()), "end": str(idx[-1].date())},
        "books_full_span_2019_2026": {},
    }
    for name, bk in books.items():
        n = bk["net"][win]
        out["books_full_span_2019_2026"][name] = {
            "cagr": cagr(n), "sharpe_arith": arith_sharpe(n), "mdd": mdd(n),
            "avg_gross": float(bk["weights"].loc[win].sum(axis=1).mean()),
            "total_x": float(np.prod(1.0 + n)),
        }
    # THE BOOK-LEVEL CEILING, with no overlapping-window double counting: 0.40 of NAV in
    # BTC and 0.30 in ETH bought on the first day and never touched again, 0.30 in USDT at
    # 0%. No signal, no selling, no timing. A long-only spot book at the shipped caps
    # cannot beat this on a rising market, so it is the honest answer to "what is the most
    # we could have captured".
    w0 = {"BTC": CAPS["BTC"], "ETH": CAPS["ETH"]}
    first = np.where(win)[0][0]
    units0 = {a: w0[a] / float(cl[a].iloc[first]) for a in CORE}
    navs = np.array([sum(units0[a] * float(cl[a].iloc[t]) for a in CORE)
                     + (1.0 - sum(w0.values())) for t in range(first, len(idx))])
    navs = navs * (1.0 - 0.0015 * sum(w0.values()))      # the one entry, 15 bps a side
    caps_bh = np.concatenate([[0.0], navs[1:] / navs[:-1] - 1.0])
    out["books_full_span_2019_2026"]["caps_buy_and_hold_CEILING"] = {
        "cagr": cagr(caps_bh[1:]), "sharpe_arith": arith_sharpe(caps_bh[1:]),
        "mdd": mdd(caps_bh[1:]), "avg_gross": float(np.mean(
            [sum(units0[a] * float(cl[a].iloc[t]) for a in CORE) / navs[t - first]
             for t in range(first, len(idx))])),
        "total_x": float(np.prod(1.0 + caps_bh[1:])),
    }
    out["books_full_span_2019_2026"]["hold_btc"] = {
        "cagr": cagr(hold_btc[win]), "sharpe_arith": arith_sharpe(hold_btc[win]),
        "mdd": mdd(hold_btc[win]), "avg_gross": 1.0,
        "total_x": float(np.prod(1.0 + hold_btc[win])),
    }

    # ------------------------------------------------ rallies on the CORE assets
    out["core_rallies"] = {}
    for rev in (0.20, 0.25, 0.30):
        tab = []
        for a in CORE:
            for r in zigzag_rallies(cl[a].loc[START:], reversal=rev):
                t0, t1 = r["trough"], r["peak"]
                row = {"asset": a, "trough": str(t0.date()), "peak": str(t1.date()),
                       "days": r["days"], "asset_gain": r["gain"],
                       "open_at_end": r["open_at_end"]}
                for name, bk in books.items():
                    br = seg_return(bk["net"], bk["index"], t0, t1)
                    row[f"{name}_return"] = br
                    row[f"{name}_capture"] = br / r["gain"] if r["gain"] else None
                    m = (bk["index"] > t0) & (bk["index"] <= t1)
                    wa = bk["weights"][a].to_numpy(float)[m]
                    row[f"{name}_avg_w_{a}"] = float(wa.mean()) if wa.size else None
                    row[f"{name}_days_flat_{a}"] = (float(np.mean(wa <= 1e-9))
                                                    if wa.size else None)
                ex = [e for e in books["shipped"]["exits"]
                      if e["asset"] == a and t0 < books["shipped"]["index"][e["bar"]] <= t1]
                row["shipped_exits_inside"] = len(ex)
                row["shipped_exit_kinds"] = ",".join(sorted({e["kind"] for e in ex}))
                row["cap"] = CAPS[a]
                # THE ACHIEVABLE CEILING, and the one the owner's question is about: bought
                # at the trough at the full cap, held to the peak, the rest of the book in
                # USDT at 0%, one round trip paid. A long-only spot book that holds w of NAV
                # through a rally of R ends at 1 + w*R, so its capture ratio IS w. The cap
                # is the ceiling; no signal, however good, can raise it.
                row["cap_pinned_return"] = CAPS[a] * r["gain"] - 0.0030 * CAPS[a]
                row["cap_pinned_capture"] = row["cap_pinned_return"] / r["gain"]
                # both legs pinned at once — the real book can hold BTC and ETH together
                both = (CAPS["BTC"] * (float(cl["BTC"].loc[t1] / cl["BTC"].loc[t0]) - 1.0)
                        + CAPS["ETH"] * (float(cl["ETH"].loc[t1] / cl["ETH"].loc[t0]) - 1.0)
                        - 0.0030 * (CAPS["BTC"] + CAPS["ETH"]))
                row["both_legs_pinned_return"] = both
                row["both_legs_pinned_capture"] = both / r["gain"] if r["gain"] else None
                row["shipped_over_ceiling"] = (row["shipped_return"]
                                               / row["cap_pinned_return"]
                                               if row["cap_pinned_return"] else None)
                row["missed_usdt_on_20k_vs_ceiling"] = (
                    (row["cap_pinned_return"] - row["shipped_return"]) * POT_USDT)
                row["missed_usdt_on_20k_vs_daily_foresight"] = (
                    (row["oracle_return"] - row["shipped_return"]) * POT_USDT)
                tab.append(row)
        out["core_rallies"][f"rev{int(rev*100)}"] = tab

    # ------------------------------------------------ rallies across the whole whitelist
    allc = wide(panel, WHITELIST).loc[:END]
    reach = {}
    for a in allc.columns:
        if a in CORE:
            reach[a] = "core, cap 0.40/0.30"
        elif a in ENTERABLE_SATELLITES:
            reach[a] = "satellite, cap 0.05, 2 seats inside 5% gross"
        elif a in MAJOR_TIER:
            reach[a] = "UNREACHABLE: major tier carries a 0.15 cap but SleeveA._satellites " \
                       "only selects is_satellite assets, so it can never take a seat"
        elif a in SATELLITE_TIER:
            reach[a] = "UNREACHABLE: rendered exit_only by satellite_eligibility"
        else:
            reach[a] = "not whitelisted"
    alt_rows = []
    for a in allc.columns:
        s = allc[a].loc[START:].dropna()
        for r in zigzag_rallies(s, reversal=0.25):
            t0, t1 = r["trough"], r["peak"]
            bk = books["shipped"]
            alt_rows.append({
                "asset": a, "reachable": reach[a], "trough": str(t0.date()),
                "peak": str(t1.date()), "days": r["days"], "asset_gain": r["gain"],
                "open_at_end": r["open_at_end"],
                "shipped_book_return_same_window": seg_return(bk["net"], bk["index"], t0, t1),
                "max_possible_contribution_pct_nav": (
                    CAPS[a] * r["gain"] if a in CORE else
                    (0.025 * r["gain"] if a in ENTERABLE_SATELLITES else 0.0)),
            })
    out["whitelist_rallies_rev25"] = alt_rows
    out["reachability"] = reach

    # ------------------------------------------------ the ceiling, stated three ways
    oc = books["oracle"]
    sh = books["shipped"]
    out["ceiling"] = {
        "single_asset_rally_capture_is_the_cap": {
            "BTC": CAPS["BTC"], "ETH": CAPS["ETH"], "satellite_each": 0.025,
            "why": "a long-only spot book holding w of NAV in the rallying asset and the "
                   "rest in USDT at 0% ends a rally of R at 1 + w*R, so its capture ratio "
                   "IS w. The cap is the capture ceiling; no signal can raise it.",
        },
        "both_legs_rally_together": {
            "reachable_gross_BTC_plus_ETH": CAPS["BTC"] + CAPS["ETH"],
            "with_satellites_funded_out_of_core": round(
                CAPS["BTC"] * (1 - SAT_GROSS) + CAPS["ETH"] * (1 - SAT_GROSS) + SAT_GROSS, 4),
            "gross_cap": GROSS_CAP,
            "note": "gross_cap 0.80 is NOT reachable: the only assets with a cap above the "
                    "satellite 0.05 are BTC and ETH, and 0.40 + 0.30 = 0.70. The USDT "
                    "floor of 0.20 is therefore never the binding constraint either.",
        },
        "oracle_vs_shipped_full_span": {
            "oracle_cagr": out["books_full_span_2019_2026"]["oracle"]["cagr"],
            "shipped_cagr": out["books_full_span_2019_2026"]["shipped"]["cagr"],
            "hold_btc_cagr": out["books_full_span_2019_2026"]["hold_btc"]["cagr"],
            "oracle_avg_gross": out["books_full_span_2019_2026"]["oracle"]["avg_gross"],
            "shipped_avg_gross": out["books_full_span_2019_2026"]["shipped"]["avg_gross"],
        },
    }
    # H4g: which constraint costs the most exposure?
    out["constraint_decomposition_avg_gross"] = {
        "shipped": float(sh["weights"].loc[win].sum(axis=1).mean()),
        "no_ensemble": float(books["no_ensemble"]["weights"].loc[win].sum(axis=1).mean()),
        "no_voltgt": float(books["no_voltgt"]["weights"].loc[win].sum(axis=1).mean()),
        "caps_only_oracle": float(oc["weights"].loc[win].sum(axis=1).mean()),
        "caps_sum": CAPS["BTC"] + CAPS["ETH"],
    }

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1, default=float))
    print(f"wrote {args.out}\n")

    print("=== full span 2019-01-01 .. 2026-09-24")
    for name, b in out["books_full_span_2019_2026"].items():
        print(f"  {name:<14} CAGR {b['cagr']*100:+7.2f}%  Sharpe {b['sharpe_arith']:6.3f}  "
              f"MDD {b['mdd']*100:+7.2f}%  avg gross {b['avg_gross']:.3f}  "
              f"x{b['total_x']:.2f}")
    print("\n=== average gross exposure: which constraint binds")
    for k, v in out["constraint_decomposition_avg_gross"].items():
        print(f"  {k:<18} {v:.3f}")
    print("\n=== BTC / ETH rallies >= +50%, 25% zig-zag. CEILING = pinned at the cap "
          "for the whole rally")
    print(f"  {'asset':<5}{'trough':<12}{'peak':<12}{'d':>4}{'gain':>9}"
          f"{'shipped':>9}{'capt':>7}{'ceilRet':>9}{'ceilCapt':>9}{'sh/ceil':>8}"
          f"{'avgW':>7}{'flat%':>7}{'ex':>4}{'missUSDT':>10}")
    for r in out["core_rallies"]["rev25"]:
        a = r["asset"]
        print(f"  {a:<5}{r['trough']:<12}{r['peak']:<12}{r['days']:>4}"
              f"{r['asset_gain']*100:>8.1f}%{r['shipped_return']*100:>8.1f}%"
              f"{r['shipped_capture']:>7.2f}{r['cap_pinned_return']*100:>8.1f}%"
              f"{r['cap_pinned_capture']:>9.2f}{(r['shipped_over_ceiling'] or 0):>8.2f}"
              f"{(r[f'shipped_avg_w_{a}'] or 0):>7.3f}"
              f"{(r[f'shipped_days_flat_{a}'] or 0)*100:>6.1f}%"
              f"{r['shipped_exits_inside']:>4}"
              f"{r['missed_usdt_on_20k_vs_ceiling']:>10.0f}")
    done = [r for r in out["core_rallies"]["rev25"] if not r["open_at_end"]]
    caps = [r["shipped_capture"] for r in done]
    ceil = [r["cap_pinned_capture"] for r in done]
    ratio = [r["shipped_over_ceiling"] for r in done]
    miss = [r["missed_usdt_on_20k_vs_ceiling"] for r in done]
    fore = [r["oracle_capture"] for r in done]
    print(f"\n  n {len(done)}   median shipped capture {np.median(caps):.3f}   "
          f"median ACHIEVABLE ceiling capture {np.median(ceil):.3f}   "
          f"median shipped/ceiling {np.median(ratio):.3f}")
    print(f"  total left on the table vs the ceiling, summed over all "
          f"{len(done)} rallies, on a 20,000 USDT pot: {np.sum(miss):,.0f} USDT")
    print(f"  (perfect DAILY foresight at the same caps would capture "
          f"{np.median(fore):.2f}x the rally — it is an upper bound of no practical "
          f"interest and is reported only so nobody mistakes it for the ceiling)")
    for rev in (20, 30):
        t = [r for r in out["core_rallies"][f"rev{rev}"] if not r["open_at_end"]]
        c = [r["shipped_capture"] for r in t]
        o = [r["cap_pinned_capture"] for r in t]
        rr = [r["shipped_over_ceiling"] for r in t]
        print(f"  sensitivity, {rev}% zig-zag: n {len(c)} median capture "
              f"{np.median(c):.3f} vs ceiling {np.median(o):.3f} -> "
              f"{np.median(rr):.3f} of the ceiling")
    print("\n  where the misses come from, pooled over the 21 rallies:")
    flat = [r[f"shipped_days_flat_{r['asset']}"] for r in done]
    avgw = [(r[f"shipped_avg_w_{r['asset']}"] or 0) / r["cap"] for r in done]
    nex = [r["shipped_exits_inside"] for r in done]
    print(f"    not invested at all:      median {np.median(flat)*100:.1f}% of the "
          f"rally's days (mean {np.mean(flat)*100:.1f}%)")
    print(f"    size while invested:      median {np.median(avgw)*100:.1f}% of the cap")
    print(f"    exited INSIDE the rally:  {sum(1 for x in nex if x):.0f} of "
          f"{len(done)} rallies, {sum(nex)} sells in total")

    print("\n=== whitelist rallies >= +50% (25% zig-zag), by reachability")
    rows = out["whitelist_rallies_rev25"]
    for key in sorted({r["reachable"].split(":")[0].split(",")[0] for r in rows}):
        sub = [r for r in rows if r["reachable"].startswith(key)]
        tot = sum(r["max_possible_contribution_pct_nav"] for r in sub)
        print(f"  {key:<14} rallies {len(sub):>4}  median gain "
              f"{np.median([r['asset_gain'] for r in sub])*100:6.1f}%  "
              f"max possible NAV contribution across ALL of them {tot*100:8.1f}%")


if __name__ == "__main__":
    main()
