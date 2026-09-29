"""Synthetic panels for the ML harness tests — deliberately never the real 27 MB parquet.

Two reasons this is synthetic and not a slice of production data.

**The real panel is not in the test tree.** ``earn-test`` excludes ``/data/`` from its rsync
and the wide panel lives outside the repo entirely, so a test that reads it fails on every
host but one.

**A synthetic panel is the only way to test a detector.** The point of
:func:`ml.features.assert_no_lookahead` and :func:`ml.splits.assert_no_leakage` is that they
catch a bug. A test on real data can show they do not fire; only a fixture with a *planted*
lookahead can show they do. So :func:`leaky_frame` plants one, and the tests require the
assertion to raise.

The panels are generated from a fixed seed, so a failure is reproducible.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def _series(n: int, *, seed: int, start: str = "2019-01-01", s0: float = 100.0,
            mu: float = 0.0003, sigma: float = 0.03, freq: str = "1D") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    r = rng.normal(mu, sigma, n)
    close = s0 * np.exp(np.cumsum(r))
    high = close * (1.0 + np.abs(rng.normal(0, 0.01, n)))
    low = close * (1.0 - np.abs(rng.normal(0, 0.01, n)))
    open_ = np.concatenate([[s0], close[:-1]])
    qv = np.abs(rng.lognormal(16.0, 1.0, n))
    return pd.DataFrame({
        "ts": pd.date_range(start, periods=n, freq=freq, tz="UTC"),
        "open": open_, "high": np.maximum(high, close), "low": np.minimum(low, close),
        "close": close, "volume": qv / close, "quote_volume": qv,
        "trades": rng.integers(100, 10_000, n).astype(float),
    })


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Every test gets its own cache and data root, and caching is off by default.

    A test that passes because a previous test wrote a parquet is not a test, so
    ``EARN_ML_NO_CACHE`` is set for the whole suite and the cache path is redirected anyway
    for the tests that exercise the cache on purpose.
    """
    monkeypatch.setenv("EARN_ML_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("EARN_ML_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("EARN_ML_NO_CACHE", "1")


@pytest.fixture
def panel() -> pd.DataFrame:
    """Five coins, 900 daily bars, staggered listing dates, one coin that dies.

    * ``AAAUSDT`` — full history, liquid.
    * ``BBBUSDT`` — full history, liquid.
    * ``CCCUSDT`` — lists 300 bars late, so eligibility must not see it before that.
    * ``DDDUSDT`` — **dies** 200 bars before the end. A survivor-only panel would delete it,
      which is the bias the harness exists to avoid.
    * ``PEGUSDT`` — a stablecoin at ~0.1% annualised vol, which the vol floor must remove.
    """
    parts = []
    for i, sym in enumerate(("AAAUSDT", "BBBUSDT")):
        p = _series(900, seed=10 + i)
        p["symbol"] = sym
        parts.append(p)
    c = _series(600, seed=30, start="2019-10-28")
    c["symbol"] = "CCCUSDT"
    parts.append(c)
    d = _series(700, seed=40)
    d["symbol"] = "DDDUSDT"
    parts.append(d)
    peg = _series(900, seed=50, s0=1.0, mu=0.0, sigma=0.00005)
    peg["symbol"] = "PEGUSDT"
    parts.append(peg)
    return pd.concat(parts, ignore_index=True)


@pytest.fixture
def luna_frame() -> pd.DataFrame:
    """One symbol whose price collapses to 5e-5 and is then reused at 8.87 — the LUNA shape.

    Reproduces the exact discontinuity in the real panel: ``LUNAUSDT`` 2022-05-31,
    ``prev_close`` 0.00005, ``close`` 8.87, a **177,400x** one-day "return". Untreated it
    fabricated a +724% growth episode and a 112,518x equity curve in an earlier run.
    """
    a = _series(400, seed=7, s0=80.0, mu=-0.02, sigma=0.05)
    # Geometric, not linear: a linear ramp to 5e-5 makes its own final step a 4,000x *down*
    # discontinuity, so the fixture would plant two breaks and the test would measure the wrong
    # one. Geometric decay keeps every bar-to-bar ratio constant at ~0.966 and leaves exactly
    # one break in the frame — the symbol reuse.
    a["close"] = np.geomspace(80.0, 5e-5, 400)
    a["high"] = a["close"] * 1.01
    a["low"] = a["close"] * 0.99
    a["open"] = a["close"]
    b = _series(400, seed=8, start="2020-02-04", s0=8.87)
    b["ts"] = pd.date_range(a["ts"].iloc[-1] + pd.Timedelta(days=1), periods=400,
                            freq="1D", tz="UTC")
    out = pd.concat([a, b], ignore_index=True)
    out["symbol"] = "LUNAUSDT"
    return out


@pytest.fixture
def leaky_frame(panel) -> pd.DataFrame:
    """``panel`` plus a column that is tomorrow's close. The planted bug the detector must find."""
    d = panel.sort_values(["symbol", "ts"], kind="stable").copy()
    d["cheat"] = d.groupby("symbol", sort=False)["close"].shift(-1)
    return d.reset_index(drop=True)
