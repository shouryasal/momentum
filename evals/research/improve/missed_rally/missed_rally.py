"""TRACK 6 -- THE MISSED-RALLY FORENSICS LOOP.

MEASUREMENT ONLY. Lives under evals/research/improve/. Nothing in a shipped path imports
it. It re-implements the shipped universe funnel, the shipped SleeveA book and the
computable subset of the shipped risk gate rather than importing strategies/ (which needs
freqtrade) or ops/ (which needs the live config loader), and every re-implementation names
the shipped line it copies.

THE QUESTION
------------
For every coin that rallied, reconstruct exactly WHY Earn did not own it, and turn the miss
into one of a small number of NAMED causes. Then count the causes, because the distribution
is the finding.

WHAT IT MAY NOT TOUCH
---------------------
The live databases (``~/earn-run/knowledge/earn.db``, ``~/earn-run/journal/journal.db``, the
ft_userdata sqlite files) are NEVER opened -- not read, not copied, not backed up. On
2026-09-30 heavy read-copies against those files while the bots traded coincided with 6h42m
of blocked entries. The market side comes from the survivorship-free daily panel and the
read-only feather store; the SYSTEM side is RECONSTRUCTED from first principles by replaying
the shipped rules. ``--emit-live-queries`` prints the exact SQL a future live version would
run, verified against ops/sql/journal.sql column names, without running any of it.

CONVENTIONS, inherited from evals/research/profit-audit/pa4_common.py so every number here
is comparable with the earlier audits line for line:
  * daily closes from the survivorship-free panel (747 USDT tickers that ever existed);
  * COST_ROUND_TRIP = 0.0030 (10 bps fee + 5 bps slippage per side), always on;
  * every signal is lagged one full day: a rule that sees close t trades at close t+1;
  * arithmetic Sharpe (mean/std * sqrt(365)) only, never CAGR/vol.

USAGE
    python missed_rally.py --start 2024-09-25 --end 2026-09-24
    python missed_rally.py --emit-live-queries
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

HOME = Path(os.path.expanduser("~"))
PANEL = HOME / "earn-panels" / "panel_1d.parquet"
EXCHANGE_INFO = HOME / "im6" / "ei.json"

# --------------------------------------------------------------------------- cost floor
COST_PER_SIDE = 0.0015
COST_ROUND_TRIP = 0.0030
ANNUAL_DAYS = 365.0

# ----------------------------------------------- config/earn.yaml universe.rules (verbatim)
QUOTE = "USDT"
CORE = ("BTC", "ETH")
DATA_ONLY = ("BNB",)
EXCLUDED_BASES = ("XAUT", "PAXG")
CRYPTO_ALLOWLIST = ("ARB", "BNB", "CKB", "DGB", "SHIB", "TRB")
MIN_ANN_VOL_120D = 0.20
VOL_WINDOW_DAYS = 120
MIN_WEEKEND_VOLUME_RATIO = 0.35
WEEKEND_WINDOW_DAYS = 120
EQUITY_TOKEN_PATTERN = r"^[A-Z]{2,6}B$"
MAX_TICK_BPS = 50.0
MIN_LISTING_AGE_DAYS = 180
VOLUME_WINDOW_DAYS = 90
MIN_MEDIAN_QUOTE_VOLUME_USDT = 1_000_000.0
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")

# config/earn.yaml universe.tiers -- MEMBERSHIP floors
MAJOR_MIN_VOLUME = 25_000_000.0
MAJOR_MIN_AGE_DAYS = 730
SATELLITE_MIN_VOLUME = 5_000_000.0

# config/earn.yaml universe.satellite_eligibility -- ELIGIBILITY, stricter than membership
SAT_MAX_ANN_VOL = 1.00
SAT_MIN_AGE_DAYS = 1095
SAT_MIN_VOLUME = 10_000_000.0
SAT_VOL_LOOKBACK = 60

# config/earn.yaml universe.score
LIQUIDITY_WEIGHT = 0.40
TREND_QUALITY_WEIGHT = 0.35
LONG_TREND_WEIGHT = 0.25
LONG_TREND_DAYS = 365
SCORE_MA_DAYS = 200
SCORE_VOL_LOOKBACK = 60
VOL_BAND = (0.40, 1.50)

# config/params-sleeve-a.json + config/riskgate.json risk.*
MA_DAYS = 200
HYSTERESIS = 0.02
BASE_WEIGHTS = {"BTC": 0.40, "ETH": 0.30}
TIER_CAPS = {"core": 0.40, "major": 0.15, "satellite": 0.05, "watchlist": 0.0}
CORE_CAPS = {"BTC": 0.40, "ETH": 0.30}
VOL_TARGET = 0.30
VOL_LOOKBACK = 20
GROSS_CAP = 0.80
USDT_FLOOR = 0.20
MAX_OPEN_POSITIONS = 8
MAX_SATELLITE_POSITIONS = 2
MAX_SATELLITE_GROSS = 0.05
MIN_POSITION_PCT_NAV = 0.02
MIN_NOTIONAL_USDT = 25.0
STOP_PCT = 0.10
HYSTERESIS_RANKS = 3
UNIVERSE_REFRESH_DOW = 6  # Sunday; cron "0 18 * * 0"

# config/earn.yaml signals.* -- the capacity limits the pipeline actually runs under
SCANNER_CRON_MIN = 5
SCANNER_MAX_CANDIDATES_PER_CYCLE = 5
SCANNER_DEDUPE_MIN = 240
VALIDATOR_MAX_PER_DAY = 6
VALIDATOR_COOLDOWN_MIN_PER_ASSET = 120
VALIDATOR_EXPIRE_AFTER_MIN = 90
PLANNER_MAX_PER_DAY = 3
PLANNER_COOLDOWN_HOURS = 4

# config/earn.yaml signals.scanner.detectors -- thresholds, verbatim
DET_MOVE_24H = 8.0
DET_BREAKOUT_LOOKBACK = 20
DET_RSI_LOW, DET_RSI_HIGH = 25.0, 75.0
DET_VOLUME_SPIKE_Z = 3.0
DET_DIP_FROM_HIGH_PCT = 12.0
DET_DIP_LOOKBACK_DAYS = 30
DET_MA_CROSS_ENABLED = False  # ma_cross: { enabled: false } -- shipped OFF
DETECTOR_LOOKBACK_DAYS = 5  # how far before the rally start a fire still counts

# ----------------------------------------------------------------- the named causes
# Ordered EXACTLY as the real pipeline orders them, so a rally is attributed to the FIRST
# gate it failed -- the same semantics as strategies/riskgate.py: CHECK_ORDER.
CAUSES = (
    "C01_not_listed_long_enough",   # < 180d of history at rally start: no watchlist entry
    "C02_not_in_watchlist",         # failed the watchlist funnel (which filter is reported)
    "C03_watchlist_only_no_tier",   # in the watchlist, no tier -> gate cap 0 -> `tier` refuses
    "C04_major_structurally_unreachable",  # tier major: never offered a seat (code defect)
    "C05_satellite_exit_only",      # tier satellite, fails satellite_eligibility -> exit_only
    "C06_no_detector_fired",        # SleeveB only: no shipped detector fired pre-rally
    "C07_screener_or_scan_capacity",  # SleeveB only: beyond max_candidates_per_cycle
    "C08_validator_capacity_or_expired",  # SleeveB only: beyond validator/planner per-day cap
    "C09_regime_gate_off",          # BTC below its own 200d MA -> satellite sleeve in cash
    "C10_own_regime_gate_off",      # the coin itself below its 200d MA at rally start
    "C11_below_seat_cutoff",        # eligible, regime up, ranked below the 2 seats
    "C12_sized_to_zero",            # seat available but weight < min_position / min_notional
    "C13_gate_refused",             # a computable gate check refused it (named)
    "C14_owned_exited_before_peak",  # we held it and the shipped exit fired before the peak
    "C15_captured",                 # we held it through
    "C99_unattributable",           # the falsifier bucket: must stay under 10%
)


# --------------------------------------------------------------------------- panel load


def load_panel(path: Path) -> pd.DataFrame:
    p = pd.read_parquet(path, columns=["date", "o", "h", "l", "c", "v", "qv", "symbol"])
    p["date"] = pd.to_datetime(p["date"], utc=True).dt.normalize()
    return p.sort_values(["symbol", "date"])


def wide(panel: pd.DataFrame, col: str) -> pd.DataFrame:
    return panel.pivot_table(index="date", columns="symbol", values=col, aggfunc="last")


def listed_today(ei_path: Path) -> tuple[set[str], dict[str, float]]:
    """The venue's CURRENT USDT spot TRADING set, plus each symbol's tickSize.

    Read from a file fetched once from the public /api/v3/exchangeInfo. No key, no
    trading endpoint, no live DB.
    """
    ei = json.loads(ei_path.read_text())
    out: set[str] = set()
    ticks: dict[str, float] = {}
    for s in ei["symbols"]:
        if s.get("quoteAsset") != QUOTE:
            continue
        if s.get("status") != "TRADING":
            continue
        if "SPOT" not in (s.get("permissions") or []) and not s.get("isSpotTradingAllowed"):
            continue
        out.add(s["symbol"])
        for f in s.get("filters", []):
            if f.get("filterType") == "PRICE_FILTER":
                ticks[s["symbol"]] = float(f.get("tickSize") or 0.0)
    return out, ticks


# --------------------------------------------------------------------------- rally rule


def rally_episodes(close: pd.Series, thresh: float, window: int,
                   dedupe: int) -> list[dict]:
    """Non-overlapping up-episodes: >= `thresh` inside `window` calendar days.

    The rule, stated so a reader can re-derive it: walk forward; at each day t compute the
    highest close in (t, t+window]; if that is >= (1+thresh) x close[t] and no episode is
    open, an episode STARTS at t and PEAKS at the argmax day; the next episode may not
    start until `dedupe` days after that peak. The dedupe is what stops one 90-day rally
    being counted as seventy overlapping 20-day rallies.
    """
    c = close.dropna()
    if len(c) < window + 2:
        return []
    v = c.to_numpy(float)
    d = c.index
    n = len(v)
    out: list[dict] = []
    t = 0
    while t < n - 1:
        hi = min(n, t + window + 1)
        seg = v[t + 1:hi]
        if seg.size == 0:
            break
        j = int(np.argmax(seg)) + t + 1
        gain = v[j] / v[t] - 1.0
        if gain >= thresh:
            out.append({"start": d[t], "peak": d[j], "gain": float(gain),
                        "days_to_peak": int(j - t),
                        "start_close": float(v[t]), "peak_close": float(v[j])})
            t = j + dedupe
        else:
            t += 1
    return out


# --------------------------------------- point-in-time metrics (ops/universe.py: Metrics)


def rolling_metrics(cw: pd.DataFrame, qw: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Every point-in-time metric the shipped funnel needs, vectorised, no look-ahead.

    Each frame is date x symbol and row `d` uses ONLY data up to and including `d`, which
    is what the Sunday resolver sees when it runs.
    """
    ret = cw.pct_change()
    m: dict[str, pd.DataFrame] = {}
    m["ann_vol_120"] = ret.rolling(VOL_WINDOW_DAYS, min_periods=60).std() * np.sqrt(ANNUAL_DAYS)
    m["ann_vol_60"] = ret.rolling(SAT_VOL_LOOKBACK, min_periods=30).std() * np.sqrt(ANNUAL_DAYS)
    m["ann_vol_20"] = ret.rolling(VOL_LOOKBACK, min_periods=10).std() * np.sqrt(ANNUAL_DAYS)
    m["adv90"] = qw.rolling(VOLUME_WINDOW_DAYS, min_periods=30).median()
    m["ma200"] = cw.rolling(MA_DAYS, min_periods=MA_DAYS).mean()
    m["ret_365"] = cw / cw.shift(LONG_TREND_DAYS) - 1.0
    m["close"] = cw
    # weekend volume ratio: median weekend qv / median weekday qv over 120d.
    # ops/universe.py computes it on the same window the config names.
    we = np.repeat(np.asarray(cw.index.dayofweek >= 5)[:, None], qw.shape[1], axis=1)
    mask_we = pd.DataFrame(we, index=qw.index, columns=qw.columns)
    qw_we = qw.where(mask_we)
    qw_wd = qw.where(~mask_we)
    med_we = qw_we.rolling(WEEKEND_WINDOW_DAYS, min_periods=10).median()
    med_wd = qw_wd.rolling(WEEKEND_WINDOW_DAYS, min_periods=20).median()
    m["weekend_ratio"] = med_we / med_wd.replace(0.0, np.nan)
    # age: days since the symbol's first non-null close
    first = cw.notna().idxmax()
    ages = {}
    for s in cw.columns:
        f = first[s]
        if pd.isna(f) or not cw[s].notna().any():
            ages[s] = pd.Series(np.nan, index=cw.index)
        else:
            ages[s] = pd.Series((cw.index - f).days, index=cw.index).where(cw.index >= f)
    m["age_days"] = pd.DataFrame(ages)
    # detectors, at daily resolution
    m["ret_1d"] = ret * 100.0
    m["range_high_20d"] = cw.rolling(DET_BREAKOUT_LOOKBACK, min_periods=DET_BREAKOUT_LOOKBACK).max().shift(1)
    m["high_30d"] = cw.rolling(DET_DIP_LOOKBACK_DAYS, min_periods=10).max()
    m["dip_from_high_pct"] = (1.0 - cw / m["high_30d"]) * 100.0
    m["qv_z"] = (qw - qw.rolling(30, min_periods=15).mean()) / qw.rolling(30, min_periods=15).std()
    up = ret.clip(lower=0.0)
    dn = (-ret).clip(lower=0.0)
    au = up.ewm(alpha=1 / 14.0, min_periods=14, adjust=False).mean()
    ad = dn.ewm(alpha=1 / 14.0, min_periods=14, adjust=False).mean()
    m["rsi14"] = 100.0 - 100.0 / (1.0 + au / ad.replace(0.0, np.nan))
    return m


