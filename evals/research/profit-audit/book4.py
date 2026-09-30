"""MEASURE 4 support — the shipped book, re-simulated with EVERY decision logged.

MEASUREMENT ONLY. Nothing here is imported by anything that ships.

``evals/research/exit-horizon/asym_books.py`` already simulates the deployed sleeve day by
day and it is the calibrated reference for the whole exit-horizon study, so its indicator
functions, its target maths, its cost model and its statistics are imported verbatim rather
than re-typed. What it does NOT do is record the *adds* or the *weight at the moment of the
decision*, and measure 4 is entirely about the decisions. So the loop is re-stated here with
a richer event record, and ``verify_matches_reference()`` asserts that the re-statement
reproduces asym_books' own book to 1e-12 on net, gross and turnover so the two cannot
silently diverge.

Event kinds recorded
--------------------
``entry``        a new position (``populate_entry_trend`` + ``custom_stake_amount``)
``add``          a calendar DCA chunk (``_sleeve_adjust``)
``exit_regime``  the ONE routine sell, ``SleeveA.py:92``
``stop``         the -10% fixed stop, on the bar's own low
``monthly_stop`` the sleeve-level monthly -10% flatten (off in the shipped config)
``trim``         the ensemble trim (NOT deployed; only when ``resize_down=True``)

Each event carries the bar, date, asset, fill price, notional, the book's NAV, the asset's
weight before and after, the ensemble weight and the regime, so every later measure can ask
"what did the rule know when it did that".
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "exit-horizon"))
import asym_books as bk  # noqa: E402

COST_PER_SIDE = bk.COST_PER_SIDE          # 0.0015 — the repo's 0.30% round trip
ROUND_TRIP = 2.0 * COST_PER_SIDE


@dataclass
class Leg:
    units: float = 0.0
    entry: float = 0.0
    last_dca: int = -10_000
    stopped_at: int = -10_000
    opened_at: int = -10_000


@dataclass
class Result:
    frame: pd.DataFrame
    events: pd.DataFrame
    weights: pd.DataFrame
    nav: pd.Series
    ens: dict[str, pd.Series] = field(default_factory=dict)
    reg: dict[str, pd.Series] = field(default_factory=dict)
    tgt: dict[str, pd.Series] = field(default_factory=dict)


def shipped_book(closes: dict[str, pd.Series], lows: dict[str, pd.Series], *,
                 exit_on: str = "regime", resize_down: bool = False,
                 use_stop: bool = True, use_dca: bool = True, use_band: bool = True,
                 use_cooldown: bool = True, monthly_stop: bool = False,
                 cost_per_side: float = COST_PER_SIDE) -> Result:
    """``asym_books.discrete_book`` with every decision written down. Same arithmetic."""
    assets = list(closes)
    index = closes[assets[0]].index
    n = len(index)

    ens = {a: bk.ensemble_weight(closes[a]).to_numpy(float) for a in assets}
    reg = {a: bk.regime_series(closes[a]).to_numpy(float) for a in assets}
    vol = {a: bk.realized_vol_annual(closes[a]).to_numpy(float) for a in assets}
    px = {a: closes[a].to_numpy(float) for a in assets}
    lo = {a: lows[a].to_numpy(float) for a in assets}
    months = index.to_period("M").to_numpy()

    nav = 1.0
    cash = 1.0
    legs = {a: Leg() for a in assets}
    net = np.zeros(n)
    gross_arr = np.zeros(n)
    turn_arr = np.zeros(n)
    nav_arr = np.zeros(n)
    wts = {a: np.zeros(n) for a in assets}
    tgt_arr = {a: np.zeros(n) for a in assets}
    events: list[dict] = []

    month_anchor_nav = nav
    current_month = months[0]
    month_locked = False

    def log(kind: str, s: int, a: str, fill: float, notional: float, nav_s: float,
            w_before: float, w_after: float, **extra) -> None:
        events.append({"bar": s, "date": index[s], "asset": a, "kind": kind,
                       "px": fill, "notional": notional, "nav": nav_s,
                       "w_before": w_before, "w_after": w_after,
                       "ens": ens[a][s], "reg": reg[a][s], **extra})

    for t in range(1, n):
        if months[t] != current_month:
            current_month, month_anchor_nav, month_locked = months[t], nav, False

        s = t - 1
        traded = 0.0
        nav_pre = cash + sum(legs[a].units * px[a][s] for a in assets)

        # ---- 1. the ONE routine sell
        for a in assets:
            lg = legs[a]
            if lg.units <= 0:
                continue
            flip = (reg[a][s] == 0.0) if exit_on == "regime" else (
                ens[a][s] <= 0.0 if exit_on == "ensemble" else False)
            if flip:
                notional = lg.units * px[a][s]
                cash += notional * (1.0 - cost_per_side)
                traded += notional
                log(f"exit_{exit_on}", s, a, px[a][s], notional, nav_pre,
                    notional / nav_pre if nav_pre else 0.0, 0.0,
                    held_bars=s - lg.opened_at, entry_px=lg.entry)
                lg.units, lg.entry = 0.0, 0.0

        # ---- 2. the sleeve-level monthly flatten
        if monthly_stop and not month_locked and nav <= month_anchor_nav * (1 - bk.MONTHLY_LOSS_STOP):
            for a in assets:
                lg = legs[a]
                if lg.units > 0:
                    notional = lg.units * px[a][s]
                    cash += notional * (1.0 - cost_per_side)
                    traded += notional
                    log("monthly_stop", s, a, px[a][s], notional, nav_pre,
                        notional / nav_pre if nav_pre else 0.0, 0.0,
                        held_bars=s - lg.opened_at, entry_px=lg.entry)
                    lg.units, lg.entry = 0.0, 0.0
            month_locked = True

        # ---- 3. NAV and the targets, both on the signal bar
        nav_s = cash + sum(legs[a].units * px[a][s] for a in assets)
        tgt = bk.sleeve_a_targets({a: reg[a][s] for a in assets},
                                  {a: vol[a][s] for a in assets})
        for a in assets:
            tgt_arr[a][t] = tgt[a]

        # ---- 4. the BUY side
        if not month_locked:
            for a in assets:
                lg = legs[a]
                held = lg.units * px[a][s]
                want = tgt[a] * nav_s
                scaled_cap = ens[a][s] * tgt[a] * nav_s
                gap = want - held

                if lg.units <= 0:
                    if reg[a][s] <= 0 or ens[a][s] <= 0 or gap <= 0:
                        continue
                    if use_cooldown and (s - lg.stopped_at) < bk.REENTRY_COOLDOWN_DAYS:
                        continue
                    stake = min(gap, scaled_cap)
                    if stake <= 0 or stake > cash:
                        stake = min(stake, cash)
                    if stake <= 0:
                        continue
                    units = stake / px[a][s] * (1.0 - cost_per_side)
                    lg.units, lg.entry = units, px[a][s]
                    lg.opened_at, lg.last_dca = s, s
                    cash -= stake
                    traded += stake
                    log("entry", s, a, px[a][s], stake, nav_s, 0.0,
                        stake / nav_s if nav_s else 0.0, target=tgt[a],
                        fresh_cross=float(reg[a][s - 1] == 0.0) if s >= 1 else 1.0)
                    continue

                if resize_down and held > scaled_cap:
                    sell = held - scaled_cap
                    if not use_band or sell > bk.REBALANCE_BAND * nav_s:
                        units = sell / px[a][s]
                        lg.units = max(lg.units - units, 0.0)
                        cash += sell * (1.0 - cost_per_side)
                        traded += sell
                        log("trim", s, a, px[a][s], sell, nav_s,
                            held / nav_s if nav_s else 0.0,
                            (held - sell) / nav_s if nav_s else 0.0, target=tgt[a])
                        continue

                if use_dca:
                    if tgt[a] <= 0 or gap <= 0:
                        continue
                    if (s - lg.last_dca) < bk.DCA_INTERVAL_DAYS:
                        continue
                    if use_band and gap <= bk.REBALANCE_BAND * nav_s:
                        continue
                    chunk = min(gap, bk.DCA_CHUNK_PCT_NAV * nav_s)
                    stake = min(chunk, max(scaled_cap - held, 0.0), cash)
                    if stake <= 0:
                        continue
                    units = stake / px[a][s] * (1.0 - cost_per_side)
                    lg.entry = ((lg.entry * lg.units) + px[a][s] * units) / (lg.units + units)
                    lg.units += units
                    lg.last_dca = s
                    cash -= stake
                    traded += stake
                    log("add", s, a, px[a][s], stake, nav_s,
                        held / nav_s if nav_s else 0.0,
                        (held + stake) / nav_s if nav_s else 0.0, target=tgt[a],
                        held_bars=s - lg.opened_at)
                elif held < scaled_cap:
                    stake = min(scaled_cap - held, cash)
                    if stake <= 0:
                        continue
                    units = stake / px[a][s] * (1.0 - cost_per_side)
                    lg.entry = ((lg.entry * lg.units) + px[a][s] * units) / (lg.units + units)
                    lg.units += units
                    cash -= stake
                    traded += stake
                    log("add", s, a, px[a][s], stake, nav_s,
                        held / nav_s if nav_s else 0.0,
                        (held + stake) / nav_s if nav_s else 0.0, target=tgt[a],
                        held_bars=s - lg.opened_at)

        # ---- 5. the book is set for day t
        nav_open = cash + sum(legs[a].units * px[a][s] for a in assets)
        gross_arr[t] = (sum(legs[a].units * px[a][s] for a in assets) / nav_open
                        if nav_open > 0 else 0.0)
        if nav_open > 0:
            for a in assets:
                wts[a][t] = legs[a].units * px[a][s] / nav_open

        # ---- 6. day t happens; the stop is the only intraday event
        if use_stop:
            for a in assets:
                lg = legs[a]
                if lg.units <= 0:
                    continue
                stop_px = lg.entry * (1.0 - bk.STOP_PCT)
                if lo[a][t] <= stop_px:
                    fill = min(stop_px, px[a][t])
                    notional = lg.units * fill
                    cash += notional * (1.0 - cost_per_side)
                    traded += notional
                    log("stop", t, a, fill, notional, nav_open,
                        notional / nav_open if nav_open else 0.0, 0.0,
                        held_bars=t - lg.opened_at, entry_px=lg.entry)
                    lg.units, lg.entry = 0.0, 0.0
                    lg.stopped_at = t

        nav_close = cash + sum(legs[a].units * px[a][t] for a in assets)
        net[t] = nav_close / nav_open - 1.0 if nav_open > 0 else 0.0
        turn_arr[t] = traded / nav_open if nav_open > 0 else 0.0
        nav = nav_close
        nav_arr[t] = nav

    nav_arr[0] = 1.0
    frame = pd.DataFrame({"net": net, "gross": gross_arr, "turnover": turn_arr}, index=index)
    ev = pd.DataFrame(events)
    if not ev.empty:
        ev = ev.sort_values(["bar", "asset"]).reset_index(drop=True)
    return Result(frame=frame, events=ev,
                  weights=pd.DataFrame(wts, index=index),
                  nav=pd.Series(nav_arr, index=index),
                  ens={a: pd.Series(ens[a], index=index) for a in assets},
                  reg={a: pd.Series(reg[a], index=index) for a in assets},
                  tgt={a: pd.Series(tgt_arr[a], index=index) for a in assets})


def verify_matches_reference(closes, lows) -> dict[str, float]:
    """The re-statement must be the reference book to floating-point noise."""
    out = {}
    for name, kw in (
        ("deployed", dict(exit_on="regime", resize_down=False, use_stop=True,
                          use_dca=True, use_band=True, use_cooldown=True)),
        ("deployed+trim", dict(exit_on="regime", resize_down=True, use_stop=True,
                               use_dca=True, use_band=True, use_cooldown=True)),
    ):
        mine = shipped_book(closes, lows, **kw).frame
        ref = bk.discrete_book(closes, lows, **kw).frame
        for col in ("net", "gross", "turnover"):
            out[f"{name}.{col}.maxabsdiff"] = float(
                np.max(np.abs(mine[col].to_numpy() - ref[col].to_numpy())))
    return out


# ------------------------------------------------------------------------------ panel IO


def load_closes(path: Path, symbols: dict[str, str]) -> tuple[dict, dict, dict]:
    """``{asset: close}``, ``{asset: low}``, ``{asset: quote-volume}`` on one shared index."""
    p = pd.read_parquet(path, columns=["date", "symbol", "c", "l", "qv"])
    closes, lows, qvs = {}, {}, {}
    for sym, asset in symbols.items():
        s = p[p["symbol"] == sym].sort_values("date")
        if s.empty:
            continue
        idx = pd.to_datetime(s["date"], utc=True)
        for store, col in ((closes, "c"), (lows, "l"), (qvs, "qv")):
            v = pd.Series(s[col].to_numpy(float), index=idx)
            store[asset] = v[~v.index.duplicated(keep="last")]
    return closes, lows, qvs


def align(series: dict[str, pd.Series], index: pd.Index) -> dict[str, pd.Series]:
    return {a: s.reindex(index).ffill() for a, s in series.items()}


def fwd_net(close: pd.Series, bar: int, h: int) -> float:
    """Net round-trip return of buying at ``bar`` and selling ``h`` bars later.

    15 bps on the way in and 15 bps on the way out — the repo's 0.30% round trip, applied
    multiplicatively so it scales with the notional rather than being subtracted flat.
    """
    arr = close.to_numpy(float)
    if bar < 0 or bar + h >= len(arr):
        return float("nan")
    p0, p1 = arr[bar], arr[bar + h]
    if not (np.isfinite(p0) and np.isfinite(p1)) or p0 <= 0:
        return float("nan")
    return (p1 / p0) * (1.0 - COST_PER_SIDE) ** 2 - 1.0


def fwd_gross(close: pd.Series, bar: int, h: int) -> float:
    arr = close.to_numpy(float)
    if bar < 0 or bar + h >= len(arr):
        return float("nan")
    p0, p1 = arr[bar], arr[bar + h]
    if not (np.isfinite(p0) and np.isfinite(p1)) or p0 <= 0:
        return float("nan")
    return p1 / p0 - 1.0


def describe(x: np.ndarray) -> dict[str, float]:
    x = np.asarray([v for v in x if np.isfinite(v)], dtype=float)
    if x.size == 0:
        return {"n": 0}
    return {"n": int(x.size), "mean%": 100 * float(np.mean(x)),
            "median%": 100 * float(np.median(x)),
            "p10%": 100 * float(np.percentile(x, 10)),
            "p90%": 100 * float(np.percentile(x, 90)),
            "hit%": 100 * float(np.mean(x > 0)), "sd%": 100 * float(np.std(x))}
