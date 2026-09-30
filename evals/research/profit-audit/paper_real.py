"""MEASURE 4 — the REAL paper record: every fill the two sleeves have ever taken, and what
the price did after each entry and after each exit.

    ~/earn-dev/.venv/bin/python evals/research/profit-audit/paper_real.py \
        --db-dir /tmp/pa4/db --out /tmp/pa4/paper.json

Reads COPIES of the live freqtrade databases (never the originals, never a write) and pulls
1h klines from Binance's PUBLIC market-data endpoint ``/api/v3/klines`` — no key, no order
path, no account endpoint — so the forward windows extend past the feather store's last
refresh.

Two sleeves, two databases each:
  ``ft_userdata/<s>/runs/test-<s>-000.sqlite``  the 1h ``fast-test`` profile (SleeveFast)
  ``ft_userdata/<s>/tradesv3.sqlite``           the 4h shipped sleeve (SleeveA / SleeveB)

This is the only place in measure 4 where the exit reasons ``roi``, ``trailing_stop_loss``,
``force_exit`` and ``target_zero`` exist at all: the shipped 4h configuration switches every
one of them off, so a nine-year backtest of the shipped rules cannot produce them.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

COST_PER_SIDE = 0.0015
BASE = "https://api.binance.com/api/v3/klines"
HORIZONS_H = (1, 6, 24, 72, 168)


def klines(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """1h closes for ``symbol`` over [start, end]. Public endpoint, paged, polite."""
    rows: list[list] = []
    cur = start_ms
    while cur < end_ms:
        q = urllib.parse.urlencode({"symbol": symbol, "interval": "1h",
                                    "startTime": cur, "endTime": end_ms, "limit": 1000})
        with urllib.request.urlopen(f"{BASE}?{q}", timeout=20) as fh:
            batch = json.load(fh)
        if not batch:
            break
        rows.extend(batch)
        nxt = int(batch[-1][0]) + 3_600_000
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.15)
    if not rows:
        return pd.DataFrame(columns=["close"])
    df = pd.DataFrame({"open_ms": [int(r[0]) for r in rows],
                       "close": [float(r[4]) for r in rows]})
    df = df.drop_duplicates("open_ms").sort_values("open_ms").reset_index(drop=True)
    df.index = pd.to_datetime(df["open_ms"], unit="ms", utc=True)
    return df[["close"]]


def read_trades(path: Path, sleeve: str, book: str) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id,pair,is_open,open_date,close_date,open_rate,close_rate,amount,"
            "stake_amount,fee_open,fee_close,exit_reason,enter_tag,close_profit,"
            "close_profit_abs,max_rate,min_rate FROM trades ORDER BY open_date")]
    finally:
        con.close()
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["sleeve"], df["book"] = sleeve, book
    for c in ("open_date", "close_date"):
        df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
    return df


def fwd(px: pd.DataFrame, when: pd.Timestamp, h: int) -> float:
    """Gross % move from the hour containing ``when`` to ``h`` hours later."""
    if px.empty or pd.isna(when):
        return float("nan")
    i = px.index.searchsorted(when.floor("h"))
    j = i + h
    if i >= len(px) or j >= len(px):
        return float("nan")
    p0, p1 = float(px["close"].iloc[i]), float(px["close"].iloc[j])
    return p1 / p0 - 1.0 if p0 > 0 else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-dir", required=True, type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 60)

    frames = []
    for sleeve in ("a", "b"):
        for fname, book in ((f"run-{sleeve}.sqlite", "fast 1h"),
                            (f"tv3-{sleeve}.sqlite", "shipped 4h")):
            p = args.db_dir / fname
            if p.exists():
                frames.append(read_trades(p, sleeve, book))
    tr = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    print(f"# {len(tr)} real paper fills across both sleeves "
          f"({int((tr['is_open'] == 1).sum())} still open); "
          f"first {tr['open_date'].min()}, last {tr['open_date'].max()}")
    print(tr.groupby(["book", "sleeve"]).size().to_string())

    pairs = sorted(tr["pair"].unique())
    lo = int((tr["open_date"].min() - pd.Timedelta(days=2)).timestamp() * 1000)
    hi = int(time.time() * 1000)
    px = {}
    for pair in pairs:
        sym = pair.replace("/", "")
        px[pair] = klines(sym, lo, hi)
        print(f"  klines {sym}: {len(px[pair])} hourly bars "
              f"{px[pair].index.min()} -> {px[pair].index.max()}")

    rows = []
    for _, r in tr.iterrows():
        p = px[r["pair"]]
        row = {"book": r["book"], "sleeve": r["sleeve"], "pair": r["pair"],
               "in": str(r["open_date"])[:16], "out": str(r["close_date"])[:16],
               "reason": ("(open)" if pd.isna(r["exit_reason"]) or not r["exit_reason"]
                          else str(r["exit_reason"])), "tag": r["enter_tag"],
               "stake": round(float(r["stake_amount"]), 0),
               "held_h": (round((r["close_date"] - r["open_date"]).total_seconds() / 3600, 1)
                          if pd.notna(r["close_date"]) else np.nan),
               "net%": 100 * float(r["close_profit"]) if pd.notna(r["close_profit"]) else np.nan,
               "net$": float(r["close_profit_abs"]) if pd.notna(r["close_profit_abs"]) else np.nan}
        for h in HORIZONS_H:
            row[f"in+{h}h%"] = 100 * fwd(p, r["open_date"], h)
        for h in HORIZONS_H:
            row[f"out+{h}h%"] = 100 * fwd(p, r["close_date"], h)
        rows.append(row)
    df = pd.DataFrame(rows)

    print("\n# (d1) after every real ENTRY — the price move net of nothing (gross), so the "
          f"{100 * 2 * COST_PER_SIDE:.2f}% round trip is the bar each column must clear")
    print(df[["book", "sleeve", "pair", "in", "tag", "stake"]
             + [f"in+{h}h%" for h in HORIZONS_H]].round(2).to_string(index=False))

    print("\n# (d2) after every real EXIT — negative = the exit avoided a fall (good)")
    print(df[["book", "sleeve", "pair", "out", "reason", "held_h", "net%", "net$"]
             + [f"out+{h}h%" for h in HORIZONS_H]].round(2).to_string(index=False))

    print("\n# (d3) by exit reason, sleeve A and B pooled (they are near-duplicates by "
          "construction, so n is trades not independent observations)")
    summ = []
    for reason, sub in list(df[df["reason"] != "(open)"].groupby("reason")) + \
            [("ALL", df[df["reason"] != "(open)"])]:
        line = {"reason": reason, "n": len(sub), "mean net%": round(sub["net%"].mean(), 3),
                "total net$": round(sub["net$"].sum(), 2),
                "median held_h": round(sub["held_h"].median(), 1)}
        for h in HORIZONS_H:
            v = sub[f"out+{h}h%"].to_numpy(float)
            v = v[np.isfinite(v)]
            line[f"out+{h}h mean%"] = round(float(np.mean(v)), 2) if v.size else np.nan
            line[f"out+{h}h good%"] = round(100 * float(np.mean(v < 0)), 0) if v.size else np.nan
        summ.append(line)
    print(pd.DataFrame(summ).to_string(index=False))

    print("\n# (d4) the entry test: did the price beat the 0.30% round trip after entry?")
    for h in HORIZONS_H:
        v = df[f"in+{h}h%"].to_numpy(float)
        v = v[np.isfinite(v)]
        if not v.size:
            continue
        net = v / 100.0 - 2 * COST_PER_SIDE
        print(f"  +{h:3d}h: n={v.size:2d}  mean gross {np.mean(v):+.3f}%  "
              f"mean NET {100 * np.mean(net):+.3f}%  "
              f"share net-profitable {100 * np.mean(net > 0):.0f}%")

    print("\n# (d5) the money, which is the only number that settles it")
    for book, sub in df.groupby("book"):
        closed = sub[sub["reason"] != "(open)"]
        print(f"  {book}: {len(closed)} closed fills, realised "
              f"{closed['net$'].sum():+,.2f} USDT on a 10,000 USDT wallet per sleeve "
              f"({100 * closed['net$'].sum() / 20000:+.3f}% of the 20,000 simulated pot)")
    closed = df[df["reason"] != "(open)"]
    print(f"  BOTH sleeves, all books: {len(closed)} closed fills, "
          f"{closed['net$'].sum():+,.2f} USDT realised in total; "
          f"winners {int((closed['net$'] > 0).sum())}, "
          f"losers {int((closed['net$'] < 0).sum())}")

    if args.out:
        args.out.write_text(json.dumps(df.to_dict(orient="records"), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
