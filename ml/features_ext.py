"""Extended features — the wide net, every one of them trailing, every one of them labelled.

:mod:`ml.features` holds the nine the project had already measured a forward sign for. This
module holds the other ninety-odd the owner asked for ("collate all possible data that would
help it"), organised so that a selection run can tell which ones survive and a reader can tell
which ones could never have worked.

The one distinction that decides how a feature may be judged
------------------------------------------------------------
Every feature here carries ``scope`` in :data:`META`:

* ``scope="coin"`` — varies across the cross-section at a timestamp. Cross-sectional rank IC is
  a meaningful measurement of it.
* ``scope="market"`` — **constant across the cross-section at a timestamp** (breadth, dispersion,
  BTC dominance, DVOL, days-to-FOMC, day of week). Its cross-sectional rank IC is exactly zero
  by construction, at every horizon, forever. Ranking it against forward cross-sectional returns
  and reporting "no signal" would be measuring arithmetic, not the market.

A market feature can only be judged on a **time-series** target — forward market/BTC realised
vol, forward market drawdown, or as a regime conditioner interacting with a coin feature — and
:mod:`ml.select` routes it that way. This is the single most common way a feature study of a
crypto panel wastes a week, so the scope is a required field on every registration rather than
a comment.

What is genuinely here, and what is not
---------------------------------------
======================  ===================================================================
Group                   Source, and its real coverage
======================  ===================================================================
``price`` ``vol``       ``panel_1d.parquet``: 841,491 rows, 747 symbols, all of ``ohlcv`` +
``shape`` ``range``     trade count non-null. Full history, survivorship-free. No caveat.
``trend`` ``drawdown``
``micro``
``interact``            Same panel, no new source. Four features added **after** rounds 1-4 of
                        ``ml.select`` measured that every group addition made the out-of-sample
                        IC worse: the vol denominator and the age/vol quotient the growth audit's
                        double sort implies. One of the four (``age_inv_vol``) is the strongest
                        single feature in this catalogue on forward 90-day return, forward
                        realised vol and forward drawdown; the other three are measured noise and
                        are kept only because deleting a tested loser is how a catalogue starts
                        lying about its hit rate.
``xsec``                Same panel, ranked/aggregated **within the eligible universe** at each
``market``              timestamp. See :func:`cross_sectional_rank_eligible` for why the
                        landed ``_xs`` columns are not quite this.
``deriv`` funding       ``funding.parquet``: 653 perps from 2019-09 — **not** the whole panel,
                        and the extreme-funding flag fires on 6 coin-weeks in 2025-26.
``deriv`` OI/taker      ``binance_archive`` metrics rollup: **BTCUSDT only from 2020-09 and
                        ETHUSDT from 2021-12**. There is no open-interest history for the
                        other 745 symbols, so OI can only enter as a ``market`` feature. A
                        cross-sectional OI factor is not available at any price.
``deriv`` implied vol   Deribit DVOL daily, **BTC and ETH only, from 2021-03-24** (2,009 days).
                        Market-scope. This is the input behind the project's R2 0.268 on
                        forward realised vol, which is the strongest honest target it has.
``calendar``            Deterministic from the timestamp, plus the real FOMC statement dates.
                        Scheduled meetings are published two years ahead, so days-to-FOMC is
                        point-in-time; the 2020 **emergency** meetings were not, and
                        :func:`load_fomc_dates` flags them rather than quietly including them.
``news`` / text         **Not available.** ``knowledge/news/`` in the runtime copy contains one
                        ``.gitkeep`` and ``knowledge/briefs/`` one file dated today. There is no
                        historical news archive on this host, so a point-in-time news feature
                        cannot be built — not "would be hard", cannot. Any sentiment feature
                        fitted here would be fitted on an archive assembled after the fact,
                        which is the purest available form of lookahead. Nothing in this module
                        reads text.
``book``                **Not available.** ``bookDepth`` is in the archive's manifest as a known
                        dataset from 2023-01 but **has not been downloaded** — the local cache
                        holds ``metrics/`` only. Order-book imbalance is therefore absent and
                        the microstructure group is limited to what OHLCV + trade count supply.
======================  ===================================================================

Offline-safe by construction
----------------------------
:func:`load_exog` probes for DVOL, the OI rollup and the FOMC list, returns an
:class:`ExogBundle` that says what it found, and **never fetches anything**. Features whose
source is missing are dropped from the requested set and named in
:meth:`ExogBundle.missing_groups`, so a run on a host with only the parquet still produces a
complete price/vol/xsec/market study rather than a traceback.

The assertion, again
--------------------
:func:`assert_no_lookahead_ext` is the same proof as :func:`ml.features.assert_no_lookahead`:
rebuild on a prefix of the panel, require identical values on the shared rows. It is run over
every feature in this module by ``tests/test_ml/test_features_ext.py`` and it is the reason the
trailing claim above is a claim about behaviour rather than about intent. Two families in here
are exactly where a lookahead would hide and both are covered by it: the running-maximum family
(``dist_from_high_*``, ``days_since_ath``, ``maxdd_*``, ``ulcer_*``), whose windows include the
current bar, and the market aggregates, which are computed per timestamp and would silently
become full-sample statistics if a ``groupby`` were dropped.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ml.data import data_root
from ml.features import LookaheadError, cross_sectional_rank

__all__ = [
    "EXT_BUILDERS",
    "GROUPS",
    "META",
    "ExogBundle",
    "ExtContext",
    "FeatureMeta",
    "add_ext_features",
    "assert_no_lookahead_ext",
    "coin_features",
    "cross_sectional_rank_eligible",
    "ext_feature_columns",
    "load_dvol",
    "load_exog",
    "load_fomc_dates",
    "load_oi_daily",
    "market_features",
    "rank_suffix_features",
]


# --------------------------------------------------------------------------- registration


@dataclass(frozen=True)
class FeatureMeta:
    """What a feature is, where it comes from, and how it is allowed to be judged.

    ``scope`` is not documentation. :mod:`ml.select` reads it to decide whether a feature goes
    into the cross-sectional IC table or the time-series table, and a ``market`` feature scored
    cross-sectionally would report a structural zero as an empirical finding.
    """

    name: str
    group: str
    scope: str                       # "coin" | "market"
    needs: tuple[str, ...] = ()      # extra frame columns, or exog keys
    note: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "group": self.group, "scope": self.scope,
                "needs": list(self.needs), "note": self.note}


#: ``name -> builder``. A builder takes a :class:`_Work` and returns a Series on ``w.d``'s index.
EXT_BUILDERS: dict[str, Callable[[_Work], pd.Series]] = {}
#: ``name -> FeatureMeta``. Parallel to :data:`EXT_BUILDERS` and always the same keys.
META: dict[str, FeatureMeta] = {}


def _reg(name: str, group: str, *, scope: str = "coin", needs: Sequence[str] = (),
         note: str = "") -> Callable:
    """Register a builder. Raises on a duplicate name, because a silent overwrite of a feature
    builder is a study that measures one thing and reports another."""
    if scope not in ("coin", "market"):
        raise ValueError(f"{name}: scope must be 'coin' or 'market', not {scope!r}")

    def deco(fn: Callable[[_Work], pd.Series]) -> Callable:
        if name in EXT_BUILDERS:
            raise KeyError(f"duplicate feature registration: {name!r}")
        EXT_BUILDERS[name] = fn
        META[name] = FeatureMeta(name=name, group=group, scope=scope,
                                 needs=tuple(needs), note=note)
        return fn
    return deco


def _reg_fn(name: str, group: str, fn: Callable[[_Work], pd.Series], *,
            scope: str = "coin", needs: Sequence[str] = (), note: str = "") -> None:
    """Loop-friendly form of :func:`_reg`, for the parameterised families."""
    _reg(name, group, scope=scope, needs=needs, note=note)(fn)


def ext_feature_columns(groups: Sequence[str] | None = None,
                        scope: str | None = None) -> list[str]:
    """Feature names filtered by group and/or scope, in registration order."""
    out = []
    for n, m in META.items():
        if groups is not None and m.group not in groups:
            continue
        if scope is not None and m.scope != scope:
            continue
        out.append(n)
    return out


def coin_features() -> list[str]:
    """The features a cross-sectional rank IC is a meaningful measurement of."""
    return ext_feature_columns(scope="coin")


def market_features() -> list[str]:
    """The features whose cross-sectional IC is zero by construction. Judge on a time series."""
    return ext_feature_columns(scope="market")


# --------------------------------------------------------------------------- exogenous data


@dataclass(frozen=True)
class ExogBundle:
    """The optional external series, and an honest statement of which of them exist here.

    Every field may be ``None``. :func:`load_exog` never raises and never fetches; a source
    that is not on disk is a missing group, not an exception, because the price/vol/xsec study
    is the bulk of the value and must run on a bare host.
    """

    #: ``ts``, ``dvol`` (annualised, in vol points / 100) — BTC by default.
    dvol: pd.DataFrame | None = None
    #: ``ts``, ``oi``, ``oi_value``, ``taker_ls``, ``toptrader_ls``, ``count_ls`` — BTCUSDT daily.
    oi: pd.DataFrame | None = None
    #: FOMC statement dates as UTC timestamps.
    fomc: tuple[pd.Timestamp, ...] = ()
    #: ``True`` when the FOMC list is known to contain the 2020 emergency meetings.
    fomc_has_unscheduled: bool = False
    #: ``key -> why it is absent``, printed by a study rather than discovered by its reader.
    absent: tuple[tuple[str, str], ...] = ()

    def has(self, key: str) -> bool:
        if key == "dvol":
            return self.dvol is not None and not self.dvol.empty
        if key == "oi":
            return self.oi is not None and not self.oi.empty
        if key == "fomc":
            return bool(self.fomc)
        return False

    def missing_groups(self) -> list[str]:
        return [k for k in ("dvol", "oi", "fomc") if not self.has(k)]

    def report(self) -> dict:
        def span(df):
            if df is None or df.empty:
                return None
            return [str(df["ts"].min()), str(df["ts"].max()), int(len(df))]
        return {
            "dvol": span(self.dvol), "oi": span(self.oi),
            "fomc_n": len(self.fomc),
            "fomc_span": None if not self.fomc else [str(self.fomc[0]), str(self.fomc[-1])],
            "fomc_has_unscheduled": self.fomc_has_unscheduled,
            "absent": dict(self.absent),
            "news": "unavailable: no historical archive on this host (see module docstring)",
            "book_depth": "unavailable: bookDepth not downloaded; metrics/ only",
        }


def _candidates(rel: str) -> list[Path]:
    """Where a staged copy would be, then where the running bot keeps it. Env var wins."""
    out = []
    env = os.environ.get("EARN_ML_EXOG")
    if env:
        out.append(Path(env) / rel)
    out.append(data_root() / rel)
    out.append(Path.home() / "earn-run" / rel)
    return out


def load_dvol(asset: str = "BTC", *, path: Path | None = None) -> pd.DataFrame | None:
    """Deribit DVOL daily close as a fraction (35.0 -> 0.35). ``None`` when absent.

    **BTC and ETH only, from 2021-03-24.** Market-scope: there is no per-coin implied vol for
    the other 745 symbols, so this enters a panel study as one column broadcast to every row and
    can only be judged on a time-series target.
    """
    srcs = [path] if path else _candidates(f"knowledge/market/dvol/{asset}-1d.csv") + [
        data_root() / "dvol" / f"{asset}-1d.csv"]
    for src in srcs:
        if src is None or not Path(src).exists():
            continue
        df = pd.read_csv(src)
        if "date" not in df.columns or "close" not in df.columns:
            continue
        out = pd.DataFrame({
            "ts": pd.to_datetime(df["date"], utc=True),
            "dvol": pd.to_numeric(df["close"], errors="coerce") / 100.0,
        }).dropna().sort_values("ts", kind="stable").reset_index(drop=True)
        return out
    return None


def load_oi_daily(symbol: str = "BTCUSDT", *, path: Path | None = None) -> pd.DataFrame | None:
    """Daily last-observation open interest and taker/positioning ratios. ``None`` when absent.

    Reads the ``binance_archive`` metrics rollup (5-minute grid, 636,710 rows for BTCUSDT from
    2020-09-01) and takes the **last** print of each UTC day, which is the value known at the
    daily close. A daily *mean* would mix the afternoon into the morning and is not what a
    close-stamped feature may use.

    ``sum_open_interest`` near zero is a known archive defect and is mapped to NaN before
    anything divides by it, matching :func:`runs.features.binance_archive.clean_metrics`.
    """
    srcs = [path] if path else _candidates(
        f"cache/binance_archive/metrics/{symbol}/{symbol}-metrics-rollup.feather") + [
        data_root() / "metrics" / f"{symbol}-metrics-rollup.feather"]
    for src in srcs:
        if src is None or not Path(src).exists():
            continue
        m = pd.read_feather(src)
        if "ts" not in m.columns:
            continue
        m = m.copy()
        m["ts"] = pd.to_datetime(m["ts"], utc=True)
        m = m.sort_values("ts", kind="stable")
        m["day"] = m["ts"].dt.floor("D")
        agg = m.groupby("day", sort=True).last(numeric_only=True)
        out = pd.DataFrame({"ts": pd.DatetimeIndex(agg.index)})
        wanted = {"oi": "sum_open_interest", "oi_value": "sum_open_interest_value",
                  "taker_ls": "sum_taker_long_short_vol_ratio",
                  "toptrader_ls": "sum_toptrader_long_short_ratio",
                  "count_ls": "count_long_short_ratio"}
        for dest, src_col in wanted.items():
            if src_col in agg.columns:
                v = pd.to_numeric(agg[src_col], errors="coerce").to_numpy(dtype=float)
            else:
                v = np.full(len(agg), np.nan)
            out[dest] = v
        # Archive defect: open-interest values at or near zero become inf under pct_change.
        out.loc[~(out["oi"] > 0), "oi"] = np.nan
        out.loc[~(out["oi_value"] > 0), "oi_value"] = np.nan
        return out.reset_index(drop=True)
    return None


#: Statements the Fed issued at short notice. A "days to next FOMC" feature that includes these
#: is telling 2020-03-14 about a meeting announced on 2020-03-15, which is a lookahead of exactly
#: the kind this package exists to refuse. They are excluded by default and the exclusion is
#: recorded on :class:`ExogBundle`.
#:
#: Derived by counting the scraped dates per year: 8 a year is the published cadence, and the
#: years that exceed it are 2019 (9), 2020 (12) and 2025 (9). The extras are the October 2019
#: repo/balance-sheet statement, the March 2020 emergency sequence, the two Jackson Hole
#: framework statements, and nothing else. 2020-04-29 is **not** here: that was the scheduled
#: April meeting, and dropping it would have been the mirror-image error.
FOMC_UNSCHEDULED = ("2019-10-11", "2020-03-03", "2020-03-15", "2020-03-23", "2020-03-31",
                    "2020-08-27", "2025-08-22")


def load_fomc_dates(*, path: Path | None = None, include_unscheduled: bool = False
                    ) -> tuple[tuple[pd.Timestamp, ...], bool]:
    """FOMC statement dates from a **cached** JSON list. Never fetches.

    Returns ``(dates, contains_unscheduled)``. The cache is written by
    ``python -m ml.select --stage-exog`` (which calls
    :func:`runs.features.macro_calendar.fetch_fomc_dates` once), so the feature is reproducible
    offline afterwards and a study run on a host with no network still gets the same numbers.

    A scheduled FOMC date is published roughly two years ahead, so knowing on 2023-01-01 that a
    meeting lands on 2023-03-22 is not lookahead. The unscheduled ones are, and are filtered.
    """
    srcs = [path] if path else [data_root() / "fomc_dates.json"] + _candidates(
        "knowledge/cache/macro/fomc_dates.json")
    raw: list[str] = []
    for src in srcs:
        if src is None or not Path(src).exists():
            continue
        try:
            blob = json.loads(Path(src).read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(blob, dict):
            blob = blob.get("dates") or blob.get("value") or []
        if isinstance(blob, list) and blob:
            raw = [str(x) for x in blob]
            break
    if not raw:
        return (), False
    ts = pd.to_datetime(pd.Series(raw), format="mixed", utc=True, errors="coerce").dropna()
    unsched = pd.to_datetime(pd.Series(FOMC_UNSCHEDULED), utc=True)
    has_unsched = bool(ts.dt.floor("D").isin(unsched).any())
    if not include_unscheduled:
        ts = ts.loc[~ts.dt.floor("D").isin(unsched)]
    return tuple(sorted(pd.DatetimeIndex(ts.dt.floor("D")).unique())), has_unsched


def load_exog(*, dvol_asset: str = "BTC", oi_symbol: str = "BTCUSDT") -> ExogBundle:
    """Probe for every optional source. Never raises, never fetches, reports what is missing."""
    absent: list[tuple[str, str]] = []
    dvol = load_dvol(dvol_asset)
    if dvol is None:
        absent.append(("dvol", f"no {dvol_asset}-1d.csv under $EARN_ML_EXOG / $EARN_ML_DATA / "
                               "~/earn-run/knowledge/market/dvol"))
    oi = load_oi_daily(oi_symbol)
    if oi is None:
        absent.append(("oi", f"no {oi_symbol}-metrics-rollup.feather under the probed roots; "
                             "run runs.features.binance_archive.backfill to create it"))
    fomc, has_unsched = load_fomc_dates()
    if not fomc:
        absent.append(("fomc", "no cached fomc_dates.json; run `python -m ml.select "
                               "--stage-exog` once with network to create it"))
    return ExogBundle(dvol=dvol, oi=oi, fomc=fomc, fomc_has_unscheduled=has_unsched,
                      absent=tuple(absent))


# --------------------------------------------------------------------------- context / work


@dataclass(frozen=True)
class ExtContext:
    """Everything a builder is allowed to know besides the panel itself.

    ``benchmark`` is the segment id of the beta/relative-strength reference. It defaults to
    ``BTCUSDT`` and a panel without it simply produces NaN in the ``btc_*`` family rather than
    substituting a different coin, because "beta against whatever was liquid" is not beta.
    """

    bars_per_year: float = 365.0
    bars_per_day: float = 1.0
    benchmark: str = "BTCUSDT"
    exog: ExogBundle = field(default_factory=ExogBundle)
    #: Restrict cross-sectional ranks/aggregates to the point-in-time eligible universe.
    eligible_only_xs: bool = True


class _Work:
    """Memoised intermediates over one sorted panel. Cuts ~150 rolling passes to ~40.

    Not part of the public API. It exists because every builder in this module wants the same
    one-bar log return, the same rolling means and the same benchmark alignment, and computing
    them ninety times is the difference between a 20-second feature build and a five-minute one.
    """

    def __init__(self, d: pd.DataFrame, ctx: ExtContext):
        self.d = d
        self.ctx = ctx
        self.sym = d["symbol"]
        self._memo: dict = {}
        self._g = d.groupby("symbol", sort=False)

    # -- primitives
    def _m(self, key, fn):
        if key not in self._memo:
            self._memo[key] = fn()
        return self._memo[key]

    def series(self, name: str) -> pd.Series:
        """A column of the panel, or one of the named derived series.

        The duplicate-column check is not paranoia: a panel that carries two columns called
        ``funding_ann`` (one joined, one rebuilt) makes this return a DataFrame, and the failure
        then surfaces twenty frames away inside pandas with a message about dimensions that says
        nothing about which feature is wrong.
        """
        if name in self.d.columns:
            got = self.d[name]
            if isinstance(got, pd.DataFrame):
                raise ValueError(
                    f"column {name!r} appears {got.shape[1]} times in the panel; a feature "
                    "builder cannot tell which one was meant. De-duplicate before building.")
            return got
        return self._m(("series", name), lambda: _DERIVED[name](self))

    def shift(self, name: str, k: int) -> pd.Series:
        s = self.series(name)
        return self._m(("shift", name, k),
                       lambda: s.groupby(self.sym, sort=False).shift(k))

    def roll(self, name: str, win: int, fn: str, *, min_frac: float = 1.0) -> pd.Series:
        """Grouped rolling aggregate, memoised on (name, win, fn, min_frac)."""
        s = self.series(name)
        mp = max(2, int(round(win * min_frac)))

        def build():
            return s.groupby(self.sym, sort=False).transform(
                lambda x: getattr(x.rolling(win, min_periods=mp), fn)())
        return self._m(("roll", name, win, fn, mp), build)

    def cummax(self, name: str) -> pd.Series:
        s = self.series(name)
        return self._m(("cummax", name), lambda: s.groupby(self.sym, sort=False).cummax())

    def bar_idx(self) -> pd.Series:
        return self._m("bar_idx", lambda: self._g.cumcount().astype(float))

    # -- market-wide helpers
    def xs_mask(self) -> pd.Series:
        """Rows that belong to the point-in-time cross-section at their timestamp."""
        def build():
            if self.ctx.eligible_only_xs and "eligible" in self.d.columns:
                return self.d["eligible"].fillna(False).astype(bool)
            return pd.Series(True, index=self.d.index)
        return self._m("xs_mask", build)

    def xs_agg(self, values: pd.Series, how: str, *, min_names: int = 5) -> pd.Series:
        """Aggregate ``values`` within each timestamp over the eligible cross-section only,
        then broadcast back to every row of that timestamp.

        Broadcasting to **every** row (not only the eligible ones) is deliberate: breadth is a
        property of the market at that instant and an ineligible coin still lives in that market.
        """
        v = values.where(self.xs_mask())
        g = v.groupby(self.d["ts"], sort=False)
        stat = getattr(g, how)()
        n = g.count()
        stat = stat.where(n >= min_names)
        return self.d["ts"].map(stat).astype(float)

    def bench(self, name: str) -> pd.Series:
        """A derived series of the benchmark symbol, aligned onto every row by timestamp."""
        def build():
            s = self.series(name)
            m = self.sym == self.ctx.benchmark
            if not m.any():
                return pd.Series(np.nan, index=self.d.index)
            ref = pd.Series(s[m].to_numpy(), index=self.d.loc[m, "ts"].to_numpy())
            ref = ref[~ref.index.duplicated(keep="last")]
            return self.d["ts"].map(ref).astype(float)
        return self._m(("bench", name), build)

    def exog_col(self, frame_key: str, col: str) -> pd.Series:
        """A column of an exogenous daily frame, aligned by the row's UTC day."""
        def build():
            src = getattr(self.ctx.exog, frame_key, None)
            if src is None or src.empty or col not in src.columns:
                return pd.Series(np.nan, index=self.d.index)
            ref = pd.Series(pd.to_numeric(src[col], errors="coerce").to_numpy(),
                            index=pd.to_datetime(src["ts"], utc=True).dt.floor("D"))
            ref = ref[~ref.index.duplicated(keep="last")]
            return self.d["ts"].dt.floor("D").map(ref).astype(float)
        return self._m(("exog", frame_key, col), build)


