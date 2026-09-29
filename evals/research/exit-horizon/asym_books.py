"""The two books: the one ``docs/design/dip-strategy.md`` §0.3 backtested, and the one
``strategies/SleeveA.py`` + ``strategies/earn_base.py`` actually deploy.

MEASUREMENT ONLY. Nothing here is imported by anything that ships. Run from the repo root:

    ~/earn-dev/.venv/bin/python evals/research/exit-horizon/measure.py --panel ~/earn-panels/panel_1d.parquet

Why this file exists
--------------------
``evals/trend_ensemble_backtest.py`` trades ``0.5 x ensemble_weight`` per leg and rebalances
to it EVERY DAY. That is Book A. The shipped sleeve applies the same ensemble weight at two
BUY-side call sites only (``earn_base.py:582`` says so in as many words: "It never touches an
exit") and exits on one line, ``SleeveA.py:92``, ``regime_1d == 0``. That is Book B. This
module builds both from the same closes with the same cost model so the difference is the
only thing that moves.

Every mechanic in Book B is read off the shipped config, not invented:

===========================  =======================  ==================================
mechanic                     value                    source
===========================  =======================  ==================================
timeframe                    4h (1d informative)      config/earn.yaml trading.timeframe
regime MA / hysteresis       200 d / 2%               config/params-sleeve-a.json
base weights                 BTC 0.40, ETH 0.30       config/params-sleeve-a.json
vol target / lookback        0.30 annual / 20 d       config/params-sleeve-a.json
caps                         BTC 0.40, ETH 0.30       config/earn.yaml risk.max_weight
fixed stop                   -10% from entry, market  earn.yaml trading...stoploss.fixed_pct
trailing stop                DISABLED                 ...stoploss.trailing.enabled: false
ATR stop                     DISABLED                 ...stoploss.atr.enabled: false
ROI                          {"0": 10.0} = off        ...take_profit.roi_table
profit ladder                [] = off                 ...take_profit.ladder
re-entry cooldown            24 h                     ...stoploss.reentry_cooldown_hours
scheduled DCA                every 7 d, 5% of NAV     ...scheduled_dca
rebalance band               5% of NAV                riskgate.json execution.rebalance_band
daily -3% response           HOLD (sells nothing)     earn.yaml risk.daily_loss_response
monthly -10% response        flatten + pause to EOM   earn.yaml risk.monthly_loss_stop
===========================  =======================  ==================================

So in the shipped configuration there is exactly ONE routine sell: the MA200 regime flip.
The ladder is empty, ROI is off, trailing is off, the daily stop holds, and neither
``_desired_stake`` nor ``_sleeve_adjust`` can return a negative stake — both return 0/None
when ``gap <= 0``, so a position is never trimmed when the ensemble weight falls.

Cost model: 15 bps per side on traded notional (the repo's 0.30% round trip), cash 0%,
every signal lagged one full day. Identical for both books.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

COST_PER_SIDE = 0.0015
ANNUAL_DAYS = 365.0

# ---- shipped constants (see the table above) -------------------------------------------
MA_DAYS = 200
HYSTERESIS = 0.02
BASE_WEIGHTS = {"BTC": 0.40, "ETH": 0.30}
CAPS = {"BTC": 0.40, "ETH": 0.30}
VOL_TARGET = 0.30
VOL_LOOKBACK = 20
STOP_PCT = 0.10
REENTRY_COOLDOWN_DAYS = 1.0
DCA_INTERVAL_DAYS = 7
DCA_CHUNK_PCT_NAV = 0.05
REBALANCE_BAND = 0.05
MONTHLY_LOSS_STOP = 0.10

MA_LOOKBACKS = (50, 75, 100, 125, 150, 175, 200, 225, 250)
DONCHIAN = ((20, 10), (40, 20), (55, 20), (80, 40), (100, 50), (120, 60))


# --------------------------------------------------------------------------- indicators
# Re-implemented here rather than imported so the measurement has no import path into
# strategies/ (which needs freqtrade) — checked against runs.features.trend in measure.py.


def ensemble_weight(close: pd.Series) -> pd.Series:
    """The 15-member trend ensemble, identical to ``runs/features/trend.py``."""
    cols = {}
    for n in MA_LOOKBACKS:
        cols[f"ma{n}"] = (close > close.rolling(n).mean()).astype(float)
    for hi, lo in DONCHIAN:
        up = close >= close.rolling(hi).max().shift(1)
        down = close <= close.rolling(lo).min().shift(1)
        st = pd.Series(np.nan, index=close.index, dtype=float)
        st[up.fillna(False)] = 1.0
        st[down.fillna(False)] = 0.0
        cols[f"dc{hi}_{lo}"] = st.ffill().fillna(0.0)
    return pd.DataFrame(cols, index=close.index).mean(axis=1)


def regime_series(close: pd.Series, ma_days: int = MA_DAYS,
                  hysteresis: float = HYSTERESIS) -> pd.Series:
    """``strategies/sleeve_common.py:38`` verbatim: MA with a symmetric hysteresis band."""
    ma = close.rolling(ma_days, min_periods=ma_days).mean()
    reg = pd.Series(np.nan, index=close.index, dtype=float)
    reg[close > ma * (1 + hysteresis)] = 1.0
    reg[close < ma * (1 - hysteresis)] = 0.0
    reg = reg.ffill().fillna(0.0)
    reg[ma.isna()] = 0.0
    return reg.astype(float)


def realized_vol_annual(close: pd.Series, lookback: int = VOL_LOOKBACK) -> pd.Series:
    """``strategies/sleeve_common.py:33`` verbatim: log-return std x sqrt(365)."""
    lr = np.log(close / close.shift(1))
    return lr.rolling(lookback, min_periods=lookback).std() * np.sqrt(ANNUAL_DAYS)


def sleeve_a_targets(regime: dict[str, float], vols: dict[str, float]) -> dict[str, float]:
    """``sleeve_common.core_satellite_targets`` with no satellites (the shipped paper book).

    Per-asset regime gate, then BOOK-level vol targeting at rho = 1, then the cap.
    """
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


# --------------------------------------------------------------------------- statistics


@dataclass(frozen=True)
class Stats:
    cagr: float
    vol: float
    sharpe_geom: float
    sharpe_arith: float
    mdd: float
    gross: float
    total: float
    days: int
    tim: float                 # fraction of days with any exposure
    turnover_per_year: float
    fee_drag_per_year: float


def stats(net: np.ndarray, gross: np.ndarray, turnover: np.ndarray) -> Stats:
    n = len(net)
    if n == 0:
        raise ValueError("empty window")
    eq = np.cumprod(1.0 + net)
    cagr = float(eq[-1] ** (ANNUAL_DAYS / n) - 1.0)
    sd = float(np.std(net))
    vol = sd * np.sqrt(ANNUAL_DAYS)
    years = n / ANNUAL_DAYS
    turn = float(np.sum(turnover))
    return Stats(
        cagr=cagr, vol=vol,
        sharpe_geom=(cagr / vol if vol > 0 else float("nan")),
        sharpe_arith=(float(np.mean(net) / sd * np.sqrt(ANNUAL_DAYS)) if sd > 0
                      else float("nan")),
        mdd=float((eq / np.maximum.accumulate(eq) - 1.0).min()),
        gross=float(np.mean(gross)), total=float(eq[-1] - 1.0), days=n,
        tim=float(np.mean(np.asarray(gross) > 1e-9)),
        turnover_per_year=turn / years, fee_drag_per_year=turn * COST_PER_SIDE / years,
    )


# --------------------------------------------------------------------------- Book A


def continuous_book(closes: dict[str, pd.Series], weights: dict[str, pd.Series],
                    *, cost_per_side: float = COST_PER_SIDE) -> pd.DataFrame:
    """A book rebalanced to ``weights`` EVERY day, one-day lag — the §0.3 convention.

    Identical arithmetic to ``evals/trend_ensemble_backtest.py: book_returns`` so Book A
    reproduces the document rather than approximating it.
    """
    index = closes[next(iter(closes))].index
    n = len(index)
    net = np.zeros(n)
    gross = np.zeros(n)
    turnover = np.zeros(n)
    for leg, c in closes.items():
        r = c.reindex(index).pct_change().fillna(0.0).to_numpy(float)
        w = weights[leg].reindex(index).fillna(0.0).to_numpy(float)
        wl = np.concatenate([[0.0], w[:-1]])           # one full day of lag
        turn = np.abs(np.diff(np.concatenate([[wl[0]], wl])))
        net += wl * np.nan_to_num(r) - turn * cost_per_side
        gross += wl
        turnover += turn
    return pd.DataFrame({"net": net, "gross": gross, "turnover": turnover}, index=index)


# --------------------------------------------------------------------------- Book B


@dataclass
class Leg:
    units: float = 0.0
    entry: float = 0.0            # volume-weighted average entry price
    last_dca: int = -10_000       # bar index of the last add
    stopped_at: int = -10_000     # bar index of the last stop-out
    opened_at: int = -10_000
    trades: int = 0
    stops: int = 0
    adds: int = 0
    exits: int = 0


@dataclass
class DiscreteResult:
    frame: pd.DataFrame
    events: list[dict] = field(default_factory=list)
    legs: dict[str, Leg] = field(default_factory=dict)
    #: per-asset weight (position value / NAV) on each bar's opening mark
    weights: pd.DataFrame = field(default_factory=pd.DataFrame)


def discrete_book(closes: dict[str, pd.Series], lows: dict[str, pd.Series], *,
                  exit_on: str = "regime",
                  resize_down: bool = False,
                  use_stop: bool = True,
                  use_dca: bool = True,
                  use_band: bool = True,
                  use_cooldown: bool = True,
                  monthly_stop: bool = False,
                  cost_per_side: float = COST_PER_SIDE) -> DiscreteResult:
    """The deployed book, simulated day by day on a NAV ledger.

    Decisions on bar ``t`` read signals from bar ``t-1`` and execute at ``close[t-1]``,
    which is exactly Book A's convention (the weight from the last closed bar is the
    exposure over the next day) — so the two books differ in their RULES, not their timing.

    ``exit_on``:
      * ``"regime"``  — the shipped rule, ``SleeveA.py:92``: full exit when ``regime_1d == 0``
      * ``"ensemble"`` — counterfactual: full exit when the ensemble weight reaches zero
      * ``"never"``    — counterfactual: only the stop can close a position

    ``resize_down=True`` turns on the trim the code cannot do, so the gap can be decomposed.
    """
    assets = list(closes)
    index = closes[assets[0]].index
    n = len(index)

    ens = {a: ensemble_weight(closes[a]).to_numpy(float) for a in assets}
    reg = {a: regime_series(closes[a]).to_numpy(float) for a in assets}
    vol = {a: realized_vol_annual(closes[a]).to_numpy(float) for a in assets}
    px = {a: closes[a].to_numpy(float) for a in assets}
    lo = {a: lows[a].to_numpy(float) for a in assets}
    months = index.to_period("M").to_numpy()

    nav = 1.0
    cash = 1.0
    legs = {a: Leg() for a in assets}
    net = np.zeros(n)
    gross_arr = np.zeros(n)
    turn_arr = np.zeros(n)
    wts = {a: np.zeros(n) for a in assets}
    events: list[dict] = []

    month_anchor_nav = nav
    current_month = months[0]
    month_locked = False

    for t in range(1, n):
        if months[t] != current_month:                     # Gulf-month re-anchor
            current_month, month_anchor_nav, month_locked = months[t], nav, False

        s = t - 1                                          # the signal / execution bar
        traded = 0.0

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
                lg.units, lg.entry = 0.0, 0.0
                lg.exits += 1
                events.append({"bar": s, "date": index[s], "asset": a,
                               "kind": f"exit_{exit_on}", "px": px[a][s]})

        # ---- 2. the sleeve-level monthly flatten (off by default; reported separately)
        if monthly_stop and not month_locked and nav <= month_anchor_nav * (1 - MONTHLY_LOSS_STOP):
            for a in assets:
                lg = legs[a]
                if lg.units > 0:
                    notional = lg.units * px[a][s]
                    cash += notional * (1.0 - cost_per_side)
                    traded += notional
                    lg.units, lg.entry, lg.exits = 0.0, 0.0, lg.exits + 1
                    events.append({"bar": s, "date": index[s], "asset": a,
                                   "kind": "monthly_stop", "px": px[a][s]})
            month_locked = True

        # ---- 3. NAV and the targets the rules want, both computed on the signal bar
        nav_s = cash + sum(legs[a].units * px[a][s] for a in assets)
        tgt = sleeve_a_targets({a: reg[a][s] for a in assets},
                               {a: vol[a][s] for a in assets})

        # ---- 4. the BUY side: custom_stake_amount / _gated_add / _sleeve_adjust
        if not month_locked:
            for a in assets:
                lg = legs[a]
                held = lg.units * px[a][s]
                want = tgt[a] * nav_s                      # desired_stake_for_target
                scaled_cap = ens[a][s] * tgt[a] * nav_s    # _trend_scaled headroom
                gap = want - held

                if lg.units <= 0:
                    # a NEW entry: enter_long needs regime up, custom_stake_amount needs
                    # a positive trend-scaled headroom
                    if reg[a][s] <= 0 or ens[a][s] <= 0 or gap <= 0:
                        continue
                    if use_cooldown and (s - lg.stopped_at) < REENTRY_COOLDOWN_DAYS:
                        continue
                    stake = min(gap, scaled_cap)
                    if stake <= 0 or stake > cash:
                        stake = min(stake, cash)
                    if stake <= 0:
                        continue
                    units = stake / px[a][s] * (1.0 - cost_per_side)
                    lg.units, lg.entry = units, px[a][s]
                    lg.opened_at, lg.last_dca = s, s
                    lg.trades += 1
                    cash -= stake
                    traded += stake
                    events.append({"bar": s, "date": index[s], "asset": a,
                                   "kind": "entry", "px": px[a][s],
                                   "w": stake / nav_s if nav_s else 0.0})
                    continue

                # an EXISTING position
                if resize_down and held > scaled_cap:
                    sell = held - scaled_cap
                    if not use_band or sell > REBALANCE_BAND * nav_s:
                        units = sell / px[a][s]
                        lg.units = max(lg.units - units, 0.0)
                        cash += sell * (1.0 - cost_per_side)
                        traded += sell
                        events.append({"bar": s, "date": index[s], "asset": a,
                                       "kind": "trim", "px": px[a][s]})
                        continue

                if use_dca:
                    # SleeveA._sleeve_adjust: calendar DCA, 7 days, one chunk, banded
                    if tgt[a] <= 0 or gap <= 0:
                        continue
                    if (s - lg.last_dca) < DCA_INTERVAL_DAYS:
                        continue
                    if use_band and gap <= REBALANCE_BAND * nav_s:
                        continue
                    chunk = min(gap, DCA_CHUNK_PCT_NAV * nav_s)
                    stake = min(chunk, max(scaled_cap - held, 0.0), cash)
                    if stake <= 0:
                        continue
                    units = stake / px[a][s] * (1.0 - cost_per_side)
                    lg.entry = ((lg.entry * lg.units) + px[a][s] * units) / (lg.units + units)
                    lg.units += units
                    lg.last_dca, lg.adds = s, lg.adds + 1
                    cash -= stake
                    traded += stake
                elif held < scaled_cap:
                    # continuous top-up variant (no DCA cadence, no band)
                    stake = min(scaled_cap - held, cash)
                    if stake <= 0:
                        continue
                    units = stake / px[a][s] * (1.0 - cost_per_side)
                    lg.entry = ((lg.entry * lg.units) + px[a][s] * units) / (lg.units + units)
                    lg.units += units
                    cash -= stake
                    traded += stake

        # ---- 5. the book is now set for day t. Mark the opening NAV …
        nav_open = cash + sum(legs[a].units * px[a][s] for a in assets)
        gross_arr[t] = (sum(legs[a].units * px[a][s] for a in assets) / nav_open
                        if nav_open > 0 else 0.0)
        if nav_open > 0:
            for a in assets:
                wts[a][t] = legs[a].units * px[a][s] / nav_open

        # ---- 6. … then let day t happen. The fixed stop is the only INTRADAY event: it
        # fires on bar t's own low, fills at the stop price (market order), and the
        # proceeds sit in cash at 0% for the rest of the day. Checked here and not
        # earlier so no decision on bar t-1 can see bar t.
        if use_stop:
            for a in assets:
                lg = legs[a]
                if lg.units <= 0:
                    continue
                stop_px = lg.entry * (1.0 - STOP_PCT)
                if lo[a][t] <= stop_px:
                    fill = min(stop_px, px[a][t])
                    notional = lg.units * fill
                    cash += notional * (1.0 - cost_per_side)
                    traded += notional
                    lg.units, lg.entry = 0.0, 0.0
                    lg.stopped_at, lg.stops, lg.exits = t, lg.stops + 1, lg.exits + 1
                    events.append({"bar": t, "date": index[t], "asset": a,
                                   "kind": "stop", "px": fill})

        nav_close = cash + sum(legs[a].units * px[a][t] for a in assets)
        net[t] = nav_close / nav_open - 1.0 if nav_open > 0 else 0.0
        turn_arr[t] = traded / nav_open if nav_open > 0 else 0.0
        nav = nav_close

    frame = pd.DataFrame({"net": net, "gross": gross_arr, "turnover": turn_arr}, index=index)
    return DiscreteResult(frame=frame, events=events, legs=legs,
                          weights=pd.DataFrame(wts, index=index))
