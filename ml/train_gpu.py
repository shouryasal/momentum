"""Walk-forward training of the GPU zoo, and the honest table it produces.

What this does and what it refuses to do
---------------------------------------
Trains every model in :func:`ml.models_gpu.zoo_specs` on the point-in-time panel from
:mod:`ml.data`, on the purged and embargoed folds from :mod:`ml.splits`, and scores them with
:mod:`ml.metrics`. It **reimplements no split and no metric**. It refuses to report a price-level
MAPE, and every return metric it prints carries the zero-forecast baseline in the same row.

The three targets, and why three
--------------------------------
=========  ===============================================  ===============================
``ret``    forward 7-day log return                          expected to be near-unforecastable
``vol``    log forward 7-day annualised realised vol         expected to work; implied vol gets R2 0.268
``dd``     forward 7-day breach of -20%                      expected to work; funding moves it 17% -> 41%
=========  ===============================================  ===============================

One model, three heads, one set of folds. A zoo that only predicted returns would report seven
failures and hide the fact that the same encoder does something useful on the other two tasks.

How the panel becomes GPU tensors — the design that makes 6 GB irrelevant
-------------------------------------------------------------------------
The naive construction materialises ``[n_samples, L, F]``: at 206k samples, L=64, F=19 that is
**1.0 GiB** in fp32 and it has to be sliced on the host and copied every batch. Instead
:class:`PanelTensors` keeps the **flat** panel ``[n_rows, F]`` resident in VRAM — 841k x 19 x 4
bytes is **61 MiB** — plus one ``int32`` index per sample, and gathers each batch's windows
on-device with ``X[start[:, None] + arange(L)]``. There is no host-to-device copy in the training
loop at all, and the whole dataset for every model in the zoo fits in 1% of this card.

The consequence is the first honest finding of this phase: **VRAM is not the binding constraint
at daily granularity.** :func:`ml.models_gpu.largest_that_fits` is run anyway, so the ceiling is
a measured number and the owner knows where it *would* bind.

Four places this could leak, and what stops each
------------------------------------------------
1. **A window reaching across a symbol or a gap.** Windows are validated by a per-row
   *consecutive-run* counter computed on the segment-sorted panel; a row whose run is shorter
   than ``L-1`` is not a sample. So no window spans two coins, a delisting gap, or a
   discontinuity split.
2. **A window reaching into the future.** The window ends **at** the decision bar inclusive and
   every feature in it is backward-looking (:func:`ml.features.assert_no_lookahead`). The TCN is
   additionally causal by construction.
3. **The scaler seeing the test period.** :class:`FoldScaler` is fitted on panel rows with
   ``ts <= train_end`` only, refitted per fold. Fitting it once on the whole panel is the
   commonest quiet leak in this kind of code and it moves the third decimal place, which is
   exactly the size of the effect being claimed.
4. **Early stopping on the test set.** The inner validation slice is the last
   ``val_frac`` of the *training* window, with its own purge of ``horizon`` bars in front of it.
   The test fold is touched exactly once per model per fold, to predict.

:func:`ml.splits.assert_no_leakage` then re-derives the overlap from ``t1`` and raises. It runs
on every study here, not as an option.

Sample weighting
----------------
Labels overlap: a 7-day forward return sampled daily shares six sevenths of its window with its
neighbour. Weights come from :func:`ml.labels.uniqueness_weights` with ``scope="symbol"``, and
that choice is deliberate and worth stating: ``scope="panel"`` would treat 394 coins carrying a
label from the same Monday as close to one observation and therefore down-weight the entire
cross-section this model exists to learn. Symbol scope is the upper bound on effective sample
size; the report prints both, so no claim here rests on the flattering end alone.

What the report is judged on
----------------------------
Directional accuracy with a Wilson CI against 50%; cross-sectional rank IC with Newey-West
t-stats at lag = horizon; Brier and reliability for the drawdown head; out-of-sample R2 against
a zero forecast for returns and against trailing realised vol for the vol head; and the Sharpe
of a costed long-only top-k book at 15 bps per side against BTC buy-and-hold over the same
out-of-sample span. Wall-clock and peak VRAM per model are in the same table, because the owner
asked what retraining costs on this machine.

Usage
-----
``python -m ml.train_gpu --quick`` — smoke run on a small symbol subset, minutes.
``python -m ml.train_gpu --out runs/ml4`` — the full zoo, and the artefacts the report cites.
``python -m ml.train_gpu --ceiling`` — the VRAM ceiling probe alone.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F

from ml.data import Dataset, PanelSpec, build_dataset, with_funding
from ml.features import add_features
from ml.labels import horizon_bars, make_labels, uniqueness_weights
from ml.metrics import (
    COST_BPS_PER_SIDE,
    brier,
    costed_backtest,
    directional_accuracy,
    rank_ic,
    reliability,
    return_errors,
)
from ml.models_gpu import (
    ModelSpec,
    amp_dtype,
    build_model,
    gpu_ceiling,
    largest_that_fits,
    param_count,
    pick_device,
    vram_probe,
    zoo_specs,
)
from ml.registry import Trials, seed_everything
from ml.splits import assert_no_leakage, walk_forward

__all__ = [
    "ALL_FEATURES",
    "FEATURES",
    "FUNDING_FEATURES",
    "FoldScaler",
    "feature_block",
    "PanelTensors",
    "TrainConfig",
    "build_panel",
    "gbdt_gpu_fold",
    "main",
    "run_model",
    "run_zoo",
    "topk_book",
]

#: The feature block. ``logret_1`` is **index 0 and must stay there** — :class:`ml.models_gpu.NBeats`
#: takes it as the univariate backcast channel, and reordering this tuple silently changes what
#: N-BEATS is decomposing. ``mom_365`` is absent on purpose: it is NaN on 17.6% of eligible rows,
#: and imputing a year of momentum for a coin that has not existed a year is inventing data.
FEATURES: tuple[str, ...] = (
    "logret_1",
    "vol_20", "vol_60", "vol_ratio_20_60",
    "mom_7", "mom_30", "mom_90",
    "adv_90", "log_adv_90", "amihud_30",
    "vol_trend_30_180", "dist_from_high_90", "drawdown_from_ath",
    "above_ma_200", "above_ma_50",
)
#: Cross-sectional rank forms, appended by :func:`ml.features.add_features`. These are the forms
#: the project measured as carrying signal (``vol_60_xs`` IC -0.140, monotone in every regime);
#: the own-history percentile form inverts in 2019-22 and is not used.
XS_FEATURES: tuple[str, ...] = ("vol_60_xs", "adv_90_xs", "vol_trend_30_180_xs",
                               "dist_from_high_90_xs")
#: Appended last, log1p-compressed. Age is the second-strongest forward feature in the project's
#: own audit (+0.13 to +0.18) and the panel carries it already.
AGE_FEATURE = "log_age_days"

ALL_FEATURES: tuple[str, ...] = FEATURES + XS_FEATURES + (AGE_FEATURE,)

#: Added only when ``TrainConfig.with_funding`` is on. Perpetual funding is the project's measured
#: driver of forward **drawdown** (17% -> 41% across buckets, ``crypto-research.md`` §1.2) and of
#: nothing else — it does not predict forward return. It is off by default because it is NaN for the
#: 125 ever-eligible symbols with no perp and for everything before 2019-09, and this pipeline drops
#: a sample whose window contains any NaN rather than imputing one. Turning it on therefore trades
#: sample size for one strong feature, and the ablation reports both sides of that trade.
FUNDING_FEATURES: tuple[str, ...] = ("funding_ann",)


def feature_block(cfg: TrainConfig) -> tuple[str, ...]:
    """The feature names for this configuration, in channel order. ``logret_1`` stays first."""
    base = FEATURES + (FUNDING_FEATURES if cfg.with_funding else ())
    return base + XS_FEATURES + (AGE_FEATURE,)


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class TrainConfig:
    """Everything that is not the model. Hashed with the :class:`ModelSpec` into the trial record."""

    horizon: str = "7d"
    timeframe: str = "1d"
    dd_threshold: int = 20
    n_splits: int = 5
    embargo_frac: float = 0.01
    min_train: int = 5_000
    #: Last fraction of each training window held back for early stopping, purged in front.
    val_frac: float = 0.15
    seed: int = 0
    cost_bps: float = COST_BPS_PER_SIDE
    #: Long-only top-k book size for the costed test. 10 names of ~30 eligible is the project's
    #: own satellite breadth; it is not swept, because sweeping it is a trial per value.
    top_k: int = 10
    #: Weekly rebalance, matching the 7-day horizon. Trading a 7-day forecast daily pays the
    #: cost seven times for the same information — growth-audit.md §2.5 measured what that does.
    rebalance_weekday: int = 0
    symbols: tuple[str, ...] | None = None
    max_rows: int | None = None
    #: Join daily annualised perpetual funding and add it as a feature. See
    #: :data:`FUNDING_FEATURES` for why this is off by default and what it costs.
    with_funding: bool = False

    def as_dict(self) -> dict:
        d = dict(vars(self))
        d["symbols"] = list(self.symbols) if self.symbols else None
        return d


# --------------------------------------------------------------------------- panel


def build_panel(cfg: TrainConfig, *, root: Path | None = None,
                frame: pd.DataFrame | None = None) -> tuple[pd.DataFrame, Dataset]:
    """The panel with features, labels and the multi-horizon path targets attached.

    Built with ``eligible_only=False`` **on purpose**, which looks like the opposite of the
    discipline and is not. A sequence model needs the bars *before* a coin became eligible to
    fill its lookback window; those bars are legitimate history. Eligibility is enforced where it
    belongs — on the rows that become *samples* — via the ``eligible`` column, so nothing is
    trained or scored on a coin before it was listed and liquid, and dead coins keep every bar
    they had.

    The ``fwd_1``..``fwd_H`` columns are the daily forward log returns that supervise the
    multi-horizon families. They sum to ``logret_7d`` by construction, which
    :func:`tests.test_ml.test_train_gpu` asserts — if they did not, the path head and the return
    head would be fitting two different questions.
    """
    spec = PanelSpec(timeframe=cfg.timeframe, symbols=cfg.symbols)
    ds = build_dataset(spec, root=root, frame=frame)
    if cfg.with_funding:
        ds = with_funding(ds, root=root)
    df = ds.frame
    h = horizon_bars(cfg.horizon, cfg.timeframe)

    # Only the *builder* names go to add_features. The ``_xs`` columns are produced by it as a
    # side effect of the base name being present, and ``log_age_days`` is derived below from the
    # panel's own ``age_days``, so neither is a builder and passing either would raise.
    builders = list(FEATURES) + (list(FUNDING_FEATURES) if cfg.with_funding else [])
    df = add_features(df, names=builders, bars_per_year=spec.bars_per_year)
    lab = make_labels(df, timeframe=cfg.timeframe, horizons=("1d", cfg.horizon),
                      dd_thresholds=(-cfg.dd_threshold / 100.0,),
                      bars_per_year=spec.bars_per_year)
    df = df.merge(lab.frame, on=["ts", "symbol"], how="left")

    df = df.sort_values(["symbol", "ts"], kind="stable").reset_index(drop=True)
    df[AGE_FEATURE] = np.log1p(df["age_days"].astype("float64"))
    # Daily forward path. shift(-k) of the one-bar log return is the return earned over
    # (t+k-1, t+k], so fwd_1..fwd_h sum to the h-bar forward log return exactly.
    g = df.groupby("symbol", sort=False)["logret_1"]
    for k in range(1, h + 1):
        df[f"fwd_{k}"] = g.shift(-k)

    # Consecutive-run length in bars, per symbol, on the exact bar spacing. This is what makes a
    # lookback window provably within one instrument and free of gaps.
    bar = pd.Timedelta(days=1) if cfg.timeframe == "1d" else pd.Timedelta(
        hours={"1h": 1, "4h": 4}[cfg.timeframe])
    dt = df.groupby("symbol", sort=False)["ts"].diff()
    contiguous = (dt == bar).to_numpy()
    run = np.zeros(len(df), dtype=np.int32)
    for i in range(1, len(df)):
        run[i] = run[i - 1] + 1 if contiguous[i] else 0
    df["run"] = run
    return df, ds


@dataclass
class PanelTensors:
    """The flat panel in VRAM plus the per-sample window index. No per-batch host copy.

    ``X`` is ``[n_rows, F]`` raw (unscaled) fp32 — scaling is per fold and applied in the gather,
    so one resident copy serves every fold and every model. ``ends`` holds the *row position* of
    each sample's decision bar; the window is ``ends[i]-L+1 .. ends[i]``.

    ``sample_rows`` maps sample index back to the DataFrame row, which is how a prediction gets
    its ``ts``, ``symbol`` and ``t1`` for the split and the metrics.
    """

    X: torch.Tensor
    y_ret: torch.Tensor
    y_vol: torch.Tensor
    y_dd: torch.Tensor
    y_path: torch.Tensor
    w: torch.Tensor
    ends: torch.Tensor
    sample_rows: np.ndarray
    meta: pd.DataFrame
    features: tuple[str, ...]
    device: torch.device
    seq_len: int
    horizon: int
    #: Both ends of the effective-sample bracket, from :func:`ml.labels.uniqueness_weights`.
    #: ``symbol`` counts the cross-section as independent (upper bound); ``panel`` counts it as one
    #: factor (lower bound). Every t-statistic in the report is somewhere between these two
    #: sample sizes, and printing one without the other is choosing a flattering end.
    eff_n_symbol: float = float("nan")
    eff_n_panel: float = float("nan")

    @property
    def n_samples(self) -> int:
        return int(self.ends.numel())

    @property
    def n_features(self) -> int:
        return int(self.X.shape[1])

    def gather(self, idx: torch.Tensor, scaler: FoldScaler) -> torch.Tensor:
        """``[B, L, F]`` scaled windows for sample positions ``idx``, entirely on device."""
        ends = self.ends[idx]
        offs = torch.arange(-self.seq_len + 1, 1, device=self.device)
        rows = ends[:, None] + offs[None, :]
        return scaler.apply(self.X[rows])

    def vram_MiB(self) -> float:
        tens = [self.X, self.y_ret, self.y_vol, self.y_dd, self.y_path, self.w, self.ends]
        return round(sum(t.element_size() * t.numel() for t in tens) / 1024**2, 2)


def make_tensors(df: pd.DataFrame, cfg: TrainConfig, spec: ModelSpec, *,
                 device: torch.device | None = None,
                 features: tuple[str, ...] | None = None) -> PanelTensors:
    """Select samples, move the flat panel to the device, and build the window index.

    A row becomes a sample only when **all** of these hold, and each exclusion is a different
    bug it prevents:

    * ``eligible`` — point-in-time universe. Without it the model trains on coins before they
      were listed or liquid, which is not survivorship bias but something worse: prices from a
      period when the series could not have been traded at all.
    * ``run >= seq_len - 1`` — a complete, gap-free lookback window inside one segment.
    * every target finite — a label at the end of a dead coin's history does not exist, and
      filling it with zero teaches the model that delisting is a flat week.
    """
    dev = device or pick_device()
    h = cfg.horizon
    ret_col, dd_col = f"logret_{h}", f"dd_{h}_{cfg.dd_threshold}"
    vol_col, t1_col = f"rvol_{h}", f"t1_logret_{h}"
    hb = horizon_bars(h, cfg.timeframe)
    path_cols = [f"fwd_{k}" for k in range(1, hb + 1)]

    feats = list(features if features is not None else feature_block(cfg))
    missing = [c for c in feats + [ret_col, dd_col, vol_col, t1_col] if c not in df.columns]
    if missing:
        raise KeyError(f"panel is missing {missing}")

    Xall = df[feats].to_numpy(dtype=np.float32, copy=True)
    ok_feat = np.isfinite(Xall).all(axis=1)
    targets = df[[ret_col, vol_col, dd_col, *path_cols]].to_numpy(dtype=np.float64)
    ok_tgt = np.isfinite(targets).all(axis=1) & (df[vol_col].to_numpy(dtype=np.float64) > 0)
    take = (df["eligible"].to_numpy(dtype=bool)
            & (df["run"].to_numpy() >= spec.seq_len - 1)
            & ok_tgt)
    # A sample's *window* must be finite too, not just its decision bar. Run the AND backwards
    # over the window with a cumulative-min trick rather than a python loop.
    win_ok = ok_feat.copy()
    if spec.seq_len > 1:
        acc = ok_feat.astype(np.int8)
        for lag in range(1, spec.seq_len):
            acc[lag:] &= ok_feat[:-lag]
        win_ok = acc.astype(bool)
    take &= win_ok
    rows = np.flatnonzero(take)
    if rows.size == 0:
        raise ValueError("no samples survived the eligibility / window / target filter")

    meta = df.loc[rows, ["ts", "symbol", t1_col, ret_col, vol_col, dd_col, "vol_20",
                         "close"]].copy()
    meta = meta.rename(columns={t1_col: "t1", ret_col: "y_ret", vol_col: "y_vol",
                                dd_col: "y_dd"}).reset_index(drop=True)

    # Uniqueness weights, symbol scope (the upper bound on effective N — see the module docstring).
    m_uw = meta.assign(t1_ts=meta["t1"])
    raw = uniqueness_weights(m_uw, time_col="ts", end_col="t1_ts",
                             scope="symbol").to_numpy(dtype=np.float64)
    raw = np.nan_to_num(raw, nan=1.0 / hb)
    eff_symbol = float(raw.sum())
    eff_panel = float(np.nan_to_num(uniqueness_weights(
        m_uw, time_col="ts", end_col="t1_ts", scope="panel").to_numpy(dtype=np.float64)).sum())
    uw = (raw / max(float(raw.mean()), 1e-9)).astype(np.float32)

    t = lambda a, dt=torch.float32: torch.as_tensor(a, dtype=dt, device=dev)  # noqa: E731
    return PanelTensors(
        X=t(Xall),
        y_ret=t(meta["y_ret"].to_numpy(dtype=np.float32)),
        y_vol=t(np.log(np.clip(meta["y_vol"].to_numpy(dtype=np.float64), 1e-4, None))),
        y_dd=t(meta["y_dd"].to_numpy(dtype=np.float32)),
        y_path=t(df.loc[rows, path_cols].to_numpy(dtype=np.float32)),
        w=t(uw),
        ends=torch.as_tensor(rows, dtype=torch.long, device=dev),
        sample_rows=rows, meta=meta, features=tuple(feats), device=dev,
        seq_len=spec.seq_len, horizon=hb,
        eff_n_symbol=round(eff_symbol, 1), eff_n_panel=round(eff_panel, 1),
    )


# --------------------------------------------------------------------------- scaling


@dataclass
class FoldScaler:
    """Robust per-feature centre and scale, fitted on **training rows only**, refitted per fold.

    Median and IQR rather than mean and standard deviation, because ``logret_1`` on this panel
    contains days like a 177,399x symbol reuse before discontinuity splitting and single-day
    listing moves after it; a mean-and-sd scaler hands one such bar the whole scale and the
    network then sees every normal day as zero.

    ``clip`` at 5 robust deviations is applied after scaling. The alternative — winsorising the
    raw panel — would change the feature definition; clipping the network's *input* leaves the
    features as :mod:`ml.features` defines them and bounds what the first layer sees.
    """

    centre: torch.Tensor
    scale: torch.Tensor
    y_ret_mu: float
    y_ret_sd: float
    y_vol_mu: float
    y_vol_sd: float
    y_path_sd: float
    clip: float = 5.0

    @classmethod
    def fit(cls, pt: PanelTensors, train_idx: torch.Tensor, *,
            panel_rows_mask: torch.Tensor, clip: float = 5.0) -> FoldScaler:
        """``panel_rows_mask`` selects the flat panel rows the scaler may see: ``ts <= train_end``.

        Passing the sample indices alone would be wrong — the windows of training samples reach
        back further than the samples themselves, and a scaler fitted on decision bars only is
        fitted on a different distribution than the one the network is fed.
        """
        Xt = pt.X[panel_rows_mask]
        q = torch.nanquantile(Xt.double(), torch.tensor([0.25, 0.5, 0.75], device=pt.device,
                                                        dtype=torch.float64), dim=0)
        centre = q[1].float()
        iqr = (q[2] - q[0]).float()
        # 1.349 converts IQR to a standard-deviation-equivalent for a normal; the floor keeps a
        # constant feature (above_ma_200 through a long uptrend) from dividing by zero.
        scale = (iqr / 1.349).clamp_min(1e-6)
        yr = pt.y_ret[train_idx]
        yv = pt.y_vol[train_idx]
        yp = pt.y_path[train_idx]
        return cls(centre=centre, scale=scale,
                   y_ret_mu=float(yr.mean()), y_ret_sd=float(yr.std().clamp_min(1e-8)),
                   y_vol_mu=float(yv.mean()), y_vol_sd=float(yv.std().clamp_min(1e-8)),
                   y_path_sd=float(yp.std().clamp_min(1e-8)), clip=clip)

    def apply(self, x: torch.Tensor) -> torch.Tensor:
        z = (x - self.centre) / self.scale
        return torch.nan_to_num(z.clamp_(-self.clip, self.clip), nan=0.0)

    def std_ret(self, y: torch.Tensor) -> torch.Tensor:
        return (y - self.y_ret_mu) / self.y_ret_sd

    def unstd_ret(self, z: np.ndarray) -> np.ndarray:
        return z * self.y_ret_sd + self.y_ret_mu

    def std_vol(self, y: torch.Tensor) -> torch.Tensor:
        return (y - self.y_vol_mu) / self.y_vol_sd

    def unstd_vol(self, z: np.ndarray) -> np.ndarray:
        return z * self.y_vol_sd + self.y_vol_mu

    def as_dict(self) -> dict:
        return {"y_ret_sd": self.y_ret_sd, "y_vol_sd": self.y_vol_sd,
                "y_path_sd": self.y_path_sd, "clip": self.clip}


# --------------------------------------------------------------------------- training


def _inner_split(meta: pd.DataFrame, train_idx: np.ndarray, cfg: TrainConfig,
                 hb: int) -> tuple[np.ndarray, np.ndarray]:
    """Cut the last ``val_frac`` of the training **rows** off for early stopping, purged in front.

    The purge is ``hb`` bars of calendar, removed from the end of inner-train, so no inner-train
    label resolves inside inner-val. Without it the early-stopping signal is contaminated by the
    same overlap the outer purge exists to remove, and the epoch count it picks is optimistic.

    **The cut is a row quantile, not a calendar fraction, and that is not a detail.** This panel's
    coin count grows from 13 names in 2018 to 239 in 2024, so the last 15% of a fold's *calendar*
    holds far more than 15% of its rows: measured on fold 2, a calendar cut left 16,502 rows to
    train on and 22,190 to validate on — more validation than training, on the fold that already
    has the least data. Cutting at the 85th percentile of the training rows' timestamps keeps the
    split strictly time-ordered and puts 85% of the rows where they belong.
    """
    ts = pd.DatetimeIndex(pd.to_datetime(meta["ts"].to_numpy()[train_idx], utc=True))
    if ts.size == 0:
        return train_idx, train_idx[:0]
    cut = ts.sort_values()[min(int(ts.size * (1.0 - cfg.val_frac)), ts.size - 1)]
    purge = pd.Timedelta(days=hb) if cfg.timeframe == "1d" else pd.Timedelta(
        hours=hb * {"1h": 1, "4h": 4}[cfg.timeframe])
    inner_tr = train_idx[np.asarray(ts < (cut - purge))]
    inner_va = train_idx[np.asarray(ts >= cut)]
    if inner_va.size < 64 or inner_tr.size < 256:      # too small to stop on: train the full span
        return train_idx, train_idx[:0]
    return inner_tr, inner_va


def _loss(out: Any, pt: PanelTensors, sc: FoldScaler, idx: torch.Tensor, *,
          use_path: bool) -> torch.Tensor:
    """Weighted multi-task loss: Huber on return, MSE on log-vol, BCE-with-logits on drawdown.

    **Huber on the return, not MSE.** A 7-day crypto return distribution has a kurtosis that puts
    most of an MSE gradient on a handful of bars; those bars are real, but fitting them is how a
    model ends up with a large in-sample R2 and a negative out-of-sample one. Huber caps each
    observation's gradient at the transition point and the loss is therefore about the middle of
    the distribution, which is where a costed strategy earns.

    Weights are the uniqueness weights, mean-normalised, so the three task terms stay comparable
    in magnitude and the sum is not implicitly re-weighting the tasks by sample count.
    """
    w = pt.w[idx]
    wn = w / w.mean().clamp_min(1e-9)
    ret = F.huber_loss(out.ret.float(), sc.std_ret(pt.y_ret[idx]), reduction="none", delta=1.0)
    vol = F.mse_loss(out.vol.float(), sc.std_vol(pt.y_vol[idx]), reduction="none")
    dd = F.binary_cross_entropy_with_logits(out.dd.float(), pt.y_dd[idx], reduction="none")
    total = (wn * (ret + vol + dd)).mean()
    if use_path and out.path is not None:
        tgt = pt.y_path[idx] / sc.y_path_sd
        p = F.huber_loss(out.path.float(), tgt, reduction="none", delta=1.0).mean(dim=-1)
        total = total + (wn * p).mean()
    return total


@torch.no_grad()
def _predict(model: Any, pt: PanelTensors, sc: FoldScaler, idx: np.ndarray, *,
             batch: int = 4096) -> dict[str, np.ndarray]:
    model.eval()
    dev = pt.device
    dtype = amp_dtype(dev)
    out: dict[str, list[np.ndarray]] = {"ret": [], "vol": [], "dd": [], "path0": []}
    for s in range(0, idx.size, batch):
        j = torch.as_tensor(idx[s:s + batch], dtype=torch.long, device=dev)
        with torch.autocast(dev.type, dtype=dtype, enabled=dtype is not None):
            f = model(pt.gather(j, sc))
        out["ret"].append(f.ret.float().cpu().numpy())
        out["vol"].append(f.vol.float().cpu().numpy())
        out["dd"].append(torch.sigmoid(f.dd.float()).cpu().numpy())
        out["path0"].append(f.path[:, 0].float().cpu().numpy() if f.path is not None
                            else np.full(j.numel(), np.nan, dtype=np.float32))
    return {k: np.concatenate(v) for k, v in out.items()}


def train_fold(spec: ModelSpec, cfg: TrainConfig, pt: PanelTensors, train_idx: np.ndarray,
               test_idx: np.ndarray, panel_mask: torch.Tensor, *,
               verbose: bool = False) -> dict:
    """Fit one model on one fold and predict its test window exactly once.

    Returns the predictions plus the cost columns — wall-clock, peak VRAM, epochs actually run
    and the best inner-validation loss. ``epochs_run`` below ``max_epochs`` means early stopping
    fired, which on this data it usually does within ten epochs and is itself a finding: the
    information runs out long before the capacity does.
    """
    dev = pt.device
    seed_everything(cfg.seed)
    model = build_model(spec, pt.n_features).to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=spec.lr, weight_decay=spec.weight_decay)
    dtype = amp_dtype(dev)
    scaler = torch.amp.GradScaler(dev.type, enabled=(dtype == torch.float16))
    use_path = spec.path_h > 0

    inner_tr, inner_va = _inner_split(pt.meta, train_idx, cfg, pt.horizon)
    sc = FoldScaler.fit(pt, torch.as_tensor(inner_tr, dtype=torch.long, device=dev),
                        panel_rows_mask=panel_mask)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, spec.max_epochs))

    if dev.type == "cuda":
        torch.cuda.reset_peak_memory_stats(dev)
    t0 = time.perf_counter()
    gen = torch.Generator(device="cpu").manual_seed(cfg.seed)
    best, best_state, bad, epochs = math.inf, None, 0, 0
    for epoch in range(spec.max_epochs):
        model.train()
        perm = torch.randperm(inner_tr.size, generator=gen).numpy()
        order = inner_tr[perm]
        for s in range(0, order.size, spec.batch_size):
            j = torch.as_tensor(order[s:s + spec.batch_size], dtype=torch.long, device=dev)
            opt.zero_grad(set_to_none=True)
            with torch.autocast(dev.type, dtype=dtype, enabled=dtype is not None):
                loss = _loss(model(pt.gather(j, sc)), pt, sc, j, use_path=use_path)
            scaler.scale(loss).backward()
            scaler.unscale_(opt)
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            scaler.step(opt)
            scaler.update()
        sched.step()
        epochs = epoch + 1
        if inner_va.size == 0:
            continue
        model.eval()
        with torch.no_grad():
            vl, n = 0.0, 0
            for s in range(0, inner_va.size, 8192):
                j = torch.as_tensor(inner_va[s:s + 8192], dtype=torch.long, device=dev)
                with torch.autocast(dev.type, dtype=dtype, enabled=dtype is not None):
                    lv = _loss(model(pt.gather(j, sc)), pt, sc, j, use_path=use_path)
                vl += float(lv) * j.numel()
                n += j.numel()
            vl /= max(n, 1)
        if vl < best - 1e-5:
            best, bad = vl, 0
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
        else:
            bad += 1
            if bad >= spec.patience:
                break
        if verbose:
            print(f"    epoch {epoch:3d}  val {vl:.5f}  best {best:.5f}")
    if best_state is not None:
        model.load_state_dict(best_state)
    wall = time.perf_counter() - t0
    peak = (torch.cuda.max_memory_allocated(dev) / 1024**2) if dev.type == "cuda" else float("nan")

    pred = _predict(model, pt, sc, test_idx)
    del model, opt
    if dev.type == "cuda":
        torch.cuda.empty_cache()
    return {"pred": pred, "scaler": sc, "wall_s": wall, "peak_MiB": peak,
            "epochs_run": epochs, "best_val": best if math.isfinite(best) else None,
            "n_train": int(inner_tr.size), "n_val": int(inner_va.size),
            "n_test": int(test_idx.size)}


# --------------------------------------------------------------------------- GBDT on GPU


def gbdt_gpu_fold(cfg: TrainConfig, pt: PanelTensors, train_idx: np.ndarray,
                  test_idx: np.ndarray, panel_mask: torch.Tensor, *,
                  lags: tuple[int, ...] = (0,), device: str = "cuda",
                  n_estimators: int = 600, max_depth: int = 5,
                  learning_rate: float = 0.03) -> dict:
    """XGBoost on the same folds and the same features, timed on GPU and CPU.

    **This is the comparison that decides whether the zoo was worth building.** Deep models on
    tabular financial data usually lose to gradient boosting, and the only way that sentence
    becomes a measurement rather than folklore is to run the tree model through the identical
    split, weights, target definition and metric.

    ``lags`` controls how much of the sequence the trees get. ``(0,)`` is the decision bar only —
    the fair *tabular* comparison against :class:`ml.models_gpu.LinearProbe`. ``(0, 1, 5, 20)``
    flattens four bars of history in, which is the fair comparison against the sequence models:
    if the trees match them with four lags, the recurrence bought nothing.
    """
    import xgboost as xgb

    Xcpu = pt.X.detach().cpu().numpy()
    ends = pt.ends.detach().cpu().numpy()

    def design(idx: np.ndarray) -> np.ndarray:
        e = ends[idx]
        return np.concatenate([Xcpu[e - lag] for lag in lags], axis=1)

    inner_tr, inner_va = _inner_split(pt.meta, train_idx, cfg, pt.horizon)
    sc = FoldScaler.fit(pt, torch.as_tensor(inner_tr, dtype=torch.long, device=pt.device),
                        panel_rows_mask=panel_mask)
    Xtr, Xva, Xte = design(inner_tr), design(inner_va), design(test_idx)
    wtr = pt.w.detach().cpu().numpy()[inner_tr]
    y = {"ret": pt.y_ret.detach().cpu().numpy(), "vol": pt.y_vol.detach().cpu().numpy(),
         "dd": pt.y_dd.detach().cpu().numpy()}

    pred, walls = {}, {}
    for task in ("ret", "vol", "dd"):
        common = dict(n_estimators=n_estimators, max_depth=max_depth,
                      learning_rate=learning_rate, subsample=0.8, colsample_bytree=0.8,
                      reg_lambda=1.0, min_child_weight=20, tree_method="hist",
                      early_stopping_rounds=50, verbosity=0, random_state=cfg.seed)
        cls = xgb.XGBClassifier if task == "dd" else xgb.XGBRegressor
        if task == "dd":
            common["eval_metric"] = "logloss"
        t0 = time.perf_counter()
        m = cls(device=device, **common)
        fit_kw = {"sample_weight": wtr, "verbose": False}
        if inner_va.size:
            fit_kw["eval_set"] = [(Xva, y[task][inner_va])]
        else:
            common.pop("early_stopping_rounds", None)
            m = cls(device=device, **{k: v for k, v in common.items()
                                      if k != "early_stopping_rounds"})
        m.fit(Xtr, y[task][inner_tr], **fit_kw)
        walls[task] = time.perf_counter() - t0
        pred[task] = (m.predict_proba(Xte)[:, 1] if task == "dd"
                      else m.predict(Xte).astype(np.float64))
    # Standardise the regression heads so the metric code is shared with the torch models.
    pred["ret"] = (pred["ret"] - sc.y_ret_mu) / sc.y_ret_sd
    pred["vol"] = (pred["vol"] - sc.y_vol_mu) / sc.y_vol_sd
    pred["path0"] = np.full(test_idx.size, np.nan)
    return {"pred": pred, "scaler": sc, "wall_s": sum(walls.values()),
            "wall_by_task": walls, "peak_MiB": float("nan"),
            "epochs_run": n_estimators, "best_val": None,
            "n_train": int(inner_tr.size), "n_val": int(inner_va.size),
            "n_test": int(test_idx.size), "n_design_cols": int(Xtr.shape[1])}


# --------------------------------------------------------------------------- the costed book


def topk_book(df_pred: pd.DataFrame, panel: pd.DataFrame, cfg: TrainConfig) -> dict:
    """Long-only top-k on the return forecast, rebalanced weekly, costed at 15 bps per side.

    Construction, stated because every choice here can flatter a result:

    * **Long-only, fully invested, equal weight.** This system is spot-only, so a short leg would
      be unimplementable and a market-neutral Sharpe would be a number about a book that cannot
      be traded.
    * **Weekly rebalance on ``rebalance_weekday``**, held for the whole week. A 7-day forecast
      traded daily pays the 15 bps seven times over for one piece of information.
    * **Forward-filled onto the daily grid** and scored against daily returns, so the drawdown
      is a real intra-week path and not a weekly-close illusion.
    * **Out-of-sample dates only.** Weights exist only on dates some fold predicted.

    Returns the book, an equal-weight-all-eligible control, and BTC buy-and-hold over the *same*
    span — the benchmark ``growth-audit.md`` records as unbeaten on return by any rule tested.
    """
    if df_pred.empty:
        return {}
    d = df_pred.copy()
    d["ts"] = pd.to_datetime(d["ts"], utc=True)
    reb = d.loc[d["ts"].dt.dayofweek == cfg.rebalance_weekday]
    if reb.empty:
        return {}

    ret = panel.pivot_table(index="ts", columns="symbol", values="ret_1d", aggfunc="first")
    ret.index = pd.to_datetime(ret.index, utc=True)
    lo, hi = d["ts"].min(), d["ts"].max()
    grid = ret.loc[(ret.index >= lo) & (ret.index <= hi)].index
    # A coin with no quoted return on a date cannot be held on that date. Without this mask, the
    # forward-fill keeps a **delisted** coin in the book for the rest of the week and
    # ``costed_backtest`` fills its missing return with zero — so a delisting would read as a flat
    # week instead of a loss. Masking makes it cash instead. The residual optimism is stated in the
    # report rather than hidden: the delisting *loss itself* is still not charged, because the
    # panel's last bar is the last price Binance published and not the recovery value.
    alive = ret.loc[grid].notna()

    def _hold(anchor_rows: pd.DataFrame) -> pd.DataFrame:
        held = anchor_rows.reindex(grid).ffill().fillna(0.0)
        return held.where(alive, 0.0)

    def weights_from(rank_col: str, ascending: bool = False) -> pd.DataFrame:
        w = pd.DataFrame(0.0, index=grid, columns=ret.columns)
        for ts, part in reb.groupby("ts", sort=True):
            if ts not in w.index:
                continue
            pick = part.sort_values(rank_col, ascending=ascending).head(cfg.top_k)
            cols = [c for c in pick["symbol"] if c in w.columns]
            if cols:
                w.loc[ts, cols] = 1.0 / len(cols)
        # Hold between rebalances. ffill with the rebalance rows as the only non-zero anchors.
        return _hold(w.loc[w.abs().sum(axis=1) > 0])

    books = {}
    books["topk_forecast"] = costed_backtest(weights_from("pred_ret"), ret,
                                             cost_bps=cfg.cost_bps, name="topk_forecast")
    # Control: equal weight across every eligible name on the same rebalance dates. This is the
    # benchmark that matters for a *selection* claim — beating BTC could be beta, but beating the
    # equal-weight book of the same eligible universe can only be selection.
    eq = pd.DataFrame(0.0, index=grid, columns=ret.columns)
    for ts, part in reb.groupby("ts", sort=True):
        if ts not in eq.index:
            continue
        cols = [c for c in part["symbol"] if c in eq.columns]
        if cols:
            eq.loc[ts, cols] = 1.0 / len(cols)
    books["equal_weight_eligible"] = costed_backtest(
        _hold(eq.loc[eq.abs().sum(axis=1) > 0]), ret,
        cost_bps=cfg.cost_bps, name="equal_weight_eligible")
    if "BTCUSDT" in ret.columns:
        bh = pd.DataFrame(0.0, index=grid, columns=ret.columns)
        bh["BTCUSDT"] = 1.0
        books["btc_buy_hold"] = costed_backtest(bh.where(alive, 0.0), ret,
                                                cost_bps=cfg.cost_bps, name="btc_buy_hold")
    # The bottom-k book, as the sign check. A forecast with real information should rank badly as
    # well as well; if top-k and bottom-k score the same, the ranking is noise and the top-k number
    # is a draw from it.
    books["bottomk_forecast"] = costed_backtest(
        weights_from("pred_ret", ascending=True), ret,
        cost_bps=cfg.cost_bps, name="bottomk_forecast")

    # ---- the two books the project's own evidence predicts will work, and the return book does not
    #
    # ``growth-audit.md`` §1.5: what survives forward testing is loser-avoidance, not winner-picking
    # — an exclusion filter on volatility, age and volume improves the median 90-day outcome and
    # halves the probability of a 40% drawdown while barely changing the chance of a double. These
    # two books ask whether the model's *risk* heads reproduce that, since they are the heads the
    # project's measurements say should be forecastable at all.
    if "pred_dd" in d.columns:
        # Pure risk selection: hold the k names with the lowest forecast drawdown probability. No
        # return forecast enters this book at all, so a good result here is attributable to the
        # drawdown head alone.
        books["lowest_dd_forecast"] = costed_backtest(
            weights_from("pred_dd", ascending=True), ret,
            cost_bps=cfg.cost_bps, name="lowest_dd_forecast")
        # The integration candidate: rank on the return forecast, but only inside the safer half of
        # the cross-section by forecast drawdown. This is the shape the existing decision layer
        # already has — a selector inside a risk gate — so it is the honest test of whether the
        # forecast adds anything to the rule that is already running.
        safe = reb.copy()
        med = safe.groupby("ts", sort=False)["pred_dd"].transform("median")
        safe = safe.loc[safe["pred_dd"] <= med]
        w = pd.DataFrame(0.0, index=grid, columns=ret.columns)
        for ts, part in safe.groupby("ts", sort=True):
            if ts not in w.index:
                continue
            pick = part.sort_values("pred_ret", ascending=False).head(cfg.top_k)
            cols = [c for c in pick["symbol"] if c in w.columns]
            if cols:
                w.loc[ts, cols] = 1.0 / len(cols)
        books["topk_inside_dd_gate"] = costed_backtest(
            _hold(w.loc[w.abs().sum(axis=1) > 0]), ret,
            cost_bps=cfg.cost_bps, name="topk_inside_dd_gate")
    if "pred_vol_real" in d.columns:
        # Lowest forecast volatility. ``growth-audit.md`` measures low volatility as the single
        # feature that held its sign across all three regimes (rank IC -0.14), so this is the book
        # most likely to work and the least likely to be the model's own contribution.
        books["lowest_vol_forecast"] = costed_backtest(
            weights_from("pred_vol_real", ascending=True), ret,
            cost_bps=cfg.cost_bps, name="lowest_vol_forecast")
    return {k: v.as_dict() | {"row": v.row()} for k, v in books.items()}


# --------------------------------------------------------------------------- scoring


def score(df_pred: pd.DataFrame, cfg: TrainConfig) -> dict:
    """Every judged metric, each beside the baseline it must beat. No price-level MAPE anywhere.

    The baselines are in the same dict rather than a separate report on purpose: a return R2 of
    -0.004 read alone looks like a near miss, and read beside ``zero forecast = 0.000 by
    construction`` it is what it is.
    """
    hb = horizon_bars(cfg.horizon, cfg.timeframe)
    d = df_pred.dropna(subset=["pred_ret", "y_ret"])
    out: dict[str, Any] = {"n_oos": int(len(d)),
                           "oos_start": str(d["ts"].min()), "oos_end": str(d["ts"].max()),
                           "n_symbols_oos": int(d["symbol"].nunique())}

    # ---- return head
    da = directional_accuracy(d["pred_ret_real"], d["y_ret"], weights=d["w"])
    ic = rank_ic(d, "pred_ret", "y_ret", horizon_periods=hb)
    err = return_errors(d["pred_ret_real"], d["y_ret"])
    zero = return_errors(np.zeros(len(d)), d["y_ret"])
    # **Cross-sectional direction, and why it is the one to read.** A crypto return forecast that
    # sits near its training mean is almost always positive, so the pooled hit rate above is close
    # to the base rate of "did the coin go up" — which the market supplies for free and which the
    # first principal component (57-69% of cross-sectional variance) already explains. Demeaning
    # both sides within each timestamp removes that factor and asks the question a long-only
    # selection actually faces: of the coins available today, is this one above or below the median?
    g = d.groupby("ts", sort=False)
    dx = d.assign(pred_cs=d["pred_ret"] - g["pred_ret"].transform("median"),
                  y_cs=d["y_ret"] - g["y_ret"].transform("median"))
    da_cs = directional_accuracy(dx["pred_cs"], dx["y_cs"], weights=dx["w"])
    out["ret"] = {
        "directional": da.as_dict(),
        "directional_cross_sectional": da_cs.as_dict(),
        "rank_ic": ic.as_dict(),
        "errors": err,
        "baseline_zero_forecast": zero,
        "r2_oos_vs_zero": err["r2_oos"],
        "note": ("MAPE is on RETURNS, clipped at |actual|>=1e-4, and is reported only because it "
                 "was asked for; r2_oos against the zero forecast is the number to read, and "
                 "directional_cross_sectional is the hit rate with the market factor removed. "
                 "The pooled 'directional' number mostly measures the base rate of up-weeks."),
    }
    # ---- vol head: baseline is trailing realised vol, which is a feature the model already has
    v = d.dropna(subset=["pred_vol_real", "y_vol_log", "vol_20"])
    if len(v) > 10:
        base = np.log(np.clip(v["vol_20"].to_numpy(dtype=float), 1e-4, None))
        act = v["y_vol_log"].to_numpy(dtype=float)
        sse_m = float(((v["pred_vol_real"].to_numpy(dtype=float) - act) ** 2).sum())
        sse_b = float(((base - act) ** 2).sum())
        sse_c = float(((act.mean() - act) ** 2).sum())
        out["vol"] = {
            "n": int(len(v)),
            "r2_vs_mean_model": 1.0 - sse_m / sse_c if sse_c > 0 else float("nan"),
            "r2_vs_mean_trailing_vol": 1.0 - sse_b / sse_c if sse_c > 0 else float("nan"),
            "r2_model_vs_trailing_vol": 1.0 - sse_m / sse_b if sse_b > 0 else float("nan"),
            "rank_ic": rank_ic(v.assign(_p=v["pred_vol_real"]), "_p", "y_vol_log",
                               horizon_periods=hb).as_dict(),
            "note": ("target is log annualised forward realised vol; the baseline is trailing "
                     "20-bar realised vol, which is feature index 1 the model is already fed, "
                     "so beating it requires more than copying an input."),
        }
    # ---- drawdown head
    dd = d.dropna(subset=["pred_dd", "y_dd"])
    if len(dd) > 10:
        p = dd["pred_dd"].to_numpy(dtype=float)
        o = dd["y_dd"].to_numpy(dtype=float)
        rate = float(o.mean())
        out["dd"] = {
            "n": int(len(dd)), "base_rate": rate,
            "brier": brier(p, o), "brier_base_rate": brier(np.full(o.size, rate), o),
            "reliability": reliability(p, o).to_dict(orient="records"),
            "auc_proxy_rank_ic": rank_ic(dd.assign(_p=p), "_p", "y_dd",
                                        horizon_periods=hb).as_dict(),
        }
    return out


# --------------------------------------------------------------------------- driver


@dataclass
class ZooResult:
    """One model's whole record: predictions, metrics, books, and what it cost to produce."""

    spec: dict
    folds: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    books: dict = field(default_factory=dict)
    cost: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"spec": self.spec, "folds": self.folds, "metrics": self.metrics,
                "books": self.books, "cost": self.cost}