def is_leveraged(base: str, listed_bases: frozenset[str]) -> bool:
    """ops/universe.py::_is_leveraged -- <BASE><SUFFIX> where <BASE> is itself listed."""
    for suf in LEVERAGED_SUFFIXES:
        if base.endswith(suf) and len(base) > len(suf):
            stub = base[: -len(suf)]
            if stub in listed_bases:
                return True
    return False


def is_rwa(base: str) -> bool:
    """ops/universe.py::_is_real_world_asset -- tokenized gold plus the equity pattern."""
    import re
    if base in EXCLUDED_BASES:
        return True
    if base in CRYPTO_ALLOWLIST:
        return False
    return bool(re.match(EQUITY_TOKEN_PATTERN, base))


def classify_pit(sym: str, base: str, d, m: dict[str, pd.DataFrame], tick: float,
                 listed_bases: frozenset[str]) -> tuple[str, str] | None:
    """ops/universe.py::classify, point-in-time. Returns (filter, detail) or None=passed.

    The order is FILTER_ORDER, so the funnel counts are meaningful.
    """
    if base in DATA_ONLY:
        return "data_only", base
    if is_leveraged(base, listed_bases):
        return "leveraged_token", base
    av = m["ann_vol_120"].at[d, sym] if sym in m["ann_vol_120"].columns else np.nan
    if not np.isnan(av) and av < MIN_ANN_VOL_120D:
        return "pegged", f"ann_vol={av:.4f}"
    wr = m["weekend_ratio"].at[d, sym] if sym in m["weekend_ratio"].columns else np.nan
    if not np.isnan(wr) and wr < MIN_WEEKEND_VOLUME_RATIO:
        return "not_24_7", f"weekend_ratio={wr:.3f}"
    if is_rwa(base):
        return "real_world_asset", base
    if not base.isascii():
        return "non_ascii_base", base
    px = m["close"].at[d, sym]
    if np.isnan(px) or px <= 0:
        return "tick_size", "no_price"
    tick_bps = (tick / px) * 10_000.0 if tick else 0.0
    if tick_bps >= MAX_TICK_BPS:
        return "tick_size", f"tick={tick_bps:.1f}bps"
    age = m["age_days"].at[d, sym]
    if np.isnan(age) or age < MIN_LISTING_AGE_DAYS:
        return "listing_age", f"{0 if np.isnan(age) else int(age)}d"
    adv = m["adv90"].at[d, sym]
    if np.isnan(adv):
        return "liquidity", "no_volume"
    if adv < MIN_MEDIAN_QUOTE_VOLUME_USDT:
        return "liquidity", f"median_vol={adv:,.0f}"
    return None


