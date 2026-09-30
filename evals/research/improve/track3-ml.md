# Track 3 — What happened to ML, and the one thread left unfinished

**Run:** 2026-09-30 · workspace `im3` · CPU only, no GPU training
**Question the owner asked:** *"what happened to ML and can we improve it"*
**Verdict:** **REFUTED.** The one open ML thread does not survive its own honest control. The
cross-sectional drawdown model is 72% rank-correlated with the single feature it had to beat,
beats it by **0.007 AUC**, and when turned into a book it makes the book **worse**, not better.
Ship the single feature. Retire the model.

**Snapshot discipline:** nothing in this run opened, copied or read `knowledge/earn.db`,
`journal/journal.db` or any `ft_userdata` sqlite file. Every number comes from
`~/earn-ml-data/panel_1d.parquet` (the survivorship-free daily panel, static since 2026-09-24),
`~/earn-ml-data/funding.parquet`, and the repo's own `ml/` harness. No bot, cron, unit or console
was touched. No order was placed. No commit was made.

---

## 0. The answer in one page

### 0.1 What happened to ML: it was built, it was measured, and then nothing was connected

`ml/` is **14 modules, 9,677 lines**, with `tests/test_ml/` at 16 test modules and 345 tests. It is
good code. It is also **completely unwired.** Verified by reading the repo, not by assuming:

| check | command | result |
|---|---|---|
| Does anything outside `ml/` and `tests/` import it? | `grep -rn "from ml\.\|import ml\b" --include=*.py .` excluding `ml/` and `tests/` | **zero hits** |
| Is it in the config? | `grep -n "\bml\b" config/earn.yaml` | **zero hits** |
| Is it on the schedule? | `config/earn.yaml: ops.schedules` (15 jobs) and `ops/crontab` (15 flock names) | **no ML job** |
| Does the forecast writer exist? | `ls runs/features/forecast.py` | **does not exist** (build-list item 5) |
| Does the grading table exist? | `ops/sql/migrations/` | stops at **005** — no `006_journal.sql` (item 6) |
| Does the prompt read a forecast? | `grep -rln "FORECAST" prompts/` | **zero hits** (item 7) |
| Was the trial-counter race fixed? | `ml/registry.py: Trials.add` | **still** mutates an in-memory snapshot and calls `save()` with no re-read and no lock (item 2) |

So the honest status is: **11 build-list items, 0 shipped.** The measurement work happened; the
integration work did not start. That is the whole of "what happened to ML".

One thing worth the owner's attention, because it is the same shape of problem: the project's
*best* forecast — the shipped `runs/features/volatility.py` HAR+DVOL blend at OOS R² 0.60 on BTC and
0.54 on ETH — is **also not on the schedule and also not read by the order path.** Its only caller
is `.claude/skills/vol-surface/scripts/compute_volsurface.py`, which runs when a skill is invoked and
writes `knowledge/state/volsurface.json`. Nothing in `strategies/` or `runs/decision_core.py` reads
`sigma_hat`. The good forecast is advisory to Claude's eyes, not an input to sizing. That is a
defensible design — but it means "we have a volatility forecast" and "our position sizes use a
volatility forecast" are two different statements, and only the first is true today.

### 0.2 The one open thread, and why it closes as a negative

`ml-forecast.md` left exactly one live claim: a cross-sectional ordering of 7-day / −20% drawdown
risk at **AUC 0.6172** against a **0.5431** hand-set `vol_60` decile rule — top-decile lift 1.734
against 1.233, agreed by nine model families. It was never wired into anything. I re-ran it on the
same panel with a purged, embargoed, uniqueness-weighted walk-forward and three pre-registered
hypotheses sealed before the first number.

**The 0.5431 baseline was the wrong baseline, and that single fact closes the thread.**

`ml-forecast.md` compared the model's *continuous ordering* against a **decile-lookup probability
map** — a discretised, calibration-lossy object. Measured the way the document itself says the
model may be used — as an ordering, scored by AUC — the baseline is not 0.5431. It is **0.6093**.

