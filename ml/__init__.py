"""``ml`` — the forecasting harness: point-in-time data, labels, splits, metrics, baselines.

**This package forecasts nothing.** It is the scaffolding that makes a later forecast
honest, and the baselines that a later forecast has to beat. Every model phase imports from
here; nothing here imports from a model phase.

Why it exists, in one measurement
---------------------------------
The owner asked for "MAPE near 1" on a crypto forecast. On **price levels** that target is
met by predicting that the next price equals the last one — a model that forecasts nothing.
:mod:`ml.baselines` measures exactly that and prints it first, so the number is on the
record before any model is fitted (run ``python -m ml.baselines``). The corrected target is
in :mod:`ml.metrics`: directional accuracy against 50% with a binomial CI, rank IC with
Newey-West t-stats, Brier score and reliability, and the **Sharpe of a costed strategy** at
15 bps per side against both persistence and buy-and-hold BTC.

The five things that make a number here trustworthy
---------------------------------------------------
1. **Point-in-time universe** (:mod:`ml.data`) — a coin is invisible until it was actually
   listed and liquid, eligibility is recomputed at every timestamp from data that existed
   then, and **dead coins are retained**. A survivor-only panel proves the opposite of what
   it appears to.
2. **Discontinuity splitting** (:mod:`ml.data`) — ``LUNAUSDT``'s symbol was reused for
   LUNA 2.0 on 2022-05-31 and produces a **177,399x** one-day return. Untreated it
   fabricates results. Series are split into segments at such breaks and each segment is a
   separate instrument.
3. **No future information in a feature** (:mod:`ml.features`) — asserted, not asserted-in-a-
   comment: :func:`ml.features.assert_no_lookahead` recomputes every feature on a prefix of
   the panel and requires the values to be identical.
4. **Purged, embargoed walk-forward only** (:mod:`ml.splits`) — with
   :func:`ml.splits.assert_no_leakage`, which **fails the run** if any training row's label
   window overlaps the test window.
5. **Uniqueness weighting** (:mod:`ml.labels`) — overlapping forward windows make a row
   count a lie; the effective sample size is the sum of average uniqueness, and it is
   roughly an order of magnitude below the row count.

Determinism and speed
---------------------
Everything is a pure function of (inputs, config, seed). :mod:`ml.registry` hashes the
config into a cache key and every expensive step is cached to parquet, because the later
phases call this hundreds of times.

Dependencies: numpy, pandas, pyarrow. Nothing here needs a GPU or scikit-learn, and that is
deliberate — the harness must run even when the model stack does not.
"""

from __future__ import annotations

__all__ = ["HARNESS_VERSION"]

#: Bumped whenever a change would alter a cached artefact. Part of every cache key, so a
#: stale cache from an older harness can never be read back as if it were current.
HARNESS_VERSION = "1"