def _rolling_cov(w: _Work, a: pd.Series, b: pd.Series, win: int, *,
                 min_frac: float = 0.8) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Grouped rolling ``(cov(a,b), var(a), var(b))`` from rolling means of products.

    Three rolling means rather than ``rolling().cov()`` per group, because the latter is a
    Python-level loop over 747 groups per feature and this is used by five of them.
    """
    sym = w.sym
    prod = (a * b)
    aa = (a * a)
    bb = (b * b)
    mp = max(3, int(round(win * min_frac)))

    def rm(s):
        return s.groupby(sym, sort=False).transform(
            lambda x: x.rolling(win, min_periods=mp).mean())
    ma, mb = rm(a), rm(b)
    cov = rm(prod) - ma * mb
    va = rm(aa) - ma * ma
    vb = rm(bb) - mb * mb
    return cov, va.clip(lower=0.0), vb.clip(lower=0.0)


def _run_length(w: _Work, flag: pd.Series) -> pd.Series:
    """Number of consecutive trailing bars for which ``flag`` has been true, per symbol.

    ``cumsum`` minus its last value at a false bar. The ``cumcount``-within-``cumsum(~flag)``
    form is the more obvious one and is **off by one**: the false bar that resets the run is
    itself a member of the group, so the first true bar after it counts as two.
    """
    f = flag.fillna(False).astype(bool)
    cs = f.astype(float).groupby(w.sym, sort=False).cumsum()
    at_false = cs.where(~f)
    base = at_false.groupby(w.sym, sort=False).ffill().fillna(0.0)
    return (cs - base).where(f, 0.0)


def _bars_since_true(w: _Work, flag: pd.Series) -> pd.Series:
    """Bars since ``flag`` was last true, per symbol. NaN before the first true."""
    f = flag.fillna(False).astype(bool)
    idx = w.bar_idx()
    last = idx.where(f).groupby(w.sym, sort=False).ffill()
    return idx - last


# --------------------------------------------------------------------------- derived series

def _d_logret(w: _Work) -> pd.Series:
    return np.log(w.d["close"] / w.shift("close", 1))


def _d_abs_logret(w: _Work) -> pd.Series:
    return w.series("_lr").abs()


def _d_neg_logret(w: _Work) -> pd.Series:
    return w.series("_lr").clip(upper=0.0)


def _d_pos_logret(w: _Work) -> pd.Series:
    return w.series("_lr").clip(lower=0.0)


def _d_neg_sq(w: _Work) -> pd.Series:
    return w.series("_neglr") ** 2


def _d_pos_sq(w: _Work) -> pd.Series:
    return w.series("_poslr") ** 2


def _d_sq_logret(w: _Work) -> pd.Series:
    return w.series("_lr") ** 2


def _d_hl(w: _Work) -> pd.Series:
    """Parkinson's per-bar term: ``log(high/low)^2 / (4 ln 2)``."""
    return np.log(w.d["high"] / w.d["low"]) ** 2 / (4.0 * np.log(2.0))