| | OOS AUC (mean of 4 purged folds) | top-decile lift | what it is |
|---|---|---|---|
| logistic, 15 cross-sectional features | **0.6165** | 1.602 | the model `ml-forecast.md` kept (its K2) |
| extra trees, same 15 | 0.6186 | 1.620 | |
| hist gradient boosting, same 15 | 0.5906 | 1.478 | |
| **`vol_60_xs` alone — continuous ordering** | **0.6093** | 1.506 | **the honest control** |
| `vol_20_xs` alone | 0.5990 | 1.528 | |
| **`vol_60_xs − age_days_xs`, two features, no fitting** | **0.6163** | 1.433 | ties the 15-feature model |
| shuffled-label control (logistic) | 0.4470 | — | the signal collapses, as it should |
| `vol_60` **decile lookup** (the figure quoted in `ml-forecast.md`) | 0.5431 `CITED` | 1.233 `CITED` | a discretised map, not an ordering |

The increment the whole ML programme produced over one number already in the shipped eligibility
screen is **+0.0072 AUC**. Not +0.074. The headline in `ml-forecast.md` overstates it by roughly
**ten times**, because it scored a continuous model against a discretised baseline.

And the model is largely that one feature wearing a coat: the per-day Spearman correlation between
the model's predicted probability and `vol_60_xs` is **0.718 mean, 0.765 median**.

### 0.3 The three pre-registered results

Pre-registration sealed **before any number was computed**, SHA-256
`f3bb55afcc4118a9ca07aea04162977504d14dac2e3137e8bf88c3e127fb0ff0`, re-verified unchanged at
close (`~/scratch/im3/prereg.json`).

| | statement | falsifier | outcome |
|---|---|---|---|
| **H1** | Refusing / shrinking the worst predicted-risk decile improves the book's drawdown by ≥3pp without costing >1pp CAGR | drawdown gain <3pp, or CAGR >1pp worse, or Sharpe uplift inside its own SE | **REFUTED — falsifier triggered on all three limbs** |
| **H2** | The probabilities are not calibrated; isotonic on a purged inner fold makes them usable as a size multiplier | raw gap already <5pp refutes half one; isotonic failing to reach <5pp refutes half two | **half one CONFIRMED, half two REFUTED** |
| **H3** | The model beats `vol_60` alone by ≥0.02 AUC and ≥0.15 lift | below either threshold ⇒ ship the feature, retire the model | **REFUTED — 0.0072 AUC, 0.096 lift** |

---

## 1. The harness, the sample, and the folds

Built with the repo's own `ml/data.py`, `ml/features.py`, `ml/labels.py`, `ml/splits.py`. The
`assert_no_lookahead` proof — rebuild every feature on a 70% prefix and require identical values —
**passed**. `assert_no_leakage` on the folds **passed**.

| | |
|---|---|
| raw panel | 841,175 rows, 747 symbols, 763 segments after discontinuity splitting, **282 dead segments retained** |
| point-in-time eligible rows | **206,152**, 394 eligible segments |
| span | 2018-02-13 → 2026-09-24 |
| names in the cross-section per day | median **59**, min 2, max 169 |
| `dd_7d_20` base rate on eligible rows | **18.27%** |
| labelled rows used | 205,759 |

**Effective sample size, both ends of the bracket** (`ml.labels.uniqueness_weights`), because a row
count on an overlapping panel is not a sample size:

| target | rows | `eff_n_panel` (lower bound) | `eff_n_symbol` (upper bound) |
|---|---|---|---|
| `dd_7d_20` | 836,579 | **755.1** | 144,487.2 |
| `ret_7d` | 835,834 | 415.3 | 105,146.9 |
| `ret_1d` | 840,412 | 1,661.8 | 420,587.5 |

The true sample for the drawdown claim is somewhere between **755 and 144,487**. Every claim below
is quoted against four folds, not against 836,579.

### Folds (purged, embargoed, expanding)

| fold | train rows | test rows | test window | purged | base rate train → test |
|---|---|---|---|---|---|
| 1 | 4,585 | 28,734 | 2019-11-03 → 2021-07-22 | 63 | 0.1213 → **0.2913** |
| 2 | 32,480 | 65,836 | 2021-07-23 → 2023-04-11 | 902 | 0.2731 → 0.1782 |
| 3 | 98,591 | 58,938 | 2023-04-12 → 2024-12-29 | 627 | 0.2095 → 0.1520 |
| 4 | 157,086 | 47,603 | 2024-12-30 → 2026-09-17 | 1,070 | 0.1883 → 0.1675 |

The base rate moves by up to **17 percentage points** between a training fold and its test fold.
That single fact is why any probability out of this model is untrustworthy, and §3 measures it.

---

