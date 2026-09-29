# ML forecast — what is forecastable, what the models actually scored, and how a forecast enters the decision path without becoming an oracle

**Status:** audit and integration design, decision-grade. Nothing here is switched on until the
build list in §6 ships through its stated tier. No running bot, no bot config and no `.env` was
touched producing it. One tier-2 file was modified by a model agent and is flagged for the owner in
§7.3.

**Scope (owner, verbatim):** *"can we build a very high precision ai/ml forecast that can forecast
crypto for our required horizon and granularity? this could be one piece that helps decision making
of buy and sell along with info from skills and research… use all algos, use cpu for the ones that
need it, use gpu for others, forecasting can be backtested with test and train, collate all
possible data that would help it, go through research papers and collate what algos and ideas we
can use, keep iterating until we reach a mape of near 1, then this would be a very high precision
forecast. build features, iterate feature, research, implement and repeat. once ml is built we test
it along with our existing decision making to see how to integrate."*

Five workstreams ran on this host on 2026-09-24/25: a literature review, a harness-and-baselines
build, a CPU model zoo, a GPU model zoo, and a feature study. **439 selection trials** are now on
the shared counter. Every number below carries the baseline it must beat, and where two workstreams
measured the same thing on different samples I say so rather than picking the flattering one.

**Every claim is labelled.** `MEASURED HERE` — computed on this host by one of the five workstreams,
artefact named. `MEASURED BY ME` — computed by the author of this document, during this run, command
named. `INCUMBENT` — a number the shipped code already produces. `CITED` — from a paper, with the
fetch status recorded in §7.2.

---

## 0. The answer, before the detail

### 0.1 The MAPE question, head-on

**The persistence price-level MAPE is 0.4408% on BTC 1h and 2.3255% on BTC 1d.** `MEASURED HERE`
(`ml/baselines.py`, 79,660 and 3,323 observations, full feather history 2017-08-17 → 2026-09-23).
ETH: 0.5714% at 1h, 3.0872% at 1d.

That forecast is "the next price is the last price". It has zero parameters, reads zero inputs and
contains zero information. **"MAPE near 1" is therefore already beaten, by more than twice over, by
a model that forecasts nothing.**

Chasing it is not a hard target, it is an anti-target, and the reason is arithmetic rather than
opinion. At `h=1`, MAPE on a price level *is* mean absolute error on the return, divided by price.
So the ratio of a 1% MAPE to persistence maps straight onto an implied return R²:

| series | persistence price MAPE | 1% MAPE ÷ persistence | implied R² on returns | implied annualised Sharpe |
|---|---|---|---|---|
| BTC 1h | 0.4408% | **2.27× worse** | −4.15 | negative |
| BTC 4h | 0.8812% | 1.13× worse | −0.29 | negative |
| BTC 1d | 2.3255% | 0.43× (i.e. 2.3× better) | **+0.815** | **40.1** |
| ETH 1h | 0.5714% | 1.75× worse | −2.06 | negative |
| ETH 1d | 3.0872% | 0.32× | +0.895 | 55.8 |

`MEASURED HERE` (`~/earn-panels/ml1/mape3.py`). The metric does not describe a difficulty level, it
describes the granularity. At 1h and 4h a 1% MAPE is *worse than forecasting nothing*. At 1d it
would be a return R² of 0.82 and an annualised Sharpe of 40 — against Campbell–Thompson's benchmark
that **0.5% monthly out-of-sample R² is already economically meaningful** `CITED`. The target is
simultaneously unreachable at one granularity and already surpassed by inaction at the other.

Two further baselines, so the corrected metric can never be read as a score:

- **MAPE on returns with a zero forecast is exactly 100.0% by construction**, at every horizon, on
  every series, with R² exactly 0.000. `MEASURED HERE`
- **Every one of the 78 CPU configurations and all 9 GPU configurations is worse than that.** Return
  MAPE ran 110–295% (CPU) and 129–263% (GPU) against persistence's 100.0%, and
  `r2_oos_vs_persistence` was negative for every deep model and for every return-level fit
  (−0.0005 to −0.275). `MEASURED HERE` (`runs/ml/cpu/scorecard.csv`, `~/earn-ml4-out/zoo_report.json`)
- **Sign-persistence hit rate is 46.4–46.7%** on BTC and ETH at both 1h and 1d. Short-horizon crypto
  returns mildly *reverse*; any directional model must be scored against 50%, not against this.
  `MEASURED HERE`

So the honest reply to the request is: the target was met before the project began, and meeting it
proves nothing. Everything below is scored on returns, volatility and drawdown, against a named
baseline, at 15 bps per side.

### 0.2 What is forecastable, in numbers, with the baseline beside each

**Volatility — yes, strongly, and it is ALREADY FORECAST. This is the finding that reorganises the
whole programme.**

The shipped `runs/features/volatility.py` blend (HAR walk-forward averaged 50/50 with a fitted
`a + b·DVOL` map) runs live across the tradeable universe today. I ran it myself:

| | rolling 250d OOS R² | HAR leg | DVOL leg |
|---|---|---|---|
| **BTC/USDT** | **0.5992** | 0.5470 | 0.3887 |
| **ETH/USDT** | **0.5406** | 0.4862 | 0.3307 |
| 24 pairs reporting a health number | min 0.0531 · **median 0.5105** · max 0.9719 | | |

`MEASURED BY ME` — `python .claude/skills/vol-surface/scripts/compute_volsurface.py` in `~/earn-run`,
`knowledge/state/volsurface.json` written `2026-09-25T00:19:47Z`, as-of bar `2026-09-23T00:00:00Z`.
31 pairs attempted, 26 published a `sigma_hat`, 5 refused by the health gate (ENA, PENGU, PUMP,
XPL, ZEC — two for short history, three for OOS R² below the 0.05 floor).

The best ML volatility result anywhere in the programme is **OOS R² 0.2359** (GPU `xgb_lags`, forward
7d log realised vol, panel-wide) and **0.1803** (CPU CatBoost, `vol_7d`). `MEASURED HERE`

**These are not comparable, and that is exactly the problem.** The incumbent number is one asset, a
rolling 250-day window, forward 7-day annualised vol from 4h-derived daily realised variance, scored
against the expanding mean of past actuals. The ML numbers are pooled across 394 coin-segments over
five purged folds, scored against the training-fold mean, on a log target. Different universe,
different target, different denominator, different window. **Nobody ran the comparison that matters
— an ML model against the incumbent blend, on the incumbent's target, universe and benchmark.**
That is the single largest gap in this programme and it is the first item on the build list.

What can be said without that experiment: volatility is the genuinely forecastable quantity, the
project already forecasts it at a level the ML panel work did not come near on its own terms, and
the ML work's *claimed* advantage — cross-sectional coverage — is one the incumbent already has, per
asset, with a live health gate and a documented fallback.

**Drawdown risk — yes, as an ORDERING; no, as a probability.**

| claim | best measured | baseline in the same table | verdict |
|---|---|---|---|
| rank a coin's 7d/−20% drawdown risk | AUC **0.6172** (logistic), 0.6062 (extra_trees) | `vol_60_xs` decile lookup **0.5431**, base rate 0.5000 | real, modest |
| top-decile lift, 7d/−20% | **1.734** (logistic) | decile lookup 1.233 | real |
| rank IC on the −20%/7d flag, GPU zoo | **+0.106 to +0.204**, NW t 14–24, 9 independent models | — | consistent across families |
| out-of-sample Brier skill vs **test** base rate | **+0.0120** | 0.0 | essentially nothing |
| reliability gaps (vol-decile lookup, 90d/−40%) | −0.167 to −0.100, systematic over-prediction | — | not calibrated |
| 7d/−40% (2.9% base rate) | every model within **0.005** of zero skill | 0.0 | not forecastable |

`MEASURED HERE`. The event rate itself moved from 10.7% in training to 18.3% in test, so most of the
headline Brier skill (+0.0482 against the *training* base rate) is the model noticing a level shift.
**AUC is the number that survives; the probabilities must not be quoted as probabilities.**

**Direction — no, not at a size that survives costs.**

| claim | best measured | baseline in the same table | verdict |
|---|---|---|---|
| cross-sectional rank IC, 7d | extra_trees **0.1035** (t 7.9) | `−vol_60_xs + age_days` ranked **0.1213** (t 8.7) | **the two features win** |
| cross-sectional rank IC, 28d | extra_trees 0.1550 (t 7.2) | same two features **0.1799** (t 7.8) | the two features win |
| cross-sectional rank IC, 1d | extra_trees **0.0861** (t 17.3) | `−vol_60_xs` 0.0778 (t 14.1) | model wins — the only one |
| cross-sectional hit rate, 1d | 0.5327 [0.5305, 0.5349] | 0.5000 by construction | above 50%, economically nil |
| costed book, best of 78 CPU configs | Sharpe **0.833** (xsret_7d extra_trees top-5) | **BTC buy-and-hold 0.833** on the same dates | a tie |
| costed book, best of 9 GPU configs | Sharpe **+0.020** | BTC buy-and-hold **+0.372** on its span | loses |
| costed book, best of 9 feature rounds (full history) | Sharpe 0.214, CAGR **−9.6%** | BTC 0.476, CAGR **+11.8%** | loses |