def _d_gk(w: _Work) -> pd.Series:
    """Garman-Klass per-bar variance term. Uses only this bar's own OHLC."""
    hl = np.log(w.d["high"] / w.d["low"]) ** 2
    co = np.log(w.d["close"] / w.d["open"]) ** 2
    return 0.5 * hl - (2.0 * np.log(2.0) - 1.0) * co


def _d_rs(w: _Work) -> pd.Series:
    """Rogers-Satchell per-bar variance term — drift-independent, unlike Garman-Klass."""
    h, lo, c, o = w.d["high"], w.d["low"], w.d["close"], w.d["open"]
    return (np.log(h / c) * np.log(h / o) + np.log(lo / c) * np.log(lo / o))


def _d_tr(w: _Work) -> pd.Series:
    """True range as a fraction of the previous close — the ATR atom."""
    pc = w.shift("close", 1)
    a = w.d["high"] - w.d["low"]
    b = (w.d["high"] - pc).abs()
    c = (w.d["low"] - pc).abs()
    return pd.concat([a, b, c], axis=1).max(axis=1) / pc


def _d_clv(w: _Work) -> pd.Series:
    """Close location value inside the bar's range: +1 at the high, -1 at the low."""
    rng = (w.d["high"] - w.d["low"]).replace(0.0, np.nan)
    return (2.0 * w.d["close"] - w.d["high"] - w.d["low"]) / rng


def _d_log_qv(w: _Work) -> pd.Series:
    return np.log1p(w.d["quote_volume"].clip(lower=0.0))


def _d_avg_trade(w: _Work) -> pd.Series:
    """Mean quote value per trade — a size proxy that does not need tick data."""
    return w.d["quote_volume"] / w.d["trades"].replace(0.0, np.nan)


def _d_lr_lag1(w: _Work) -> pd.Series:
    return w.shift("_lr", 1)


def _d_lr5(w: _Work) -> pd.Series:
    """Five-bar log return, for the variance-ratio family."""
    return np.log(w.d["close"] / w.shift("close", 5))


_DERIVED: dict[str, Callable[[_Work], pd.Series]] = {
    "_lr": _d_logret, "_abslr": _d_abs_logret, "_neglr": _d_neg_logret,
    "_poslr": _d_pos_logret, "_negsq": _d_neg_sq, "_possq": _d_pos_sq,
    "_sqlr": _d_sq_logret, "_hl": _d_hl, "_gk": _d_gk,
    "_rs": _d_rs, "_tr": _d_tr, "_clv": _d_clv, "_logqv": _d_log_qv,
    "_avgtrade": _d_avg_trade, "_lrlag1": _d_lr_lag1, "_lr5": _d_lr5,
}


# --------------------------------------------------------------------------- price / momentum

_MOM_WINS = (1, 3, 5, 14, 60, 180)     # 7/30/90/365 already live in ml.features

for _wn in _MOM_WINS:
    def _mk_mom(win=_wn):
        def b(w: _Work) -> pd.Series:
            return w.d["close"] / w.shift("close", win) - 1.0
        return b
    _reg_fn(f"mom_{_wn}", "price", _mk_mom(),
            note="trailing return; the 30-90d members of this family measured a NEGATIVE "
                 "forward IC on three panels, so a positive coefficient here is a warning")