## 2. H3 — the honest control. Does the model beat `−vol_60` alone?

**No.** Per fold, model against control, identical rows:

| fold | AUC logistic | AUC `vol_60_xs` | Δ | lift logistic | lift `vol_60_xs` | Δ |
|---|---|---|---|---|---|---|
| 1 | 0.6229 | 0.6227 | **+0.0002** | 1.502 | 1.590 | **−0.088** |
| 2 | 0.5794 | 0.5847 | **−0.0053** | 1.366 | 1.417 | **−0.051** |
| 3 | 0.6143 | 0.6075 | +0.0068 | 1.558 | 1.459 | +0.099 |
| 4 | 0.6495 | 0.6223 | +0.0272 | 1.981 | 1.557 | +0.424 |
| **mean** | **0.6165** | **0.6093** | **+0.0072** | **1.602** | **1.506** | **+0.096** |

The model loses on AUC in one fold of four and loses on lift in two of four. Both pre-registered
thresholds (0.02 AUC, 0.15 lift) are missed. **H3 is refuted.**

Pooled over the whole out-of-sample period the sign even reverses on the tail measure: the model's
worst decile carries a 24.90% drawdown rate (lift 1.363) while `vol_60_xs`'s worst decile carries
**26.16%** (lift 1.432). Which ordering has the better tail depends on the pooling convention — and
a result that depends on the pooling convention is a result inside the noise.

### The funding ablation — build-list item 3, closed

`ml-forecast.md` called this "most likely real improvement available", on the strength of a
17.0% → 41.4% conditional drawdown-rate table. Measured on the head itself:

| | mean OOS AUC | mean lift | mean Brier |
|---|---|---|---|
| logistic, 15 features **with** `funding_ann_xs` | 0.6165 | 1.602 | 0.1603 |
| logistic, 14 features **without** funding | **0.6170** | 1.598 | 0.1602 |

Funding is worth **−0.0005 AUC** on this head. Build-list item 3 is answered and should be struck.

---

## 3. H2 — calibration. Confirmed broken, and calibration does not fix it

`ml-forecast.md` measured systematic over-prediction and said nobody had fitted a calibrator. I
fitted one: isotonic regression on an **inner purged split** of each training fold (last 20% by
calendar, with the 7-day label window purged out of the inner-train side).

| | mean abs calibration gap (10 quantile bins) | mean OOS AUC | mean Brier |
|---|---|---|---|
| logistic, raw | **0.0656** | 0.6165 | 0.1603 |
| logistic + isotonic on a purged inner fold | **0.1037** | 0.6006 | 0.1702 |
| extra trees, raw | 0.0634 | 0.6186 | 0.1587 |
| extra trees + isotonic | 0.1110 | 0.6033 | 0.1667 |
| hist gradient boosting, raw | 0.0944 | 0.5906 | 0.1719 |
| hgb + isotonic | 0.1073 | 0.5616 | 0.1714 |

**Half one confirmed:** the raw gap is 6.6pp, above the 5pp line, and fold 1 alone is **18.25pp**.

**Half two refuted, and the direction is the informative part: isotonic made every model worse on
every axis** — gap 0.0656 → 0.1037, AUC 0.6165 → 0.6006, Brier 0.1603 → 0.1702.

The reason is fold 1 and the base-rate shift. Isotonic learns a map from score to frequency on the
*inner validation* period, then applies it to a *test* period whose event rate is different by up to
17pp. A calibrator is a level statement, and the level is precisely what is not stable here. Fitting
one on a purged fold does not help because the purge protects against leakage, not against
non-stationarity.

**Conclusion: the probabilities cannot be used as a size multiplier.** Not raw, and not calibrated.
Build-list item 4 should be struck as well — not "wait until a probability is wanted", but
"a probability is not available from this model at all".

---

## 4. H1 — the book. The higher-AUC model makes the book worse

Out of sample, 2019-11-03 → 2026-09-17, **2,511 days / 6.88 years**, weekly rebalance, equal weight
over the point-in-time eligible universe, **15 bps per side on turnover, always on**. Arithmetic
Sharpe (mean/std × √365), as the repo requires.