`MEASURED HERE`. And the detail that settles it, which I verified myself from `runs/ml/cpu/books.csv`:
**the single-feature book (`−vol_60_xs`, top 10) beats the best model's book at four of six targets**
— ret_1d 0.651 vs 0.284, xsret_1d 0.663 vs 0.422, ret_7d 0.704 vs 0.684, ret_28d 0.617 vs 0.397 —
losing only at xsret_7d (0.699 vs 0.833) and xsret_28d (0.621 vs 0.832). `MEASURED BY ME`

### 0.3 The best honest result, and what it is worth

**The best honest forecast this programme produced is a cross-sectional risk ORDERING, not a return
forecast and not a better volatility forecast.** Concretely: a learned rank of which coins are about
to be dangerous, at AUC 0.60–0.62 on the 7d/−20% flag against a 0.5431 hand-set decile rule, with a
rank IC of +0.106 to +0.204 that nine unrelated model families agree on.

Against buy-and-hold BTC after costs, at 15 bps per side: **nothing beats it.** The closest is a tie
(0.833 vs 0.833 on identical dates), on 309 weekly observations, where the Sharpe standard error is
**±0.412** for both books. The one book that looks like a win — xsret_28d extra_trees top-5 at
Sharpe 0.832 / CAGR +54.4% against BTC's 0.768 / +35.5% — rests on **76 observations** and a 0.064
difference against roughly 0.59 of uncertainty on the difference. It is one seventh of its own error bar.

Against the deflated hurdle, computed with the repo's own `expected_max_sharpe` at the honest
N = 439 selection trials:

| span | baseline | deflation at N=439 | **hurdle** | best measured | shortfall |
|---|---|---|---|---|---|
| 5.93y (CPU weekly) | BTC 0.833 | 1.559 | **2.392** | 0.833 | −1.56 |
| 5.83y (CPU monthly) | BTC 0.768 | 1.572 | **2.340** | 0.832 | −1.51 |
| 4.90y (GPU zoo) | BTC 0.372 | 1.715 | **2.087** | 0.020 | −2.07 |
| 3.25y (feature holdout) | BTC 0.831 | 2.106 | **2.937** | 0.667 | −2.27 |

`MEASURED BY ME` (`runs.features.sampling.expected_max_sharpe(439, T)`). **Nothing clears, and
nothing comes within a factor of three.** The choice of N barely matters: at N=78 the CPU hurdle is
still 2.150.

### 0.4 The sentence the owner should weigh

> Volatility and tail risk are forecastable; the project already forecasts volatility better, on the
> assets it actually trades, than anything the ML programme produced on its own terms. Direction is
> not forecastable at a size that survives 15 bps. The ML work's one genuine increment is a
> cross-sectional *risk ranking* the incumbent does not produce. It is worth wiring in as evidence
> and as a shrink-only risk input. It is not worth wiring in as a return signal, and it does not
> change the growth audit's conclusion that if the objective is maximum growth, the measured answer
> is to hold BTC.

---

## 1. THE SCORECARD

Every row is out-of-sample on purged, embargoed walk-forward folds, uniqueness-weighted, on the
survivorship-free panel (841,175 rows, 747 symbols, 763 segments after discontinuity splitting, 282
dead retained, point-in-time eligibility). Costs 15 bps per side throughout.

### 1.1 Volatility — forward realised vol

| family | target | R² vs train mean | R² vs test mean | Spearman | QLIKE | clears hurdle? |
|---|---|---|---|---|---|---|
| **CatBoost** | vol_7d | **0.1803** | 0.1325 | 0.4837 | 1.493 | n/a (not a Sharpe claim) |
| XGBoost | vol_7d | 0.1587 | 0.1096 | 0.4548 | 1.542 | |
| **HAR(1,5,22) — baseline** | vol_7d | **0.1356** | 0.0852 | 0.4401 | 1.517 | |
| LightGBM | vol_7d | 0.1206 | 0.0693 | 0.4400 | 1.584 | |
| random forest | vol_7d | 0.1069 | 0.0548 | 0.4555 | 1.573 | |
| extra trees | vol_7d | 0.0691 | 0.0148 | 0.4066 | 1.640 | |
| *baseline* EWMA(0.94) | vol_7d | −0.1890 | −0.2584 | 0.4379 | 13.124 | |
| *baseline* trailing RV(30) | vol_7d | −0.2751 | −0.3362 | 0.4425 | **1.369** | |
| pooled GARCH(1,1) | vol_7d | −0.3231 | −0.3733 | 0.3737 | **1.291** | |
| *baseline* trailing RV(7) | vol_7d | −0.5893 | −0.6774 | 0.4157 | 1.845 | |
| ridge (log target) | vol_7d | −1.0323 | −1.1508 | 0.4775 | 1.505 | |
| elastic net | vol_7d | −1.2678 | −1.4000 | 0.4669 | 1.533 | |
| **XGBoost** | vol_28d | **0.1973** | — | — | — | |
| CatBoost / **HAR** | vol_28d | 0.1653 / **0.1638** | — | — | — | |
| `arch` GARCH(1,1), BTC alone, frozen params | fwd vol | 0.0131 | — | corr 0.303 | — | |
| **GPU: xgb_lags** | log fwd 7d RV | **+0.2359** | — | IC +0.5147 (t 54.9) | — | |
| GPU: n-hits / n-beats / tcn / gru | log fwd 7d RV | +0.2110 / +0.1880 / +0.1859 / +0.1781 | | | | |
| GPU: linear probe (103 params) | log fwd 7d RV | +0.1202 | | | | |
| GPU: **PatchTST** | log fwd 7d RV | **−0.0389** | | | | |
| *GPU baseline* trailing 20-bar RV (a model input) | log fwd 7d RV | +0.0216 | | | | |
| **INCUMBENT: shipped blend, BTC/USDT** | fwd 7d vol | **0.5992** rolling 250d | | | | |
| **INCUMBENT: shipped blend, ETH/USDT** | fwd 7d vol | **0.5406** rolling 250d | | | | |

Three honest readings. **(a)** Gradient boosting genuinely beats HAR *on the panel*: +0.045 R² at 7d,
+0.034 at 28d, same folds, baseline in the same table. **(b)** The loss function changes the ranking
— GBDT wins R², GARCH-lite and trailing RV(30) win QLIKE, because QLIKE is scale-free and forgives
the over-prediction R² punishes. Both columns are printed for that reason. **(c)** The last two rows
are on a different target and are not a like-for-like comparison, but they are the incumbent, and the
comparison was never run. See §6 item 1.

The GARCH family is weak here, checked two ways (pooled variance-targeted, and textbook `arch` on BTC
alone with frozen parameters). Linear models on a log vol target blow up: ridge produced an
annualised vol forecast of 946 against a target near 1.0 before fold-local clipping.

### 1.2 Drawdown exceedance

| family | target | Brier skill vs train base | vs **test** base | AUC | top-decile lift | mean abs calib gap |
|---|---|---|---|---|---|---|
| **logistic** | dd_7d_20 | 0.0457 | +0.0094 | **0.6172** | **1.734** | — |
| extra trees | dd_7d_20 | **0.0482** | **+0.0120** | 0.6062 | 1.443 | — |
| random forest | dd_7d_20 | 0.0477 | +0.0115 | 0.6016 | 1.544 | — |
| *baseline* `vol_60_xs` decile lookup | dd_7d_20 | 0.0262 | −0.0108 | 0.5431 | 1.233 | −0.167 to −0.100 |
| *baseline* `age_days` decile lookup | dd_7d_20 | 0.0194 | −0.0179 | 0.5306 | 1.145 | — |
| *baseline* training base rate | dd_7d_20 | 0.0000 | −0.0380 | 0.5000 | — | — |
| LightGBM / CatBoost / XGBoost | dd_7d_20 | **−0.008 / −0.033 / −0.050** | negative | 0.54–0.56 | 1.13–1.26 | **0.18–0.22** |
| random forest | dd_28d_20 | 0.0666 | — | **0.5949** | 1.214 | — |
| *baseline* decile lookup | dd_28d_20 | **0.1037** | — | 0.5576 | 1.064 | — |
| random forest | dd_28d_40 | 0.0220 | — | 0.5724 | — | — |
| everything | dd_7d_40 (2.9% base) | within 0.005 of 0 | — | ~0.50 | — | — |
| GPU zoo, 9 families | dd_7d_20 | — | — | rank IC +0.106…+0.204, t 14–24 | — | skill ≤ +0.021 |

`MEASURED HERE`. Note the split at 28 days: the hand-set decile lookup wins Brier while the model wins
AUC and lift. **The boosted trees are the worst-calibrated models on the rare flags and the best at
ordering.** Any probability use needs isotonic or Platt calibration on a third inner fold, which
nobody fitted.

### 1.3 Returns — the target change that beat every model change

