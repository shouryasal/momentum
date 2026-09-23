# Where to look

The surfaces Earn can actually read, in the order to try them. `scout.py surfaces` prints
the same list with the machine-readable fields; this page says why each one is where it is.

## 1. The local candle store — start here, always

`data/binance/<PAIR>-<tf>.feather`, 1h / 4h / 1d, from 2017-08 for BTC, with about 1.06M
candles in the runtime copy. It is free, deep, already point-in-time, and it costs nothing
to re-read. Most questions worth asking can be asked here first, and an idea that cannot
even be *framed* against price history usually cannot be backtested at all.

Two facts about this surface decide a lot of arguments before they start:

- **Deciding at 4h instead of 1d buys nothing and costs double.** The identical rule scores
  Sharpe 1.14 at 1d with 12 turns/yr and 1.2%/yr fee drag, against Sharpe 1.13 at 4h with
  26 turns/yr, **2.6%/yr** fee drag and a *worse* drawdown (−39.9% vs −34.0%). A proposal
  that needs a faster cadence to work is paying 22% of the annual fee budget for a
  rounding error.
- **The shipped strategy has no profit taking at all** and returns +75.4% over 2021-2026
  against buy-and-hold BTC at +194%. That gap is the standing question, and it is a
  question about exits and sizing, not about finding another entry signal.

## 2. The wired feature modules

`runs/features/` (tier 2, human-written) already loads and traps-handles the derivative
surface. The registry in `runs/features/__init__.py` declares, per key: source URL, whether
a key is needed, rate limit, cadence, `max_lag_min` and the date it was verified. **A key
that is not in `REGISTRY` has no audited provenance**, so a skill that emits it is emitting
a number nobody can trace.

What is already there and already measured — do not re-derive these, cite them:

| Module | Holds | The measured result |
|---|---|---|
| `deribit.py` | DVOL, option chain, 25Δ skew | DVOL forecasts forward 7d realised vol (R² 0.268, NW-t +10.86) and **subsumes** trailing realised vol (joint: DVOL t +7.95, trailing t −0.33) |
| `volatility.py` | realised variance, HAR(d,w,m), `sigma_hat` | Raw DVOL overstates vol by 13% at DVOL 35 and 29% at DVOL 90 — the **fitted** mapping `6.28 + 0.707×DVOL` is what ships |
| `derivatives.py` | funding, OI, basis, positioning | Funding predicts **drawdown, not direction**: P(7d dd < −8%) rises 17.0% → 41.4% across buckets while median return stays positive |
| `binance_archive.py` | the bulk `data.binance.vision` downloader | The only deep history for metrics (2020-09+) and bookDepth (2023-01+) |
| `sampling.py` | triple-barrier labels, uniqueness, purged CV, hurdles | 19,879 labelled 4h rows carry **3,020** independent observations |
| `macro_calendar.py` | FOMC / CPI windows | Macro events are **1.81× volatility with no direction**; the elevated window runs T−7h to T+8h |
| `venue.py` | exchangeInfo, peg, announcements | The peg detector needs **persistence across closes**: the worst single printed low is 0.7600 on a day that closed at 0.9995 |

## 3. The news archive

`knowledge/earn.db`, whitelisted sources, classified, and governed by the two-source
corroboration rule. It is the **only** text source this system may cite, and every claim
must carry a real `news_hash` — host verification drops an invented one and counts it as a
hallucination. It is not a price history and cannot carry a backtest by itself.

## 4. The journal

`journal/journal.db` and `reports/`. This is the right surface for questions about **the
system** rather than the market: which root causes recur, which proposals were held and
why, how often the model's claimed number differed from the recomputed one, whether a
lesson decayed. These questions have the useful property that the sample is ours and the
labels are honest.

## 5. New free endpoints — last, and only after the gate

Everything in `docs/design/crypto-research.md` §1 was verified with a live request from this
host. Before building on any endpoint, three verified traps:

1. **Every `fapi/…/futures/data/*` endpoint retains about 30 days.** `openInterestHist` with
   `limit=500` returns exactly **31** daily rows; with a `startTime` a year back it returns
   HTTP 400. A backtest on one of these silently has a one-month sample.
2. **`fapi/v1/fundingRate` is the exception** — it serves full history back to 2019-09-10
   (7,711 prints). The monthly bulk funding archive does *not*: 2019-09 and 2019-10 are 404.
3. **Deep history for the rest exists only in the free `data.binance.vision` bulk archive.**

## Loader traps that silently corrupt a backtest

All four are handled in `runs/features/binance_archive.py`. They are repeated here because a
new loader written in a hurry will hit them again:

1. **The metrics archive is not a uniform grid and is not sorted.** A single day's 288 rows
   have an inter-row gap histogram of `{5: 76, 10: 48, 15: 42, 20: 37, …, 185: 1}` and
   include three **negative** gaps of about −1,400 minutes. Sort by `create_time`,
   deduplicate, resample. Never index by position.
2. **Spot klines in the bulk archive switched from milliseconds to microseconds at exactly
   2025-01**, while futures klines stayed in milliseconds and REST returns milliseconds
   everywhere. Branch on **digit count**, never on a date constant.
3. **The metrics archive contains open-interest values near zero** that produce `inf` on
   `pct_change`. Clip or drop.
4. **`markPrice` is an empty string on early `fundingRate` rows.** The loader must tolerate
   it.

## What "already measured" means

`docs/design/crypto-research.md` and `docs/design/wide-universe.md` hold the results of
studies that were actually run, with split-half tables and base rates beside every number.
**Cite them; do not re-run them.** Re-deriving a known result spends a trial, and the trial
counter only grows.