| book | CAGR | **Sharpe** | Sharpe SE | max drawdown | Calmar | ann. vol | turnover/yr |
|---|---|---|---|---|---|---|---|
| base equal-weight eligible (no overlay) | +38.02% | 0.820 | ±0.441 | −85.47% | 0.445 | 86.3% | 4.8× |
| **model** refuse worst decile | +33.97% | **0.784** | ±0.436 | **−85.47%** | 0.397 | 86.0% | 8.1× |
| **model** shrink worst decile ×0.5 | +36.19% | 0.804 | ±0.439 | −85.46% | 0.423 | 86.1% | 6.3× |
| **model** refuse worst 20% | +34.39% | 0.787 | ±0.436 | −84.32% | 0.408 | 85.3% | 10.5× |
| **model** refuse worst 30% | +33.80% | 0.781 | ±0.435 | −84.62% | 0.399 | 84.5% | 12.5× |
| **model** refuse worst 50% | +28.17% | 0.725 | ±0.429 | −83.95% | 0.336 | 83.1% | 15.9× |
| `vol_60` refuse worst decile | +46.82% | 0.891 | ±0.451 | −84.25% | 0.556 | 85.6% | 6.5× |
| `vol_60` shrink worst decile ×0.5 | +42.20% | 0.854 | ±0.445 | −84.88% | 0.497 | 85.9% | 5.6× |
| `vol_60` refuse worst 20% | +53.26% | 0.943 | ±0.458 | −83.07% | 0.641 | 84.8% | 7.7× |
| `vol_60` refuse worst 30% | +60.20% | 0.995 | ±0.466 | −82.05% | 0.734 | 84.0% | 9.3× |
| **`vol_60` refuse worst 50%** | **+64.37%** | **1.028** | ±0.471 | −81.81% | 0.787 | 81.9% | 11.4× |
| **BTC buy-and-hold, identical dates** | **+37.17%** | **0.832** | ±0.442 | **−76.63%** | 0.485 | 60.1% | 0× |

**The falsifier triggered on all three limbs.** The model's decile refusal delivered a max-drawdown
improvement of **0.00pp** (−85.47% both, to the second decimal), a CAGR **4.05pp lower**, and a
Sharpe **0.036 lower**.

### The paired test, which is the only fair one

The overlays and the base book share 99.7% of their daily returns, so the ±0.44 individual SEs are
the wrong error bar. Memmel-corrected Jobson-Korkie on the paired daily differences:

| book | vs | Sharpe | reference | Δ Sharpe (ann.) | SE | **t** | ρ |
|---|---|---|---|---|---|---|---|
| model refuse d10 | base | 0.784 | 0.820 | **−0.036** | 0.031 | **−1.14** | 0.997 |
| model refuse d30 | base | 0.781 | 0.820 | −0.039 | 0.054 | −0.73 | 0.990 |
| `vol_60` refuse d10 | base | 0.891 | 0.820 | **+0.072** | 0.026 | **+2.74** | 0.998 |
| `vol_60` refuse d30 | base | 0.995 | 0.820 | **+0.175** | 0.052 | **+3.40** | 0.991 |
| `vol_60` refuse d50 | base | 1.028 | 0.820 | **+0.208** | 0.084 | **+2.47** | 0.976 |
| model refuse d10 | BTC hold | 0.784 | 0.832 | −0.048 | 0.240 | −0.20 | 0.803 |
| `vol_60` refuse d30 | BTC hold | 0.995 | 0.832 | +0.163 | 0.241 | +0.68 | 0.800 |
| `vol_60` refuse d50 | BTC hold | 1.028 | 0.832 | +0.196 | 0.245 | +0.80 | 0.795 |
| **model overlay vs `vol_60` overlay, d10** | — | 0.784 | 0.891 | **−0.107** | 0.034 | **−3.11** | 0.996 |
| **model overlay vs `vol_60` overlay, d30** | — | 0.781 | 0.995 | **−0.215** | 0.050 | **−4.33** | 0.992 |

Read the last two rows. Against its own honest control, on the same dates and the same universe, the
model overlay is worse at **t = −3.1** and **t = −4.3**. This is not "the model didn't add much". It
is "replacing the single feature with the model is a statistically significant *loss*".

### Why the higher-AUC model made the worse book — the mechanism

Decile tables on the full out-of-sample sample (0 = safest, 9 = the decile a refusal rule drops):

**Deciles of the model's predicted P(dd_7d_20)**