| target | sampling | best model | IC (t) | best baseline | baseline IC (t) | model wins? |
|---|---|---|---|---|---|---|
| ret_1d (level) | 197,606 rows | random forest | 0.0521 (13.7) | `−vol_60_xs` | **0.0791 (14.4)** | no |
| **xsret_1d (rank)** | 195,881 | **extra trees** | **0.0861 (17.3)** | `−vol_60_xs` | 0.0778 (14.1) | **yes** |
| ret_7d (level) | 28,191 | extra trees | 0.0537 (4.9) | 2 features ranked | **0.1221 (8.7)** | no |
| xsret_7d (rank) | 27,935 | extra trees | 0.1035 (7.9) | 2 features ranked | **0.1213 (8.6)** | no |
| ret_28d (level) | 28,023 | random forest | 0.1065 (6.1) | 2 features ranked | **0.1864 (8.1)** | no |
| xsret_28d (rank) | 27,799 | extra trees | 0.1550 (7.2) | 2 features ranked | **0.1799 (7.8)** | no |

**The largest single gain in the entire programme was not a model, it was a target.** Fitting a
within-timestamp *rank* of the forward return instead of its level roughly doubled every family's
rank IC at zero extra compute — extra trees 0.0537 → 0.1035, ridge 0.0430 → 0.0950, CatBoost 0.0491
→ 0.0916. Squared error on a signed forward return spends its capacity on the market factor, which is
57–69% of cross-sectional variance and which the cross-sectional metric gives no credit for.
`MEASURED HERE`

**Cross-sectional directional accuracy** (did this coin beat the median coin, base rate 50% by
construction, Wilson CI): xsret_1d 0.5327 [0.5305, 0.5349] vs baseline 0.5272; xsret_7d 0.5336 vs
0.5401; xsret_28d 0.5510 vs 0.5693. The baseline wins two of three.

**Plain directional accuracy is uninterpretable on this panel and the shuffle control proved it:** a
*globally shuffled* label — no information whatever — scored `dir_acc` 0.6119 at 28 days, because the
median forward return is negative and the predictions were mostly negative. Only the cross-sectional
form is worth reading. `MEASURED HERE`

### 1.4 The costed books — the only economic column

CPU zoo, long-only spot, rebalance grid = label horizon, 15 bps/side:

| target | book | CAGR % | Sharpe | **Sharpe SE** | MaxDD % | turn/yr | cost drag %/yr | n obs |
|---|---|---|---|---|---|---|---|---|
| xsret_7d | extra_trees top-5 | 39.64 | **0.833** | **±0.412** | −81.33 | 21.97 | 3.30 | 309 |
| xsret_7d | **BTC buy-and-hold** | 38.10 | **0.833** | ±0.412 | −76.63 | 0.17 | 0.03 | 309 |
| xsret_7d | `−vol_60_xs` top-10 (1 feature) | 28.38 | 0.699 | ±0.412 | −77.69 | 13.40 | 2.01 | 309 |
| xsret_7d | equal-weight all eligible | −10.95 | 0.296 | ±0.411 | −95.77 | 4.76 | 0.71 | 309 |
| xsret_28d | extra_trees top-5 | **54.38** | 0.832 | ±0.420 | **−63.24** | 6.55 | 0.98 | **76** |
| xsret_28d | BTC buy-and-hold | 35.50 | 0.768 | ±0.419 | −73.38 | 0.17 | 0.03 | 76 |
| ret_7d | extra_trees top-10 | 26.36 | 0.684 | ±0.405 | −82.59 | 50.77 | 7.62 | 319 |
| ret_7d | `−vol_60_xs` top-10 (1 feature) | 28.81 | **0.704** | ±0.405 | −77.69 | 13.14 | 1.97 | 319 |
| ret_1d | random_forest top-20 | −14.17 | 0.284 | ±0.403 | −97.38 | 305.8 | **45.87** | — |
| ret_1d | `−vol_60_xs` top-10 (1 feature) | 23.10 | **0.651** | ±0.403 | −79.98 | 26.14 | 3.92 | — |
| xsret_1d | extra_trees top-5 | 3.53 | 0.415 | ±0.410 | −90.46 | **171.2** | **25.68** | 2177 |
| xsret_1d | BTC buy-and-hold | 41.42 | **0.890** | ±0.410 | −76.63 | 0.17 | 0.03 | 2177 |
| ret_28d | random_forest top-5 | −16.12 | 0.155 | ±0.406 | −95.34 | 15.54 | 2.33 | 79 |

GPU zoo, weekly rebalance, top_k=10, 2021-10-25 → 2026-09-17:

| book | CAGR % | Sharpe | MaxDD % | turn/yr | cost drag %/yr |
|---|---|---|---|---|---|
| **BTC buy-and-hold** | **+6.13** | **+0.372** | **−76.63** | 0.20 | 0.03 |
| tcn `lowest_vol_forecast` (best in the zoo) | −15.30 | +0.020 | −79.01 | 26.1 | 3.92 |
| xgb_bar `lowest_vol_forecast` | −15.75 | −0.000 | −81.66 | 28.0 | 4.20 |
| xgb_lags `topk_forecast` (best top-k) | −32.01 | −0.065 | −90.35 | 60.0 | 9.00 |
| nhits `topk_forecast` (worst) | −54.56 | −0.578 | −98.63 | 84.4 | 12.66 |
| equal-weight eligible | −40.75 | −0.270 | −95.49 | 4.78 | 0.72 |

Four things this says, and the fourth is the one that matters.

1. **The Sharpe standard error is ~0.41 for every CPU book.** Lo's formula collapses to
   `sqrt((1 + SR²/2ppy)/years)`, so six years buys a Sharpe to ±0.41 whether sampled 76 times or
   2,177. Every apparent win in these tables is inside its own error bar.
2. **The 1-day horizon has the strongest signal and is the most completely uninvestable.** Best IC
   in the study (0.0861, t 17.3) and 171× annual turnover — 25.7% of NAV in fees, CAGR 3.5% against
   BTC's 41.4%.
3. **Equal-weight-all-eligible lost 11–41% a year.** Most of what any book earns is avoidance of the
   altcoin universe, which is `growth-audit.md` §1.5's conclusion reached without a model.
4. **The BTC baseline itself ranges 0.372 to 0.890 across the three agents' spans.** No Sharpe in
   this document may be compared with a Sharpe from a different span. The GPU zoo's "+0.020 vs
   +0.372" and the CPU zoo's "0.833 vs 0.833" are not in conflict; they are different five- and
   six-year windows.

One structural result worth keeping, found independently by the GPU zoo: using the vol forecast as an
**exclusion filter** (`lowest_vol_forecast`) beat the same model's own top-k *selector* book in **6 of
9** cases and beat equal-weight in 6 of 9, cutting turnover from 43–84×/yr to 26–49×/yr — about 5
percentage points of annual cost drag. That is `growth-audit.md`'s loser-avoidance finding reproduced
by a learned forecast instead of a hand-set threshold.

### 1.5 Features — breadth did not help

109 features built (76 coin-scope, 33 market-scope), nine group-wise rounds plus a nested round:

| round | n_feat | fold IC | ΔIC | costed Sharpe | CAGR % |
|---|---|---|---|---|---|
| 0 landed base | 20 | **0.13710** | — | 0.193 | −12.3 |
| 1 +ohlcv wide | 74 | 0.13789 | **+0.0008** | 0.001 | −22.4 |
| 2 +micro/liquidity | 85 | 0.13701 | −0.0009 | −0.030 | −23.8 |
| 3 +cross-sectional | 91 | 0.13607 | −0.0009 | 0.055 | −19.9 |
| 4 +derivatives | 96 | **0.14066** | +0.0046 | 0.138 | −14.3 |
| 4i +interactions | 100 | 0.13796 | −0.0027 | 0.072 | −17.6 |
| 5 rank form | 100 | 0.13547 | −0.0025 | 0.014 | −21.2 |
| 6 pruned+stable | 40 | 0.13569 | +0.0002 | −0.006 | −22.4 |
| 7 compact evidence | 10 | 0.12265 | −0.0130 | **0.214** | −9.6 |
| **BTC buy & hold** | — | — | — | **0.476** | **+11.8** |

**Going from 20 features to 74 bought +0.0008 of IC — six times less than the fold-to-fold spread.**
The costed Sharpe is monotone *decreasing* in feature count. The only positive step of size is round
4's funding family (+0.0046), and its headline member `funding_ann` fully reverses sign between
2019-22 (−0.054) and 2025-26 (+0.045), so that gain is not a finding.

A properly nested greedy search — selecting only on rows before 2023-07-01, scored on folds after it —
accepted **exactly one of 76 extended features** and then stopped, because step 2's best gain was
+0.00000:

| set | n_feat | holdout IC | IC t | Sharpe | CAGR % | MaxDD % |
|---|---|---|---|---|---|---|
| landed base (never selected on) | 20 | 0.16529 | 7.68 | 0.464 | +10.45 | −70.5 |
| compact 10 (hand-picked) | 10 | 0.14075 | 4.24 | 0.656 | +24.47 | −55.1 |
| **greedy (base + `corr_btc_90`)** | 21 | **0.16870** | 8.12 | **0.667** | **+27.11** | −61.5 |
| **BTC buy & hold** | — | — | — | **0.831** | **+33.79** | — |