for _wn in (7, 30, 90):
    def _mk_mom_skip(win=_wn):
        def b(w: _Work) -> pd.Series:
            """Return over ``win`` bars ending **one bar ago** — the short-term-reversal form."""
            return w.shift("close", 1) / w.shift("close", win + 1) - 1.0
        return b
    _reg_fn(f"mom_{_wn}_skip1", "price", _mk_mom_skip(),
            note="momentum with the most recent bar skipped; separates reversal from trend")


@_reg("mom_accel_30_90", "price",
      note="30d return minus a third of the 90d return: is the trend speeding up")
def _f_mom_accel(w: _Work) -> pd.Series:
    m30 = w.d["close"] / w.shift("close", 30) - 1.0
    m90 = w.d["close"] / w.shift("close", 90) - 1.0
    return m30 - m90 / 3.0


# --------------------------------------------------------------------------- volatility

_VOL_WINS = (5, 10, 30, 250)           # 20/60/120 already live in ml.features

for _wn in _VOL_WINS:
    def _mk_vol(win=_wn):
        def b(w: _Work) -> pd.Series:
            return w.roll("_lr", win, "std") * np.sqrt(w.ctx.bars_per_year)
        return b
    _reg_fn(f"vol_{_wn}", "vol", _mk_vol(),
            note="annualised close-to-close vol; low is good, and ONLY ranked cross-sectionally")

for _a, _b in ((5, 60), (20, 250), (60, 250)):
    def _mk_volratio(a=_a, b=_b):
        def f(w: _Work) -> pd.Series:
            lo = w.roll("_lr", b, "std")
            return w.roll("_lr", a, "std") / lo.replace(0.0, np.nan)
        return f
    _reg_fn(f"vol_ratio_{_a}_{_b}", "vol", _mk_volratio(),
            note="vol regime: short vol against long vol. >1 means vol is expanding")


@_reg("vol_of_vol_60", "vol",
      note="std of the trailing 20d vol series over 60 bars — instability of the vol level")
def _f_vov(w: _Work) -> pd.Series:
    v20 = w.roll("_lr", 20, "std")
    tmp = _Work(w.d.assign(_v20=v20.to_numpy()), w.ctx)
    return tmp.roll("_v20", 60, "std", min_frac=0.8)


@_reg("semivol_dn_60", "vol",
      note="downside semideviation, annualised: RMS of the negative returns only. The leg that "
           "hurts, and not the same ordering as total vol whenever skew differs across coins")
def _f_semivol_dn(w: _Work) -> pd.Series:
    return np.sqrt(w.roll("_negsq", 60, "mean", min_frac=0.8).clip(lower=0.0)) * np.sqrt(
        w.ctx.bars_per_year)


@_reg("semivol_up_60", "vol", note="upside semideviation, annualised: RMS of positive returns")
def _f_semivol_up(w: _Work) -> pd.Series:
    return np.sqrt(w.roll("_possq", 60, "mean", min_frac=0.8).clip(lower=0.0)) * np.sqrt(
        w.ctx.bars_per_year)


@_reg("semivol_ratio_60", "vol",
      note="downside/upside dispersion. >1 means the distribution's weight is on the loss side")
def _f_semivol_ratio(w: _Work) -> pd.Series:
    up = _f_semivol_up(w)
    return _f_semivol_dn(w) / up.replace(0.0, np.nan)


for _wn in (60, 180):
    def _mk_skew(win=_wn):
        def b(w: _Work) -> pd.Series:
            return w.roll("_lr", win, "skew", min_frac=0.8)
        return b
    _reg_fn(f"skew_{_wn}", "shape", _mk_skew(),
            note="rolling skew of daily log returns; lottery-like names skew positive")

    def _mk_kurt(win=_wn):
        def b(w: _Work) -> pd.Series:
            return w.roll("_lr", win, "kurt", min_frac=0.8)
        return b
    _reg_fn(f"kurt_{_wn}", "shape", _mk_kurt(),
            note="rolling excess kurtosis; a jump-prone name is not the same as a volatile one")


@_reg("tail_ratio_180", "shape",
      note="|5th percentile| / 95th percentile of daily returns — asymmetry of the tails")
def _f_tail_ratio(w: _Work) -> pd.Series:
    lo = w.series("_lr").groupby(w.sym, sort=False).transform(
        lambda x: x.rolling(180, min_periods=90).quantile(0.05))
    hi = w.series("_lr").groupby(w.sym, sort=False).transform(
        lambda x: x.rolling(180, min_periods=90).quantile(0.95))
    return lo.abs() / hi.replace(0.0, np.nan)


# --------------------------------------------------------------------------- range measures

for _name, _key in (("park_vol_20", "_hl"), ("gk_vol_20", "_gk"), ("rs_vol_20", "_rs")):
    def _mk_rangevol(key=_key):
        def b(w: _Work) -> pd.Series:
            return np.sqrt(w.roll(key, 20, "mean", min_frac=0.8).clip(lower=0.0)) * np.sqrt(
                w.ctx.bars_per_year)
        return b
    _reg_fn(_name, "range", _mk_rangevol(), needs=("high", "low", "open"),
            note="range-based vol estimator: ~5x the efficiency of close-to-close at the same "
                 "window, which matters most on the short windows where close-to-close is noise")


@_reg("range_eff_20", "range", needs=("high", "low"),
      note="Parkinson vol / close-to-close vol. <1 means the bar's path is quieter than its "
           "endpoints, i.e. trending; >1 means intrabar chop")
def _f_range_eff(w: _Work) -> pd.Series:
    cc = w.roll("_lr", 20, "std")
    pk = np.sqrt(w.roll("_hl", 20, "mean", min_frac=0.8).clip(lower=0.0))
    return pk / cc.replace(0.0, np.nan)


@_reg("atr_14", "range", needs=("high", "low"),
      note="average true range as a fraction of price — the risk unit a stop is sized in")
def _f_atr(w: _Work) -> pd.Series:
    return w.roll("_tr", 14, "mean", min_frac=0.8)


@_reg("clv_5", "range", needs=("high", "low"),
      note="5-bar mean close location inside the bar range; a weak intraday pressure proxy")
def _f_clv5(w: _Work) -> pd.Series:
    return w.roll("_clv", 5, "mean", min_frac=0.6)


# --------------------------------------------------------------------------- trend / distance

for _wn in (20, 50, 100, 200):
    def _mk_dist_ma(win=_wn):
        def b(w: _Work) -> pd.Series:
            ma = w.roll("close", win, "mean", min_frac=0.8)
            return w.d["close"] / ma - 1.0
        return b
    _reg_fn(f"dist_ma_{_wn}", "trend", _mk_dist_ma(),
            note="continuous form of above_ma_<win>, which measured IC -0.003 at 30d. Included "
                 "because a binary version of a worthless feature can still hide a live one")


@_reg("ma_slope_50", "trend", note="20-bar change in the 50-bar moving average")
def _f_ma_slope(w: _Work) -> pd.Series:
    ma = w.roll("close", 50, "mean", min_frac=0.8)
    tmp = _Work(w.d.assign(_ma50=ma.to_numpy()), w.ctx)
    return ma / tmp.shift("_ma50", 20) - 1.0


@_reg("ma_cross_50_200", "trend", note="ma50/ma200 - 1: the golden-cross family, continuous")
def _f_ma_cross(w: _Work) -> pd.Series:
    ma200 = w.roll("close", 200, "mean", min_frac=0.8)
    return w.roll("close", 50, "mean", min_frac=0.8) / ma200 - 1.0


for _wn in (30, 180):
    def _mk_dfh(win=_wn):
        def b(w: _Work) -> pd.Series:
            col = "high" if "high" in w.d.columns and w.d["high"].notna().any() else "close"
            mx = w.roll(col, win, "max", min_frac=0.5)
            return w.d["close"] / mx - 1.0
        return b
    _reg_fn(f"dist_from_high_{_wn}", "drawdown", _mk_dfh(), needs=("high",),
            note="the growth audit's finding: distance from the high carries information that "
                 "trailing return does not (IC +0.054 at 30d for the 90d member)")


@_reg("dist_from_low_90", "drawdown", needs=("low",),
      note="close over the trailing 90-bar low. High values mean the name has already bounced")
def _f_dist_low(w: _Work) -> pd.Series:
    col = "low" if "low" in w.d.columns and w.d["low"].notna().any() else "close"
    mn = w.roll(col, 90, "min", min_frac=0.5)
    return w.d["close"] / mn.replace(0.0, np.nan) - 1.0


@_reg("days_since_ath", "drawdown",
      note="bars since the running-max close was last set. The expanding max includes the "
           "current bar, which is correct and is where a one-bar lookahead would hide")
def _f_days_since_ath(w: _Work) -> pd.Series:
    at_high = w.d["close"] >= w.cummax("close") - 1e-12
    return _bars_since_true(w, at_high)


@_reg("log_days_since_ath", "drawdown", note="log1p of the above; the raw form is heavy-tailed")
def _f_log_days_since_ath(w: _Work) -> pd.Series:
    return np.log1p(_f_days_since_ath(w).clip(lower=0))


for _wn in (90, 365):
    def _mk_maxdd(win=_wn):
        def b(w: _Work) -> pd.Series:
            mx = w.roll("close", win, "max", min_frac=0.5)
            dd = w.d["close"] / mx - 1.0
            tmp = _Work(w.d.assign(_dd=dd.to_numpy()), w.ctx)
            return tmp.roll("_dd", win, "min", min_frac=0.5)
        return b
    _reg_fn(f"maxdd_{_wn}", "drawdown", _mk_maxdd(),
            note="worst trailing drawdown inside the window — drawdown STATE, not forward risk")

    def _mk_ulcer(win=_wn):
        def b(w: _Work) -> pd.Series:
            mx = w.roll("close", win, "max", min_frac=0.5)
            dd2 = (w.d["close"] / mx - 1.0) ** 2
            tmp = _Work(w.d.assign(_dd2=dd2.to_numpy()), w.ctx)
            return np.sqrt(tmp.roll("_dd2", win, "mean", min_frac=0.5).clip(lower=0.0))
        return b
    _reg_fn(f"ulcer_{_wn}", "drawdown", _mk_ulcer(),
            note="RMS drawdown over the window: depth and duration in one number")