def assign_tier_pit(base: str, sym: str, d, m: dict[str, pd.DataFrame]) -> str:
    """ops/universe.py::assign_tier, point-in-time."""
    if base in CORE:
        return "core"
    vol = m["adv90"].at[d, sym]
    vol = 0.0 if np.isnan(vol) else float(vol)
    age = m["age_days"].at[d, sym]
    age = 0.0 if np.isnan(age) else float(age)
    if vol >= MAJOR_MIN_VOLUME and age >= MAJOR_MIN_AGE_DAYS:
        return "major"
    if vol >= SATELLITE_MIN_VOLUME:
        return "satellite"
    return "watchlist"


def satellite_eligible_pit(sym: str, d, m: dict[str, pd.DataFrame]) -> tuple[bool, str]:
    """config/earn.yaml universe.satellite_eligibility, applied by gen_freqtrade_config.

    A failing satellite is rendered exit_only in riskgate.json, so it cannot be entered.
    """
    v60 = m["ann_vol_60"].at[d, sym]
    age = m["age_days"].at[d, sym]
    adv = m["adv90"].at[d, sym]
    fails = []
    if np.isnan(v60) or v60 > SAT_MAX_ANN_VOL:
        fails.append(f"vol60={'nan' if np.isnan(v60) else f'{v60:.2f}'}>1.00")
    if np.isnan(age) or age < SAT_MIN_AGE_DAYS:
        fails.append(f"age={0 if np.isnan(age) else int(age)}d<1095")
    if np.isnan(adv) or adv < SAT_MIN_VOLUME:
        fails.append(f"adv90={0 if np.isnan(adv) else adv / 1e6:.1f}M<10M")
    return (not fails), ",".join(fails)


def trend_quality_pit(sym: str, d, m: dict[str, pd.DataFrame]) -> float:
    """ops/universe.py::_trend_quality -- above its own 200d MA, 60d vol inside the band."""
    px, ma = m["close"].at[d, sym], m["ma200"].at[d, sym]
    above = 1.0 if (not np.isnan(ma) and not np.isnan(px) and px > ma) else 0.0
    v = m["ann_vol_60"].at[d, sym]
    in_band = 1.0 if (not np.isnan(v) and VOL_BAND[0] <= v <= VOL_BAND[1]) else 0.0
    return (above + in_band) / 2.0


def pct_ranks(vals: list[float | None]) -> list[float]:
    """ops/universe.py::_percentile_ranks -- rank each value in [0,1]; None ranks lowest."""
    present = sorted(v for v in vals if v is not None and not np.isnan(v))
    n = len(present)
    out = []
    for v in vals:
        if v is None or np.isnan(v) or n == 0:
            out.append(0.0)
        elif n == 1:
            out.append(1.0)
        else:
            out.append(sum(1 for p in present if p < v) / (n - 1))
    return out


# --------------------------------------------------------------- the weekly snapshot


def snapshot(d, syms: list[str], m: dict[str, pd.DataFrame], ticks: dict[str, float],
             listed_bases: frozenset[str]) -> dict:
    """What the Sunday resolver (`cron: 0 18 * * 0`) would have written on date `d`."""
    watchlist: dict[str, str] = {}
    excluded: dict[str, str] = {}
    for sym in syms:
        base = sym[: -len(QUOTE)]
        v = classify_pit(sym, base, d, m, ticks.get(sym, 0.0), listed_bases)
        if v is None:
            watchlist[sym] = base
        else:
            excluded[sym] = f"{v[0]}:{v[1]}"
    tiers = {s: assign_tier_pit(b, s, d, m) for s, b in watchlist.items()}
    rankable = [s for s in watchlist if tiers[s] != "core"]
    liq = pct_ranks([float(m["adv90"].at[d, s]) for s in rankable])
    lng = pct_ranks([float(m["ret_365"].at[d, s]) for s in rankable])
    scores = {}
    for i, s in enumerate(rankable):
        scores[s] = (LIQUIDITY_WEIGHT * liq[i] + LONG_TREND_WEIGHT * lng[i]
                     + TREND_QUALITY_WEIGHT * trend_quality_pit(s, d, m))
    elig, why = {}, {}
    for s in watchlist:
        if tiers[s] == "satellite":
            ok, reason = satellite_eligible_pit(s, d, m)
            elig[s] = ok
            why[s] = reason
        else:
            elig[s] = tiers[s] == "core"
            why[s] = "" if tiers[s] == "core" else f"tier={tiers[s]}"
    return {"date": d, "watchlist": watchlist, "excluded": excluded, "tiers": tiers,
            "scores": scores, "enterable": elig, "why_not": why}


def rank_satellites(scores: dict[str, float], ages: dict[str, float],
                    advs: dict[str, float]) -> list[str]:
    """ops/universe.py ordering -- ties break toward longer-listed, more liquid."""
    return sorted(scores, key=lambda s: (-scores[s], -ages.get(s, 0.0),
                                         -advs.get(s, 0.0), s))


def select_satellites(order: list[str], incumbents: list[str], max_n: int,
                      hysteresis_ranks: int = HYSTERESIS_RANKS) -> list[str]:
    """strategies/sleeve_common.py::select_satellites, copied line for line."""
    if max_n <= 0:
        return []
    rank = {a: i for i, a in enumerate(order)}
    result = [a for a in dict.fromkeys(incumbents) if a in rank][:max_n]
    protected = set(result)
    for asset in order:
        if len(result) >= max_n:
            break
        if asset not in result:
            result.append(asset)
    while True:
        challengers = [a for a in order if a not in result]
        cand = [a for a in result if a in protected]
        weakest = max(cand, key=lambda a: rank[a]) if cand else None
        if not challengers or weakest is None:
            break
        if rank[weakest] - rank[challengers[0]] < hysteresis_ranks:
            break
        result[result.index(weakest)] = challengers[0]
        protected.discard(weakest)
    return sorted(result, key=lambda a: rank[a])


def regime_series(close: pd.Series) -> pd.Series:
    """params-sleeve-a.json trend: 200d MA with 2% hysteresis, latched."""
    ma = close.rolling(MA_DAYS, min_periods=MA_DAYS).mean()
    up_th, dn_th = ma * (1 + HYSTERESIS), ma * (1 - HYSTERESIS)
    state = np.zeros(len(close), dtype=float)
    cur = 0.0
    cv, uv, dv = close.to_numpy(float), up_th.to_numpy(float), dn_th.to_numpy(float)
    for i in range(len(cv)):
        if np.isnan(uv[i]):
            state[i] = 0.0
            continue
        if cur <= 0 and cv[i] > uv[i]:
            cur = 1.0
        elif cur > 0 and cv[i] < dv[i]:
            cur = 0.0
        state[i] = cur
    return pd.Series(state, index=close.index)