`corr_btc_90` is the one feature that survived everything: univariate IC +0.131 (t 7.80), sign-stable
across all three regimes (+0.139 / +0.088 / +0.162), accepted over ten rivals, and it carried into a
holdout the search never saw. **It still loses to BTC on both Sharpe and CAGR.**

58 of 96 coin features hold their sign across all three eras, and they are about **four distinct
ideas**: volatility (any estimator, any window — `vol_120`, `resid_vol_90`, `semivol_dn_60`,
`rs_vol_20`, `gk_vol_20`, `park_vol_20` all measure the same thing), listing age, illiquidity
(Amihud/Kyle/Roll, mostly size), and correlation to BTC. Regime-*specific* and therefore untradeable:
`has_perp`, `funding_ann`, `mom_1`, `kurt_60`, `dd_runlen_10`, `funding_ann_7`, `tail_ratio_180`.

Two features beyond `growth-audit.md` §1.2's list reproduce in all three regimes: **`corr_btc_90`**
(+0.131, t 7.80 — coins that track BTC more closely do better) and **`avg_trade_size`** (+0.087,
t 4.82). **`resid_vol_90`** (−0.153, t −7.77) is the largest |IC| in the catalogue and a
better-behaved form of `vol_60`.

And the market-scope table confirms the whole thesis independently, scored on BTC's own forward series
with Newey-West errors:

| target | best features | Spearman | NW t |
|---|---|---|---|
| **forward 30d realised vol** | **DVOL** | **+0.623** | **6.31** |
| | `mkt_btc_vol_60` | +0.513 | 6.10 |
| | `vrp_20` | +0.283 | 3.64 |
| forward 30d −20% drawdown | `vrp_20` / `mkt_btc_vol_60` / `oi_price_div_7` / `oi_chg_7` | +0.161 / +0.164 / +0.102 / +0.070 | 2.34 / 2.20 / 2.15 / 2.11 |
| **forward 30d return** | **2 of 33 features reach \|t\| > 2**; everything else \|t\| < 1.6 | ≤0.191 | ≤2.11 |

### 1.6 Validation controls — all fired, and two are informative

| control | result |
|---|---|
| `assert_no_lookahead`, 32+35 feature columns on real data | **max_abs_diff exactly 0.0**, all ok; the test suite plants four bugs (shift(−1), full-sample z-score, centred window, backfill) and requires it to raise |
| `assert_no_leakage`, real weekly panel, all folds, before any fit | passed; label_overlap 0, train-inside-test 0, in-embargo 0; purge bit **19–628 rows per fold** |
| **global label shuffle (pure null)** | return IC −0.0016 to −0.0055 (\|t\| ≤ 0.77); vol R² −0.077/−0.082; dd Brier skill −0.0008 to −0.0115, AUC 0.496–0.501. **No leak anywhere.** |
| **within-timestamp shuffle** | xsret_1d IC 0.0861 → **0.0007** (the return edge is *entirely* coin selection); vol Spearman 0.4837 → **0.2545** (half the vol edge is knowing which week is dangerous for everything); dd AUC 0.6062 → 0.5294 (~30% is market timing) |
| LUNA symbol reuse | detected at **177,400×**, split; 39 breaks across 32 symbols — **8 more non-leveraged names than `growth-audit.md` found**, all *downward* redenominations (VEN, QUICK, SUN, BNX, VIDT, STRAX) that fabricate catastrophic losses and make an exclusion rule look better than it is |
| dead coins | 282 of 763 segments retained |
| **adversarial "which half of the sample is this row in"** | AUC **0.9993** on all features, **0.9499** on cross-sectional ranks alone, 0.9991 with `age_days` removed. **Every fold is an extrapolation.** |
| `age_days` correlation with the clock | Spearman **+0.415**, and it ranks 5th–7th in the winners' permutation importance |
| cross-model agreement (9 GPU models, 145,802 aligned rows) | return median pairwise Spearman **0.271**; **volatility 0.815**; drawdown 0.500. Noise-fitters disagree; signal-fitters agree. |

The permutation-importance fix is worth recording as method. Run on the winning round's full 96
correlated columns, permutation reported **every extended feature at ~0.000** — because shuffling
`vol_60` while five near-duplicates remain measures nothing. Pruning to 67 columns at ρ 0.85 *first*
surfaced 11 features whose shuffle hurt in **100%** of (fold, repeat) pairs. Both tables were kept so
the difference is inspectable. Read `folds_worse`, not `drop_mean`: `tail_ratio_180` and
`vol_ratio_60_250` have large means at `folds_worse` 0.44, which is a coin flip.

### 1.7 Hardware, verified

| quantity | measured | correction to the brief |
|---|---|---|
| GPU | Quadro RTX 3000, capability **7.5** (Turing), 30 SMs, driver 580.92 | **no bf16 and no tf32** — fp32 or fp16 AMP with a GradScaler only |
| VRAM | 6,143 MiB total, **5,105 MiB free** | the real ceiling is ~5.0 GB, not 6 |
| torch | 2.14.0+cu126, `cuda_available: true` | verified twice, independently |
| RAM | **31 GB visible to WSL**, not 64 | WSL defaults to half the host |
| CPU | 12 logical = **6 physical** (Xeon W-10855M) | LightGBM at `n_jobs=12` is **8.33s vs 1.76s at 4** — five times slower |
| resident dataset in VRAM | **73.7 MiB** (841k × 20 fp32 flat panel) | 1.4% of free VRAM |
| peak training VRAM | PatchTST 2,965 · LSTM 1,690 · GRU 1,353 · TCN 689 · N-HiTS 218 · linear 215 MiB | **6 GB did not force a single design choice** |

Two ceiling findings the brief did not anticipate. **This host does not OOM**: PatchTST at
`seq_len=512` reported `peak_alloc 9,090 MiB` on a 6,143 MiB card *without raising*, because WSL2
serves the overflow from host RAM over PCIe — step time went 188 ms → **4,639 ms, a 25× slowdown, no
error**. A sizing rule written against `OutOfMemoryError` would never fire. And **a kernel limit binds
before VRAM does**: PatchTST is channel-independent so its effective attention batch is `B × F`, and
torch's efficient-attention kernel refuses a batch above 65,535, capping `batch_size` at 3,276 at 20
features. Batch 4,096 fails with `RuntimeError`, not OOM.

**GBDT on GPU is not a speed win.** Identical folds and features: 20 columns — CPU 4.27s, CUDA 5.36s
(GPU **26% slower**); 80 columns — CPU 10.79s, CUDA 7.25s (1.49×). And the two devices produce
*different models*, not just different speeds: fold-3 Pearson(pred, actual) was 0.0416 on CPU and
0.0744 on CUDA, because the hist binning differs. **`device=` is a model choice and counts as a
trial.**

### 1.8 Which results clear the deflated hurdle

**None of the strategy claims.** §0.3 has the arithmetic: the hurdle is 2.087–2.937 depending on
span; the best measured book is 0.833.

The results that *do* stand are the ones a Sharpe is the wrong test for, and `growth-audit.md` §4.3's
evidence-class rule says so explicitly. Cross-sectional structure — the volatility ordering, the
drawdown ordering, the four stable feature families, the direction-is-noise finding — is judged on
sample size, monotonicity and regime stability, not on a Sharpe:

| claim | evidence class | judged how | verdict |
|---|---|---|---|
| GBDT > HAR on panel forward RV | cross-sectional structure | +0.045 R², same folds, baseline in table, DM not run | **holds, panel only** |
| drawdown *ordering* AUC 0.60–0.62 > 0.5431 | cross-sectional structure | 9 independent families agree, t 14–24, shuffle collapses it | **holds** |
| drawdown *probability* | calibration | Brier skill ≤ +0.012 vs test base, gaps 0.18–0.22 | **fails** |
| `corr_btc_90` | forward feature | nested selection, 3-regime sign stability, holdout carry | **holds** |
| any costed book | backtested rule | deflated hurdle | **fails, by ≥1.5 Sharpe** |
| "MAPE near 1" | — | persistence | **met before we started; meaningless** |

**Total trial count: 439 selection trials, 288 all-time measurements** on the shared counter
(`~/earn-ml-data/cache/ml_trial_counter.json`), composed of 177 inherited from `growth-audit.md`, 207
registered by the CPU zoo (78 distinct target×family configurations plus 129 pre-final evaluations
that happened and changed the design), 10 from the GPU zoo, 50+3 from the feature study, and 4 from
an interrupted run. Inner-fold grid points (258 in the final CPU run alone) are *screening* trials
that do not raise the hurdle, because `tune` builds its inner split from training rows only and a test
asserts the inner validation indices are disjoint from the outer test set.

Two multiple-testing costs the deflation does **not** capture, and both are recorded rather than
excused. The inner grids were tuned on the metric the result is reported on, so the reported metric is
the one the search optimised, and the formula assumes independent trials rather than a hill-climb. And
the cross-sectional-rank target was adopted *after* seeing a test-fold IC — a researcher degree of
freedom no counter can charge for. The only available remedy is that the level-target rows are in the
same scorecard and they are all worse.

---

## 2. WHAT TO KEEP