def run_model(spec: ModelSpec, cfg: TrainConfig, panel: pd.DataFrame, *,
              device: torch.device | None = None, kind: str = "torch",
              gbdt_lags: tuple[int, ...] = (0,), gbdt_device: str = "cuda",
              verbose: bool = True) -> tuple[ZooResult, pd.DataFrame]:
    """Walk-forward one configuration end to end. Returns the record and the OOS prediction frame.

    The leakage assertion runs here, on this model's own folds, before a single epoch. It is
    cheap and it is the difference between a table of numbers and a table of numbers that mean
    something.
    """
    pt = make_tensors(panel, cfg, spec, device=device)
    wf = walk_forward(pt.meta["ts"], pt.meta["t1"], n_splits=cfg.n_splits,
                      embargo_frac=cfg.embargo_frac, mode="expanding",
                      min_train=cfg.min_train)
    assert_no_leakage(wf, pt.meta["ts"], pt.meta["t1"])
    if len(wf) == 0:
        raise ValueError(f"no usable folds at min_train={cfg.min_train} on {pt.n_samples} samples")

    # tz-naive datetime64 on both sides. A tz-aware ``Series.to_numpy()`` yields an object array
    # of Timestamps, and comparing that to a ``np.datetime64`` raises rather than masking wrongly
    # — but only at runtime, so it is normalised here once instead of discovered per fold.
    panel_ts = pd.to_datetime(panel["ts"], utc=True).dt.tz_localize(None).to_numpy()
    res = ZooResult(spec=spec.as_dict() | {"kind": kind, "gbdt_lags": list(gbdt_lags)})
    frames = []
    for fold in wf:
        cut = np.datetime64(pd.Timestamp(fold.train_end).tz_localize(None))
        mask = torch.as_tensor(panel_ts <= cut, device=pt.device)
        if kind == "torch":
            r = train_fold(spec, cfg, pt, fold.train, fold.test, mask)
        else:
            r = gbdt_gpu_fold(cfg, pt, fold.train, fold.test, mask,
                              lags=gbdt_lags, device=gbdt_device)
        sc = r["scaler"]
        m = pt.meta.iloc[fold.test].reset_index(drop=True)
        frames.append(pd.DataFrame({
            "ts": m["ts"], "symbol": m["symbol"], "fold": fold.fold,
            "pred_ret": r["pred"]["ret"],
            "pred_ret_real": sc.unstd_ret(r["pred"]["ret"]),
            "pred_vol_real": sc.unstd_vol(r["pred"]["vol"]),
            "pred_dd": r["pred"]["dd"],
            "pred_ret_1d": r["pred"]["path0"] * sc.y_path_sd,
            "y_ret": m["y_ret"], "y_vol_log": np.log(np.clip(m["y_vol"], 1e-4, None)),
            "y_dd": m["y_dd"], "vol_20": m["vol_20"],
            "w": pt.w.detach().cpu().numpy()[fold.test],
        }))
        rec = fold.as_dict() | {k: v for k, v in r.items() if k not in ("pred", "scaler")}
        rec["scaler"] = sc.as_dict()
        res.folds.append(rec)
        if verbose:
            print(f"  fold {fold.fold}: train {r['n_train']:>7,} val {r['n_val']:>6,} "
                  f"test {r['n_test']:>6,}  {r['wall_s']:6.1f}s  "
                  f"peak {r['peak_MiB'] if isinstance(r['peak_MiB'], float) else 0:7.1f} MiB  "
                  f"epochs {r['epochs_run']}  purged {fold.n_purged:,}")

    pred = pd.concat(frames, ignore_index=True)
    res.metrics = score(pred, cfg)
    res.books = topk_book(pred, panel, cfg)
    res.cost = {
        "wall_s_total": round(sum(f["wall_s"] for f in res.folds), 2),
        "wall_s_per_fold": [round(f["wall_s"], 2) for f in res.folds],
        "peak_MiB_max": max((f["peak_MiB"] for f in res.folds
                             if isinstance(f["peak_MiB"], float)
                             and math.isfinite(f["peak_MiB"])), default=float("nan")),
        "resident_data_MiB": pt.vram_MiB(),
        "n_folds": len(res.folds),
        "n_samples": pt.n_samples,
        "n_features": pt.n_features,
        # The bracket, both ends. A parameter count above the *upper* end is over-parameterised
        # against the most generous reading of the sample size, which is the claim worth making.
        "eff_n_symbol_scope": pt.eff_n_symbol,
        "eff_n_panel_scope": pt.eff_n_panel,
    }
    if kind == "torch":
        res.cost["params"] = param_count(build_model(spec, pt.n_features))
    del pt
    if (device or pick_device()).type == "cuda":
        torch.cuda.empty_cache()
    return res, pred