@_reg("dd_runlen_10", "drawdown",
      note="consecutive bars spent more than 10% below the trailing 365-bar high")
def _f_dd_runlen(w: _Work) -> pd.Series:
    mx = w.roll("close", 365, "max", min_frac=0.25)
    return _run_length(w, (w.d["close"] / mx - 1.0) <= -0.10)


# --------------------------------------------------------------------------- persistence

@_reg("acf1_60", "shape",
      note="lag-1 autocorrelation of daily log returns over 60 bars. Negative = mean-reverting")
def _f_acf1(w: _Work) -> pd.Series:
    a, b = w.series("_lr"), w.series("_lrlag1")
    cov, va, vb = _rolling_cov(w, a, b, 60)
    den = np.sqrt(va * vb)
    return cov / den.replace(0.0, np.nan)


@_reg("var_ratio_5_120", "shape",
      note="Var(5-bar return) / (5 Var(1-bar return)) over 120 bars. >1 trending, <1 reverting; "
           "the model-free version of a Hurst exponent and far more stable at this sample size")
def _f_var_ratio(w: _Work) -> pd.Series:
    s5 = w.roll("_lr5", 120, "std", min_frac=0.8)
    s1 = w.roll("_lr", 120, "std", min_frac=0.8)
    return (s5 ** 2) / (5.0 * (s1 ** 2)).replace(0.0, np.nan)


@_reg("up_frac_60", "shape", note="fraction of the last 60 bars that closed up")
def _f_up_frac(w: _Work) -> pd.Series:
    up = (w.series("_lr") > 0).astype(float).where(w.series("_lr").notna())
    tmp = _Work(w.d.assign(_up=up.to_numpy()), w.ctx)
    return tmp.roll("_up", 60, "mean", min_frac=0.8)


@_reg("streak", "shape",
      note="signed consecutive same-direction bars. The short-horizon reversal candidate")
def _f_streak(w: _Work) -> pd.Series:
    lr = w.series("_lr")
    up = _run_length(w, lr > 0)
    dn = _run_length(w, lr < 0)
    return up - dn


@_reg("rsi_14", "shape",
      note="Wilder RSI via an EWM with alpha=1/14. Registered because it is the single most "
           "commonly proposed crypto feature and the study should have to reject it explicitly")
def _f_rsi(w: _Work) -> pd.Series:
    lr = w.series("_lr")
    gain = lr.clip(lower=0.0)
    loss = (-lr).clip(lower=0.0)
    ag = gain.groupby(w.sym, sort=False).transform(
        lambda s: s.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean())
    al = loss.groupby(w.sym, sort=False).transform(
        lambda s: s.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean())
    rs = ag / al.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


@_reg("z_close_60", "trend", note="(close - ma60) / rolling std of close. A stationary level")
def _f_z_close(w: _Work) -> pd.Series:
    ma = w.roll("close", 60, "mean", min_frac=0.8)
    sd = w.roll("close", 60, "std", min_frac=0.8)
    return (w.d["close"] - ma) / sd.replace(0.0, np.nan)


# --------------------------------------------------------------------------- microstructure

@_reg("log_qv", "micro", note="log1p of this bar's quote volume — the raw size level")
def _f_log_qv(w: _Work) -> pd.Series:
    return w.series("_logqv")


for _wn in (20, 60):
    def _mk_qvz(win=_wn):
        def b(w: _Work) -> pd.Series:
            m = w.roll("_logqv", win, "mean", min_frac=0.8)
            s = w.roll("_logqv", win, "std", min_frac=0.8)
            return (w.series("_logqv") - m) / s.replace(0.0, np.nan)
        return b
    _reg_fn(f"qv_z_{_wn}", "micro", _mk_qvz(),
            note="volume surprise in log space. Rising volume forecast DECLINE (IC -0.069 for "
                 "the 30/180 median-ratio form), so a positive loading is the warning sign")


@_reg("qv_trend_5_60", "micro", note="5/60 median quote-volume ratio — the short-window sibling "
                                     "of the measured vol_trend_30_180")
def _f_qv_trend(w: _Work) -> pd.Series:
    lo = w.roll("quote_volume", 60, "median", min_frac=0.8)
    return w.roll("quote_volume", 5, "median") / lo.replace(0.0, np.nan)


@_reg("trades_z_60", "micro", needs=("trades",),
      note="trade-count surprise. Separates 'more money' from 'more participants'")
def _f_trades_z(w: _Work) -> pd.Series:
    lt = np.log1p(w.d["trades"].clip(lower=0.0))
    tmp = _Work(w.d.assign(_lt=lt.to_numpy()), w.ctx)
    m = tmp.roll("_lt", 60, "mean", min_frac=0.8)
    s = tmp.roll("_lt", 60, "std", min_frac=0.8)
    return (lt - m) / s.replace(0.0, np.nan)


@_reg("avg_trade_size", "micro", needs=("trades",),
      note="quote volume per trade, logged. A crude retail/institution mix proxy")
def _f_avg_trade(w: _Work) -> pd.Series:
    return np.log1p(w.series("_avgtrade"))


@_reg("avg_trade_size_z_60", "micro", needs=("trades",),
      note="change in the participant mix rather than its level")
def _f_avg_trade_z(w: _Work) -> pd.Series:
    la = np.log1p(w.series("_avgtrade"))
    tmp = _Work(w.d.assign(_la=la.to_numpy()), w.ctx)
    m = tmp.roll("_la", 60, "mean", min_frac=0.8)
    s = tmp.roll("_la", 60, "std", min_frac=0.8)
    return (la - m) / s.replace(0.0, np.nan)


@_reg("amihud_90", "micro",
      note="illiquidity: mean |return|/quote volume. A forecast that only works on names too "
           "illiquid to trade is not a forecast, and this is what makes that visible")
def _f_amihud90(w: _Work) -> pd.Series:
    il = w.series("_abslr") / w.d["quote_volume"].replace(0.0, np.nan)
    tmp = _Work(w.d.assign(_il=il.to_numpy()), w.ctx)
    return tmp.roll("_il", 90, "mean", min_frac=0.5) * 1e9


@_reg("roll_spread_20", "micro",
      note="Roll's effective spread: 2*sqrt(-cov(r_t, r_t-1)) when the covariance is negative, "
           "NaN when it is not. A cost proxy with no tick data, and it is NaN about half the "
           "time by construction — which is honest, not broken")
def _f_roll_spread(w: _Work) -> pd.Series:
    cov, _, _ = _rolling_cov(w, w.series("_lr"), w.series("_lrlag1"), 20, min_frac=0.9)
    return 2.0 * np.sqrt((-cov).clip(lower=0.0)).where(cov < 0)


@_reg("kyle_lambda_60", "micro",
      note="mean |return| / sqrt(quote volume) — price impact per root-dollar, Kyle's lambda in "
           "its cheapest form")
def _f_kyle(w: _Work) -> pd.Series:
    k = w.series("_abslr") / np.sqrt(w.d["quote_volume"].clip(lower=1.0))
    tmp = _Work(w.d.assign(_k=k.to_numpy()), w.ctx)
    return tmp.roll("_k", 60, "mean", min_frac=0.8) * 1e3


@_reg("log_age_days", "micro", needs=("age_days",),
      note="log1p of listing age. Age is the project's second-strongest measured feature "
           "(IC +0.128 to +0.182) and the real break is at ~2 years, not the config's 180 days")
def _f_log_age(w: _Work) -> pd.Series:
    if "age_days" not in w.d.columns:
        return pd.Series(np.nan, index=w.d.index)
    return np.log1p(w.d["age_days"].clip(lower=0))


# --------------------------------------------------------------------------- vs benchmark

@_reg("beta_btc_90", "xsec",
      note="rolling 90-bar beta of daily log returns against the benchmark. One factor explains "
           "57-69% of daily cross-sectional variance, so this is the dominant exposure and any "
           "'alpha' feature correlated with it is mostly this")
def _f_beta(w: _Work) -> pd.Series:
    cov, _, vb = _rolling_cov(w, w.series("_lr"), w.bench("_lr"), 90)
    return cov / vb.replace(0.0, np.nan)


@_reg("corr_btc_90", "xsec", note="rolling correlation to the benchmark over 90 bars")
def _f_corr_btc(w: _Work) -> pd.Series:
    cov, va, vb = _rolling_cov(w, w.series("_lr"), w.bench("_lr"), 90)
    den = np.sqrt(va * vb)
    return cov / den.replace(0.0, np.nan)


@_reg("resid_vol_90", "xsec",
      note="idiosyncratic vol: the part of a coin's variance the benchmark does not explain, "
           "annualised. The 'which coin' form of the strongest measured feature")
def _f_resid_vol(w: _Work) -> pd.Series:
    cov, va, vb = _rolling_cov(w, w.series("_lr"), w.bench("_lr"), 90)
    beta = cov / vb.replace(0.0, np.nan)
    resid = (va - beta ** 2 * vb).clip(lower=0.0)
    return np.sqrt(resid) * np.sqrt(w.ctx.bars_per_year)


for _wn in (30, 90):
    def _mk_resid_mom(win=_wn):
        def b(w: _Work) -> pd.Series:
            cov, _, vb = _rolling_cov(w, w.series("_lr"), w.bench("_lr"), 90)
            beta = cov / vb.replace(0.0, np.nan)
            mi = w.d["close"] / w.shift("close", win) - 1.0
            bm = w.bench("close")
            # Lagged on the TIMESTAMP axis, not by symbol: the benchmark column is one value
            # per timestamp broadcast across the cross-section, so a groupby(symbol).shift
            # would lag it by `win` bars OF THAT COIN and give a coin that trades 5 days a
            # week a different BTC return from its neighbour on the same date.
            mb = bm / _lag_by_day(w, bm, win) - 1.0
            return mi - beta * mb
        return b
    _reg_fn(f"resid_mom_{_wn}", "xsec", _mk_resid_mom(),
            note="beta-adjusted trailing return. The honest version of 'this coin outperformed', "
                 "because the raw version is mostly the coin's beta times BTC")