The smallest set that earns its keep. Retraining cost is measured on this machine.

### 2.1 Keep — four things

| # | keep | why it earns its place | retrain cost |
|---|---|---|---|
| **K1** | **The harness**: `ml/data.py`, `labels.py`, `splits.py`, `features.py`, `metrics.py`, `registry.py`, `baselines.py` | It is the reason the rest of this document can be trusted. Point-in-time builder over 841k rows in 2.5s; `assert_no_lookahead` and `assert_no_leakage` that *raise* and are themselves tested against planted bugs; purge that bites 19–628 rows per fold; uniqueness weighting; the persistence baseline printed first; cache 20.5s cold → **0.02s warm (1243×)**; byte-identical report across runs | none — it is infrastructure |
| **K2** | **One GBDT risk model**: XGBoost or CatBoost on `dd_7d_20` and `vol_7d`, cross-sectional, **ranking output only** | The only genuinely new forecast: AUC 0.6172 vs a 0.5431 hand-set rule, rank IC +0.106…+0.204 agreed by 9 unrelated families, shuffle-controlled. Produces something the incumbent does not | **XGBoost across all folds and heads: 5.4–7.2s.** With the cached panel, a nightly retrain is **~30 seconds** |
| **K3** | **`corr_btc_90` and `resid_vol_90`** as features, and the **within-timestamp rank target** as the default construction | One feature survived a nested search out of 76. The rank target doubled every family's IC for free. Both are cheap and both were properly out-of-sample | trivial |
| **K4** | **`runs/features/volatility.py` exactly as it is** — the incumbent | BTC 0.5992 / ETH 0.5406 rolling 250d OOS R², live, today, with a health floor and a documented degraded fallback. Nothing in the ML programme was shown to beat it on its own target | already scheduled |

### 2.2 Discard — with the measurement

| discard | measurement |
|---|---|
| **All seven deep sequence families** (GRU, LSTM, TCN, PatchTST, N-BEATS, N-HiTS, linear probe) | Every one scores **negative** R² against a zero return forecast (−0.057 to −0.163) while both XGBoost configs are positive; best deep vol R² 0.2110 vs xgb 0.2359; at **4× to 33×** the wall-clock. Early stopping fired at **5–11 epochs** of a 25–40 budget — the information runs out long before the capacity does |
| **PatchTST specifically** | Most expensive model in the zoo (236.2s, 2,965 MiB, 33× xgb_bar) and **worst on every single metric**: r2_ret −0.163, vol R² −0.0389, Brier skill −0.0443, return MAPE 263% |
| **GARCH as a primary vol model** | −0.323 at 7d, −0.423 at 28d pooled; 0.0131 on BTC alone with frozen params |
| **Linear/ridge/elastic-net on a log vol target** | ridge R² −1.03 after fold-local clipping, −1.96e6 before |
| **Every return-level book** | Best is a tie with BTC inside a ±0.41 error bar; four of six single-feature books beat the model's book |
| **The 1-day horizon** | 171×/yr turnover = 25.7% of NAV in fees; CAGR 3.5% vs BTC 41.4% |
| **The vol-head ensemble** | IC 0.5228 vs xgb_lags 0.5153 but **R² 0.2298 vs 0.2462** — loses on the metric that matters. Deep-7-only ensemble worse still |
| **89 of 109 features** | +0.0008 IC for 54 features; Sharpe monotone decreasing in feature count |
| **GBDT on GPU** | 26% slower than 12 CPU threads at 20 columns |
| **Drawdown probabilities as probabilities** | Brier skill ≤ +0.012 vs the test base rate; GBDT calibration gaps 0.18–0.22 |
| **`dd_7d_40`** | Every model within 0.005 of zero skill at a 2.9% base rate |
| **`age_inv_vol`** despite being the strongest univariate feature on three targets | It was the **worst** of 11 greedy candidates when added to the base: inner IC 0.19508 → **0.17583**. Its univariate strength was entirely its two components, both already in the base. It is a screen, not a model input |

---

## 3. INTEGRATION — evidence, never an oracle

The design rule, stated once: **a forecast may inform a decision and may shrink risk. It may never
place an order, never raise exposure, and never be the reason a limit moves.** Claude is already out
of the order path; a model file must be too.

### 3.1 Four properties, each with the mechanism that enforces it

| property | mechanism |
|---|---|
| It cannot trade | The forecast writes a state file. `strategies/riskgate.py`'s 17 checks do not read it and must not. The path from forecast to order runs through the decide stage, the proposal schema and the gate, exactly as a model's opinion does today |
| It can only ever shrink | Its one mechanical authority is a multiplier on `exposure_scale`, clipped to `[floor, 1.0]` — the same invariant `runs/features/volatility.py: vol_target_scalar` already has, whose docstring says "may shrink a position and may never grow one, so a stuck or absent input degrades to *no change*, never to *add*" |
| It must say how healthy it is | Every published number is accompanied by a rolling out-of-sample score and is **withheld** below a floor, with a cautious fallback — the `MIN_OOS_R2 = 0.05` / `trailing_30d_fallback` contract the incumbent already uses, which I watched refuse 5 of 31 pairs today |
| It is graded against its own realised outcome | Every live prediction is journalled with its as-of time, horizon and target, and a resolver writes the realised outcome when the horizon closes. Its weight moves on that record and on nothing else |

### 3.2 Where it plugs in — five named places

**(1) Computation and publication — `runs/features/` + a skill script. Tier 2 for the module, tier 2
for the script.**

Add `runs/features/forecast.py` beside `volatility.py`, with the same shape: pure pandas/numpy, no
`ops` import, a frozen dataclass with an `as_dict()`, and a refusal path. It loads the trained
artefact and emits, per asset:

```
risk_rank        cross-sectional percentile of P(drawdown) — a RANK, never a probability
risk_decile      1-10, the consumable form
auc_250d         rolling 250-day out-of-sample AUC of this model on this asset class
n_obs_250d       how many resolved observations that AUC rests on
model_version    content hash of the artefact
trained_through  the last date in the training sample
as_of            the last closed bar
degraded         true when auc_250d is below the floor
refused          the reason, when there is one
```

Register every key in `runs/features/__init__.py: REGISTRY` with `_spec(...)`, source, cadence and
`max_lag_min` — that registry is the audited place that says where every number came from, and an
unregistered key is how a three-day-stale number re-enters a prompt as if it were current. A skill
script (`.claude/skills/<name>/scripts/`) writes `knowledge/state/forecast.json` with
`write_json_atomic`, exactly as `vol-surface` writes `volsurface.json`, and records its source
through `ops.lib.freshness.record` so the existing staleness plumbing covers it for free.

**Deliberately NOT emitted: a return forecast, a price, and a probability.** §1.3 says the return
signal loses to two features; §1.2 says the probabilities are uncalibrated. Emitting them would be
handing the decide stage a number this document has shown to be worse than what it already has.

**(2) The decide prompt evidence block — `prompts/research.v5.md`, tier 1.**

A new section, styled on the existing VALIDATED SIGNAL block whose framing is already right:

```markdown
### Forecast (deterministic; a cross-sectional RISK RANK, not a return forecast)

Computed by Python from the panel, out of sample, with its own rolling health score beside
it. It ranks which assets are likely to be dangerous over the horizon. It does NOT forecast
return, price or direction — measured, its directional edge loses to two features already in
your inputs, and its drawdown PROBABILITIES are not calibrated. Treat the ranking as
evidence and the levels as absent. `degraded: true` or a missing block means ignore it.

```json
{{FORECAST}}
```
```

Plumbing, all tier 2: `INPUT_BUDGETS["forecast"] = 600` in `runs/build_prompt.py`; a line in
`gather_inputs` reading `knowledge/state/forecast.json`; `.replace("{{FORECAST}}", inputs.get("forecast") or "null")`
in `build`; `"forecast": _read("forecast.json")` in `build_from_snapshot`; and `evals/snapshot.py`
writing `forecast.json` into every decision snapshot. **The last one is not optional** — a decision
that cannot be replayed byte-identically is a decision that cannot be graded, and the whole
integration rests on grading.

Then one line in the `decide` skill body and `prompts/research.v4.md`'s checklist (both tier 1, kept
in sync by `strategy-lab`): at step 7, `exposure_scale` may be reduced by the forecast's risk rank
and may not be raised by it.

**(3) The panel — `runs/llm/panel.py`. A deliberate design refusal.**

**The forecast does not vote, and the reason is worth stating rather than hiding.** The panel
aggregates one thing, a categorical verdict, by counting. Turning a continuous rank into a verdict
needs a threshold; a threshold is a fitted parameter; fitting it is another selection trial against a
hurdle already 1.5 Sharpe out of reach. And a deterministic voter would corrupt the panel's actual
purpose, which is measuring whether a cheap model row can be trusted alone — a vote that is always
right by construction makes every `disagreement_rates` number unreadable.

What it does instead, and this is the honest reading of "a weight in the panel":

- **It is in every pass's inputs.** All passes see the identical forecast block, so a pass that
  contradicts it does so knowingly, and `journal/snapshots/panels/<month>.jsonl` records the
  forecast's content hash beside each pass. That makes "did the passes that agreed with the forecast
  do better" a query rather than an argument.