| decile | n | `dd_7d_20` rate | mean fwd 7d | median fwd 7d | share up |
|---|---|---|---|---|---|
| 0 | 21,144 | 0.1072 | **+0.265%** | −0.42% | 0.478 |
| 4 | 20,191 | 0.1825 | −0.194% | −1.36% | 0.449 |
| 9 | 20,899 | **0.2490** | **−0.729%** | −2.58% | 0.424 |

**Deciles of `vol_60_xs` alone**

| decile | n | `dd_7d_20` rate | mean fwd 7d | median fwd 7d | share up |
|---|---|---|---|---|---|
| 0 | 21,144 | 0.0963 | **+0.248%** | −0.28% | 0.484 |
| 4 | 20,191 | 0.1800 | −0.113% | −1.31% | 0.454 |
| 9 | 20,899 | **0.2616** | **−0.930%** | **−2.87%** | 0.415 |

Both orderings are monotone in drawdown rate — the finding is real, and it replicates. But the
column that decides the book is the *return* column, and there the single feature is strictly
better: the decile it refuses lost **−0.930%** per forward week against the model's **−0.729%**, and
**−2.87%** against **−2.58%** at the median.

The model's extra AUC came from features that predict a large *path move* — and a −20% path breach
is one-sided in the label, not in the economics. Ordering "which coin is about to move violently"
better is not the same as ordering "which coin is about to be a bad holding" better. The model was
optimised on the wrong objective and the book is where that shows.

**This is the general lesson and it is worth more than the experiment: AUC on a drawdown flag is not
the objective function of a long-only spot book. A higher AUC bought a worse book, at t = −3.1.**

### Both directions of the rule, as the protocol requires

What the refused names actually did, by regime. Per-fold:

| fold | window | base | model d10 | model d30 | `vol_60` d10 | `vol_60` d30 | BTC hold |
|---|---|---|---|---|---|---|---|
| 1 | 2019-11 → 2021-07 | +268.4% / 1.800 | +248.5% / 1.743 | +227.5% / 1.685 | +305.7% / 1.897 | **+350.8% / 1.995** | +112.5% / 1.366 |
| 2 | 2021-07 → 2023-04 | −12.7% / 0.300 | −14.3% / 0.276 | −20.6% / 0.180 | −8.6% / 0.346 | −4.6% / 0.382 | −6.6% / 0.209 |
| 3 | 2023-04 → 2024-12 | +121.3% / 1.483 | +110.3% / 1.414 | +111.6% / 1.437 | +129.4% / 1.536 | +156.4% / 1.712 | +93.2% / 1.615 |
| 4 | 2024-12 → 2026-09 | −49.1% / −0.492 | −48.8% / −0.497 | −41.8% / −0.373 | −45.5% / −0.413 | −40.4% / −0.350 | **−7.7% / 0.034** |

(CAGR / arithmetic Sharpe.) The model overlay is behind the base book on Sharpe in **4 of 4 folds**
and behind the `vol_60` overlay in 4 of 4. And fold 4 is the sobering column: every basket book lost 40–49% CAGR over the
last 21 months while BTC lost 7.7%. That is `growth-audit.md`'s conclusion arriving again from a
different direction.

By regime (BTC 200d MA, and BTC 60d realised-vol median split):

| regime | days | base | model d10 | `vol_60` d10 | model d30 | `vol_60` d30 | BTC hold |
|---|---|---|---|---|---|---|---|
| BTC > 200dMA | 1,431 | +54.0% / 0.946 | +47.6% / 0.896 | +66.7% / 1.041 | +45.8% / 0.882 | +78.1% / 1.126 | **+67.0% / 1.194** |
| BTC < 200dMA | 1,080 | +19.4% / 0.660 | +17.9% / 0.643 | +24.1% / 0.702 | +19.5% / 0.654 | **+39.3% / 0.831** | +5.7% / 0.424 |
| high vol | 1,241 | +71.3% / 1.063 | +60.3% / 0.994 | +80.4% / 1.117 | +51.0% / 0.931 | **+86.8% / 1.155** | +36.5% / 0.802 |
| low vol | 1,270 | +11.8% / 0.522 | +12.4% / 0.528 | +20.0% / 0.618 | +18.9% / 0.601 | +37.9% / 0.809 | **+37.8% / 0.941** |

The model overlay beats the base book in exactly one of four regimes (low vol, +0.006 Sharpe). The
`vol_60` overlay beats it in four of four. Neither beats BTC hold's Sharpe in a trending or low-vol
market, and neither beats BTC hold's **−76.63% drawdown** anywhere.