@_reg("rel_strength_90", "xsec",
      note="coin/benchmark price ratio return over 90 bars. Only 3.4% of coins beat BTC in "
           "2023-24, so this is almost always negative and its cross-sectional rank is the "
           "question of interest, not its level")
def _f_rel_strength(w: _Work) -> pd.Series:
    ratio = w.d["close"] / w.bench("close").replace(0.0, np.nan)
    tmp = _Work(w.d.assign(_rat=ratio.to_numpy()), w.ctx)
    return ratio / tmp.shift("_rat", 90) - 1.0


# ------------------------------------------------------------------- interactions (round 8 add)
#
# Round 0-7 of ``ml.select`` measured that adding features in groups makes the out-of-sample IC
# WORSE than the 20 landed base features. That is a result about breadth, and it is the reason
# this group is four features rather than forty.
#
# Each one encodes a shape the project has already MEASURED and that a depth-4 tree on 76
# correlated columns was not finding on its own:
#
# * The volatility effect is the strongest result in ``growth-audit.md`` (rank IC -0.140 at 30d)
#   and it is a *denominator*: the same 40% trailing return means something different on a coin
#   at 60% annualised vol than on one at 200%. Dividing by vol is not a new signal, it is the
#   existing two signals combined in the way the measurement says they combine.
# * The audit's double sort is explicitly an INTERACTION, not two effects: oldest x lowest-vol
#   ran a median forward 30d of -1.5% against newest x highest-vol at -15.7%, a 14-point spread
#   that neither margin shows alone. ``age_inv_vol`` is that cell as one number — and its first
#   functional form was wrong, which is on the record in its own note because the four-cell test
#   that caught it is the reason the group is worth offering to the search at all.
#
# They are all trailing, all functions of quantities this module already computes, and all
# covered by :func:`assert_no_lookahead_ext`. Their vol denominator is rebuilt here from ``_lr``
# rather than read from ``ml.features``'s ``vol_60`` column, so that the group still builds on a
# raw panel slice — which is exactly what the prefix-rebuild proof hands it.


def _vol60(w: _Work) -> pd.Series:
    """60-bar annualised realised vol, memoised, floored so the ratios cannot explode.

    The floor matters: a stablecoin or a frozen listing can post a 60-bar return std of ~1e-6,
    and ``mom_90 / 1e-6`` is a 1e8 outlier that a tree will happily split on and that no amount
    of regularisation removes. 5% annualised is below every genuinely traded coin in this panel.
    """
    v = w.roll("_lr", 60, "std", min_frac=0.8) * np.sqrt(w.ctx.bars_per_year)
    return v.clip(lower=0.05)


for _wn in (90, 365):
    def _mk_mom_vadj(win=_wn):
        def b(w: _Work) -> pd.Series:
            return (w.d["close"] / w.shift("close", win) - 1.0) / _vol60(w)
        return b
    _reg_fn(f"mom_{_wn}_vol_adj", "interact", _mk_mom_vadj(),
            note="trailing return divided by 60-bar annualised vol — the trailing Sharpe of the "
                 "window. Trailing return alone measured a NEGATIVE forward IC at 30-90d while "
                 "low vol measured the strongest positive one; this is the ratio those two "
                 "measurements imply, and it is not what either column carries on its own")


@_reg("dist_from_high_90_vol_adj", "interact", needs=("high",),
      note="distance below the 90-bar high, in units of 60-bar vol. The growth audit found "
           "distance-from-high carries information trailing return does not (IC +0.054, t 4.33); "
           "in vol units it asks whether the coin is unusually far down for a coin this noisy, "
           "which is a different question from whether it is far down")
def _f_dist_high_90_vadj(w: _Work) -> pd.Series:
    hi = w.roll("high", 90, "max", min_frac=0.5)
    return (w.d["close"] / hi.replace(0.0, np.nan) - 1.0) / _vol60(w)


@_reg("age_inv_vol", "interact", needs=("age_days",),
      note="log1p(age) / vol_60: the growth audit's oldest x lowest-vol cell as one number. That "
           "double sort ran median fwd 30d -1.5% against -15.7% for newest x highest-vol, and "
           "the two margins are INDEPENDENT, so the cell is a product and not a sum. A QUOTIENT "
           "rather than log1p(age) x -log(vol): the product form inverts in age above 100% "
           "annualised vol, where -log(vol) turns negative and more age makes the score worse. "
           "tests/test_ml/test_features_ext.py caught that on a four-cell fixture; the quotient "
           "is monotone in age at every vol and monotone in vol at every age, which is the only "
           "shape the audit's two margins support")
def _f_age_inv_vol(w: _Work) -> pd.Series:
    if "age_days" not in w.d.columns:
        return pd.Series(np.nan, index=w.d.index)
    return np.log1p(w.d["age_days"].clip(lower=0)) / _vol60(w)


# --------------------------------------------------------------------------- market scope

@_reg("mkt_breadth_ma200", "market", scope="market",
      note="share of the ELIGIBLE universe trading above its own 200-bar mean")
def _f_breadth_ma200(w: _Work) -> pd.Series:
    ma = w.roll("close", 200, "mean", min_frac=0.8)
    above = (w.d["close"] > ma).astype(float).where(ma.notna())
    return w.xs_agg(above, "mean")


@_reg("mkt_breadth_pos_30", "market", scope="market",
      note="share of the eligible universe with a positive 30-bar return")
def _f_breadth_pos30(w: _Work) -> pd.Series:
    m30 = w.d["close"] / w.shift("close", 30) - 1.0
    return w.xs_agg((m30 > 0).astype(float).where(m30.notna()), "mean")


@_reg("mkt_dispersion_1", "market", scope="market",
      note="cross-sectional std of one-bar log returns across the eligible universe. High "
           "dispersion is the only state in which coin selection can pay for its costs")
def _f_dispersion(w: _Work) -> pd.Series:
    return w.xs_agg(w.series("_lr"), "std")


@_reg("mkt_ret_ew_1", "market", scope="market",
      note="equal-weighted mean one-bar log return of the eligible universe")
def _f_mkt_ret(w: _Work) -> pd.Series:
    return w.xs_agg(w.series("_lr"), "mean")


@_reg("mkt_ret_ew_30", "market", scope="market", note="30-bar cumulative equal-weighted market "
                                                      "return, summed from the daily means")
def _f_mkt_ret30(w: _Work) -> pd.Series:
    r = w.xs_agg(w.series("_lr"), "mean")
    ts = w.d["ts"]
    one = pd.Series(r.to_numpy(), index=ts.to_numpy())
    one = one[~one.index.duplicated(keep="last")].sort_index()
    cum = one.rolling(30, min_periods=24).sum()
    return ts.map(cum).astype(float)


@_reg("mkt_universe_n", "market", scope="market",
      note="how many names were eligible at this timestamp. 32 today against 747 ever, and a "
           "cross-sectional claim made on a 6-name universe is not a cross-sectional claim")
def _f_universe_n(w: _Work) -> pd.Series:
    ok = w.xs_mask().astype(float)
    cnt = ok.groupby(w.d["ts"], sort=False).sum()
    return w.d["ts"].map(cnt).astype(float)


@_reg("mkt_btc_dom_qv", "market", scope="market",
      note="benchmark quote volume as a share of the eligible universe's. A turnover-based "
           "dominance proxy; there is no market-cap history on this host")
def _f_btc_dom(w: _Work) -> pd.Series:
    tot = w.xs_agg(w.d["quote_volume"], "sum", min_names=3)
    return w.bench("quote_volume") / tot.replace(0.0, np.nan)


@_reg("mkt_btc_vol_60", "market", scope="market", note="the benchmark's own 60-bar annualised vol")
def _f_btc_vol60(w: _Work) -> pd.Series:
    v = w.roll("_lr", 60, "std") * np.sqrt(w.ctx.bars_per_year)
    tmp = _Work(w.d.assign(_bv=v.to_numpy()), w.ctx)
    return tmp.bench("_bv")


@_reg("mkt_btc_dd", "market", scope="market",
      note="the benchmark's drawdown from its running-max close — the regime variable the "
           "growth audit's three eras are really about")
def _f_btc_dd(w: _Work) -> pd.Series:
    dd = w.d["close"] / w.cummax("close") - 1.0
    tmp = _Work(w.d.assign(_bdd=dd.to_numpy()), w.ctx)
    return tmp.bench("_bdd")


@_reg("mkt_corr_med_90", "market", scope="market",
      note="median pairwise-to-benchmark correlation across the eligible universe. When this is "
           "high there is one asset with 500 tickers and nothing to select between")
def _f_corr_med(w: _Work) -> pd.Series:
    return w.xs_agg(_f_corr_btc(w), "median")


# --------------------------------------------------------------------------- derivatives

@_reg("funding_ann_7", "deriv", needs=("funding_ann",),
      note="7-bar mean annualised funding. Use as an ABSOLUTE level (>=40% ann is a 7d tail "
           "flag); the rolling self-percentile form is measured noise at every horizon")
def _f_funding7(w: _Work) -> pd.Series:
    if "funding_ann" not in w.d.columns:
        return pd.Series(np.nan, index=w.d.index)
    return w.roll("funding_ann", 7, "mean", min_frac=0.5)


@_reg("funding_extreme", "deriv", needs=("funding_ann",),
      note="1.0 when 7d mean funding >= 40% annualised. Ratio 1.41 on P(7d dd < -8%) and 1.67 "
           "inside the low-vol tercile — but it fired on SIX coin-weeks in 2025-26 against "
           "12.25% in 2019-22, so it is a rare-event flag with almost no live value today")
