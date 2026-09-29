"""The costed daily backtest of the trend ensemble, and the reproduction of its headline.

TIER 2 (``evals/**``). pandas/numpy plus ``runs.features.trend``; no docker, no freqtrade,
no network. This is the arithmetic of ``docs/design/dip-strategy.md`` §0.3 written once so
the document's headline can be re-derived on demand::

    ~/earn-dev/.venv/bin/python -m evals.trend_ensemble_backtest --panel ~/earn-panels/panel_1d.parquet
    ~/earn-dev/.venv/bin/python -m evals.trend_ensemble_backtest --data-root ~/earn-run/data

Conventions, all the study's (``~/dp3/inc.py``), none of them tunable from here:

* one book, long-only spot, gross never above 1, cash earns 0%;
* the BTC/ETH book holds ``0.5 × weight`` of each asset (equal split of the core);
* every weight is **lagged one full day**: the ensemble on the close of day *t−1* is the
  position over day *t* — :func:`runs.features.trend.applied_weight`;
* **15 bps per side on every weight change** (:data:`COST_PER_SIDE`), charged on the
  absolute change in the lagged weight, so the position carried INTO a window is free and
  every rebalance inside it is paid for;
* ``CAGR = Π(1+r)^(365/n) − 1``, ``vol = σ(r)·√365`` (population σ), ``Sharpe = CAGR / vol``,
  ``MaxDD`` from the compounded equity curve.

What this does not do: no intrabar path, no stops, no slippage beyond the flat 15 bps, no
market impact — §7 of the doc lists every one of those limits, and every number here moves
down once real execution applies (§10.2 item 5).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from runs.features import trend

__all__ = [
    "COST_PER_SIDE",
    "DOC_HEADLINE",
    "DOC_WINDOW",
    "BookStats",
    "book_returns",
    "headline_books",
    "load_feather_closes",
    "load_panel_closes",
    "reproduction_table",
    "stats",
]

#: 15 bps per side (§0.3). The TCA-measured round trip the whole repo assumes is 0.30%.
COST_PER_SIDE = 0.0015
ANNUAL_DAYS = 365.0

#: The document's headline window (§0.3): 3,326 days, 9.11 years.
DOC_WINDOW = ("2017-08-17", "2026-09-24")

#: §0.3, verbatim, so a reproduction prints the doc beside itself. Percent units.
DOC_HEADLINE: dict[str, dict[str, float]] = {
    "BTC hold": {"cagr": 38.6, "vol": 66.9, "sharpe": 0.58, "mdd": -83.2, "gross": 1.00},
    "BTC trend ensemble": {"cagr": 39.5, "vol": 37.5, "sharpe": 1.05, "mdd": -44.5,
                           "gross": 0.46},
    "BTC/ETH trend ensemble": {"cagr": 43.1, "vol": 39.9, "sharpe": 1.08, "mdd": -45.6,
                               "gross": 0.45},
}


@dataclass(frozen=True)
class BookStats:
    cagr: float
    vol: float
    #: GEOMETRIC ratio, ``cagr / vol`` — what dip-strategy.md §0.3's table quotes. Kept so
    #: the reproduction can be checked against the document line for line, but it is NOT
    #: comparable with anything else in this repo and must never be used for a hurdle.
    sharpe: float
    #: The repo's STANDARD estimator, ``mean/std × √365`` on daily net returns — the same
    #: one ``evals/backtest_api.py: sharpe_daily``, ``ml.metrics`` and the Bailey–López de
    #: Prado ``expected_max_sharpe`` hurdle in ``runs/features/sampling.py`` assume.
    #: The two disagree by roughly σ²/2 of annual return, which flatters a low-vol book
    #: against a high-vol benchmark: on the full window the ensemble reads 1.08 vs BTC
    #: hold's 0.58 geometrically, but 1.11 vs 0.83 arithmetically. The edge is 1.33×, not
    #: 1.86×, and §6.2's "1.08 against a 2.07 hurdle" compared two different estimators.
    #: Report both; compare like with like.
    sharpe_arith: float
    mdd: float
    gross: float
    total: float
    days: int
    turnover_per_year: float
    fee_drag_per_year: float

    def as_pct(self) -> dict[str, float]:
        """Percent units for CAGR/vol/MaxDD/total, matching the document's tables."""
        d = asdict(self)
        for k in ("cagr", "vol", "mdd", "total", "fee_drag_per_year"):
            d[k] = 100.0 * d[k]
        return d