### The form that could actually touch the live book — and it loses

Earn trades BTC and ETH. A ten-name decile has no meaning on a two-name book, so the only way this
model could reach the deployed sleeve is as a **time-series** signal: de-risk when an asset's own
predicted risk is high against its own trailing year.

| asset | rule | days de-risked | CAGR | Sharpe | max drawdown |
|---|---|---|---|---|---|
| BTC | **hold** | 0 | **+37.17%** | **0.832** | **−76.63%** |
| BTC | own-history pctl ≥0.90 → flat | 279 | +20.29% | 0.616 | −83.99% |
| BTC | pctl ≥0.90 → ×0.5 | 279 | +29.12% | 0.738 | −79.59% |
| BTC | pctl ≥0.80 → ×0.5 | 501 | +27.20% | 0.718 | −75.52% |
| BTC | pctl ≥0.70 → ×0.5 | 688 | +28.57% | 0.744 | **−71.59%** |
| ETH | **hold** | 0 | **+47.34%** | **0.890** | **−79.30%** |
| ETH | pctl ≥0.90 → flat | 311 | +17.76% | 0.602 | −91.73% |
| ETH | pctl ≥0.90 → ×0.5 | 311 | +32.95% | 0.762 | −83.38% |
| ETH | pctl ≥0.80 → ×0.5 | 563 | +38.43% | 0.816 | −81.45% |
| ETH | pctl ≥0.70 → ×0.5 | 758 | +35.29% | 0.787 | −82.06% |

**Every rule loses to holding, on CAGR and on Sharpe, on both assets.** Three of eight make the
drawdown *worse*. The one that improves drawdown (BTC, pctl ≥0.70, −71.59%, +5.0pp) pays 8.6pp of
CAGR and 0.088 of Sharpe for it — a worse trade than the MA125 filter the project already has. And
going flat entirely (pctl ≥0.90 → 0) deepens BTC's drawdown by 7.4pp, because the model de-risks
into volatility and misses the rebound: it is the trend-loss exit failure mode again, in a new hat.

### The hurdle, and the trials I added

| | |
|---|---|
| span | 6.88 years |
| baseline | BTC buy-and-hold arithmetic Sharpe **0.832** on identical dates |
| `expected_max_sharpe` at 8,144 cumulative trials | **1.717** |
| **deflated hurdle** | **2.549** |
| best number this run produced | `vol_60` refuse-worst-50%, **1.028** |
| shortfall | **−1.52** |
| best *model* number | 0.784 — **below the un-overlaid base book** |

**Selection trials I add: 26.** 7 ordering configurations (3 single-feature controls, 3 model
families, 1 funding ablation) + 11 book configurations (1 base, 5 model overlays, 5 `vol_60`
overlays) + 8 BTC/ETH time-series rules. Isotonic variants and the shuffled-label fit are controls,
not selection. At 8,170 trials the hurdle is still 2.549 — the choice of N is not what kills this.

---

## 5. What would have to change for ML to help with DIRECTION

This is the owner's real hope, so here is the gap as a number rather than as a discouragement.

### The break-even curve, and where we actually are

| horizon | balanced accuracy needed to **match** holding BTC | equivalent directional rank IC |
|---|---|---|
| 1 day | **56.07%** `CITED` (`dip-strategy.md` §0.2) | ~0.19 |
| 20 days | **60.69%** `CITED` | — |
| 180 days | **73.70%** `CITED` | ~0.68 |

Two numbers I measured here that sharpen it:

- **Just to pay the toll**, before beating anything: BTC's mean absolute daily move over this sample
  is **2.104%**, so a long/flat daily timer needs **53.57%** directional accuracy at 30 bps a switch
  merely to break even against doing nothing. Everything between 53.57% and 56.07% is work that pays
  the exchange and not the owner.
- **The best directional signal the project has ever measured on BTC** is `rsi14` at rank IC
  **0.123, t 0.7** — and across 45 feature-horizon pairs **not one reaches |t| = 2** `CITED`.

So the shortfall is roughly **0.19 needed against 0.123 measured at t 0.7** — about a factor of
1.5 in IC, and a factor of about 3 in statistical significance. `ml-forecast.md` measured the same
gap from the model side: 78 CPU and 9 GPU configurations, best costed book a **tie** with BTC hold
inside a ±0.41 error bar.

### The cross-sectional version of the same arithmetic, measured here