def run_zoo(cfg: TrainConfig | None = None, *, specs: list[ModelSpec] | None = None,
            root: Path | None = None, frame: pd.DataFrame | None = None,
            out_dir: Path | None = None, device: torch.device | None = None,
            with_gbdt: bool = True, record_trials: bool = True,
            verbose: bool = True) -> dict:
    """Train the whole zoo, write the artefacts, and record the trial count.

    Each configuration is recorded as **one selection trial**, because a maximum was taken across
    them when the report names a best model. The VRAM probes are recorded with
    ``selection=False``: nothing was chosen from them, so they belong in the audit trail and not
    in the hurdle's N.
    """
    cfg = cfg or TrainConfig()
    seed_everything(cfg.seed)
    dev = device or pick_device()
    specs = specs if specs is not None else zoo_specs()
    out_dir = Path(out_dir) if out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "gpu": gpu_ceiling(dev),
        "config": cfg.as_dict(),
        "features": list(feature_block(cfg)),
        "seed": seed_everything(cfg.seed),
        "models": {},
    }
    t0 = time.perf_counter()
    if verbose:
        print("device:", json.dumps(report["gpu"], indent=2))
    panel, ds = build_panel(cfg, root=root, frame=frame)
    report["panel"] = ds.summary() | {
        "rows_with_features": int(len(panel)),
        "build_s": round(time.perf_counter() - t0, 1),
    }
    if verbose:
        print("panel:", json.dumps(report["panel"], indent=2, default=str))

    trials = Trials.load() if record_trials else None
    jobs: list[tuple[ModelSpec, str, tuple[int, ...]]] = [(s, "torch", (0,)) for s in specs]
    if with_gbdt:
        base = specs[0] if specs else ModelSpec("x", "linear", seq_len=64)
        jobs += [
            (ModelSpec("xgb_bar", "linear", seq_len=base.seq_len,
                       tag="XGBoost, decision bar only"), "gbdt", (0,)),
            (ModelSpec("xgb_lags", "linear", seq_len=base.seq_len,
                       tag="XGBoost, 4 lags flattened"), "gbdt", (0, 1, 5, 20)),
        ]

    for spec, kind, lags in jobs:
        if verbose:
            print(f"\n=== {spec.name} ({kind}) ===")
        try:
            res, pred = run_model(spec, cfg, panel, device=dev, kind=kind, gbdt_lags=lags,
                                  verbose=verbose)
        except Exception as exc:                       # a family that cannot run is a result
            report["models"][spec.name] = {"error": f"{type(exc).__name__}: {exc}"}
            if verbose:
                print(f"  FAILED: {type(exc).__name__}: {exc}")
            continue
        report["models"][spec.name] = res.as_dict()
        if out_dir:
            pred.to_parquet(out_dir / f"pred_{spec.name}.parquet", index=False)
        if trials is not None:
            trials.add(
                f"ml4 GPU zoo: {spec.name} ({kind}, L={spec.seq_len}, h={spec.hidden}, "
                f"{spec.layers}L) -> {cfg.horizon} ret/vol/dd, purged WF {cfg.n_splits} splits",
                selection=True,
                hypothesis="a sequence model beats persistence and buy-and-hold BTC after 15bps",
                metrics={
                    "dir_acc": res.metrics.get("ret", {}).get("directional", {}).get("accuracy"),
                    "ic": res.metrics.get("ret", {}).get("rank_ic", {}).get("ic"),
                    "ic_t": res.metrics.get("ret", {}).get("rank_ic", {}).get("tstat"),
                    "r2_ret": res.metrics.get("ret", {}).get("r2_oos_vs_zero"),
                    "r2_vol_vs_trailing": res.metrics.get("vol", {}).get(
                        "r2_model_vs_trailing_vol"),
                    "sharpe_topk": res.books.get("topk_forecast", {}).get("sharpe"),
                    "wall_s": res.cost.get("wall_s_total"),
                })
        if verbose:
            _print_row(spec.name, report["models"][spec.name])

    report["wall_s_total"] = round(time.perf_counter() - t0, 1)
    if trials is not None:
        report["trials"] = trials.hurdle(
            baseline_sharpe=report["models"].get("linear", {}).get("books", {})
            .get("btc_buy_hold", {}).get("sharpe", 0.83),
            years=_oos_years(report))
    if out_dir:
        (out_dir / "zoo_report.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8")
        (out_dir / "zoo_table.md").write_text(zoo_table(report), encoding="utf-8")
    if verbose:
        print("\n" + zoo_table(report))
    return report