def _f_funding_extreme(w: _Work) -> pd.Series:
    f7 = _f_funding7(w)
    return (f7 >= 0.40).astype(float).where(f7.notna())


@_reg("funding_pos_runlen", "deriv", needs=("funding_ann",),
      note="consecutive bars of positive funding — how long the crowd has been paying to be long")
def _f_funding_run(w: _Work) -> pd.Series:
    if "funding_ann" not in w.d.columns:
        return pd.Series(np.nan, index=w.d.index)
    f = w.d["funding_ann"]
    return _run_length(w, f > 0).where(f.notna())


@_reg("funding_chg_7", "deriv", needs=("funding_ann",),
      note="change in the 7-bar funding mean over 7 bars: positioning building or unwinding")
def _f_funding_chg(w: _Work) -> pd.Series:
    f7 = _f_funding7(w)
    tmp = _Work(w.d.assign(_f7=f7.to_numpy()), w.ctx)
    return f7 - tmp.shift("_f7", 7)


@_reg("has_perp", "deriv", needs=("funding_ann",),
      note="1.0 when this coin has a perpetual at all. 125 of 498 ever-eligible symbols do not, "
           "and having one is itself a venue-attention signal that must not be confused with "
           "the funding level")
def _f_has_perp(w: _Work) -> pd.Series:
    if "funding_ann" not in w.d.columns:
        return pd.Series(0.0, index=w.d.index)
    ever = w.d["funding_ann"].notna().groupby(w.sym, sort=False).cummax()
    return ever.astype(float)


@_reg("oi_chg_1", "deriv", scope="market", needs=("oi",),
      note="one-day change in benchmark open interest. BTCUSDT ONLY from 2020-09 — there is no "
           "OI history for the other 745 symbols, so this can never be a cross-sectional factor")
def _f_oi_chg1(w: _Work) -> pd.Series:
    oi = w.exog_col("oi", "oi")
    return oi / _lag_by_day(w, oi, 1) - 1.0


@_reg("oi_chg_7", "deriv", scope="market", needs=("oi",),
      note="seven-day OI change. Leverage building into a move is the cascade precondition")
def _f_oi_chg7(w: _Work) -> pd.Series:
    oi = w.exog_col("oi", "oi")
    return oi / _lag_by_day(w, oi, 7) - 1.0


@_reg("oi_price_div_7", "deriv", scope="market", needs=("oi",),
      note="7d OI change minus the benchmark's 7d return: OI rising into a flat or falling tape "
           "is the crowded-short/crowded-long state, not a trend confirmation")
def _f_oi_div(w: _Work) -> pd.Series:
    bm = w.bench("close")
    m7 = bm / _lag_by_day(w, bm, 7) - 1.0
    return _f_oi_chg7(w) - m7


@_reg("taker_ls", "deriv", scope="market", needs=("oi",),
      note="taker buy/sell volume ratio, day's last 5-minute print. Benchmark only")
def _f_taker(w: _Work) -> pd.Series:
    return w.exog_col("oi", "taker_ls")


@_reg("taker_ls_z_60", "deriv", scope="market", needs=("oi",),
      note="taker imbalance as a 60-day z-score — the level is venue-specific, the surprise less so")
def _f_taker_z(w: _Work) -> pd.Series:
    return _ts_z(w, w.exog_col("oi", "taker_ls"), 60)


@_reg("toptrader_ls", "deriv", scope="market", needs=("oi",),
      note="top-trader long/short account ratio. Benchmark only, from 2020-09")
def _f_toptrader(w: _Work) -> pd.Series:
    return w.exog_col("oi", "toptrader_ls")


@_reg("dvol", "deriv", scope="market", needs=("dvol",),
      note="Deribit BTC DVOL as a fraction. BTC/ETH only, from 2021-03-24. Implied vol already "
           "gets R2 0.268 on forward realised vol in this project — the strongest honest target "
           "it has, and market-scope by necessity")
def _f_dvol(w: _Work) -> pd.Series:
    return w.exog_col("dvol", "dvol")


@_reg("dvol_chg_5", "deriv", scope="market", needs=("dvol",),
      note="5-day change in implied vol — the vol-of-vol leg the level cannot supply")
def _f_dvol_chg(w: _Work) -> pd.Series:
    dv = w.exog_col("dvol", "dvol")
    return dv - _lag_by_day(w, dv, 5)


@_reg("dvol_pct_365", "deriv", scope="market", needs=("dvol",),
      note="DVOL's own trailing 365-day percentile. The own-history percentile form is WRONG for "
           "a per-coin feature (it inverts the measured vol effect) and right here, because "
           "there is one series and no cross-section to rank against")
def _f_dvol_pct(w: _Work) -> pd.Series:
    return _ts_pct(w, w.exog_col("dvol", "dvol"), 365)


@_reg("vrp_20", "deriv", scope="market", needs=("dvol",),
      note="variance risk premium: implied vol minus the benchmark's trailing 20-day realised. "
           "Positive is the normal state; a collapse to zero has preceded vol expansions")
def _f_vrp(w: _Work) -> pd.Series:
    rv = w.roll("_lr", 20, "std") * np.sqrt(w.ctx.bars_per_year)
    tmp = _Work(w.d.assign(_rv20=rv.to_numpy()), w.ctx)
    return w.exog_col("dvol", "dvol") - tmp.bench("_rv20")


def _lag_by_day(w: _Work, series: pd.Series, k: int) -> pd.Series:
    """Lag a **market-scope** series by ``k`` calendar bars on the timestamp axis.

    A market series is one value per timestamp repeated across the cross-section, so a
    ``groupby(symbol).shift`` would lag it by k *of that symbol's* bars — which is the same
    thing only when every symbol trades every bar. It does not, so this goes through the
    timestamp axis instead. Getting this wrong makes a market feature quietly per-coin.
    """
    ts = w.d["ts"]
    one = pd.Series(series.to_numpy(), index=ts.to_numpy())
    one = one[~one.index.duplicated(keep="last")].sort_index()
    return ts.map(one.shift(k)).astype(float)