- **It can force escalation, never suppress it.** In `runs/decision_core.py`, after `run_panel`
  returns: if the panel's proposal raises gross exposure while the forecast's risk rank for a named
  asset is in the top decile *and* the model is not degraded, the adjudicator pass runs. The panel
  already has that machinery; this only adds a trigger. It can spend money on more scrutiny and can
  never save money by skipping it.
- **Its only numeric authority is shrink-only.** `exposure_scale` is multiplied by a ladder value in
  `[floor, 1.0]`, journalled as a clamp with the forecast version that caused it. A benign forecast
  multiplies by 1.0 — it cannot add.

That asymmetry is the whole of the accountability design. A forecast that is wrong in the dangerous
direction costs opportunity; it cannot cost capital.

**(4) Journalling predictions with their realised outcomes — `ops/sql/migrations/006_journal.sql`,
`SCHEMA_VERSION` 5 → 6. Tier 2.**

Two tables, modelled on `decision_grades`'s shape (predictions immutable, outcome columns NULL until
the horizon resolves):

```sql
CREATE TABLE IF NOT EXISTS forecast_predictions (
  pred_id        TEXT PRIMARY KEY,        -- sha256(model_version|asset|as_of|horizon|target)
  as_of          TEXT NOT NULL,           -- the last CLOSED bar, UTC ISO-8601 Z
  published_at   TEXT NOT NULL,           -- when the state file was written
  model_version  TEXT NOT NULL,
  trained_through TEXT NOT NULL,          -- so a leak is visible as a row, not a rumour
  asset          TEXT NOT NULL,
  target         TEXT NOT NULL,           -- 'dd_7d_20' | 'vol_7d' | ...
  horizon_days   INTEGER NOT NULL,
  risk_rank      REAL,                    -- 0-1 cross-sectional percentile
  risk_decile    INTEGER,
  auc_250d       REAL,                    -- the model's OWN claim about itself, at publish time
  degraded       INTEGER NOT NULL DEFAULT 0,
  run_id         TEXT,                    -- the decision run that consumed it, NULL if none
  consumed       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS forecast_grades (
  pred_id        TEXT PRIMARY KEY REFERENCES forecast_predictions(pred_id),
  resolved_at    TEXT NOT NULL,
  realised       REAL NOT NULL,           -- realised vol, or the 0/1 drawdown outcome
  outcome        TEXT NOT NULL CHECK (outcome IN ('hit','miss','na')),
  baseline_pred  REAL NOT NULL,           -- the incumbent's prediction for the same (asset, as_of)
  baseline_outcome TEXT NOT NULL CHECK (baseline_outcome IN ('hit','miss','na')),
  resolver       TEXT NOT NULL            -- the job that wrote this row; never a model
);
```

Three properties that matter more than the columns. **The resolver is Python**, invoked by a cron job
with `flock`, and a model never writes a grade — `growth-audit.md` §3.3's rule. **`baseline_pred` and
`baseline_outcome` are mandatory**, so every grade is a paired comparison and "the forecast was right"
can never be recorded without "and so was the thing it replaces". And **`consumed` plus `run_id`**
make the counterfactual answerable: a prediction published but never used is still graded, which is
how a shadow arm works at all.

**(5) The console — a page under `console/routers/`, tier 2.** The numbers the owner needs at a
glance are the rolling AUC against its baseline, the fraction of predictions where the model and the
incumbent disagreed, the realised hit rate on those disagreements, the current weight and the last
time it moved, and the clamp log. One line in `docs/contracts.md` §9.6 per endpoint.

### 3.3 How the weight moves — on measured hit rate, and constrained by what the audit already measured

`growth-audit.md` §4.2 and §4.3 set the rules and they bind here. Four of them:

1. **Walk-forward best-picking is banned**, because it measured worse than no selection: best-of-15 by
   trailing 3-year Sharpe gave 1.22 where equal-weighting all 15 gave 1.31, and the 2022 pick had an
   in-sample Sharpe of 2.22 and returned **−1.00** out of sample. So the weight is **not** "use the
   model when it has been hot". It is a monotone function of a *long-window* out-of-sample score with
   a floor and a ceiling.
2. **The hit rate is defined against a named baseline**, and for a risk ranking that baseline is the
   `vol_60_xs` decile rule at AUC 0.5431 — not 0.5000. A model that reaches 0.56 has not earned
   anything; it has been beaten by one feature.
3. **The trial counter is cumulative and persisted.** N only grows. Which brings the defect in §7.1.
4. **Evidence class, not just score.** The ranking is cross-sectional structure and is judged on
   AUC, regime stability and the shuffle control. Any *strategy* claim built on it is a backtested
   rule and owes the deflated hurdle.

The ladder, pre-registered before the first live prediction so it cannot be tuned after the fact:

| rolling 250-obs AUC vs baseline 0.5431 | state | `exposure_scale` multiplier for a top-decile risk rank |
|---|---|---|
| no resolved record yet, or n_obs < 100 | **shadow** | **1.0** — published, journalled, consumes nothing |
| below 0.5431 | **refused** | 1.0, `degraded: true`, and the reason printed |
| 0.5431 – 0.58 | observe | 1.0 |
| 0.58 – 0.62 | **live, minimum weight** | 0.90 |
| above 0.62 | live | 0.80 |

The ceiling of 0.80 is not modesty, it is the measurement: the best AUC anywhere in this programme is
0.6172 on a *fitted* fold, live AUC on a new regime will be lower, and `exposure_scale` already moves
0.5–1.0 on vol regime under the decide checklist's step 7. A second input allowed to halve exposure
on top of that would let two correlated signals compound into a position the risk framework never
sanctioned.

**Demotion is automatic and promotion is not.** Falling below the band demotes on the next resolver
run, with no human in the loop, because that direction is always safe. Rising into a band raises a
`changes/*.json` proposal for a human, because that direction spends capital.

---

## 4. THE COMPARISON THE OWNER ASKED FOR

*"Once ml is built we test it along with our existing decision making to see how to integrate."*
Here is the experiment, and the honest answer about how long it takes.

### 4.1 Design — parallel shadow forecasts on live decisions, graded after the fact

**Arm A (control):** the decision path exactly as it is today. This is the live sleeve.

**Arm B (shadow):** the identical run — same `run_id`, same snapshot, same panel spec, same seeds —
with the `{{FORECAST}}` block present. Runs immediately after A, through the existing shadow
machinery (`proposals/shadow/`, `runs/research_run.py`'s `shadow` stage, which already has a 240s
deadline in `config/earn.yaml: research.stage_deadlines_s`). Arm B **never reaches the gate.**

The paired structure is the point. Both arms see byte-identical inputs but one block, so the
difference between them is attributable to the forecast and nothing else. Three things get measured,
in increasing order of how long they take:

| # | question | metric | resolves in |
|---|---|---|---|
| **M1** | Is the forecast itself any good live? | rolling AUC on `forecast_grades`, paired against `baseline_outcome` | **one quarter** |
| **M2** | Does it change decisions, and how? | divergence rate; a signed taxonomy of divergences (cut exposure / changed asset / changed module / abstained) | **one month** |
| **M3** | Does it make decisions better? | paired difference in `outcome_vs_btc_bps` over divergent decisions only | **see below** |

M2 is the one that must be read first, because it has a failure mode worth naming: **if the divergence
rate is near zero the forecast is decorative**, and if it is near one the model is deferring to it,
which is the oracle failure the whole design exists to prevent. Either extreme is a reason to stop,
and neither needs an outcome.

### 4.2 How long until it could answer — computed, not asserted

**M1, the forecast's own accuracy: one quarter, honestly.** Non-overlapping 7-day windows give 52
independent observations per asset per year. The eligible universe is wider than BTC/ETH for a
cross-sectional ranking, so the rank IC accumulates per cross-section rather than per asset: 52
weekly cross-sections a year, each with 20–30 names. An AUC of 0.60 against a 0.5431 baseline, paired
on the same cross-sections, is measurable to a useful precision inside ~40 cross-sections. **This is
the fast, answerable question and it should be the gate on everything else.** `MEASURED BY ME`: at
n=104 independent 7-day observations the approximate standard error on an R² of 0.39 is ±0.075, and at
n=416 it is ±0.037 — so distinguishing 0.39 from 0.45 needs about four years, but distinguishing 0.39
from "below the 0.05 floor" needs about one quarter.

**M2, divergence: one month.** Two research slots a day (`research.slots: ["08:30", "16:00"]`) is
**730 runs a year**. A divergence rate is a proportion; 60 runs pins a 20% rate to roughly ±10
percentage points, which is enough to tell "decorative" from "deferring".

**M3, decision quality: it will not answer, and this is the part to be blunt about.** `MEASURED BY ME`
(`runs.features.sampling.years_to_detect`, two-sided α 0.05, power 0.80):

| comparison | independent tracks | **paired** |
|---|---|---|
| Sharpe 1.14 vs 0.83 | 244.6 years | **134.7 years** |
| Sharpe 1.00 vs 0.83 | 772.5 years | 407.4 years |
| Sharpe 0.95 vs 0.83 | 1,523.8 years | 791.0 years |
| Sharpe 1.25 vs 0.83 | 139.1 years | 79.3 years |