def _oos_years(report: dict) -> float:
    for m in report["models"].values():
        met = m.get("metrics", {})
        if met.get("oos_start") and met.get("oos_end"):
            a = pd.Timestamp(met["oos_start"])
            b = pd.Timestamp(met["oos_end"])
            return max((b - a).days / 365.25, 0.1)
    return 1.0


def _print_row(name: str, rec: dict) -> None:
    m = rec.get("metrics", {})
    r = m.get("ret", {})
    print(f"  {name}: dir_acc {r.get('directional', {}).get('accuracy')} "
          f"IC {r.get('rank_ic', {}).get('ic')} (t {r.get('rank_ic', {}).get('tstat')}) "
          f"r2_ret {r.get('r2_oos_vs_zero')} "
          f"vol_r2_vs_trailing {m.get('vol', {}).get('r2_model_vs_trailing_vol')} "
          f"Sharpe {rec.get('books', {}).get('topk_forecast', {}).get('sharpe')}")


def zoo_table(report: dict) -> str:
    """The comparison table, with the baselines as rows rather than as prose.

    Every column is a judged metric or a cost. ``MAPE`` is absent by design; ``r2_ret`` is the
    return column, and its baseline — the zero forecast — is 0.000 by construction, which is the
    number every row here should be read against.
    """
    rows = []
    for name, rec in report.get("models", {}).items():
        if "error" in rec:
            rows.append({"model": name, "error": rec["error"][:60]})
            continue
        m, c, b = rec["metrics"], rec["cost"], rec["books"]
        ret, vol, dd = m.get("ret", {}), m.get("vol", {}), m.get("dd", {})
        ic = ret.get("rank_ic", {})
        da = ret.get("directional_cross_sectional", ret.get("directional", {}))
        tk = b.get("topk_forecast", {})
        rows.append({
            "model": name,
            "params": c.get("params", ""),
            "dir_acc_xs": round(da.get("accuracy", float("nan")), 4),
            "ci_low": round(da.get("ci_low", float("nan")), 4),
            "beats_coin": da.get("beats_coinflip"),
            "rank_IC": round(ic.get("ic", float("nan")), 4),
            "IC_t_NW": round(ic.get("tstat", float("nan")), 2),
            "r2_ret": round(ret.get("r2_oos_vs_zero", float("nan")), 5),
            "mape_ret%": round(ret.get("errors", {}).get("mape_ret", float("nan")), 1),
            # vol_r2_vs_mean is the primary volatility column and vs_trail is the secondary one.
            # ml.baselines measures trailing realised vol at r2 -0.158 against the mean on BTC —
            # it is *worse than the unconditional mean* — so beating it is not an achievement, and
            # a table that led with that column would read far better than the model is.
            "vol_r2_vs_mean": round(vol.get("r2_vs_mean_model", float("nan")), 4),
            "vol_r2_vs_trail": round(vol.get("r2_model_vs_trailing_vol", float("nan")), 4),
            # ``ml.metrics.brier`` returns a **dict**, not a float: the raw score, the base-rate
            # score, and the skill between them. Unpacked here rather than rounded whole, and
            # ``dd_skill`` is the only one of the three worth reading — a 16.6% base rate scores
            # Brier 0.138 for free, so the raw column flatters every row equally.
            "dd_brier": round(dd.get("brier", {}).get("brier", float("nan")), 5),
            "dd_brier_base": round(dd.get("brier", {}).get("brier_base", float("nan")), 5),
            "dd_skill": round(dd.get("brier", {}).get("skill", float("nan")), 4),
            "Sharpe": round(tk.get("sharpe", float("nan")), 3),
            "CAGR%": round(tk.get("cagr", float("nan")) * 100, 1),
            "MaxDD%": round(tk.get("max_dd", float("nan")) * 100, 1),
            "wall_s": c.get("wall_s_total"),
            "peak_MiB": round(c.get("peak_MiB_max", float("nan")), 1),
        })
    tbl = pd.DataFrame(rows)
    parts = [tbl.to_markdown(index=False) if not tbl.empty else "(no models ran)"]

    # Every book for every model, in one table. Two of these books are *benchmarks* and identical
    # across models (``equal_weight_eligible``, ``btc_buy_hold``); the rest are the model's own
    # construction. They are printed together rather than split, because the comparison that
    # matters is a model's book against the benchmark on the same span at the same cost, and a
    # reader should not have to join two tables to make it.
    brows = []
    for name, rec in report.get("models", {}).items():
        for bname, b in rec.get("books", {}).items():
            brows.append({"model": name, "book": bname} | b["row"])
    if brows:
        span = next((r["metrics"] for r in report["models"].values() if r.get("metrics")), {})
        parts += ["", f"Costed books, {span.get('oos_start', '?')[:10]} -> "
                  f"{span.get('oos_end', '?')[:10]}, {report['config']['cost_bps']} bps/side, "
                  "long-only spot, weekly rebalance "
                  f"(top_k={report['config']['top_k']}):",
                  pd.DataFrame(brows).to_markdown(index=False)]
    if "trials" in report:
        parts += ["", f"Trial counter / deflated hurdle: {json.dumps(report['trials'])}"]
    return "\n".join(parts)