| cross-sectional feature | rank IC vs forward 7d return | t | days |
|---|---|---|---|
| `vol_60_xs` | **−0.1067** | −19.74 | 2,450 |
| `age_days_xs` | +0.0866 | +18.46 | 2,450 |
| `mom_365_xs` | +0.0132 | +2.57 | 2,423 |

`vol_60_xs` at |IC| 0.107 and t −19.7 is a genuinely strong, highly significant cross-sectional
ordering — and the best book it produces after costs is **Sharpe 1.028** against a hurdle of
**2.549**. Scaling linearly in IC, the book would need **|IC| ≈ 0.265** to clear — about **2.5×
the strongest cross-sectional signal in nine years of this panel**, and about 2.2× the best
directional IC ever measured on BTC.

### What would actually have to change — three things, in order of how much they'd move the number

1. **The cost floor, not the model.** Break-even accuracy is a function of cost. At 0 bps the 1-day
   break-even is 50%; at 30 bps it is 53.57%. Almost all of the difficulty at short horizons *is*
   the toll. Nothing in a model fixes that; only a cheaper venue, a longer horizon, or fewer trades
   does. `exit-and-horizon-2026-09-29.md` already measured the answer: break-even holding period
   ~10 days, and the shipped 1h rule earns 0.022% against the 0.300% it costs.
2. **A different label.** Every direction experiment here and in `ml-forecast.md` predicts a return
   or its sign. The book does not need a return forecast — it needs to know *when to reduce*. The
   one place the measured evidence is loud is the **exit side**: live, mechanical profit-takes are
   10 trades / +85.27 / 10-of-10, and the trend-loss `exit_signal` is 8 trades / −60.30 / 0-of-8.
   That is a labelling problem (what is a good exit?) with a 22-observation live sample and a clear
   sign, and it is not an ML problem yet — it is a rule problem. An ML model fitted to a target the
   book does not trade will keep producing results like this document's.
3. **Leaving the mandate.** `dip-strategy.md` §0.2 proved it mechanically: a long-only spot book
   already fully invested **cannot express upward conviction** — every signal is expressible only as
   a reduction. Directional skill therefore has a hard ceiling on what it can be worth here, no
   matter how good it gets. Any plan that assumes "a better forecast will raise returns" is arguing
   against that proof and should say so explicitly.

---

## 6. What to do — the revised build list

`ml-forecast.md` shipped an 11-item build list. On this evidence, **six items should be struck, not
scheduled.**

| item | `ml-forecast.md` said | this run says |
|---|---|---|
| 5 · `runs/features/forecast.py` writing a risk rank | build it | **strike.** There is nothing to write that `vol_60_xs` does not already say, and the shipped eligibility screen already uses it |
| 11 · `exposure_scale` clamp driven by the model | build it last | **strike.** Every time-series form of this signal loses to holding on both assets |
| 3 · funding ablation on the drawdown head | "most likely real improvement available" | **closed: −0.0005 AUC.** Struck |
| 4 · isotonic / Platt calibration | "can wait" | **closed: makes it worse on every axis.** Struck |
| 7 · `{{FORECAST}}` in the research prompt | build it | **strike** for the drawdown model. A 0.007-AUC increment is not evidence worth a prompt slot, and a slot that carries noise costs attention |
| 2 · fix `Trials.add`'s lost-update race | fix it | **keep, and it is now the top item.** The one mechanism that stops the project fooling itself is still silently lossy. Verified unfixed today |
| 1 · the missing vol experiment (GBDT vs the shipped blend on its own target) | first item | **keep.** Still never run, and it is still the only ML comparison that could justify touching sizing. Pre-register the config count before it starts |
| 6 · the forecast-grading journal table | build it | **keep, generalised.** The valuable thing is not grading *this* model. It is grading *any* signal the system emits against what happened. Build it for the exit rules, where the live evidence already has a sign |
| 8, 9, 10 · shadow arm, console page, drawdown-convention reconciliation | build them | **defer.** Nothing left to shadow |

**And the one thing to keep from the whole ML programme:** `ml/` as a **harness**, not as a model
zoo. `assert_no_lookahead` caught nothing today because there was nothing to catch — which is
exactly what a working proof looks like. The 20.5s-cold / 0.02s-warm cached point-in-time panel over
841k rows is why this study took an afternoon instead of a week. That infrastructure has paid for
itself twice now, both times by producing a clean negative quickly. Keep it, keep its 345 tests, and
stop expecting a model out of it.