The paired design genuinely halves it, which is the reason to run the arms on identical inputs rather
than as two independent sleeves. It halves it to **135 years**.

A per-decision paired test does better, because it throws away the common variance and counts only
divergent decisions. `MEASURED BY ME`, at power 0.80:

| per-decision SD of the difference | effect size to detect | divergent decisions needed |
|---|---|---|
| 600 bps | 200 bps | **71** |
| 600 bps | 100 bps | 283 |
| 600 bps | 50 bps | 1,130 |
| 300 bps | 100 bps | **71** |
| 300 bps | 50 bps | 283 |
| 300 bps | 25 bps | 1,130 |

At 730 runs a year and a 20% divergence rate that is **146 divergent decisions a year**. So:

- **a 200 bps per-decision effect is detectable in about six months.** An effect that large would be
  visible in the NAV curve anyway.
- **a 100 bps effect takes about two years.**
- **a 50 bps effect takes about eight years, and that is the realistic size.**

**The honest answer to the owner's question, therefore: the experiment can tell you within a quarter
whether the forecast is any good, and within a month whether it is changing anything. It cannot tell
you whether it improves returns — not in a year, not in five, and the arithmetic is in the table
above.** The decision to keep it must rest on M1 and M2 plus the shrink-only asymmetry, which is a
bet that a measurably better risk ranking with no upside authority cannot hurt. That is a defensible
bet. It is not the same thing as evidence that it helps, and it should never be reported as though it
were.

One consequence for the build order: `modes.live.min_test_days` proves the plumbing and nothing about
edge, and the same is true here. A good quarter is not validation. The 135-year figure should be
attached to the console page beside the forecast's own hit rate, for the same reason it is attached to
every post-mortem.

---

## 5. WHAT THIS CANNOT DO, AND THE MAINTENANCE BURDEN

### 5.1 Hard limits, each with the measurement

1. **The effective sample size is bracketed 250× wide and nobody knows where in it the truth is.**
   `ret_7d`: n = 835,834 rows, effective n **415** panel-wide and **105,147** per-symbol. The GPU
   zoo: 192,598 samples → **24,779** at symbol scope and **373** at panel scope. Every t-statistic and
   every Wilson CI in this document is computed on the raw overlapping count. At the symbol-scope end
   a 51.3% cross-sectional hit rate is ~4σ; at the panel-scope end it is ~0.5σ and not significant at
   all. `coverage()` prints both columns rather than choosing, and a reader who takes
   `beats_coinflip: True` at face value is reading the flattering end.
2. **Every fold is an extrapolation, not an interpolation.** Adversarial AUC 0.9993 on all features and
   0.9499 on cross-sectional ranks alone. The walk-forward result is a statement about drift as much as
   about skill.
3. **`age_days` is partly a clock and it is in the winners' top features.** Spearman +0.415 with the
   timestamp, 5th–7th in permutation importance. A model cannot tell "older coins do better" from
   "later dates did better", and neither can this document.
4. **Three regimes in nine years.** The GPU zoo got **3 usable folds of 5** (`min_train=5,000`), so its
   out-of-sample starts 2021-10-25. Three folds is three observations of "did this generalise".
5. **Intraday is survivor-biased and cannot be fixed.** Only 108 currently-listed pairs have feather
   candles; the 282 dead symbols have daily bars only. Every 1h/4h cross-sectional result would be a
   study of survivors. BTC/ETH intraday is fine because there is no cross-section to bias. **Nothing
   in this programme addressed the 1h horizon, where 79,661 BTC/ETH bars sit unused** — and that is
   precisely where the microstructure literature's case for sequence models is strongest.
6. **Funding was OFF in the GPU zoo.** Turning it on drops every sample with no perp and everything
   before 2019-09. The project's own measurement is that funding buckets move P(7d dd < −8%) from
   **17.0% to 41.4%** monotonically, so the drawdown head reported here is the funding-*free* version
   and is probably weaker than it needs to be. **This is the single most likely source of a real
   improvement and it is untested.**
7. **No cross-sectional implied volatility exists.** DVOL is BTC-only (ETH from 2023-12-27), so the
   strongest vol predictor the project owns (ρ +0.623, t 6.31) cannot reach the other ~390 names.
8. **Per-coin open interest does not exist.** BTCUSDT from 2020-09, ETHUSDT from 2021-12, nothing else.
   The largest single data gap, unchanged from `growth-audit.md`.