# --------------------------------------------------------------------------- ceiling probe


def ceiling_report(*, n_features: int = len(ALL_FEATURES),
                   device: torch.device | None = None) -> dict:
    """Measure the real VRAM ceiling per family, and where it would actually bind.

    Recorded as a **screening** measurement: no model is selected from it, so it must not raise
    the deflated hurdle. What it produces is the batch-size and sequence-length edge, which is
    the number the owner asked for when they asked what 6 GB forces.
    """
    dev = device or pick_device()
    out: dict[str, Any] = {"gpu": gpu_ceiling(dev), "n_features": n_features, "probes": {},
                           "ceiling": {}}
    for spec in zoo_specs():
        try:
            out["probes"][spec.name] = vram_probe(spec, n_features, device=dev)
        except Exception as exc:
            out["probes"][spec.name] = {"error": f"{type(exc).__name__}: {exc}"}
    # Where it binds: walk batch size and then sequence length up on the two heaviest families.
    #
    # **The ranges stop at 16,384 and 512 deliberately, and the stopping point is a measurement in
    # its own right.** An earlier version walked to 262,144 and 2,048; it did not OOM, it ground
    # for twenty minutes at 100% utilisation and 5,747 MiB resident, because a PatchTST forward at
    # batch 65,536 with 20 channel-independent series is 14.4 million tokens of attention. The
    # ceiling on this card is therefore reached by *time* long before it is reached by *memory*,
    # which is the honest answer to "what does 6 GB force" and is not what the question expected.
    for name in ("patchtst", "lstm"):
        spec = next(s for s in zoo_specs() if s.name == name)
        out["ceiling"][f"{name}_batch"] = largest_that_fits(
            spec, n_features, axis="batch_size", values=(1024, 4096, 16384), device=dev)
        out["ceiling"][f"{name}_seq"] = largest_that_fits(
            spec.sized(batch_size=1024), n_features, axis="seq_len",
            values=(96, 256, 512), device=dev)
    # The implied ceiling, from the measured peak rather than from a walk that would take an hour:
    # activations scale linearly in batch size, so free VRAM divided by peak-per-sample is the
    # batch a training step could reach. Reported as arithmetic, and labelled as arithmetic.
    for key, probe in list(out["ceiling"].items()):
        best = probe.get("largest_that_fit") or {}
        peak, bs = best.get("peak_alloc_MiB"), best.get("batch")
        if peak and bs and out["gpu"].get("free_MiB"):
            out["ceiling"][key]["implied_max_batch_linear"] = int(
                out["gpu"]["free_MiB"] / (peak / bs) * 0.85)
            out["ceiling"][key]["implied_note"] = (
                "linear extrapolation from the largest measured peak, with a 15% allocator "
                "margin; not measured, and time-bound before it is memory-bound")
    return out


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=None, help="artefact directory")
    ap.add_argument("--quick", action="store_true",
                    help="small symbol subset and 6 epochs: a smoke test, not a result")
    ap.add_argument("--ceiling", action="store_true", help="VRAM ceiling probe only")
    ap.add_argument("--models", default="", help="comma-separated subset of the zoo")
    ap.add_argument("--no-gbdt", action="store_true")
    ap.add_argument("--no-trials", action="store_true",
                    help="skip the trial counter (tests only; a real run must count)")
    ap.add_argument("--cpu", action="store_true", help="force CPU, for the timing comparison")
    ap.add_argument("--seq-len", type=int, default=64)
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--funding", action="store_true",
                    help="add perpetual funding as a feature; drops every sample with no perp")
    a = ap.parse_args(argv)

    dev = torch.device("cpu") if a.cpu else pick_device()
    if a.ceiling:
        rep = ceiling_report(device=dev)
        print(json.dumps(rep, indent=2, default=str))
        if a.out:
            Path(a.out).mkdir(parents=True, exist_ok=True)
            (Path(a.out) / "ceiling.json").write_text(json.dumps(rep, indent=2, default=str),
                                                      encoding="utf-8")
        if not a.no_trials:
            Trials.load().add("ml4 screening: VRAM ceiling probe, batch/seq walk per family",
                              selection=False,
                              hypothesis="6 GB binds on batch size or sequence length")
        return 0

    specs = zoo_specs(seq_len=a.seq_len)
    if a.models:
        want = {s.strip() for s in a.models.split(",") if s.strip()}
        specs = [s for s in specs if s.name in want]
    cfg = TrainConfig(n_splits=a.splits, with_funding=a.funding)
    if a.quick:
        cfg = TrainConfig(n_splits=3, min_train=2_000, with_funding=a.funding,
                          symbols=("BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
                                   "SOLUSDT", "DOGEUSDT", "LTCUSDT", "LINKUSDT", "AVAXUSDT",
                                   "MATICUSDT", "DOTUSDT", "ATOMUSDT", "ETCUSDT", "XLMUSDT"))
        specs = [s.sized(max_epochs=6, patience=3) for s in specs]
    run_zoo(cfg, specs=specs, out_dir=a.out, device=dev, with_gbdt=not a.no_gbdt,
            record_trials=not a.no_trials)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