# --------------------------------------------------------------------------- arithmetic


def _lag1(w: np.ndarray) -> np.ndarray:
    return np.concatenate([[0.0], np.asarray(w, dtype=float)[:-1]])


def book_returns(closes: Mapping[str, pd.Series], weights: Mapping[str, pd.Series], *,
                 cost_per_side: float = COST_PER_SIDE, lag: int = 1,
                 start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """Daily net return, gross and one-way turnover of one book over several legs.

    ``closes[leg]`` and ``weights[leg]`` share an index (daily, UTC); ``weights`` are the
    UNLAGGED per-leg target weights (already scaled by the leg's share of the book, e.g.
    ``0.5 × ensemble`` for the BTC/ETH book). The lag is applied here, once, then the
    window is cut — so the position carried into the window costs nothing and every change
    inside it is charged ``cost_per_side``.
    """
    if lag < 1:
        raise ValueError("lag must be at least one bar")
    legs = list(weights)
    if not legs:
        raise ValueError("a book needs at least one leg")
    index = closes[legs[0]].index
    mask = np.ones(len(index), dtype=bool)
    if start is not None:
        mask &= index >= pd.Timestamp(start, tz="UTC")
    if end is not None:
        mask &= index <= pd.Timestamp(end, tz="UTC")
    net = np.zeros(int(mask.sum()))
    gross = np.zeros(int(mask.sum()))
    turnover = np.zeros(int(mask.sum()))
    for leg in legs:
        c = closes[leg].reindex(index)
        r = c.pct_change().fillna(0.0).to_numpy(dtype=float)
        w = weights[leg].reindex(index).fillna(0.0).to_numpy(dtype=float)
        for _ in range(lag):
            w = _lag1(w)
        wl = w[mask]
        turn = np.abs(np.diff(np.concatenate([[wl[0]], wl])))
        net += wl * np.nan_to_num(r[mask]) - turn * cost_per_side
        gross += wl
        turnover += turn
    return pd.DataFrame({"net": net, "gross": gross, "turnover": turnover},
                        index=index[mask])


def stats(book: pd.DataFrame, *, cost_per_side: float = COST_PER_SIDE) -> BookStats:
    """The §0.3 statistics of a :func:`book_returns` frame."""
    net = book["net"].to_numpy(dtype=float)
    n = len(net)
    if n == 0:
        raise ValueError("empty window")
    equity = np.cumprod(1.0 + net)
    cagr = float(equity[-1] ** (ANNUAL_DAYS / n) - 1.0)
    vol = float(np.std(net) * np.sqrt(ANNUAL_DAYS))
    mdd = float((equity / np.maximum.accumulate(equity) - 1.0).min())
    years = n / ANNUAL_DAYS
    turn = float(book["turnover"].sum())
    sd = float(np.std(net))
    return BookStats(
        cagr=cagr, vol=vol, sharpe=(cagr / vol if vol > 0 else float("nan")),
        sharpe_arith=(float(np.mean(net) / sd * np.sqrt(ANNUAL_DAYS))
                      if sd > 0 else float("nan")),
        mdd=mdd,
        gross=float(book["gross"].mean()), total=float(equity[-1] - 1.0), days=n,
        turnover_per_year=turn / years, fee_drag_per_year=turn * cost_per_side / years,
    )


def headline_books(btc: pd.Series, eth: pd.Series | None) -> dict[str, tuple[dict, dict]]:
    """The three §0.3 books as ``(closes, weights)`` pairs, ready for :func:`book_returns`."""
    ones = pd.Series(1.0, index=btc.index)
    e_btc = trend.ensemble_weight(btc)
    books: dict[str, tuple[dict, dict]] = {
        "BTC hold": ({"BTC": btc}, {"BTC": ones}),
        "BTC trend ensemble": ({"BTC": btc}, {"BTC": e_btc}),
    }
    if eth is not None:
        eth = eth.reindex(btc.index)
        e_eth = trend.ensemble_weight(eth)
        books["BTC/ETH trend ensemble"] = ({"BTC": btc, "ETH": eth},
                                           {"BTC": 0.5 * e_btc, "ETH": 0.5 * e_eth})
    return books


def reproduction_table(btc: pd.Series, eth: pd.Series | None, *,
                       start: str = DOC_WINDOW[0], end: str = DOC_WINDOW[1]) -> pd.DataFrame:
    """Our numbers beside the document's, one row per book, percent units."""
    rows = []
    for name, (closes, weights) in headline_books(btc, eth).items():
        s = stats(book_returns(closes, weights, start=start, end=end)).as_pct()
        doc = DOC_HEADLINE.get(name, {})
        rows.append({
            "book": name, "days": s["days"],
            "cagr": s["cagr"], "doc_cagr": doc.get("cagr"),
            "vol": s["vol"], "doc_vol": doc.get("vol"),
            # `sharpe` is the document's geometric ratio (reproduction only);
            # `sharpe_arith` is the repo's estimator and the only one comparable with a
            # deflated hurdle or with anything evals/backtest_api.py produces.
            "sharpe": s["sharpe"], "doc_sharpe": doc.get("sharpe"),
            "sharpe_arith": s["sharpe_arith"],
            "mdd": s["mdd"], "doc_mdd": doc.get("mdd"),
            "gross": s["gross"], "doc_gross": doc.get("gross"),
            "turn_yr": s["turnover_per_year"], "fee_yr": s["fee_drag_per_year"],
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- data


def load_panel_closes(path: Path, symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
                      ) -> dict[str, pd.Series]:
    """Daily closes per symbol from the survivorship-free panel (``date, symbol, c``)."""
    p = pd.read_parquet(Path(path), columns=["date", "symbol", "c"])
    out: dict[str, pd.Series] = {}
    for sym in symbols:
        s = p[p["symbol"] == sym].sort_values("date")
        ser = pd.Series(s["c"].to_numpy(dtype=float), index=pd.to_datetime(s["date"], utc=True))
        out[sym.replace("USDT", "")] = ser[~ser.index.duplicated(keep="last")]
    return out


def load_feather_closes(data_root: Path, pairs: tuple[str, ...] = ("BTC/USDT", "ETH/USDT"),
                        ) -> dict[str, pd.Series]:
    """Daily closes per asset from the feather store the containers read."""
    out: dict[str, pd.Series] = {}
    for pair in pairs:
        df = pd.read_feather(trend.candle_path(pair, "1d", Path(data_root)))
        ser = pd.Series(df["close"].to_numpy(dtype=float),
                        index=pd.to_datetime(df["date"], utc=True))
        out[pair.split("/")[0]] = ser[~ser.index.duplicated(keep="last")].sort_index()
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--panel", type=Path, help="panel_1d.parquet (date, symbol, c)")
    src.add_argument("--data-root", type=Path, help="a data/ root holding binance/*.feather")
    ap.add_argument("--start", default=DOC_WINDOW[0])
    ap.add_argument("--end", default=DOC_WINDOW[1])
    args = ap.parse_args(argv)
    closes = (load_panel_closes(args.panel) if args.panel
              else load_feather_closes(args.data_root))
    btc, eth = closes["BTC"], closes.get("ETH")
    table = reproduction_table(btc, eth, start=args.start, end=args.end)
    pd.set_option("display.width", 200)
    print(f"window {args.start} -> {args.end}, {COST_PER_SIDE * 1e4:.0f} bps/side, lag 1 day,"
          " cash 0%")
    print(table.to_string(index=False, float_format=lambda x: f"{x:8.2f}"))
    last = btc.index[-1]
    print(f"\nlatest bar {last:%Y-%m-%d}: BTC weight {trend.ensemble_weight(btc).iloc[-1]:.3f}"
          + (f", ETH weight {trend.ensemble_weight(eth.reindex(btc.index)).iloc[-1]:.3f}"
             if eth is not None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