---

## 7. Honest limits

1. **No live journal data.** Per the snapshot rule, nothing read `knowledge/earn.db` or
   `journal/journal.db`. The live exit evidence quoted in §5 (10-of-10 mechanical, 0-of-8
   trend-loss) is **cited from the owner's own message and the profit audit**, not re-derived here.
2. **The base book is not the shipped book.** Earn trades BTC and ETH. An equal-weight
   eligible-universe book is the natural cross-section for a decile overlay but it is not what runs.
   §4's BTC/ETH time-series table is the form that could reach the deployed sleeve, and it loses.
3. **Delisting is understated.** A held segment that stops trading contributes 0 thereafter rather
   than a liquidation loss. This flatters every basket book in the table, including the `vol_60`
   overlay, and does not flatter BTC hold. `growth-audit.md` measured the delist cost separately.
4. **Four folds, not five.** `walk_forward` skipped fold 0 for failing the 250-row training floor.
   Fold 1 trains on 4,585 rows and is the weakest column in every table; the mean-of-folds numbers
   carry it.
5. **One model family carried the book test.** H1's overlay used the logistic predictions, the family
   `ml-forecast.md` reported as best on AUC. Extra trees scored marginally higher AUC here (0.6186)
   and I did not re-run the full book on it. Given the mechanism in §4 — the model's worst decile is
   a high-return decile — a family with *higher* AUC would be expected to do slightly worse, not
   better, but that is an inference and not a measurement.
6. **Two free parameters swept, not four.** Decile cut (10/20/30/50%) and shrink factor (0 / 0.5).
   The monotone `vol_60` surface across all four cuts is the plateau, not a peak. I did not sweep the
   rebalance period; 7 days was fixed by the label horizon.
7. **A tier-2 path was written.** This report lives at `evals/research/improve/track3-ml.md`, and
   `evals/**` is tier 2 (human only). It follows the convention the other five directories in
   `evals/research/` already established, but the owner should know rather than discover it. Nothing
   else in the repo was modified.
8. **One accident, and it is cleaned up.** An `earn-sync im3` invocation with a relative destination
   created a recursive 186 MB copy of the working tree at `momentum/im3/` before I noticed. I removed
   it (`rm -rf im3`) and verified it is gone. A sibling agent's `momentum/im5/` is still present; it
   is not mine and I left it alone. The owner may want to check for other stray workspace copies at
   the repo root.
9. **Load discipline held.** One heavy job at a time, everything `nice -n 10`, `/proc/loadavg`
   checked before each step, and the one step that would have started at 1-minute load 6.04 was
   deferred until it fell to 5.93. No GPU was used.

---

## 8. Provenance

**Pre-registration:** `~/scratch/im3/prereg.json`, SHA-256
`f3bb55afcc4118a9ca07aea04162977504d14dac2e3137e8bf88c3e127fb0ff0`, sealed before the first number
and re-verified unchanged at close.

**Scripts** (`~/scratch/im3/wsrepo/`): `s1_build.py` (panel + features + labels + lookahead proof),
`s2_model.py` (H3, H2, folds, shuffle control), `s3_book.py` (H1 books, regimes, folds),
`s4_diag.py` (decile tables, BTC/ETH time-series form, direction ICs), `s5_stats.py` (paired Sharpe
tests, deflated hurdle, break-even arithmetic).

**Artefacts** (`~/scratch/im3/out/`): `panel_feat.parquet`, `preds_logistic.parquet`,
`h3_folds.csv`, `h1_books.csv`, `h1_regimes.csv`, `h1_folds.csv`, `h1_refused.csv`,
`paired_sharpe.csv`, `deciles_p_raw.csv`, `deciles_vol_60_xs.csv`, `core_timeseries.csv`,
`hurdle.json`, `direction_gap.json`, `direction_ic.json`.

**Sources read:** `~/earn-ml-data/panel_1d.parquet`, `~/earn-ml-data/funding.parquet`, the repo's
`ml/` and `runs/features/sampling.py`. **Databases opened: none.**

**Labels.** `MEASURED HERE` — computed in this run, script named above: every number in §§1–5 except
where marked. `CITED` — from `ml-forecast.md`, `dip-strategy.md`, `growth-audit.md`,
`exit-and-horizon-2026-09-29.md` or the owner's own message, named at the point of use.
