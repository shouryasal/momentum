"""Determinism, caching, and the trial counter that makes the deflated hurdle mean something.

Three jobs, all of them about making a later number auditable rather than about producing one.

**1. A cache key that cannot lie.** :func:`config_hash` hashes a config dict together with
:data:`ml.HARNESS_VERSION`, so a cached artefact built by an older harness can never be read
back as if it were current. :func:`cached` wraps any DataFrame-producing function in a parquet
cache under ``$EARN_ML_CACHE``. The later phases call the dataset builder hundreds of times;
without this they call it hundreds of times.

**2. A seed that reaches everything.** :func:`seed_everything` seeds ``random``, numpy, and —
if they are importable — torch's CPU and CUDA generators. It returns what it actually seeded,
so a run that claims determinism can be checked rather than believed.

**3. The trial counter.** A system that searches over model configurations **is** a search
process, and the expected best Sharpe from N zero-skill trials over 9.1 years is 0.86 at N=10,
1.05 at N=50 and **1.19 at N=200**. So a model that reports Sharpe 1.10 after trying 200
configurations has reported noise. :func:`Trials` accumulates the count across runs in a JSON
file and every total in it only ever grows — a counter that resets when somebody forgets is a
hurdle that only ever falls.

It deliberately *shares the file shape* with ``knowledge/state/trial_counter.json`` used by
``runs/discovery.py`` and the ``edge-audit`` skill, and writes to its own
``ml_trial_counter.json`` by default so an ML sweep cannot silently inflate the trading loop's
hurdle. :func:`Trials.hurdle` gives the number a candidate must clear.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ml import HARNESS_VERSION
from ml.data import PanelSpec, build_dataset, cache_root
from ml.features import add_features
from ml.labels import make_labels
from ml.metrics import deflated_sharpe_hurdle

__all__ = [
    "StudySpec",
    "Trials",
    "cache_path",
    "cached",
    "config_hash",
    "device_report",
    "seed_everything",
    "study_frame",
]


# --------------------------------------------------------------------------- hashing


def _canon(obj: Any) -> Any:
    """Canonicalise for hashing: sorted dicts, lists for tuples, repr for the rest.

    A hash that depends on dict insertion order produces cache misses that look like data
    changes, so ordering is removed rather than trusted.
    """
    if isinstance(obj, Mapping):
        return {str(k): _canon(obj[k]) for k in sorted(obj, key=str)}
    if isinstance(obj, (list, tuple)):
        return [_canon(v) for v in obj]
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    if isinstance(obj, float):
        # Round so 0.1+0.2 and 0.30000000000000004 are the same config.
        return round(obj, 12)
    if isinstance(obj, (pd.Timestamp, datetime)):
        return obj.isoformat()
    if hasattr(obj, "as_dict"):
        return _canon(obj.as_dict())
    return repr(obj)


def config_hash(config: Any, *, length: int = 16) -> str:
    """A stable short hash of any config, including :data:`ml.HARNESS_VERSION`.

    Including the harness version is the point: bump it whenever a change would alter a cached
    artefact and every stale cache entry becomes unreachable instead of subtly wrong.
    """
    payload = json.dumps({"v": HARNESS_VERSION, "c": _canon(config)},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def cache_path(kind: str, config: Any, *, suffix: str = ".parquet") -> Path:
    """``<cache_root>/<kind>/<hash><suffix>``. Directory created."""
    d = cache_root() / kind
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{config_hash(config)}{suffix}"


def cached(kind: str, config: Any, build: Callable[[], pd.DataFrame], *,
           refresh: bool = False) -> pd.DataFrame:
    """Read ``kind``/``hash`` from the parquet cache, or build it and write it.

    ``refresh=True`` rebuilds and overwrites, which is what a caller passes after changing a
    builder without bumping :data:`ml.HARNESS_VERSION`. A corrupt or unreadable cache file is
    rebuilt rather than raised on: a half-written parquet from an interrupted run must not wedge
    every subsequent run.

    Set ``$EARN_ML_NO_CACHE=1`` to bypass entirely — which is what the test suite does, so a
    test can never pass because of a stale artefact from a previous test.
    """
    if os.environ.get("EARN_ML_NO_CACHE"):
        return build()
    p = cache_path(kind, config)
    if p.exists() and not refresh:
        try:
            return pd.read_parquet(p)
        except Exception:
            p.unlink(missing_ok=True)
    df = build()
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        df.to_parquet(tmp, index=False)
        tmp.replace(p)
    except Exception:
        tmp.unlink(missing_ok=True)
    return df


# --------------------------------------------------------------------------- the pipeline


@dataclass(frozen=True)
class StudySpec:
    """One study's whole configuration: panel, features, labels, sampling. Hashed into a cache key.

    This is what a sweep varies. Everything downstream of it is a pure function, so two runs
    with the same ``StudySpec`` produce the same frame, and the cache is therefore safe rather
    than merely fast.
    """

    panel: PanelSpec = field(default_factory=PanelSpec)
    features: tuple[str, ...] = ("vol_60", "mom_30", "mom_90", "mom_365", "adv_90",
                                 "vol_trend_30_180", "dist_from_high_90", "above_ma_200")
    horizons: tuple[str, ...] = ("1d", "7d")
    dd_thresholds: tuple[float, ...] = (-0.20, -0.40)
    #: ``None`` keeps every bar. ``0`` = Mondays only, which is the sampling the project's own
    #: cross-sectional studies use to cut label overlap from 12x to 1x at a 7-day horizon.
    weekday: int | None = None
    #: Keep only rows the point-in-time funnel passed. Turning this off is survivorship bias
    #: with a friendly name, so it defaults on.
    eligible_only: bool = True

    def as_dict(self) -> dict:
        return {"panel": self.panel.as_dict(), "features": list(self.features),
                "horizons": list(self.horizons), "dd_thresholds": list(self.dd_thresholds),
                "weekday": self.weekday, "eligible_only": self.eligible_only}


def study_frame(spec: StudySpec | None = None, *, root: Path | None = None,
                frame: pd.DataFrame | None = None, refresh: bool = False) -> pd.DataFrame:
    """dataset -> features -> labels -> join -> filter, cached on the whole :class:`StudySpec`.

    The one call a later phase should make. On the real panel the uncached path is about 25
    seconds — 2.5s to build, 8s for features, 13s for labels — which at 300 configurations
    would be two hours of recomputing identical columns. Cached it is a parquet read.

    Every ``t1_<target>`` column survives the join, because :func:`ml.splits.walk_forward`
    needs them and a split built without them leaks.

    ``frame`` injects a panel (tests, and a planted-signal sanity check) and bypasses the cache,
    since a synthetic panel is not identified by the spec.
    """
    def build() -> pd.DataFrame:
        s = spec or StudySpec()
        ds = build_dataset(s.panel, root=root, frame=frame)
        feat = add_features(ds.frame, names=list(s.features),
                            bars_per_year=s.panel.bars_per_year)
        lab = make_labels(ds.frame, timeframe=s.panel.timeframe, horizons=s.horizons,
                          dd_thresholds=s.dd_thresholds,
                          bars_per_year=s.panel.bars_per_year)
        out = feat.merge(lab.frame, on=["ts", "symbol"], how="left")
        if s.eligible_only:
            out = out.loc[out["eligible"]]
        if s.weekday is not None:
            out = out.loc[out["ts"].dt.dayofweek == s.weekday]
        return out.reset_index(drop=True)

    if frame is not None:
        return build()
    return cached("study", (spec or StudySpec()), build, refresh=refresh)


# --------------------------------------------------------------------------- determinism


def seed_everything(seed: int = 0) -> dict:
    """Seed ``random``, numpy and (when present) torch. Returns what was actually seeded.

    Returning the list rather than nothing is deliberate: "everything is deterministic given a
    seed" is a claim, and this is the evidence for it. ``cudnn_deterministic`` is set as well,
    because a convolution on this GPU is otherwise free to pick a different algorithm per run
    and produce a different third decimal place.
    """
    done = {"seed": int(seed), "random": True, "numpy": True, "torch": False, "cuda": False}
    random.seed(seed)
    np.random.seed(seed)
    os.environ.setdefault("PYTHONHASHSEED", str(seed))
    try:
        import torch
        torch.manual_seed(seed)
        done["torch"] = True
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False
            done["cuda"] = True
    except Exception:
        pass
    return done


def device_report() -> dict:
    """What compute is actually available, and the VRAM ceiling a model must fit inside.

    The binding constraint on this host is **6 GB of VRAM** on a Quadro RTX 3000, against 12
    logical CPUs and 64 GB of RAM. So the honest split is: gradient-boosted trees and every
    linear model on CPU where 64 GB is the resource, sequence models on GPU in batches sized to
    6 GB, and nothing that assumes a 24 GB card.

    Reports ``torch: false`` rather than raising when torch is absent — the harness and the
    baselines are numpy-only by design and must run on a host with no CUDA at all.
    """
    rep: dict = {"torch": False, "cuda": False, "devices": [], "cpu_count": os.cpu_count()}
    try:
        import torch
        rep["torch"] = True
        rep["torch_version"] = torch.__version__
        rep["cuda_build"] = torch.version.cuda
        rep["cuda"] = bool(torch.cuda.is_available())
        for i in range(torch.cuda.device_count() if rep["cuda"] else 0):
            p = torch.cuda.get_device_properties(i)
            free, total = torch.cuda.mem_get_info(i)
            rep["devices"].append({
                "index": i, "name": p.name,
                "total_MiB": int(p.total_memory // 1024 ** 2),
                "free_MiB": int(free // 1024 ** 2),
                "capability": f"{p.major}.{p.minor}",
                "multiprocessors": p.multi_processor_count,
            })
    except Exception as exc:
        rep["error"] = f"{type(exc).__name__}: {exc}"
    return rep


# --------------------------------------------------------------------------- trial counter


@dataclass
class Trials:
    """A persistent, monotonic count of model configurations tried, and the hurdle it implies.

    Two totals, because they answer different questions:

    * ``n_trials`` — every measurement ever made. The audit trail. Never a hurdle on its own.
    * ``n_selection_trials`` — trials that could have produced a *chosen* model. **This is the
      hurdle's N**, because the hurdle is the expected maximum over the trials a selection was
      actually taken across.

    A screening measurement — a sweep run to see whether a family is worth pursuing, from which
    nothing was selected — is recorded with ``selection=False``. It lands in ``n_trials``
    forever and does not raise the hurdle, because no maximum was taken over it.

    File shape is shared with ``knowledge/state/trial_counter.json`` (``runs/discovery.py``,
    the ``edge-audit`` skill) on purpose, and the default path is a **separate** file so an ML
    sweep of 400 configurations cannot silently move the trading loop's hurdle.
    """

    path: Path
    state: dict

    @classmethod
    def load(cls, path: Path | None = None) -> Trials:
        p = Path(path) if path else (cache_root() / "ml_trial_counter.json")
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            st = {}
        st.setdefault("n_trials", 0)
        st.setdefault("n_selection_trials", st["n_trials"])
        st.setdefault("history", [])
        st["n_trials"] = max(int(st["n_trials"]), 0)
        st["n_selection_trials"] = max(int(st["n_selection_trials"]), 0)
        return cls(path=p, state=st)

    def add(self, what: str, *, selection: bool = True,
            hypothesis: str = "", metrics: dict | None = None) -> Trials:
        """Record one trial. Both totals only ever grow; ``what`` is written to the history.

        ``what`` should name the configuration, not the outcome: "lgbm depth 6, 500 trees, 7d
        return" and not "tried a model". The history is what an auditor reads to decide whether
        N is honest, and a history of 400 rows that all say "tried a model" is not evidence.
        """
        self.state["n_trials"] += 1
        if selection:
            self.state["n_selection_trials"] += 1
        self.state["history"].append({
            "utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "what": what, "selection": bool(selection), "hypothesis": hypothesis,
            "metrics": metrics or {}, "harness": HARNESS_VERSION,
        })
        self.save()
        return self

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        return self.path

    @property
    def n(self) -> int:
        """The hurdle's N: selection trials, not the all-time measurement count."""
        return int(self.state["n_selection_trials"])

    def hurdle(self, baseline_sharpe: float, years: float) -> dict:
        """``baseline + expected_max_sharpe(N, years)`` — what a candidate must actually clear.

        At N=200 over 9.1 years the deflation alone is **1.19**. A candidate reporting Sharpe
        1.25 against a 0.80 baseline therefore clears by 0.06, which is thin and should be
        reported as thin rather than as a win.
        """
        return deflated_sharpe_hurdle(baseline_sharpe, self.n, years) | {
            "n_measurements_all_time": int(self.state["n_trials"]),
            "note": ("N is the count of SELECTION trials, which only ever grows. A hurdle that "
                     "resets is decorative."),
        }
