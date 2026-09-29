"""MEASURE 1 — the entry/exit asymmetry. Is the live book the book that was backtested?

    ~/earn-dev/.venv/bin/python evals/research/exit-horizon/measure.py \
        --panel ~/earn-panels/panel_1d.parquet

Prints, in order:
  0. a calibration check — Book A against ``docs/design/dip-strategy.md`` §0.3's own table
  1. the book ladder, full window and per regime
  2. the decomposition of the A→B gap into re-sizing, exit rule and fees
  3. the five worst BTC drawdowns, with each book's exposure into and out of them
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asym_books as bk  # noqa: E402

WINDOW = ("2017-08-17", "2026-09-24")
REGIMES = {
    "2019-22": ("2019-01-01", "2022-12-31"),
    "2023-24": ("2023-01-01", "2024-12-31"),
    "2025-26": ("2025-01-01", "2026-09-24"),
    "full": WINDOW,
}


def load_panel(path: Path) -> tuple[dict[str, pd.Series], dict[str, pd.Series]]:
    p = pd.read_parquet(path, columns=["date", "symbol", "c", "l"])
    closes: dict[str, pd.Series] = {}
    lows: dict[str, pd.Series] = {}
    for sym, asset in (("BTCUSDT", "BTC"), ("ETHUSDT", "ETH")):
        s = p[p["symbol"] == sym].sort_values("date")
        idx = pd.to_datetime(s["date"], utc=True)
        c = pd.Series(s["c"].to_numpy(float), index=idx)
        low = pd.Series(s["l"].to_numpy(float), index=idx)
        closes[asset] = c[~c.index.duplicated(keep="last")]
        lows[asset] = low[~low.index.duplicated(keep="last")]
    idx = closes["BTC"].index
    for a in ("BTC", "ETH"):
        closes[a] = closes[a].reindex(idx).ffill()
        lows[a] = lows[a].reindex(idx).ffill()
    return closes, lows


def cut(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    m = (frame.index >= pd.Timestamp(start, tz="UTC")) & (frame.index <= pd.Timestamp(end, tz="UTC"))
    return frame[m]


def row(name: str, frame: pd.DataFrame) -> dict:
    s = bk.stats(frame["net"].to_numpy(float), frame["gross"].to_numpy(float),
                 frame["turnover"].to_numpy(float))
    return {"book": name, "days": s.days, "CAGR%": 100 * s.cagr, "vol%": 100 * s.vol,
            "Sh_arith": s.sharpe_arith, "Sh_geom": s.sharpe_geom, "MaxDD%": 100 * s.mdd,
            "gross": s.gross, "TiM%": 100 * s.tim, "turn/yr": s.turnover_per_year,
            "fee%/yr": 100 * s.fee_drag_per_year, "total%": 100 * s.total}


def build(closes, lows) -> tuple[dict[str, pd.DataFrame], dict[str, object]]:
    """Every book in the ladder, plus the raw discrete results for the drawdown study."""
    idx = closes["BTC"].index
    e = {a: bk.ensemble_weight(closes[a]) for a in closes}
    reg = {a: bk.regime_series(closes[a]) for a in closes}
    vol = {a: bk.realized_vol_annual(closes[a]) for a in closes}

    # the shipped target weights, day by day (regime gate + book vol targeting + caps)
    tg = {a: np.zeros(len(idx)) for a in closes}
    for i in range(len(idx)):
        t = bk.sleeve_a_targets({a: reg[a].iloc[i] for a in closes},
                                {a: vol[a].iloc[i] for a in closes})
        for a in closes:
            tg[a][i] = t[a]
    tgt = {a: pd.Series(tg[a], index=idx) for a in closes}

    frames: dict[str, pd.DataFrame] = {}
    frames["0 BTC hold"] = bk.continuous_book(
        {"BTC": closes["BTC"]}, {"BTC": pd.Series(1.0, index=idx)})
    frames["A doc (0.5/0.5 x ens, daily)"] = bk.continuous_book(
        closes, {"BTC": 0.5 * e["BTC"], "ETH": 0.5 * e["ETH"]})
    frames["A' shipped targets x ens, daily"] = bk.continuous_book(
        closes, {"BTC": bk.BASE_WEIGHTS["BTC"] * e["BTC"],
                 "ETH": bk.BASE_WEIGHTS["ETH"] * e["ETH"]})
    frames["A'' + regime gate + vol target, daily"] = bk.continuous_book(
        closes, {a: tgt[a] * e[a] for a in closes})

    discrete: dict[str, object] = {}
    # C1 — same targets, but held: no trim when members switch off. Exit on the ensemble.
    discrete["C1 held, exit on ensemble=0"] = bk.discrete_book(
        closes, lows, exit_on="ensemble", resize_down=False, use_stop=False,
        use_dca=False, use_band=False, use_cooldown=False)
    # C2 — the shipped exit rule replaces the ensemble exit. Still no stop, no DCA cadence.
    discrete["C2 held, exit on MA200 regime"] = bk.discrete_book(
        closes, lows, exit_on="regime", resize_down=False, use_stop=False,
        use_dca=False, use_band=False, use_cooldown=False)
    # B — as deployed: MA200 exit, -10% stop, 7d/5% DCA, 5% band, 24h re-entry cooldown.
    discrete["B AS DEPLOYED"] = bk.discrete_book(
        closes, lows, exit_on="regime", resize_down=False, use_stop=True,
        use_dca=True, use_band=True, use_cooldown=True)
    # B+ — the same, with the sleeve-level monthly -10% flatten switched on.
    discrete["B+ deployed + monthly -10% flatten"] = bk.discrete_book(
        closes, lows, exit_on="regime", resize_down=False, use_stop=True,
        use_dca=True, use_band=True, use_cooldown=True, monthly_stop=True)
    # D — the one-line fix: the deployed book, but allowed to trim to the ensemble weight.
    discrete["D deployed + trim to ensemble"] = bk.discrete_book(
        closes, lows, exit_on="regime", resize_down=True, use_stop=True,
        use_dca=True, use_band=True, use_cooldown=True)
    for k, v in discrete.items():
        frames[k] = v.frame
    return frames, discrete


def worst_btc_drawdowns(closes, frames, k=5) -> pd.DataFrame:
    """The k deepest peak-to-trough BTC drawdowns, and what each book carried through them."""
    c = closes["BTC"]
    peak = c.cummax()
    dd = c / peak - 1.0
    # split into episodes: a new episode starts each time BTC makes a new all-time high
    at_high = c >= peak * (1 - 1e-12)
    ep = at_high.cumsum()
    rows = []
    for _, g in dd.groupby(ep):
        if len(g) < 5:
            continue
        trough_i = g.idxmin()
        rows.append({"peak": g.index[0], "trough": trough_i, "depth": float(g.min()),
                     "end": g.index[-1]})
    eps = sorted(rows, key=lambda r: r["depth"])[:k]
    out = []
    for r in eps:
        rec = {"peak": r["peak"].date(), "trough": r["trough"].date(),
               "BTC_dd%": 100 * r["depth"],
               "days_p2t": int((r["trough"] - r["peak"]).days)}
        for name in ("A doc (0.5/0.5 x ens, daily)", "A'' + regime gate + vol target, daily",
                     "B AS DEPLOYED"):
            f = frames[name]
            seg = f[(f.index >= r["peak"]) & (f.index <= r["trough"])]
            if len(seg) == 0:
                continue
            g = seg["gross"]
            short = {"A doc (0.5/0.5 x ens, daily)": "A",
                     "A'' + regime gate + vol target, daily": "A''",
                     "B AS DEPLOYED": "B"}[name]
            eq = np.cumprod(1.0 + seg["net"].to_numpy(float))
            rec[f"{short}_g@peak"] = float(g.iloc[0])
            rec[f"{short}_g_min"] = float(g.min())
            # days from the peak until the book is below a tenth of its peak-day gross
            flat = np.where(g.to_numpy(float) <= max(0.1 * g.iloc[0], 1e-9))[0]
            rec[f"{short}_days_to_flat"] = int(flat[0]) if len(flat) else -1
            rec[f"{short}_loss%"] = 100 * float(eq[-1] - 1.0)
        out.append(rec)
    return pd.DataFrame(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", type=Path, required=True)
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()

    closes, lows = load_panel(args.panel)

    # ---- 0. calibration against runs/features/trend.py and the document -----------------
    try:
        repo = Path(__file__).resolve().parents[3]
        sys.path.insert(0, str(repo))
        from runs.features import trend as prod_trend
        mine = bk.ensemble_weight(closes["BTC"])
        theirs = prod_trend.ensemble_weight(closes["BTC"])
        agree = float(np.nanmax(np.abs(mine.to_numpy() - theirs.to_numpy())))
        print(f"[calib] ensemble weight vs runs/features/trend.py: max |diff| = {agree:.2e}")
    except Exception as exc:  # noqa: BLE001
        print(f"[calib] could not import runs.features.trend ({exc})")

    frames, discrete = build(closes, lows)

    pd.set_option("display.width", 260)
    fmt = lambda x: f"{x:9.2f}"  # noqa: E731

    print("\n" + "=" * 130)
    print("0. CALIBRATION — Book A against dip-strategy.md §0.3 (window "
          f"{WINDOW[0]} -> {WINDOW[1]}, 15 bps/side, lag 1d, cash 0%)")
    print("=" * 130)
    doc = {"0 BTC hold": (38.6, 66.9, 0.58, -83.2, 1.00),
           "A doc (0.5/0.5 x ens, daily)": (43.1, 39.9, 1.08, -45.6, 0.45)}
    cal = []
    for name, d in doc.items():
        r = row(name, cut(frames[name], *WINDOW))
        cal.append({**r, "doc_CAGR%": d[0], "doc_vol%": d[1], "doc_Sh_geom": d[2],
                    "doc_MaxDD%": d[3], "doc_gross": d[4]})
    print(pd.DataFrame(cal)[["book", "days", "CAGR%", "doc_CAGR%", "vol%", "doc_vol%",
                             "Sh_geom", "doc_Sh_geom", "Sh_arith", "MaxDD%", "doc_MaxDD%",
                             "gross", "doc_gross"]].to_string(index=False, float_format=fmt))

    print("\n" + "=" * 130)
    print("1. THE LADDER — full window")
    print("=" * 130)
    full = pd.DataFrame([row(n, cut(f, *WINDOW)) for n, f in frames.items()])
    print(full.to_string(index=False, float_format=fmt))

    for label, (s, e_) in REGIMES.items():
        if label == "full":
            continue
        print(f"\n--- regime {label} ({s} -> {e_}) ---")
        tab = pd.DataFrame([row(n, cut(f, s, e_)) for n, f in frames.items()])
        print(tab[["book", "days", "CAGR%", "vol%", "Sh_arith", "MaxDD%", "gross",
                   "TiM%", "turn/yr", "fee%/yr"]].to_string(index=False, float_format=fmt))

    print("\n" + "=" * 130)
    print("2. DECOMPOSITION of the A''->B gap (full window, CAGR pp and MaxDD pp)")
    print("=" * 130)
    steps = [
        ("A'' daily rebalance to ens x target", "A'' + regime gate + vol target, daily"),
        ("  + stop re-sizing while held  -> C1", "C1 held, exit on ensemble=0"),
        ("  + exit on MA200 not ensemble -> C2", "C2 held, exit on MA200 regime"),
        ("  + stop/DCA/band/cooldown     -> B", "B AS DEPLOYED"),
    ]
    prev = None
    dec = []
    for lab, key in steps:
        r = row(key, cut(frames[key], *WINDOW))
        d = {"step": lab, "CAGR%": r["CAGR%"], "Sh_arith": r["Sh_arith"],
             "MaxDD%": r["MaxDD%"], "gross": r["gross"], "fee%/yr": r["fee%/yr"],
             "turn/yr": r["turn/yr"]}
        if prev is not None:
            d["dCAGR_pp"] = r["CAGR%"] - prev["CAGR%"]
            d["dMaxDD_pp"] = r["MaxDD%"] - prev["MaxDD%"]
            d["dSharpe"] = r["Sh_arith"] - prev["Sh_arith"]
        prev = r
        dec.append(d)
    print(pd.DataFrame(dec).to_string(index=False, float_format=fmt))

    print("\n" + "=" * 130)
    print("3. THE OWNER'S QUESTION — the five worst BTC drawdowns: who sold, and how late")
    print("=" * 130)
    dds = worst_btc_drawdowns(closes, frames, k=5)
    print(dds.to_string(index=False, float_format=fmt))

    print("\n--- trade counts, Book B as deployed (full window) ---")
    for a, lg in discrete["B AS DEPLOYED"].legs.items():
        print(f"  {a}: {lg.trades} entries, {lg.adds} DCA adds, {lg.exits} exits "
              f"({lg.stops} of them stop-outs)")
    ev = pd.DataFrame(discrete["B AS DEPLOYED"].events)
    if len(ev):
        print("  event mix:", ev["kind"].value_counts().to_dict())

    if args.json_out:
        args.json_out.write_text(json.dumps({
            "full": full.to_dict("records"),
            "decomposition": pd.DataFrame(dec).to_dict("records"),
            "drawdowns": dds.to_dict("records"),
        }, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