def book_realised_vol(weights: dict[str, float], vols: dict[str, float]) -> float:
    """strategies/sleeve_common.py::book_realised_vol -- exposure-weighted mean, rho=1."""
    gross = sum(w for w in weights.values() if w > 0)
    if gross <= 0:
        return 0.0
    num = sum(w * float(vols.get(a, 0.0) or 0.0) for a, w in weights.items() if w > 0)
    return num / gross


def core_satellite_targets(core_weights: dict[str, float],
                           core_regime_up: dict[str, bool], satellites: list[str],
                           vols: dict[str, float], caps: dict[str, float],
                           risk_on: bool) -> dict[str, float]:
    """strategies/sleeve_common.py::core_satellite_targets, copied line for line.

    ``core_weights`` is passed in rather than read from the module constant because this
    replay works in SYMBOL space (``BTCUSDT``) while the shipped sleeve works in BASE space
    (``BTC``). Reading the constant here silently gave every core asset a weight of zero and
    made the whole replayed book flat -- caught by cross-checking the BTC rally of
    2024-10-22, which the attribution reported as "sized to zero" while BTC was plainly
    1.06x above its own 200d MA.
    """
    sat_gross = MAX_SATELLITE_GROSS if (risk_on and satellites) else 0.0
    core_scale = max(1.0 - sat_gross, 0.0)
    raw: dict[str, float] = {}
    for a, w in core_weights.items():
        raw[a] = w * core_scale if core_regime_up.get(a, False) else 0.0
    if sat_gross > 0:
        per = sat_gross / len(satellites)
        for a in satellites:
            raw[a] = raw.get(a, 0.0) + per
    bv = book_realised_vol(raw, vols)
    if bv <= 0:
        return {a: 0.0 for a in raw}
    scale = min(VOL_TARGET / bv, 1.0)
    return {a: float(min(w * scale, float(caps.get(a, 0.0)))) for a, w in raw.items()}


# --------------------------------------------------------------------------- detectors


