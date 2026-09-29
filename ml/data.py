"""Point-in-time dataset builder over the real panel — the layer every honest number rests on.

What this module refuses to do
------------------------------
* It will not hand back a survivor-only panel. All 747 USDT symbols that ever existed are
  in ``panel_1d.parquet``, 282 of them now dead, and they stay in. A panel of the coins
  that are still listed proves the *opposite* of what it appears to: the losers were
  deleted by the sample, not by the strategy.
* It will not hand back a series with a symbol-reuse break in it. ``LUNAUSDT`` was reused
  for LUNA 2.0 on 2022-05-31 and carries a **177,399x** one-day "return"; untreated it
  fabricated a +724% growth episode and a 112,518x equity curve in an earlier run
  (``docs/design/growth-audit.md`` §5). :func:`split_segments` cuts every series at such a
  break and labels the pieces ``LUNAUSDT#0``, ``LUNAUSDT#1`` — different instruments,
  because that is what they are. 24 symbols in the full history carry a >8x single-day
  discontinuity; 23 are leveraged tokens rebasing.
* It will not let a feature see a coin before the coin was visible. :func:`eligibility` is
  recomputed **at every timestamp** from trailing windows only, so a name enters the
  universe on the day it actually qualified and leaves on the day it stopped.

Where the data is
-----------------
``$EARN_ML_DATA`` (default ``~/earn-ml-data``) holds

===========================  ==============================================================
``panel_1d.parquet``         841,491 daily rows, 747 USDT symbols, 2017-08-17 -> 2026-09-24
``funding.parquet``          2,567,781 funding prints, 653 perps, from 2019-09
``candles/<PAIR>-<tf>.feather``  108 pairs x {1h, 4h, 1d}; BTC/ETH 1h from 2017-08 (79,661 bars)
===========================  ==============================================================

The daily panel is the **wide** universe and the only survivorship-free source. The feather
candles are the *narrow* intraday source — 108 live pairs, so any study on them is
survivor-biased by construction and :func:`load_intraday` says so in its docstring rather
than leaving the next reader to find out.

Eligibility, and why these thresholds
-------------------------------------
The funnel mirrors ``config/earn.yaml: universe.rules`` so a backtest and the live universe
mean the same thing, with one deliberate difference recorded in :class:`Eligibility`: the
config's ``min_listing_age_days: 180`` is justified there as *data sufficiency*, and
``growth-audit.md`` §1.2 measured it as one of the two strongest forward-predictive features
in the universe with **the break at about 2 years, not 180 days** (median forward 90d:
-26.6% at 180-270d against -14.7% past 4 years). The default here stays at the config value
so the harness measures the universe the bots actually trade; ``age_days`` is emitted as a
feature so a model can find the real break itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

__all__ = [
    "DEFAULT_DATA_ROOT",
    "Dataset",
    "Eligibility",
    "PanelSpec",
    "build_dataset",
    "data_root",
    "discontinuities",
    "eligibility",
    "funding_daily",
    "load_daily_panel",
    "load_intraday",
    "resample_panel",
    "segment_id",
    "split_segments",
]

DEFAULT_DATA_ROOT = Path.home() / "earn-ml-data"

#: A single-bar close-to-close ratio outside ``[1/BREAK_RATIO, BREAK_RATIO]`` is treated as a
#: symbol reuse or a rebase, not a return. 8x is the threshold ``growth-audit.md`` §5 used to
#: find the 24 affected symbols; the largest genuine single-day move in the panel is far
#: below it and LUNA's 177,399x is far above, so the choice is not close.
BREAK_RATIO = 8.0

#: Segments shorter than this are dropped: a 3-bar fragment left behind by a rebase supports
#: no trailing window and only adds noise to a cross-section.
MIN_SEGMENT_BARS = 30

_BARS_PER_YEAR = {"1h": 24 * 365, "4h": 6 * 365, "1d": 365.0}

#: Leveraged-token suffixes from ``config/earn.yaml: universe.rules.leveraged_suffixes``.
#: These rebase daily, which is where 23 of the 24 discontinuities live, and they are never
#: tradeable. Excluded by default rather than segmented, because their price is not a price.
_LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")


def data_root() -> Path:
    """``$EARN_ML_DATA`` or ``~/earn-ml-data``. Read at call time so a test can redirect it."""
    return Path(os.environ.get("EARN_ML_DATA") or DEFAULT_DATA_ROOT)


def cache_root() -> Path:
    """``$EARN_ML_CACHE`` or ``<data_root>/cache``. Created on demand."""
    root = Path(os.environ.get("EARN_ML_CACHE") or (data_root() / "cache"))
    root.mkdir(parents=True, exist_ok=True)
    return root


# --------------------------------------------------------------------------- spec


@dataclass(frozen=True)
class Eligibility:
    """The point-in-time universe funnel. Every window is trailing; nothing looks forward.

    Defaults mirror ``config/earn.yaml: universe.rules`` plus the ``satellite`` tier floor,
    which is the *tradeable* bar (32 pairs today) rather than the watchlist bar.
    """

    min_age_days: int = 180
    min_median_quote_volume: float = 5_000_000.0
    volume_window_days: int = 90
    min_ann_vol: float = 0.20
    vol_window_days: int = 120
    #: Drop leveraged tokens (UP/DOWN/BULL/BEAR). Their price is a rebasing derivative.
    exclude_leveraged: bool = True
    #: Tokenized gold, from ``universe.rules.excluded_bases``.
    exclude_bases: tuple[str, ...] = ("XAUT", "PAXG")

    def as_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in vars(self).items()}


@dataclass(frozen=True)
class PanelSpec:
    """Everything that decides what :func:`build_dataset` returns. Hashed into the cache key.

    ``horizons`` are in **bars of** ``timeframe``: at ``1d`` the tuple ``(1, 7)`` is one day
    and one week. :func:`ml.labels.horizon_bars` converts the owner-facing names
    (``1h``/``4h``/``1d``/``7d``) for whichever timeframe is loaded.
    """

    timeframe: Literal["1h", "4h", "1d"] = "1d"
    start: str | None = None
    end: str | None = None
    #: ``None`` means the whole survivorship-free universe.
    symbols: tuple[str, ...] | None = None
    eligibility: Eligibility = field(default_factory=Eligibility)
    #: Cut series at symbol-reuse / rebase breaks and treat the pieces as separate instruments.
    split_at_discontinuities: bool = True
    seed: int = 0

    @property
    def bars_per_year(self) -> float:
        return _BARS_PER_YEAR[self.timeframe]

    @property
    def bars_per_day(self) -> float:
        return {"1h": 24.0, "4h": 6.0, "1d": 1.0}[self.timeframe]

    def as_dict(self) -> dict:
        d = dict(vars(self))
        d["eligibility"] = self.eligibility.as_dict()
        d["symbols"] = list(self.symbols) if self.symbols else None
        return d


@dataclass(frozen=True)
class Dataset:
    """A long-format point-in-time panel plus its provenance.

    ``frame`` columns: ``ts``, ``symbol`` (the *segment* id after splitting), ``base``,
    ``open``, ``high``, ``low``, ``close``, ``volume``, ``quote_volume``, ``trades``,
    ``age_days``, ``eligible``, and whatever :mod:`ml.features` adds.

    ``eligible`` is the only gate a study may use to select rows. Filtering on anything else
    at panel level — "coins with enough history", "coins still listed" — is survivorship
    bias with a friendly name.
    """

    frame: pd.DataFrame
    spec: PanelSpec
    #: Symbols that entered the raw panel, before eligibility. Includes every dead coin.
    n_symbols_raw: int
    #: Segments after discontinuity splitting.
    n_segments: int
    #: Segments that were eligible on at least one bar.
    n_symbols_eligible: int
    #: ``{symbol: n_breaks}`` for every series that was cut.
    breaks: dict[str, int]
    #: Segments whose last bar is before the panel end — coins that died.
    n_dead: int

    def eligible(self) -> pd.DataFrame:
        """The rows a study may score. Never reach past this for row selection."""
        return self.frame.loc[self.frame["eligible"]]

    def summary(self) -> dict:
        f = self.frame
        elig = f["eligible"]
        return {
            "timeframe": self.spec.timeframe,
            "rows": int(len(f)),
            "rows_eligible": int(elig.sum()),
            "symbols_raw": self.n_symbols_raw,
            "segments": self.n_segments,
            "symbols_eligible": self.n_symbols_eligible,
            "segments_dead": self.n_dead,
            "symbols_split": len(self.breaks),
            "breaks_total": int(sum(self.breaks.values())),
            "ts_min": None if f.empty else str(f["ts"].min()),
            "ts_max": None if f.empty else str(f["ts"].max()),
        }


# --------------------------------------------------------------------------- loading


_RAW_RENAME = {"date": "ts", "o": "open", "h": "high", "l": "low", "c": "close",
               "v": "volume", "qv": "quote_volume", "n": "trades"}


def load_daily_panel(*, root: Path | None = None,
                     symbols: tuple[str, ...] | None = None,
                     path: Path | None = None) -> pd.DataFrame:
    """The survivorship-free wide daily panel, long format, sorted by (symbol, ts).

    747 symbols including 282 that are dead. Nothing is filtered here — not leveraged
    tokens, not stablecoins, not the LUNA break. Filtering is :func:`build_dataset`'s job
    and it records what it dropped.
    """
    src = path or ((root or data_root()) / "panel_1d.parquet")
    if not src.exists():
        raise FileNotFoundError(
            f"daily panel not found at {src}. Set $EARN_ML_DATA to the directory holding "
            "panel_1d.parquet (see ml/data.py module docstring)."
        )
    df = pd.read_parquet(src)
    df = df.rename(columns=_RAW_RENAME)
    if symbols is not None:
        df = df.loc[df["symbol"].isin(set(symbols))]
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    keep = ["ts", "symbol", "open", "high", "low", "close", "volume", "quote_volume", "trades"]
    for col in keep:
        if col not in df.columns:
            df[col] = np.nan
    df = df[keep].sort_values(["symbol", "ts"], kind="stable").reset_index(drop=True)
    return df


def load_intraday(pair: str, timeframe: Literal["1h", "4h", "1d"], *,
                  root: Path | None = None) -> pd.DataFrame:
    """One pair's feather candles, e.g. ``load_intraday("BTC/USDT", "1h")``.

    **Survivor-biased on purpose and by necessity.** Only 108 currently-listed pairs have
    feather candles; the 282 dead symbols have daily bars only. So an intraday
    cross-sectional study on this source is a study of the survivors, and its results must
    not be quoted as if they came from the wide panel. Intraday work on BTC/ETH — where the
    history reaches 2017-08 and there is no cross-section to bias — is fine.
    """
    base = (root or data_root()) / "candles"
    fname = pair.replace("/", "_") + f"-{timeframe}.feather"
    src = base / fname
    if not src.exists():
        raise FileNotFoundError(f"no candles at {src}")
    df = pd.read_feather(src)
    df = df.rename(columns={"date": "ts"})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df["symbol"] = pair.replace("/", "")
    df["quote_volume"] = df["close"] * df["volume"]
    df["trades"] = np.nan
    cols = ["ts", "symbol", "open", "high", "low", "close", "volume", "quote_volume", "trades"]
    return df[cols].sort_values("ts", kind="stable").reset_index(drop=True)


def funding_daily(*, root: Path | None = None,
                  path: Path | None = None) -> pd.DataFrame:
    """Perp funding aggregated to a daily **annualised** rate, per symbol.

    Columns ``ts``, ``symbol``, ``funding_ann``, ``funding_prints``. Three prints a day at
    8h intervals, so the annualisation is ``mean(rate) * 3 * 365``.

    Coverage is 653 perps from 2019-09, not the whole panel — and ``growth-audit.md`` §1.6
    measured the extreme-funding flag firing on **6 observations** in 2025-26 against 12.25%
    of coin-weeks in 2019-22. It is a rare-event tail flag with almost no live value today,
    and it must be used as an **absolute level** (>= 40% annualised), never as a rolling
    self-percentile, which converts the signal into noise.
    """
    src = path or ((root or data_root()) / "funding.parquet")
    if not src.exists():
        raise FileNotFoundError(f"funding not found at {src}")
    f = pd.read_parquet(src).rename(columns={"t": "ts", "fr": "rate"})
    f["ts"] = pd.to_datetime(f["ts"], utc=True)
    f["day"] = f["ts"].dt.floor("D")
    g = f.groupby(["symbol", "day"], sort=True)["rate"].agg(["mean", "size"]).reset_index()
    g = g.rename(columns={"day": "ts", "size": "funding_prints"})
    g["funding_ann"] = g["mean"] * 3.0 * 365.0
    return g[["ts", "symbol", "funding_ann", "funding_prints"]]


def resample_panel(df: pd.DataFrame, timeframe: Literal["1h", "4h", "1d"]) -> pd.DataFrame:
    """Down-sample a long panel to ``timeframe`` with correct OHLCV aggregation."""
    rule = {"1h": "1h", "4h": "4h", "1d": "1D"}[timeframe]
    agg = {"open": "first", "high": "max", "low": "min", "close": "last",
           "volume": "sum", "quote_volume": "sum", "trades": "sum"}
    out = []
    for sym, part in df.groupby("symbol", sort=True):
        r = part.set_index("ts").resample(rule).agg(agg).dropna(subset=["close"])
        r["symbol"] = sym
        out.append(r.reset_index())
    if not out:
        return df.iloc[:0].copy()
    cols = ["ts", "symbol", "open", "high", "low", "close", "volume", "quote_volume", "trades"]
    return pd.concat(out, ignore_index=True)[cols].sort_values(
        ["symbol", "ts"], kind="stable").reset_index(drop=True)


# --------------------------------------------------------------- discontinuity handling


def segment_id(symbol: str, k: int) -> str:
    """``BTCUSDT`` for the first segment, ``LUNAUSDT#1`` for the second and later.

    The unsuffixed name is kept for segment 0 so that the overwhelming majority of symbols —
    which have exactly one segment — read identically to their raw ticker.
    """
    return symbol if k == 0 else f"{symbol}#{k}"


def discontinuities(df: pd.DataFrame, *, ratio: float = BREAK_RATIO) -> pd.DataFrame:
    """Every single-bar close-to-close jump beyond ``ratio``, one row per break.

    Columns ``symbol``, ``ts``, ``prev_close``, ``close``, ``ratio``. This is a *report*, not
    a filter: it exists so the panel's known landmines can be printed and compared against
    the 24 symbols ``growth-audit.md`` §5 found, rather than silently handled.
    """
    d = df.sort_values(["symbol", "ts"], kind="stable")
    prev = d.groupby("symbol", sort=False)["close"].shift(1)
    r = d["close"] / prev
    hit = r.notna() & ((r >= ratio) | (r <= 1.0 / ratio))
    out = pd.DataFrame({
        "symbol": d.loc[hit, "symbol"].to_numpy(),
        "ts": d.loc[hit, "ts"].to_numpy(),
        "prev_close": prev[hit].to_numpy(),
        "close": d.loc[hit, "close"].to_numpy(),
        "ratio": r[hit].to_numpy(),
    })
    return out.sort_values("ratio", ascending=False, kind="stable").reset_index(drop=True)


def split_segments(df: pd.DataFrame, *, ratio: float = BREAK_RATIO,
                   min_bars: int = MIN_SEGMENT_BARS) -> tuple[pd.DataFrame, dict[str, int]]:
    """Cut every series at its discontinuities and rename the pieces. Returns (panel, breaks).

    The bar **at** the break starts the new segment, so no return is ever computed across a
    break. Segments under ``min_bars`` are dropped. ``breaks`` maps each cut symbol to how
    many cuts it took, which is what a report prints.
    """
    d = df.sort_values(["symbol", "ts"], kind="stable").reset_index(drop=True)
    prev = d.groupby("symbol", sort=False)["close"].shift(1)
    r = d["close"] / prev
    is_break = r.notna() & ((r >= ratio) | (r <= 1.0 / ratio))
    # cumsum of break flags within each symbol == segment index
    seg = is_break.groupby(d["symbol"]).cumsum().astype(int)
    breaks = {sym: int(n) for sym, n in seg.groupby(d["symbol"]).max().items() if n > 0}
    d["symbol"] = [segment_id(s, k) for s, k in zip(d["symbol"], seg, strict=True)]
    sizes = d.groupby("symbol", sort=False)["close"].transform("size")
    d = d.loc[sizes >= min_bars].reset_index(drop=True)
    return d, breaks


# --------------------------------------------------------------------------- eligibility


def eligibility(df: pd.DataFrame, rules: Eligibility, *,
                bars_per_day: float = 1.0,
                bars_per_year: float = 365.0) -> pd.DataFrame:
    """Recompute the universe funnel at **every** timestamp. Adds ``age_days`` and ``eligible``.

    Every input is a trailing window ending at the bar being judged, so the value on row *i*
    could have been computed on that bar and nothing later is used. The four gates:

    * ``age_days >= min_age_days`` — bars since this segment's first print. A segment created
      by a discontinuity split starts its clock at the split, which is correct: LUNA 2.0 was
      four days old on 2022-06-04 whatever the ticker said.
    * trailing median quote volume over ``volume_window_days`` ``>= min_median_quote_volume``
      — the tradeable liquidity floor, and the third of the three features that held its sign
      in all three regimes.
    * trailing annualised close-to-close vol over ``vol_window_days`` ``>= min_ann_vol`` —
      removes pegs. U sits at 0.4% annualised; TRX at 0.21 is a real coin, which is why the
      floor is 0.20 and not 0.30.
    * not a leveraged token, not an excluded base.

    A partial window does **not** qualify: ``min_periods`` equals the full window, because a
    coin whose median volume is computed from 3 days of data has not demonstrated liquidity.
    """
    d = df.sort_values(["symbol", "ts"], kind="stable").reset_index(drop=True)
    g = d.groupby("symbol", sort=False)

    d["age_days"] = g.cumcount() / float(bars_per_day)

    vol_win = max(1, int(round(rules.volume_window_days * bars_per_day)))
    med_qv = g["quote_volume"].transform(
        lambda s: s.rolling(vol_win, min_periods=vol_win).median())

    logret = np.log(d["close"] / g["close"].shift(1))
    vwin = max(2, int(round(rules.vol_window_days * bars_per_day)))
    ann_vol = logret.groupby(d["symbol"], sort=False).transform(
        lambda s: s.rolling(vwin, min_periods=vwin).std()) * np.sqrt(bars_per_year)

    base = d["symbol"].str.replace(r"#\d+$", "", regex=True)
    leveraged = pd.Series(False, index=d.index)
    if rules.exclude_leveraged:
        for suf in _LEVERAGED_SUFFIXES:
            leveraged |= base.str.endswith(suf)
    excluded = base.str.replace("USDT$", "", regex=True).isin(set(rules.exclude_bases))

    d["median_quote_volume"] = med_qv
    d["ann_vol_window"] = ann_vol
    d["eligible"] = (
        (d["age_days"] >= rules.min_age_days)
        & (med_qv >= rules.min_median_quote_volume)
        & (ann_vol >= rules.min_ann_vol)
        & ~leveraged
        & ~excluded
    ).fillna(False)
    return d


# --------------------------------------------------------------------------- builder


def build_dataset(spec: PanelSpec | None = None, *,
                  root: Path | None = None,
                  frame: pd.DataFrame | None = None) -> Dataset:
    """The one entry point. Load, split at breaks, gate point-in-time, return with provenance.

    ``frame`` injects a panel instead of reading one, which is how the tests run without the
    real 27 MB parquet and how a later phase can hand in a synthetic panel to prove a model
    recovers a signal it knows is there.

    Order matters and is not arbitrary: **split before gating**, so a fragment left by a
    rebase starts its own age clock and cannot inherit four years of the previous coin's
    listing history.
    """
    spec = spec or PanelSpec()
    # Checked BEFORE the load, so the refusal is about the request rather than about whether the
    # parquet happened to be reachable on this host.
    if spec.timeframe != "1d" and frame is None:
        raise ValueError(
            "the wide survivorship-free panel is daily only. For 1h/4h use load_intraday() "
            "per pair and read its survivorship warning first."
        )
    raw = frame.copy() if frame is not None else load_daily_panel(
        root=root, symbols=spec.symbols)
    if frame is not None:
        raw["ts"] = pd.to_datetime(raw["ts"], utc=True)
        if spec.symbols is not None:
            raw = raw.loc[raw["symbol"].isin(set(spec.symbols))]
    n_raw = int(raw["symbol"].nunique())
    panel_end = raw["ts"].max() if not raw.empty else pd.NaT
    if spec.split_at_discontinuities:
        raw, breaks = split_segments(raw)
    else:
        breaks = {}

    d = eligibility(raw, spec.eligibility,
                    bars_per_day=spec.bars_per_day, bars_per_year=spec.bars_per_year)
    d["base"] = d["symbol"].str.replace(r"#\d+$", "", regex=True).str.replace(
        "USDT$", "", regex=True)

    # Date window is applied LAST, after trailing windows are computed, so a study starting
    # in 2021 still uses 2020 history for its 120-day volatility gate rather than spending
    # the first four months ineligible for a reason that is an artefact of the slice.
    if spec.start is not None:
        d = d.loc[d["ts"] >= pd.Timestamp(spec.start, tz="UTC")]
    if spec.end is not None:
        d = d.loc[d["ts"] <= pd.Timestamp(spec.end, tz="UTC")]
    d = d.reset_index(drop=True)

    last = d.groupby("symbol", sort=False)["ts"].max()
    # "Dead" is measured against the RAW panel end, not the slice end: every segment looks
    # dead if you cut the panel in 2022.
    cutoff = panel_end - pd.Timedelta(days=14) if pd.notna(panel_end) else pd.NaT
    n_dead = 0 if pd.isna(cutoff) else int((last < cutoff).sum())

    return Dataset(
        frame=d, spec=spec, n_symbols_raw=n_raw,
        n_segments=int(d["symbol"].nunique()),
        n_symbols_eligible=int(d.loc[d["eligible"], "symbol"].nunique()),
        breaks=breaks, n_dead=n_dead,
    )


def with_funding(ds: Dataset, *, root: Path | None = None,
                 path: Path | None = None) -> Dataset:
    """Left-join daily annualised funding onto a dataset. Missing means *no perp*, not zero.

    ``funding_ann`` stays NaN where the coin has no perpetual (125 of 498 ever-eligible
    symbols) and where funding history had not started. Filling it with 0 would assert that
    a spot-only coin has neutral funding, which is a different and false claim.
    """
    f = funding_daily(root=root, path=path)
    d = ds.frame.copy()
    d["_base_symbol"] = d["symbol"].str.replace(r"#\d+$", "", regex=True)
    d["_day"] = d["ts"].dt.floor("D")
    merged = d.merge(f.rename(columns={"symbol": "_base_symbol", "ts": "_day"}),
                     on=["_base_symbol", "_day"], how="left")
    merged = merged.drop(columns=["_base_symbol", "_day"])
    return replace(ds, frame=merged)