9. **Sentiment cannot be made point-in-time on this host.** `knowledge/news/` holds a `.gitkeep`;
   `knowledge/briefs/` holds one file dated today. Any sentiment feature would be fitted on an archive
   assembled after the fact — look-ahead with a friendly face. Order-book imbalance is not downloaded;
   futures basis has no source (`funding.parquet`'s `markPrice` is empty).
10. **Nothing here went through Freqtrade.** Every costed book is a vectorised panel with a one-bar
    shift and 15 bps per side. It ignores minimum notionals, step sizes, the 17 gate checks, limit fill
    risk and the monthly fee budget. Any Sharpe would move once real execution applies, **and the
    direction of that move is down.**
11. **The two drawdown conventions in this project are not comparable.** The ML harness measures
    drawdown on the **intrabar low** (P(90d dd < −40%) = 49.05%); `growth-audit.md` appears to use
    closes (41.2%). Nobody reconciled them. **The two documents' drawdown tables must not be quoted
    side by side.**
12. **The default universe is the live one, not the measured-optimum one.** `min_listing_age_days` stays
    at 180 because that is what the bots trade, while the audit measured the real break at ~2 years.
    `age_days` is emitted so a model can find the break itself.
13. **M6's central finding stands unrefuted by anything here:** of ~hundreds of teams over 48 weeks,
    23.3% beat the forecasting benchmark, 28.8% beat on investment, **6.7% did both, and the
    correlation between forecasting accuracy and investment return was zero** `CITED` (search snippet;
    the paper itself could not be fetched — §7.2). A better AUC that changes no decision the gate makes
    is a null result and must be reported as one.

### 5.2 Retraining cadence and cost

| what | cost on this machine | cadence | why that cadence |
|---|---|---|---|
| Panel build | 23.9s cold, **0.02s warm** (cached) | daily, with the existing candle ingest | it is the input to everything |
| K2 retrain (XGBoost, all folds, all heads) | **5.4–7.2s**; **~30s** including the panel | **monthly**, not nightly | a nightly refit is a nightly selection event, and the counter would grow 365/yr for no measured gain |
| Health resolver (`forecast_grades`) | seconds | daily, `flock` + `timeout` + `ops/envwrap.sh` | the weight ladder reads it |
| Full re-audit (all families, the scorecard) | ~1,072s weekly/monthly + 3,188s daily targets under contention | **quarterly**, hooked to `edge-audit`'s existing re-test | matches the decay re-test already in the system |
| The deep zoo | 621s for 10.4 min end to end | **never again** | it lost on every head |

Monthly, not nightly, is a deliberate choice against the owner's "keep iterating". Every refit that
*chooses* something is a trial; the counter is at 439 and the hurdle is already 1.5 Sharpe out of
reach. Averaging beats choosing here (`growth-audit.md` §4.3: 1.31 vs 1.22), and averaging owes no
deflation because it selects nothing.

### 5.3 How we will know it has stopped working — four triggers, each with a threshold

| # | trigger | threshold | action |
|---|---|---|---|
| **D1** | Rolling 250-observation AUC falls below the `vol_60_xs` baseline | AUC < 0.5431 | automatic demotion to `refused`, multiplier → 1.0, `degraded: true`. No human needed; this direction is always safe |
| **D2** | Divergence rate collapses or saturates | < 5% or > 60% of runs | stop and investigate. Decorative, or the model is deferring |
| **D3** | Calibration drift on the *rank*, not the probability: top-decile lift falls to the base rate | lift < 1.10 | demote; the ordering is the only thing being used |
| **D4** | The cross-section becomes distinguishable — the one change that would make coin selection worth paying for | sustained fall in **PC1 share (69.0% and rising)** with a rise in median two-factor alpha | **re-open the return question.** Until then, do not try |

D4 is `growth-audit.md` §4.4's re-test and it is the same hook. The four numbers to log weekly are
already specified there: median coin R² on BTC + alt factor (0.54–0.62), PC1 share (69.0%), rolling
90d alt-minus-BTC (−31 log points), down-beta minus up-beta (1.38 vs 0.86).

And one trigger in the other direction, because a monitoring rule that can only turn things off is not
honest either: **if the incumbent vol blend's own rolling OOS R² falls below its floor on BTC or ETH**
— which I watched happen to 3 of 31 pairs today — the ML model's cross-sectional ranking is the
natural fallback for the assets it covers, and that is a case for promotion that should be stated in
advance rather than discovered in an incident.

---

## 6. BUILD LIST, in priority order

| # | item | tier | why it is here |
|---|---|---|---|
| **1** | **The missing experiment: one GBDT on BTC/ETH forward 7d vol, on `runs/features/volatility.py`'s own target, universe, benchmark and rolling window, against the shipped blend at 0.5992 / 0.5406, with Diebold-Mariano.** | 2 (`ml/`, `runs/`) | This is the only comparison that could justify touching the live sizing path, and it was never run. If boosting does not clear the blend, the answer is "keep the blend" and the ML vol work ships as a cross-sectional risk rank only — which is what §2 already assumes. **Pre-register the config count.** |
| **2** | Fix `ml/registry.py: Trials.add` — re-read under a lock, or append history as JSONL with `O_APPEND` and derive the counts | 2 | §7.1. The one mechanism the project relies on to stop fooling itself is silently lossy |
| **3** | Funding ablation on the drawdown head | 2 (`ml/`) | The project's own strongest tail-risk measurement (17.0% → 41.4%) was switched off. Most likely real improvement available |
| **4** | Isotonic or Platt calibration on a third inner fold for the drawdown head | 2 (`ml/`) | Only needed if a *probability* is ever wanted. Until then the ranking is enough and this can wait |
| **5** | `runs/features/forecast.py` + registry specs + the skill script writing `knowledge/state/forecast.json` | 2 | §3.2(1) |
| **6** | `ops/sql/migrations/006_journal.sql`, `SCHEMA_VERSION` 5 → 6, and the daily resolver job | 2 | §3.2(4). Nothing else can be graded until this exists |
| **7** | `prompts/research.v5.md` with `{{FORECAST}}`, plus the `build_prompt.py` / `snapshot.py` plumbing and the `decide` skill line | 1 prompt / 2 plumbing | §3.2(2). The snapshot write is not optional |
| **8** | The shadow arm in `runs/research_run.py` and the divergence taxonomy | 2 | §4.1. M2 answers in a month and gates everything after it |
| **9** | The console page and the `docs/contracts.md` §9.6 lines | 2 | §3.2(5) |
| **10** | Reconcile the two drawdown conventions (intrabar low vs close) and state one | 2 | §5.1 item 11. Two incomparable tables in two design docs is a trap for a future reader |
| **11** | The `exposure_scale` clamp in `runs/decision_core.py` and the escalation trigger | 2 | §3.2(3). **Last**, because it is the only item that can move a real position |

Item 11 is deliberately last. Everything before it is measurement and plumbing; only that one changes
what the system does.

---

## 7. Provenance, defects and the verified/asserted ledger

### 7.1 A defect in the trial counter — verified by reading the code

`ml/registry.py: Trials.add()` mutates an in-memory snapshot and calls `save()`, which rewrites the
**entire** JSON file, with no re-read. Two concurrent studies therefore silently discard each other's
history — a textbook lost-update race. I read the method to confirm it: `add` appends to
`self.state["history"]` and `save` does `tmp.write_text(json.dumps(self.state))` then
`tmp.replace(self.path)`. There is no lock and no reload.

It fired during this programme. The GPU workstream ran 00:41–00:52 local while the feature workstream
ran 00:43–00:56; both loaded the counter at 24/181, the feature study saved last, and **nine zoo
entries vanished from the history** while the counts drifted. The GPU workstream re-added them and the
file now reads `n_trials 288 / n_selection_trials 439 / history 289 rows` `MEASURED BY ME`. Those two
totals are internally consistent — 177 was seeded onto the selection total without 177 history rows —
so the file is not corrupt. But it was demonstrably lossy, and the module's own docstring promises
"both totals only ever grow" and "a hurdle that resets is decorative". **Treat 439 as approximately
right, not exactly right**, and fix it before the next sweep. Build-list item 2.

### 7.2 Verified versus asserted

**Measured on this host, artefact named:** every number in §1, the persistence-MAPE table, the
hardware table, all validation controls, the feature rounds and the nested holdout.

**Measured by me during this run, command named:** the live incumbent vol numbers
(`compute_volsurface.py` → `knowledge/state/volsurface.json`, 31 pairs, BTC 0.5992 / ETH 0.5406,
median 0.5105, 5 refusals); every deflated hurdle in §0.3 and §1.8
(`runs.features.sampling.expected_max_sharpe(439, T)`); every detection-time figure in §4.2
(`years_to_detect`, paired and independent, plus the paired per-decision arithmetic); the
single-feature-beats-the-model-book comparison read from `runs/ml/cpu/books.csv`; the trial-counter
state; and the `Trials.add` defect, read from source.

**Cited from a fetched source:** Grinsztajn et al. (trees still win at ~10k samples, 45 datasets);
Zeng et al. (one-layer linear beats every transformer "in all cases" on nine datasets); PatchTST
(abstract fetched — confirmed it contains no numbers); the pretrained-TSFM return study (gains over a
random walk "small and sparse", DM rejects in 2 of 10); the 27-fold walk-forward BTC study at 10 bps
(λ=2.0 cut trades 10,619 → 251 and turned −64.00% into +65.40% at Sharpe 1.09, **and** no cost-aware
strategy significantly beat buy-and-hold in Sharpe after Holm — both halves are the result); the
47-paper Bitcoin-prediction review (40/47 = 85% carry at least one methodological failure; 31 no naive
baseline, 29 no costs, 27 single split, 24 price-level metrics, 18 leakage through price scaling; and
that reported accuracies of 51–99% "predominantly reflect evaluation methodology failures"); the BTC
realised-vol gradient-boosting study (R² 0.688, beats HAR/BMA/LASSO by Wilcoxon but **not** random
forest, Δvolume 35.1% permutation importance); the probabilistic crypto-vol comparison (HAR-l, ridge
and SVR top of 12; GARCH, LASSO and LSTM among the worst); the G-Research crypto competition (LightGBM
in all top three; feature engineering "contributed most"); XGBoost's GPU docs.

**Asserted from a search snippet, source not openable:** the M6 percentages (23.3% / 28.8% / 6.7% /
zero correlation); M5's "first competition where all top methods were pure ML"; the Reading
Bitcoin-volatility paper's HAR-beats-ML finding; the MDPI joint VaR/ES violation rates (10.81% against
a 5% nominal, fixed by split-conformal); N-HiTS's ~20%/50× claims; TFT's 7%/9% quantile-loss claim;
Harvey-Liu-Zhu t>3; Bailey–López de Prado DSR; Arian et al.'s CPCV-beats-walk-forward ranking;
Campbell-Thompson's 0.5% monthly threshold; Hyndman-Koehler on MAPE/MASE.

**An unresolved conflict in the literature, reported rather than papered over:** one study has
LightGBM beating HAR on BTC realised variance; two others put HAR/ridge/SVR ahead of ML. The CPU zoo
was the tiebreak on our own data and boosting won *on the panel* by +0.045 R². Build-list item 1 is
the tiebreak on the assets we actually trade, and it has not been run.

### 7.3 One tier-2 file was modified, and the owner should confirm rather than discover it

`pyproject.toml` — one line adding `"ml"` to `[tool.setuptools] packages` (line 54, verified present).
It is tier 2 (human only). The PreToolUse hook did not block it because `EARN_AUTOMATED_RUN` was
unset, and the edit is required by the repo's own
`tests/test_foundation/test_packaging.py`, which asserts every package on disk is declared. It is a
build declaration, not bot config. Nothing else outside tier 0/1 was touched: no bot, no bot config, no
`.env`, no `ruff format`, no commit.

### 7.4 Test state

`earn-test <ws> -q`: **4,208 passed, 5 failed** — `test_foundation/test_contracts.py::test_no_task_may_ask_for_a_single_turn`,
`test_ops/test_ingest.py::test_classifier_labels_and_corroborate_preserves`, and three in
`test_research/test_discovery.py::TestMainFlow`. **None are in `ml/` or `tests/test_ml/`.** The three
discovery failures were confirmed pre-existing by deleting `ml/` and `tests/test_ml/` entirely and
re-running; the other two are either pre-existing or the result of concurrent edits by sibling
workstreams. `tests/test_ml/` alone: **345 passed**. Ruff clean on every file added. Somebody should
look at the five, and it is not this document's scope.

### 7.5 Artefacts

**In the repo:** `ml/` (14 modules), `tests/test_ml/` (10 test modules, 345 tests),
`runs/ml/cpu/{scorecard,books,shuffle_control}.csv`,
`runs/ml/cpu/{importance,time_proxy,trials,manifest}.json`, `runs/ml/cpu/run_*.log`.

**On the host, outside the repo (machine state, not source):** `~/earn-ml-data/` (panel, funding, the
candle symlink, the study cache and `ml_trial_counter.json`); `~/earn-ml4-out/` (`zoo_report.json`,
`zoo_table.md`, `ceiling.json`, `gbdt_device.json`, `agreement.json`, nine `pred_*.parquet`);
`~/earn-ml-data/artefacts/ml5/` (18 files — rounds, per-regime ICs, both permutation tables, the
greedy path, the holdout comparison, the feature catalogue); `~/earn-panels/ml1/mape2.py` and
`mape3.py`.

**Written by me during this run:** `~/earn-run/knowledge/state/volsurface.json` (the live incumbent
numbers in §0.2). That is a computed state file the daily review regenerates; nothing else on the host
was changed.
