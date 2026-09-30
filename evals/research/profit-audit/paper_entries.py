"""MEASURE 4 (a)+(b) on the REAL paper trades — the entries and exits that actually
happened on this machine, 2026-09-24 .. 2026-09-30.

MEASUREMENT ONLY. evals/research/profit-audit/paper_entries.py.

Two facts the reader must hold on to. First, these trades were NOT taken by the rule
measured over 2019-2026: every one carries enter_tag ``fast_breakout``, which is
``strategies/SleeveFast.py`` on the 1h ``fast-test`` runtime profile, not
``strategies/SleeveA.py``'s 200d-MA regime entry. Second, the sample is 18 trades (9 per
sleeve) over about 29 waking hours across seven days, so nothing here is evidence about
anything; it is a check that the live path does what the code says.

Forward prices come from Binance's PUBLIC /api/v3/klines (no key, no trading endpoint,
read-only), because the local feathers stop at 2026-09-23 16:00Z — before paper trading
began. Forward horizons are +1h/+4h/+24h/+72h, not +1/+7/+30 days, because the newest exit
is hours old; the 30-day column cannot exist yet and is reported as absent rather than
guessed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

COST_ROUND_TRIP = 0.0030
HORIZONS_H = (1, 4, 24, 72)


def klines(symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float]]:
    """(open_time_ms, close) 1h bars from the public endpoint. Market data only."""
    out: list[tuple[int, float]] = []
    cur = start_ms
    while cur < end_ms:
        url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h"
               f"&startTime={cur}&limit=1000")
        with urllib.request.urlopen(url, timeout=30) as fh:
            rows = json.loads(fh.read())
        if not rows:
            break
        out.extend((int(r[0]), float(r[4])) for r in rows)
        cur = int(rows[-1][0]) + 3_600_000
        time.sleep(0.25)
        if len(rows) < 1000:
            break
    return [(t, c) for t, c in out if t <= end_ms]


def px_at(series: list[tuple[int, float]], ms: int) -> float | None:
    """Close of the 1h bar that contains ms, or the last bar at or before it."""
    best = None
    for t, c in series:
        if t <= ms:
            best = c
        else:
            break
    return best


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dbdir", default="/tmp/pa4db")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    trades = []
    for sleeve, stem in [(s, f"{k}-{s}-000.sqlite") for s in ("a", "b")
                         for k in ("test", "live")]:
        p = Path(args.dbdir) / stem
        if not p.exists():
            continue
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        for r in con.execute(
                "select id,pair,open_date,close_date,open_rate,close_rate,amount,"
                "stake_amount,fee_open,fee_close,close_profit,close_profit_abs,"
                "exit_reason,enter_tag,is_open from trades order by open_date"):
            d = dict(r)
            d["sleeve"] = sleeve
            d["db"] = stem
            trades.append(d)
        con.close()

    def ms(s: str | None) -> int | None:
        if not s:
            return None
        return int(datetime.fromisoformat(str(s)).replace(tzinfo=UTC).timestamp() * 1000)

    symbols = sorted({t["pair"].replace("/", "") for t in trades})
    lo = min(ms(t["open_date"]) for t in trades) - 6 * 3_600_000
    hi = int(time.time() * 1000)
    series = {}
    for s in symbols:
        series[s] = klines(s, lo, hi)
        print(f"  {s}: {len(series[s])} 1h bars "
              f"{datetime.fromtimestamp(series[s][0][0]/1000, UTC):%Y-%m-%d %H:%M} .. "
              f"{datetime.fromtimestamp(series[s][-1][0]/1000, UTC):%Y-%m-%d %H:%M}")

    out: dict = {"n_trades": len(trades), "note":
                 "enter_tag fast_breakout = SleeveFast on the 1h fast-test profile, NOT "
                 "SleeveA's 200d-MA regime entry. Horizons are HOURS, not days.",
                 "trades": [], "entry_summary": {}, "exit_summary": {}}

    for t in trades:
        sym = t["pair"].replace("/", "")
        s = series[sym]
        o_ms, c_ms = ms(t["open_date"]), ms(t["close_date"])
        rec = {k: t[k] for k in ("sleeve", "db", "id", "pair", "open_date", "close_date",
                                 "open_rate", "close_rate", "stake_amount", "close_profit",
                                 "close_profit_abs", "exit_reason", "enter_tag", "is_open")}
        # ENTRY quality: what the price did after the buy, net of the round trip
        for h in HORIZONS_H:
            p = px_at(s, o_ms + h * 3_600_000)
            rec[f"entry_fwd_{h}h"] = (p / float(t["open_rate"]) - 1.0 - COST_ROUND_TRIP
                                      if p else None)
        # ENTRY timing counterfactual: buying one bar later, and 24h later
        for lag in (1, 24):
            p0 = px_at(s, o_ms + lag * 3_600_000)
            rec[f"entry_lag{lag}h_fwd_24h"] = None
            if p0:
                p1 = px_at(s, o_ms + (lag + 24) * 3_600_000)
                if p1:
                    rec[f"entry_lag{lag}h_fwd_24h"] = p1 / p0 - 1.0 - COST_ROUND_TRIP
        # EXIT quality: what the price did after the sell (gross — the counterfactual of
        # simply not having sold)
        if c_ms and t["close_rate"]:
            for h in HORIZONS_H:
                p = px_at(s, c_ms + h * 3_600_000)
                rec[f"exit_fwd_{h}h"] = (p / float(t["close_rate"]) - 1.0) if p else None
            last = s[-1][1]
            rec["exit_fwd_to_now"] = last / float(t["close_rate"]) - 1.0
            # money: the stake that was sold times what it would have done
            rec["missed_usdt_to_now"] = (float(t["stake_amount"])
                                         * rec["exit_fwd_to_now"])
        out["trades"].append(rec)

    closed = [r for r in out["trades"] if not r["is_open"]]
    out["realised"] = {
        "n_closed": len(closed),
        "total_profit_abs": float(sum(r["close_profit_abs"] or 0 for r in closed)),
        "wins": int(sum(1 for r in closed if (r["close_profit_abs"] or 0) > 0)),
        "mean_profit_pct": float(np.mean([r["close_profit"] for r in closed])),
        "median_profit_pct": float(np.median([r["close_profit"] for r in closed])),
    }
    for h in HORIZONS_H:
        v = np.asarray([r[f"entry_fwd_{h}h"] for r in out["trades"]
                        if r.get(f"entry_fwd_{h}h") is not None], float)
        out["entry_summary"][f"{h}h"] = {
            "n": int(v.size), "mean": float(v.mean()), "median": float(np.median(v)),
            "hit": float((v > 0).mean())} if v.size else {"n": 0}
        e = np.asarray([r.get(f"exit_fwd_{h}h") for r in closed
                        if r.get(f"exit_fwd_{h}h") is not None], float)
        out["exit_summary"][f"{h}h"] = {
            "n": int(e.size), "mean": float(e.mean()), "median": float(np.median(e)),
            "avoided_a_fall": float((e < 0).mean())} if e.size else {"n": 0}

    by_reason: dict = {}
    for r in closed:
        by_reason.setdefault(r["exit_reason"], []).append(r)
    out["exit_by_reason"] = {}
    for reason, rows in by_reason.items():
        rec = {"n": len(rows),
               "realised_pnl_usdt": float(sum(x["close_profit_abs"] or 0 for x in rows))}
        for h in HORIZONS_H:
            v = np.asarray([x[f"exit_fwd_{h}h"] for x in rows
                            if x.get(f"exit_fwd_{h}h") is not None], float)
            rec[f"fwd_{h}h_mean"] = float(v.mean()) if v.size else None
            rec[f"fwd_{h}h_avoided_a_fall"] = float((v < 0).mean()) if v.size else None
        v = np.asarray([x["exit_fwd_to_now"] for x in rows
                        if x.get("exit_fwd_to_now") is not None], float)
        rec["fwd_to_now_mean"] = float(v.mean()) if v.size else None
        rec["missed_usdt_to_now"] = float(sum(x.get("missed_usdt_to_now") or 0
                                              for x in rows))
        out["exit_by_reason"][reason] = rec

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1, default=float))
    print(f"\nwrote {args.out}")
    print(f"\nrealised: {out['realised']}")
    print("\nentry, forward net of the 0.30% round trip:")
    for h, d in out["entry_summary"].items():
        if d.get("n"):
            print(f"  +{h:>3}: n {d['n']:>2} mean {d['mean']*100:+6.2f}% "
                  f"med {d['median']*100:+6.2f}% hit {d['hit']*100:5.1f}%")
    print("\nexit, forward GROSS (what the money would have done if not sold):")
    for h, d in out["exit_summary"].items():
        if d.get("n"):
            print(f"  +{h:>3}: n {d['n']:>2} mean {d['mean']*100:+6.2f}% "
                  f"med {d['median']*100:+6.2f}% avoided-a-fall {d['avoided_a_fall']*100:5.1f}%")
    print("\nby exit reason:")
    for reason, r in out["exit_by_reason"].items():
        print(f"  {reason:<20} n={r['n']} realised {r['realised_pnl_usdt']:+8.2f} USDT | "
              f"+24h {(r['fwd_24h_mean'] or 0)*100:+6.2f}% | to now "
              f"{(r['fwd_to_now_mean'] or 0)*100:+6.2f}% -> missed "
              f"{r['missed_usdt_to_now']:+8.2f} USDT")


if __name__ == "__main__":
    main()