def detectors_fired(sym: str, d0, m: dict[str, pd.DataFrame], idx: pd.DatetimeIndex
                    ) -> list[str]:
    """Which shipped detectors would have fired in the 5 days ending at the rally start.

    DAILY RESOLUTION, declared: `move` is configured on 1h/4h/24h and `volume_spike` on 1h,
    and the panel is daily, so those two are modelled on their daily analogue and the
    modelling is named in the report. `breakout` (tf 1d, lookback 20) and `dip_from_high`
    (tf 1d) are EXACT. `rsi_extreme` is configured on 4h and modelled on 1d. `ma_cross` is
    shipped DISABLED and is therefore NOT counted -- that is a finding, not an omission.
    """
    try:
        i = idx.get_loc(d0)
    except KeyError:
        return []
    lo = max(0, i - DETECTOR_LOOKBACK_DAYS + 1)
    fired: list[str] = []
    win = idx[lo:i + 1]

    def any_true(frame: str, fn) -> bool:
        if sym not in m[frame].columns:
            return False
        s = m[frame].loc[win, sym]
        return bool(fn(s).any())

    if any_true("ret_1d", lambda s: s.abs() > DET_MOVE_24H):
        fired.append("move_24h")
    if sym in m["close"].columns and sym in m["range_high_20d"].columns:
        c = m["close"].loc[win, sym]
        hi = m["range_high_20d"].loc[win, sym]
        if bool((c > hi).any()):
            fired.append("breakout")
    if any_true("qv_z", lambda s: s >= DET_VOLUME_SPIKE_Z):
        fired.append("volume_spike")
    if any_true("rsi14", lambda s: (s <= DET_RSI_LOW) | (s >= DET_RSI_HIGH)):
        fired.append("rsi_extreme")
    if any_true("dip_from_high_pct", lambda s: s >= DET_DIP_FROM_HIGH_PCT):
        fired.append("dip_from_high")
    # ma_cross: shipped `enabled: false`. Deliberately not evaluated.
    return fired


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-09-25")
    ap.add_argument("--end", default="2026-09-24")
    ap.add_argument("--thresh", type=float, default=0.30)
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--dedupe", type=int, default=20)
    ap.add_argument("--out", default=str(HOME / "im6" / "out"))
    ap.add_argument("--emit-live-queries", action="store_true")
    ap.add_argument("--plateau", action="store_true")
    a = ap.parse_args()

    if a.emit_live_queries:
        print(LIVE_QUERIES)
        return 0

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    start, end = pd.Timestamp(a.start, tz="UTC"), pd.Timestamp(a.end, tz="UTC")

    panel = load_panel(PANEL)
    listed, ticks = listed_today(EXCHANGE_INFO)
    have = set(panel["symbol"].unique())
    universe = sorted(listed & have)
    listed_bases = frozenset(s[: -len(QUOTE)] for s in listed)

    cw = wide(panel, "c")
    qw = wide(panel, "qv")
    # full history is needed for the 365d and 200d windows; the STUDY window is [start,end]
    m = rolling_metrics(cw, qw)
    idx = cw.index

    # ---------------- 1. the shipped book, replayed day by day over the study window
    btc_reg = regime_series(cw["BTCUSDT"]) if "BTCUSDT" in cw else pd.Series(dtype=float)
    eth_reg = regime_series(cw["ETHUSDT"]) if "ETHUSDT" in cw else pd.Series(dtype=float)
    own_reg = {}
    for s in universe:
        if cw[s].notna().sum() >= MA_DAYS + 5:
            own_reg[s] = regime_series(cw[s])

    study = idx[(idx >= start) & (idx <= end)]
    snaps: dict[pd.Timestamp, dict] = {}
    held_by_day: dict[pd.Timestamp, list[str]] = {}
    weights_by_day: dict[pd.Timestamp, dict[str, float]] = {}
    seat_rank_by_day: dict[pd.Timestamp, dict[str, int]] = {}
    cur_snap: dict | None = None

    # one snapshot per Sunday, exactly like the cron; the book uses the latest snapshot
    snap_dates = [d for d in study if d.dayofweek == UNIVERSE_REFRESH_DOW]
    if snap_dates and snap_dates[0] > study[0]:
        snap_dates = [study[0]] + snap_dates
    snap_set = set(snap_dates)

    for d in study:
        if d in snap_set or cur_snap is None:
            cur_snap = snapshot(d, universe, m, ticks, listed_bases)
            snaps[d] = cur_snap

    def replay_book(eligible_of, max_sats: int, sat_gross: float, cap: float,
                    enforce_min_position: bool = True) -> dict:
        """Replay SleeveA day by day over `study` with ONE rule relaxed.

        `eligible_of(snapshot) -> list[symbol]` is the only thing a counterfactual changes,
        plus the seat count / satellite gross. Everything else -- the score, the rotation
        hysteresis, both regime gates, the book vol target, the caps, the min_position floor
        and the 0.30% round trip -- is the shipped rule, unchanged. That is what makes the
        difference between two books attributable to the one rule that moved.
        """
        w_by_day: dict[pd.Timestamp, dict[str, float]] = {}
        h_by_day: dict[pd.Timestamp, list[str]] = {}
        r_by_day: dict[pd.Timestamp, dict[str, int]] = {}
        zeroed = 0
        inc: list[str] = []
        snap_now: dict | None = None
        for d in study:
            if d in snaps:
                snap_now = snaps[d]
            sn = snap_now
            eligible = eligible_of(sn)
            ages, advs = {}, {}
            for s in eligible:
                av = m["age_days"].at[d, s]
                qv = m["adv90"].at[d, s]
                ages[s] = 0.0 if np.isnan(av) else float(av)
                advs[s] = 0.0 if np.isnan(qv) else float(qv)
            order = rank_satellites({s: sn["scores"].get(s, 0.0) for s in eligible},
                                    ages, advs)
            r_by_day[d] = {s: i + 1 for i, s in enumerate(order)}
            chosen = select_satellites(order, inc, max_sats)
            # ... then the coin's OWN regime gate: `if states.get(a)[0]`
            chosen_up = [s for s in chosen
                         if s in own_reg and float(own_reg[s].get(d, 0.0)) > 0]
            risk_on = bool(float(btc_reg.get(d, 0.0)) > 0)
            vols = {}
            for s in list(chosen_up) + ["BTCUSDT", "ETHUSDT"]:
                v = m["ann_vol_20"].at[d, s] if s in m["ann_vol_20"].columns else np.nan
                vols[s] = 0.0 if np.isnan(v) else float(v)
            caps = {"BTCUSDT": CORE_CAPS["BTC"], "ETHUSDT": CORE_CAPS["ETH"]}
            for s in chosen_up:
                caps[s] = cap
            sg = sat_gross if (risk_on and chosen_up) else 0.0
            core_scale = max(1.0 - sg, 0.0)
            raw: dict[str, float] = {
                "BTCUSDT": BASE_WEIGHTS["BTC"] * core_scale
                if float(btc_reg.get(d, 0.0)) > 0 else 0.0,
                "ETHUSDT": BASE_WEIGHTS["ETH"] * core_scale
                if float(eth_reg.get(d, 0.0)) > 0 else 0.0,
            }
            if sg > 0:
                per = sg / len(chosen_up)
                for s in chosen_up:
                    raw[s] = raw.get(s, 0.0) + per
            bv = book_realised_vol(raw, vols)
            if bv <= 0:
                tg = {k: 0.0 for k in raw}
            else:
                scale = min(VOL_TARGET / bv, 1.0)
                tg = {k: float(min(v * scale, float(caps.get(k, 0.0))))
                      for k, v in raw.items()}
            final = {}
            for k, v in tg.items():
                if k in ("BTCUSDT", "ETHUSDT") or not enforce_min_position or v >= MIN_POSITION_PCT_NAV:
                    final[k] = v
                else:
                    final[k] = 0.0
                    if v > 0:
                        zeroed += 1
            w_by_day[d] = final
            h_by_day[d] = [s for s, w in final.items()
                           if w > 0 and s not in ("BTCUSDT", "ETHUSDT")]
            inc = chosen
        return {"weights": w_by_day, "held": h_by_day, "rank": r_by_day,
                "min_position_zeroed_days": zeroed}

    def shipped_eligible(sn: dict) -> list[str]:
        """SleeveA._book_targets: eligible = satellite tier, not core, enterable."""
        return [s for s in sn["tiers"]
                if sn["tiers"][s] == "satellite" and sn["enterable"].get(s)]

    B0 = replay_book(shipped_eligible, MAX_SATELLITE_POSITIONS, MAX_SATELLITE_GROSS,
                     TIER_CAPS["satellite"])
    weights_by_day = B0["weights"]
    held_by_day = B0["held"]
    seat_rank_by_day = B0["rank"]

    # ---------------- 2. rallies in the currently-listed universe over the study window
    rallies: list[dict] = []
    for s in universe:
        for r in rally_episodes(cw[s].loc[cw.index <= end], a.thresh, a.window, a.dedupe):
            if start <= r["start"] <= end:
                r = dict(r)
                r["symbol"] = s
                r["base"] = s[: -len(QUOTE)]
                rallies.append(r)
    rallies.sort(key=lambda r: (r["start"], r["symbol"]))

    # ---------------- 3. attribute each rally to the FIRST cause in pipeline order
    rows: list[dict] = []
    for r in rallies:
        s, d0 = r["symbol"], r["start"]
        if d0 not in weights_by_day:
            continue
        # the snapshot in force on d0
        sn_dates = [x for x in snaps if x <= d0]
        sn = snaps[max(sn_dates)] if sn_dates else None
        cause, detail = "C99_unattributable", ""
        held = held_by_day.get(d0, [])
        w0 = weights_by_day.get(d0, {}).get(s, 0.0)
        age = m["age_days"].at[d0, s] if s in m["age_days"].columns else np.nan
        tier = sn["tiers"].get(s) if sn else None
        risk_on = bool(float(btc_reg.get(d0, 0.0)) > 0)
        own_up = bool(s in own_reg and float(own_reg[s].get(d0, 0.0)) > 0)
        fired = detectors_fired(s, d0, m, idx)
        rank = seat_rank_by_day.get(d0, {}).get(s)

        if w0 > 0:
            # we owned it: did the shipped exit fire before the peak?
            wpath = [weights_by_day.get(d, {}).get(s, 0.0)
                     for d in study[(study > d0) & (study <= r["peak"])]]
            exited = any(w <= 0 for w in wpath)
            cause = "C14_owned_exited_before_peak" if exited else "C15_captured"
            detail = f"weight={w0:.4f}"
        elif tier == "core":
            # A core asset never competes for a satellite seat and is never screened: its
            # ONLY entry condition is its own 200d regime gate. Attributing it through the
            # satellite chain would misreport BTC as "sized to zero".
            if not own_up:
                cause = "C10_own_regime_gate_off"
                detail = "core asset below its own 200d MA at rally start"
            else:
                cause = "C12_sized_to_zero"
                detail = f"core regime up but target {w0:.4f} (vol target / cap)"
        elif np.isnan(age) or age < MIN_LISTING_AGE_DAYS:
            cause = "C01_not_listed_long_enough"
            detail = f"age={0 if np.isnan(age) else int(age)}d"
        elif sn is not None and s in sn["excluded"]:
            cause = "C02_not_in_watchlist"
            detail = sn["excluded"][s]
        elif tier == "watchlist":
            cause = "C03_watchlist_only_no_tier"
            adv = m["adv90"].at[d0, s]
            detail = f"adv90={0 if np.isnan(adv) else adv / 1e6:.1f}M<5M"
        elif tier == "major":
            cause = "C04_major_structurally_unreachable"
            detail = "SleeveA._book_targets offers seats to satellites only"
        elif tier == "satellite" and not sn["enterable"].get(s):
            cause = "C05_satellite_exit_only"
            detail = sn["why_not"].get(s, "")
        # ORDER CORRECTION (2026-09-30): the detector check used to sit here, ahead of the
        # regime gates, and it mislabelled 8 of 10 rallies whose real cause was the coin's
        # own 200d gate. SleeveA is the RULES sleeve: its entry path is the trend gate, the
        # cross-sectional score and the risk gate. The signal pipeline (scanner -> screener
        # -> validator -> proposal) feeds SleeveB and the event-fired research run; it is
        # NOT in SleeveA's entry path. So "did a detector fire" is reported as an
        # INDEPENDENT column on every rally and as its own coverage table, never as a step
        # in this funnel. C06 stays in CAUSES only for a future SleeveB replay.
        elif not risk_on:
            cause = "C09_regime_gate_off"
            detail = "BTC below its own 200d MA -> satellite sleeve in cash"
        elif not own_up:
            cause = "C10_own_regime_gate_off"
            detail = "coin below its own 200d MA at rally start"
        elif rank is not None and rank > MAX_SATELLITE_POSITIONS:
            cause = "C11_below_seat_cutoff"
            detail = (f"shipped score rank {rank} of {len(seat_rank_by_day.get(d0, {}))} "
                      f"eligible; seats held by {','.join(held) or 'none'}")
        elif w0 == 0:
            cause = "C12_sized_to_zero"
            detail = (f"rank {rank}, regime up, but target {w0:.4f} < min_position "
                      f"{MIN_POSITION_PCT_NAV} (satellite_gross/seats = "
                      f"{MAX_SATELLITE_GROSS / MAX_SATELLITE_POSITIONS} before vol scaling)")
        rows.append({**r, "cause": cause, "detail": detail, "tier": tier,
                     "detectors": ",".join(fired), "btc_risk_on": risk_on,
                     "own_regime_up": own_up, "seat_rank": rank,
                     "weight_on_start": w0, "seats_held": ",".join(held)})

    df = pd.DataFrame(rows)
    df.to_csv(outdir / "rallies_attributed.csv", index=False)

    # ---------------- 4. forgone return AT THE SHIPPED POSITION SIZE
    # A satellite can hold at most max_satellite_gross / max_satellite_positions of NAV.
    shipped_w = MAX_SATELLITE_GROSS / MAX_SATELLITE_POSITIONS
    df["forgone_peak_nav_pct"] = 0.0
    df["forgone_shipped_exit_nav_pct"] = 0.0
    for i, r in df.iterrows():
        if r["cause"] in ("C15_captured",):
            continue
        s = r["symbol"]
        # (a) perfect foresight: buy at the rally start close, sell at the peak
        gross = r["gain"]
        df.at[i, "forgone_peak_nav_pct"] = shipped_w * (gross - COST_ROUND_TRIP) * 100.0
        # (b) the HONEST number: what the SHIPPED RULE would have realised holding the coin
        # from the rally start until ITS OWN exit fires (200d MA flip or the 10% stop).
        if s in own_reg:
            fut = study[(study > r["start"])]
            px = cw[s]
            entry = r["start_close"]
            peak_run = entry
            exit_px = None
            for d in fut:
                p = px.get(d, np.nan)
                if np.isnan(p):
                    continue
                peak_run = max(peak_run, p)
                if p <= peak_run * (1 - STOP_PCT):
                    exit_px = p
                    break
                if float(own_reg[s].get(d, 0.0)) <= 0:
                    exit_px = p
                    break
            if exit_px is None:
                exit_px = float(px.loc[fut[-1]]) if len(fut) else entry
            net = exit_px / entry - 1.0 - COST_ROUND_TRIP
            df.at[i, "forgone_shipped_exit_nav_pct"] = shipped_w * net * 100.0

    dist = (df.groupby("cause")
              .agg(n=("symbol", "size"),
                   median_gain=("gain", "median"),
                   mean_gain=("gain", "mean"),
                   forgone_peak_nav_pct=("forgone_peak_nav_pct", "sum"),
                   forgone_shipped_exit_nav_pct=("forgone_shipped_exit_nav_pct", "sum"))
              .sort_values("n", ascending=False))
    dist["share_pct"] = (dist["n"] / len(df) * 100.0).round(1)
    dist.to_csv(outdir / "cause_distribution.csv")

    # ---------------- 5. baselines over the same window, both of them
    btc = cw["BTCUSDT"].loc[(cw.index >= start) & (cw.index <= end)]
    btc_ret = float(btc.iloc[-1] / btc.iloc[0] - 1.0)
    bd = btc.pct_change().dropna()
    btc_sharpe = float(bd.mean() / bd.std() * np.sqrt(ANNUAL_DAYS)) if bd.std() > 0 else 0.0
    def evaluate(w_by_day: dict[pd.Timestamp, dict[str, float]]) -> dict:
        """NAV, arithmetic Sharpe and max drawdown of a replayed weight path, costs on.

        Weights are lagged one full day: the weight set on close d-1 earns d's return, and
        every change pays COST_PER_SIDE on the traded notional.
        """
        nav = [1.0]
        prev_w: dict[str, float] = {}
        for k in range(1, len(study)):
            d, dprev = study[k], study[k - 1]
            w = w_by_day.get(dprev, {})
            rr = 0.0
            for asset, wt in w.items():
                if wt <= 0 or asset not in cw.columns:
                    continue
                p0, p1 = cw[asset].get(dprev, np.nan), cw[asset].get(d, np.nan)
                if not (np.isnan(p0) or np.isnan(p1)) and p0 > 0:
                    rr += wt * (p1 / p0 - 1.0)
            turn = sum(abs(w.get(x, 0.0) - prev_w.get(x, 0.0))
                       for x in set(w) | set(prev_w))
            nav.append(nav[-1] * (1.0 + rr - turn * COST_PER_SIDE))
            prev_w = w
        navs = pd.Series(nav, index=study)
        nd = navs.pct_change().dropna()
        sh = float(nd.mean() / nd.std() * np.sqrt(ANNUAL_DAYS)) if nd.std() > 0 else 0.0
        dd = float((navs / navs.cummax() - 1.0).min())
        yrs = (study[-1] - study[0]).days / ANNUAL_DAYS
        c = float(navs.iloc[-1] ** (1 / yrs) - 1.0) if yrs > 0 else 0.0
        return {"ret": float(navs.iloc[-1] - 1.0), "sharpe": sh, "mdd": dd, "cagr": c}

    b0 = evaluate(weights_by_day)
    book_ret, book_sharpe = b0["ret"], b0["sharpe"]

    # ---------------- 5b. COUNTERFACTUAL BOOKS -- relax ONE cause, change nothing else.
    # 6 new selection trials, declared. Each is the SHIPPED rule with one rule widened, so
    # the difference is attributable. NONE of these is a proposal; they are the price tag on
    # each named cause, measured rather than accounted.
    def all_sat_tier(sn):          # relax C05: satellite_eligibility off
        return [s for s in sn["tiers"] if sn["tiers"][s] == "satellite"]

    def sat_plus_major(sn):        # relax C04: majors get seats (fix the code defect)
        return [s for s in sn["tiers"]
                if sn["tiers"][s] in ("satellite", "major") and
                (sn["tiers"][s] == "major" or sn["enterable"].get(s))]

    def whole_watchlist(sn):       # relax C03: every watchlist name tradeable
        return [s for s in sn["tiers"] if sn["tiers"][s] != "core"]

    books = {
        "B0_shipped": (shipped_eligible, MAX_SATELLITE_POSITIONS, MAX_SATELLITE_GROSS, True),
        "B1_relax_C05_eligibility_off": (all_sat_tier, MAX_SATELLITE_POSITIONS,
                                         MAX_SATELLITE_GROSS, True),
        "B2_relax_C04_majors_get_seats": (sat_plus_major, MAX_SATELLITE_POSITIONS,
                                          MAX_SATELLITE_GROSS, True),
        "B3_relax_C03_whole_watchlist": (whole_watchlist, MAX_SATELLITE_POSITIONS,
                                         MAX_SATELLITE_GROSS, True),
        "B4_more_seats_6_at_15pct": (shipped_eligible, 6, 0.15, True),
        "B5_whole_watchlist_6_seats_15pct": (whole_watchlist, 6, 0.15, True),
        "B6_min_position_floor_removed": (shipped_eligible, MAX_SATELLITE_POSITIONS,
                                          MAX_SATELLITE_GROSS, False),
    }
    cf: dict[str, dict] = {}
    cf_held: dict[str, dict] = {}
    for name, (efn, ms, sg, mp) in books.items():
        bk = replay_book(efn, ms, sg, TIER_CAPS["satellite"], enforce_min_position=mp)
        cf[name] = evaluate(bk["weights"])
        names = sorted({x for v in bk["held"].values() for x in v})
        cf_held[name] = {
            "distinct_satellites_ever_held": len(names),
            "names": names[:25],
            "asset_days_held": int(sum(len(v) for v in bk["held"].values())),
            "min_position_zeroed_asset_days": bk["min_position_zeroed_days"],
        }
        if name == "B0_shipped":
            B0 = bk

    # ---------------- 6. regime split
    reg = []
    for label, mask in (("btc_uptrend", df["btc_risk_on"]),
                        ("btc_downtrend", ~df["btc_risk_on"])):
        sub = df[mask]
        if sub.empty:
            continue
        top = sub["cause"].value_counts()
        reg.append({"regime": label, "n": len(sub),
                    "top_cause": top.index[0], "top_n": int(top.iloc[0]),
                    "top_share_pct": round(100 * top.iloc[0] / len(sub), 1)})

    # ---------------- 6b. VALIDATION: the replayed funnel against the shipped snapshot.
    # The shipped riskgate.json on 2026-09-30 carries 107 watchlist / 31 tradeable /
    # 16 enterable (profit-audit). If the replayed funnel on the last snapshot date is far
    # from that, the replay is wrong and nothing below it can be trusted.
    # The CONDITIONAL table: of the rallies that actually reached the enterable universe,
    # what happened? The ordered causes mean a universe failure masks everything downstream,
    # so this is the only table in which a detection or sizing cause can be read.
    downstream = ("C06_no_detector_fired", "C07_screener_or_scan_capacity",
                  "C08_validator_capacity_or_expired", "C09_regime_gate_off",
                  "C10_own_regime_gate_off", "C11_below_seat_cutoff", "C12_sized_to_zero",
                  "C13_gate_refused", "C14_owned_exited_before_peak", "C15_captured",
                  "C99_unattributable")
    sub = df[df["cause"].isin(downstream)]
    cond = {"n": len(sub),
            "share_of_all_rallies_pct": round(100 * len(sub) / max(len(df), 1), 2),
            "by_cause": {k: int(v) for k, v in sub["cause"].value_counts().items()},
            "forgone_shipped_exit_nav_pct": round(
                float(sub["forgone_shipped_exit_nav_pct"].sum()), 3)}

    sm: dict[str, set[str]] = defaultdict(set)
    for d, hs in held_by_day.items():
        for x in hs:
            sm[d.strftime("%Y-%m")].add(x)
    seat_months = {k: sorted(v) for k, v in sorted(sm.items())}

    last_snap = snaps[max(snaps)]
    n_tier = Counter(last_snap["tiers"].values())
    funnel_now = {
        "snapshot_date": str(max(snaps).date()),
        "considered": len(universe),
        "watchlist": len(last_snap["watchlist"]),
        "excluded": len(last_snap["excluded"]),
        "excluded_by_filter": dict(Counter(v.split(":")[0]
                                           for v in last_snap["excluded"].values())),
        "tier_counts": dict(n_tier),
        "tradeable": int(n_tier["core"] + n_tier["major"] + n_tier["satellite"]),
        "enterable": int(sum(1 for s, ok in last_snap["enterable"].items() if ok)),
        "shipped_for_comparison": {"watchlist": 107, "tradeable": 31, "enterable": 16},
    }

    res = {
        "window": [str(start.date()), str(end.date())],
        "prereg_seal": "ffda34182aa214d8e160607a4dc83983 (2026-09-30T13:54:54Z)",
        "funnel_validation": funnel_now,
        "seat_holders_by_month": seat_months,
        "rally_rule": {"thresh_pct": a.thresh * 100, "window_days": a.window,
                       "dedupe_days": a.dedupe},
        "universe": {"binance_usdt_spot_TRADING": len(listed),
                     "with_panel_history": len(universe),
                     "no_panel_history": sorted(listed - have)},
        "n_rallies": len(df),
        "n_symbols_with_a_rally": int(df["symbol"].nunique()),
        "shipped_satellite_weight": shipped_w,
        "distribution": json.loads(dist.reset_index().to_json(orient="records")),
        "unattributable_share_pct": float(
            dist.loc["C99_unattributable", "share_pct"]) if "C99_unattributable" in dist.index else 0.0,
        "baselines": {"btc_buy_and_hold_ret": btc_ret, "btc_buy_and_hold_sharpe": btc_sharpe,
                      "shipped_book_ret": book_ret, "shipped_book_sharpe": book_sharpe},
        "counterfactual_books": cf,
        "counterfactual_held_sets": cf_held,
        # INDEPENDENT of the funnel: would the signal pipeline have NOTICED the rally at all?
        # This is SleeveB's question, not SleeveA's, and is reported on its own because the
        # rules sleeve never consults a detector.
        "detector_coverage": {
            "n_rallies": len(df),
            "at_least_one_detector_fired_in_5d_before": int((df["detectors"] != "").sum()),
            "share_pct": round(100 * float((df["detectors"] != "").mean()), 1),
            "by_detector": {
                k: int(df["detectors"].str.contains(k, na=False).sum())
                for k in ("move_24h", "breakout", "volume_spike", "rsi_extreme",
                          "dip_from_high")},
            "ma_cross": "shipped enabled: false -- not evaluated",
            "silent_on_enterable_rallies": int(
                ((df["detectors"] == "") & df["cause"].isin(downstream)).sum()),
        },
        "min_position_zeroed_days": B0["min_position_zeroed_days"],
        "conditional_on_reaching_the_enterable_universe": cond,
        "trials_added": 6,
        "regime_split": reg,
        "captured": int((df["cause"] == "C15_captured").sum()),
        "owned_exited_early": int((df["cause"] == "C14_owned_exited_before_peak").sum()),
        "seats_ever_held": sorted({x for v in held_by_day.values() for x in v}),
        "days_risk_on": int(sum(1 for d in study if float(btc_reg.get(d, 0.0)) > 0)),
        "days_total": len(study),
    }
    (outdir / "track6.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))

    if a.plateau:
        pl = []
        for th, wd in ((0.20, 20), (0.30, 10), (0.30, 20), (0.30, 60), (0.50, 20)):
            n = 0
            for s in universe:
                for r in rally_episodes(cw[s].loc[cw.index <= end], th, wd, a.dedupe):
                    if start <= r["start"] <= end:
                        n += 1
            pl.append({"thresh": th, "window": wd, "n_rallies": n})
        (outdir / "plateau.json").write_text(json.dumps(pl, indent=1))
        print(json.dumps(pl, indent=1))
    return 0


# ------------------------------------------------- the queries a LIVE version would run
# Verified against ops/sql/journal.sql and ops/sql/knowledge.sql column names on
# 2026-09-30. NOT RUN by this script: the live databases are off limits.
LIVE_QUERIES = r"""
-- =============================================================================
-- TRACK 6 LIVE MODE -- the exact queries, per cause. Every table/column verified
-- against ops/sql/journal.sql and ops/sql/knowledge.sql. Read with
-- ops.db.opened(path, readonly=True) and row_factory = sqlite3.Row (never by
-- column position: migrated and fresh DBs differ in column ORDER).
--
-- RUN THESE ONLY AGAINST A SNAPSHOT THE OPS LOCK HANDED YOU, NEVER AGAINST THE
-- LIVE FILE WHILE A BOT IS TRADING.
-- =============================================================================

-- C02..C05  Was the pair in the point-in-time universe, and with what tier?
--           knowledge/universe/<YYYY-MM-DD>.json is the artifact; the DB mirror is:
SELECT captured_at, payload_json
  FROM state_snapshots                      -- knowledge.db
 WHERE captured_at <= :asof
 ORDER BY captured_at DESC LIMIT 1;

-- C06  Did any detector fire on this pair in the pre-rally window?
SELECT signal_id, ts_utc, detector, direction, detector_score, strength,
       status, status_reason, features_json
  FROM signals                              -- journal.db
 WHERE pair = :pair AND ts_utc BETWEEN :from_utc AND :to_utc
 ORDER BY ts_utc;

-- C07  It fired, but the SCREENER dropped it (or the cycle was full).
SELECT signal_id, ts_utc, detector, detector_score, screen_score, screen_provider,
       screen_model, screen_rationale, status, status_reason
  FROM signals
 WHERE pair = :pair AND ts_utc BETWEEN :from_utc AND :to_utc
   AND status IN ('screened_out', 'candidate')
 ORDER BY ts_utc;
-- 'screened_out' = the model said no (screen_rationale says why).
-- 'candidate' still sitting at the end of the cycle = scan capacity
--   (signals.scanner.max_candidates_per_cycle = 5) or the dedupe bucket
--   (dedupe_key, signals.scanner.dedupe_minutes = 240).

-- C08  Screened but never validated, or validated too late.
SELECT s.signal_id, s.ts_utc, s.status, s.status_reason,
       v.ts_utc AS validated_utc, v.verdict, v.confidence, v.horizon_hours,
       v.escalated, v.error, v.outcome_ret, v.outcome_hit
  FROM signals s
  LEFT JOIN signal_validations v ON v.signal_id = s.signal_id
 WHERE s.pair = :pair AND s.ts_utc BETWEEN :from_utc AND :to_utc
 ORDER BY s.ts_utc;
-- status='expired' + no signal_validations row = validator capacity
--   (signals.validator.max_per_day = 6) or the per-asset cooldown
--   (cooldown_min_per_asset = 120) or expire_after_min = 90.

-- C08b  Was the run that WOULD have validated it missed or throttled?
SELECT run_id, stage, kind, started_utc, finished_utc, status, error,
       signal_id, trigger_reason, escalated, escalation_reasons
  FROM runs                                 -- journal.db
 WHERE started_utc BETWEEN :from_utc AND :to_utc
   AND stage IN ('decide', 'validate', 'scan')
 ORDER BY started_utc;
-- status IN ('missed','throttled','killed','failed') is the answer for a whole window.

-- C09..C12  The proposal: did it abstain, and what did it ask for?
SELECT run_id, shadow, ts_utc, module, targets_json, exposure_scale, confidence,
       abstain, horizon_days, valid, invalid_reason, consumed_status,
       consumed_reason, signal_id, approval_status
  FROM proposals                            -- journal.db
 WHERE ts_utc BETWEEN :from_utc AND :to_utc AND shadow = 0
 ORDER BY ts_utc;
-- abstain=1 -> C09-ish (the model declined); targets_json missing the base -> it was
-- never asked for; consumed_status='rejected' + consumed_reason -> the loader refused it.

-- C13  The GATE refused it, and WHICH of the 27 checks.
SELECT id, ts_utc, sleeve, pair, side, intent, callback, allowed, reason, severity,
       checks_json, proposed_stake, nav, gross_exposure, action, run_id
  FROM gate_decisions                       -- journal.db
 WHERE pair = :pair AND intent = 'entry'
   AND ts_utc BETWEEN :from_utc AND :to_utc
 ORDER BY ts_utc;
-- `reason` is the machine-readable slug of the FIRST failing check in CHECK_ORDER
-- (e.g. 'weight_cap:SOL/USDT', 'satellite_count', 'min_position', 'tier:PEPE').
-- `checks_json` is every check name -> pass/fail, so a single row names the cause
-- exactly. Count reasons with:
SELECT reason, COUNT(*) AS n
  FROM gate_decisions
 WHERE intent = 'entry' AND allowed = 0 AND ts_utc BETWEEN :from_utc AND :to_utc
 GROUP BY reason ORDER BY n DESC;

-- C12  Sized to zero: the gate allowed it but cap_stake() returned 0.
SELECT ts_utc, pair, reason, action, proposed_stake, nav, checks_json
  FROM gate_decisions
 WHERE pair = :pair AND callback = 'custom_stake_amount'
   AND ts_utc BETWEEN :from_utc AND :to_utc
 ORDER BY ts_utc;
-- action='clamp' with proposed_stake below risk.min_notional_usdt (25) is the signature.

-- C14/C15  We DID own it: when did we get in, when out, and at what price?
SELECT o.id, o.ts_utc, o.sleeve, o.pair, o.side, o.order_type, o.status,
       o.amount, o.price, o.ft_trade_id, o.mode,
       f.ts_utc AS fill_utc, f.fill_amount, f.fill_price, f.fee_amount
  FROM orders o LEFT JOIN fills f ON f.order_id = o.id
 WHERE o.pair = :pair AND o.ts_utc BETWEEN :from_utc AND :to_utc
 ORDER BY o.ts_utc;

-- C14  WHY we exited before the peak.
SELECT ts_utc, pair, intent, callback, reason, action, trade_id, checks_json
  FROM gate_decisions
 WHERE pair = :pair AND intent = 'exit' AND ts_utc BETWEEN :from_utc AND :to_utc
 ORDER BY ts_utc;

-- Cross-cutting: was the whole SYSTEM down or locked for that window?
SELECT opened_utc, closed_utc, kind, severity, subkind, detail, root_cause
  FROM incidents                            -- journal.db
 WHERE opened_utc <= :to_utc
   AND (closed_utc IS NULL OR closed_utc >= :from_utc)
 ORDER BY opened_utc;

SELECT sleeve, key, value, updated_utc
  FROM risk_state                           -- journal.db
 WHERE key IN ('locked_until', 'monthly_locked', 'trades_today');

SELECT asset, scope, kind, expires_at, set_at, reason
  FROM flags                                -- knowledge.db (written ONLY via ops.lib.flags)
 WHERE expires_at >= :from_utc;

SELECT started_utc, finished_utc, status, rows_written, error
  FROM ingest_runs                          -- knowledge.db: a data gap is its own cause
 WHERE started_utc BETWEEN :from_utc AND :to_utc
 ORDER BY started_utc;

-- =============================================================================
-- COLUMNS A LIVE VERSION NEEDS THAT DO NOT EXIST TODAY
-- =============================================================================
-- 1. signals has NO per-candidate row for a pair the scanner LOOKED AT and did not
--    flag. "No detector fired" is therefore an ABSENCE of evidence, not evidence.
--    NEEDED: a `scan_cycles` table (scan_id, ts_utc, pairs_seen INTEGER,
--    candidates_found INTEGER, candidates_kept INTEGER, dropped_reason TEXT) or a
--    `scanned_pairs_json` column on a per-cycle row, so a miss can be told apart
--    from a blind spot.
-- 2. signals.screen_score is written only for candidates that reached the screener.
--    NEEDED: a row (or a JSON array on the cycle) for every candidate the screener
--    saw INCLUDING those cut by max_candidates_per_cycle, with the cut reason.
-- 3. There is no record of the UNIVERSE SNAPSHOT that was in force at a given
--    instant inside the DB -- only the dated file under knowledge/universe/.
--    NEEDED: a `universe_snapshots` table (captured_at, pairs_json, tiers_json,
--    scores_json, exit_only_json, excluded_json) so a point-in-time replay does not
--    depend on a file surviving.
-- 4. gate_decisions has no row for an entry that was never ATTEMPTED because the
--    strategy's target weight was already zero. The `instrument("want_none", ...)`
--    call in SleeveA emits a log line, not a row.
--    NEEDED: a `want_none` / `sized_zero` row (ts_utc, sleeve, pair, reason) so
--    "the trend gate said no" is distinguishable from "nothing looked at it".
-- 5. No table records which satellites HELD the seats at a given time, so
--    "lost the seat competition" has to be rebuilt from nav_daily.positions_json.
--    NEEDED: `satellite_seats` (ts_utc, sleeve, seat_index, asset, score, rank).
"""


if __name__ == "__main__":
    sys.exit(main())