def _ts_z(w: _Work, series: pd.Series, win: int) -> pd.Series:
    """Trailing z-score of a market-scope series on the timestamp axis."""
    ts = w.d["ts"]
    one = pd.Series(series.to_numpy(), index=ts.to_numpy())
    one = one[~one.index.duplicated(keep="last")].sort_index()
    m = one.rolling(win, min_periods=max(10, win // 4)).mean()
    s = one.rolling(win, min_periods=max(10, win // 4)).std()
    z = (one - m) / s.replace(0.0, np.nan)
    return ts.map(z).astype(float)


def _ts_pct(w: _Work, series: pd.Series, win: int) -> pd.Series:
    """Trailing own-percentile of a market-scope series, current value included."""
    ts = w.d["ts"]
    one = pd.Series(series.to_numpy(), index=ts.to_numpy())
    one = one[~one.index.duplicated(keep="last")].sort_index()
    pct = one.rolling(win, min_periods=max(30, win // 4)).apply(
        lambda a: float((a[:-1] <= a[-1]).mean()) if len(a) > 1 else np.nan, raw=True)
    return ts.map(pct).astype(float)


# --------------------------------------------------------------------------- calendar

@_reg("cal_dow", "calendar", scope="market",
      note="day of week, 0=Monday. The project's own cross-sectional studies sample Mondays to "
           "cut 7-day label overlap from 12x to 1x, so this is also a sampling diagnostic")
def _f_dow(w: _Work) -> pd.Series:
    return w.d["ts"].dt.dayofweek.astype(float)


@_reg("cal_dom", "calendar", scope="market", note="day of month")
def _f_dom(w: _Work) -> pd.Series:
    return w.d["ts"].dt.day.astype(float)


@_reg("cal_month", "calendar", scope="market", note="calendar month, 1-12")
def _f_month(w: _Work) -> pd.Series:
    return w.d["ts"].dt.month.astype(float)


@_reg("cal_is_month_end", "calendar", scope="market",
      note="1.0 in the last three days of a month — the options/futures expiry neighbourhood")
def _f_month_end(w: _Work) -> pd.Series:
    ts = w.d["ts"]
    return (ts.dt.days_in_month - ts.dt.day <= 2).astype(float)


@_reg("cal_hour", "calendar", scope="market",
      note="UTC hour. Constant zero on a daily panel, and registered anyway so an intraday "
           "study gets it from the same place")
def _f_hour(w: _Work) -> pd.Series:
    return w.d["ts"].dt.hour.astype(float)


@_reg("cal_hours_to_funding", "calendar", scope="market",
      note="hours until the next 00/08/16 UTC perp funding settlement. Zero on a daily panel; "
           "this is the intraday clock the funding-rate literature keys on")
def _f_hours_to_funding(w: _Work) -> pd.Series:
    h = w.d["ts"].dt.hour + w.d["ts"].dt.minute / 60.0
    return ((8.0 - (h % 8.0)) % 8.0).astype(float)


@_reg("cal_days_to_fomc", "calendar", scope="market", needs=("fomc",),
      note="days until the next FOMC statement. Scheduled meetings are published ~2 years "
           "ahead so this is point-in-time; the 2020 EMERGENCY meetings are excluded by "
           "FOMC_UNSCHEDULED because nobody knew about those in advance")
def _f_days_to_fomc(w: _Work) -> pd.Series:
    return _fomc_distance(w, forward=True)


@_reg("cal_days_from_fomc", "calendar", scope="market", needs=("fomc",),
      note="days since the last FOMC statement. Unlike days-to, this needs no scheduling "
           "assumption at all")
def _f_days_from_fomc(w: _Work) -> pd.Series:
    return _fomc_distance(w, forward=False)


@_reg("cal_fomc_window", "calendar", scope="market", needs=("fomc",),
      note="1.0 within one day either side of an FOMC statement — the risk gate's blackout shape")
def _f_fomc_window(w: _Work) -> pd.Series:
    to = _fomc_distance(w, forward=True)
    fr = _fomc_distance(w, forward=False)
    near = (to <= 1.0) | (fr <= 1.0)
    return near.astype(float).where(to.notna() | fr.notna())


def _fomc_distance(w: _Work, *, forward: bool) -> pd.Series:
    """Calendar days to the next / from the previous FOMC statement.

    Both sides are converted to naive ``datetime64[ns]`` before the search and the difference is
    divided by ``np.timedelta64(1, "D")``. Doing this through ``astype("int64")`` instead — which
    is the obvious way to write it — is a **silent unit bug**: this pandas build backs the panel's
    ``ts`` with ``datetime64[us]`` while ``pd.Timestamp.value`` is always nanoseconds, so the two
    integer grids differ by 1000x, ``searchsorted`` returns 0 or -1 for every row, and the
    feature comes out either all-NaN or a constant 16,000 days. The first form of that failure is
    obvious; the second is not, and it is what a coverage check alone would have let through.
    """
    dates = w.ctx.exog.fomc
    if not dates:
        return pd.Series(np.nan, index=w.d.index)
    grid = pd.DatetimeIndex(dates)
    if grid.tz is not None:
        grid = grid.tz_convert("UTC").tz_localize(None)
    grid = np.sort(grid.to_numpy(dtype="datetime64[ns]"))
    day = w.d["ts"].dt.floor("D").dt.tz_localize(None).to_numpy(dtype="datetime64[ns]")
    out = np.full(day.shape, np.nan)
    if forward:
        pos = np.searchsorted(grid, day, side="left")
        ok = pos < grid.size
        out[ok] = (grid[pos[ok]] - day[ok]) / np.timedelta64(1, "D")
    else:
        pos = np.searchsorted(grid, day, side="right") - 1
        ok = pos >= 0
        out[ok] = (day[ok] - grid[pos[ok]]) / np.timedelta64(1, "D")
    return pd.Series(out, index=w.d.index)


# --------------------------------------------------------------------------- groups

def _build_groups() -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for n, m in META.items():
        out.setdefault(m.group, []).append(n)
    return {k: tuple(v) for k, v in out.items()}


#: ``group -> feature names``. Built once from :data:`META`, so it can never drift from it.
GROUPS: dict[str, tuple[str, ...]] = _build_groups()


# --------------------------------------------------------------------------- cross-section

def cross_sectional_rank_eligible(frame: pd.DataFrame, col: str, *, time_col: str = "ts",
                                  min_names: int = 5,
                                  eligible_col: str = "eligible") -> pd.Series:
    """Rank ``col`` within each timestamp **over the eligible names only**, scaled to [0, 1].

    This is not the same column as :func:`ml.features.cross_sectional_rank`, and the difference
    is not cosmetic. The landed ``_xs`` features are ranked over every segment in the panel
    — including coins that were 40 days old, coins with $12k of daily volume, and pegs — and the
    rows are filtered to eligible **afterwards**. So an eligible coin's ``vol_60_xs`` of 0.3 is
    its rank among *everything listed*, not its rank among the names the bots could actually
    have bought, and the two differ by however much the ineligible tail distorts the
    distribution. The audit's IC -0.140 was measured on the eligible universe.

    Rows outside the eligible set get NaN, because a rank inside a universe they are not in is
    not a number about them.
    """
    if eligible_col not in frame.columns:
        return cross_sectional_rank(frame, col, time_col=time_col, min_names=min_names)
    mask = frame[eligible_col].fillna(False).astype(bool)
    vals = frame[col].where(mask)
    keys = frame[time_col]
    # Vectorised on purpose. The obvious ``groupby(...).transform(lambda s: ...)`` form goes
    # through pandas' slow path and, on this pandas build, raises ``'Series' object has no
    # attribute 'columns'`` from inside ``concat`` as soon as one timestamp's group is entirely
    # NaN — which happens for every early timestamp at which no coin was eligible yet.
    # ``SeriesGroupBy.rank`` is C-implemented, keeps NaN as NaN, and is also ~30x faster.
    ranked = vals.groupby(keys, sort=False).rank(pct=True)
    live = vals.notna().groupby(keys, sort=False).transform("sum")
    return ranked.where(live >= min_names)


def rank_suffix_features(names: Sequence[str]) -> list[str]:
    """``["vol_60"] -> ["vol_60_xse"]``. The suffix is ``_xse`` (eligible-universe rank) so it
    cannot be confused with the landed ``_xs`` (whole-panel rank)."""
    return [f"{n}_xse" for n in names]


# --------------------------------------------------------------------------- the builder

def add_ext_features(frame: pd.DataFrame, names: Sequence[str] | None = None, *,
                     ctx: ExtContext | None = None,
                     cross_sectional: Sequence[str] = (),
                     drop_unavailable: bool = True) -> pd.DataFrame:
    """Attach the requested extended features, plus ``<name>_xse`` ranks for ``cross_sectional``.

    ``drop_unavailable`` silently skips features whose exogenous source is not on this host and
    whose group is in :meth:`ExogBundle.missing_groups` — with ``False`` it raises instead, which
    is what a test wants. Either way the skip is never a column of NaN pretending to be a
    feature: :func:`ml.select` reads the returned columns, so a missing group simply is not
    measured rather than being measured as zero.

    Sorted by (symbol, ts) internally and restored to the caller's index on the way out.
    """
    ctx = ctx or ExtContext()
    want = list(names) if names is not None else list(EXT_BUILDERS)
    unknown = [n for n in want if n not in EXT_BUILDERS]
    if unknown:
        raise KeyError(f"unknown ext features {unknown}; known: {sorted(EXT_BUILDERS)}")

    missing = set(ctx.exog.missing_groups())
    skipped = []
    keep = []
    for n in want:
        need_exog = {k for k in META[n].needs if k in ("dvol", "oi", "fomc")}
        if need_exog & missing:
            skipped.append((n, sorted(need_exog & missing)))
            continue
        keep.append(n)
    if skipped and not drop_unavailable:
        raise KeyError(f"exogenous sources missing for {skipped}; load_exog() reported "
                       f"{sorted(missing)} absent")

    d = frame.sort_values(["symbol", "ts"], kind="stable")
    order = d.index
    d = d.reset_index(drop=True)
    w = _Work(d, ctx)
    built: dict[str, pd.Series] = {}
    for n in keep:
        s = EXT_BUILDERS[n](w)
        built[n] = pd.Series(np.asarray(s, dtype=float), index=d.index)
    res = pd.concat([d, pd.DataFrame(built, index=d.index)], axis=1)

    for n in cross_sectional:
        if n in res.columns:
            res[f"{n}_xse"] = cross_sectional_rank_eligible(res, n)
    res.index = order
    out = res.loc[frame.index]
    out.attrs["ext_skipped"] = skipped
    return out


# --------------------------------------------------------------------------- the assertion

def assert_no_lookahead_ext(frame: pd.DataFrame, names: Sequence[str] | None = None, *,
                            ctx: ExtContext | None = None,
                            cross_sectional: Sequence[str] = (),
                            cut_frac: float = 0.7, tol: float = 1e-8) -> pd.DataFrame:
    """Rebuild on a prefix of the panel and require identical values. Raises on any mismatch.

    Same proof as :func:`ml.features.assert_no_lookahead`, extended to the two families in this
    module that a code read would not settle:

    * the running-maximum family, whose windows include the current bar on purpose;
    * the **market** aggregates, which are ``groupby("ts")`` statistics and would become
      full-sample constants if that grouping were ever dropped — a mistake that improves every
      score in a study and is invisible in the output.

    ``tol`` is 1e-8 rather than 1e-9 because three features here go through
    ``rolling(mean of products)`` covariance algebra, whose float error at 841k rows exceeds
    1e-9 without anything being wrong. A lookahead does not produce a 1e-9 disagreement; it
    produces a visible one.
    """
    d = frame.sort_values(["symbol", "ts"], kind="stable").reset_index(drop=True)
    if d.empty:
        return pd.DataFrame(columns=["feature", "n_compared", "max_abs_diff", "ok"])
    ts = pd.to_datetime(d["ts"], utc=True)
    cut = ts.quantile(cut_frac)
    full = add_ext_features(d, names, ctx=ctx, cross_sectional=cross_sectional)
    prefix = add_ext_features(d.loc[ts <= cut].reset_index(drop=True), names, ctx=ctx,
                              cross_sectional=cross_sectional)

    key = ["symbol", "ts"]
    cols = [c for c in full.columns if c not in d.columns]
    a = full.loc[ts <= cut, key + cols].set_index(key).sort_index()
    b = prefix.set_index(key).sort_index()[[c for c in cols if c in prefix.columns]]
    shared = a.index.intersection(b.index)
    a, b = a.loc[shared], b.loc[shared]

    rows, bad = [], []
    for c in cols:
        if c not in b.columns:
            continue
        x, y = a[c].astype(float), b[c].astype(float)
        both = x.notna() & y.notna()
        nan_mismatch = int((x.notna() != y.notna()).sum())
        diff = float((x[both] - y[both]).abs().max()) if both.any() else 0.0
        ok = (diff <= tol) and nan_mismatch == 0
        rows.append({"feature": c, "group": META[c].group if c in META else "xse",
                     "n_compared": int(both.sum()), "max_abs_diff": diff,
                     "nan_mismatch": nan_mismatch, "ok": ok})
        if not ok:
            bad.append(c)
    rep = pd.DataFrame(rows)
    if bad:
        raise LookaheadError(
            "these extended features changed when the future was removed, so they use future "
            f"information: {bad}\n" + rep.loc[~rep["ok"]].to_string(index=False))
    return rep
