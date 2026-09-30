# Track 1 — Learn from every coin: does the wide universe make a better rule?

Run date 2026-09-30. Workspace `im1`. Snapshot-only: no live database was opened.

**Answer in one line.** Training the rule on 700+ coins instead of 16 makes the rule
*perfectly stable* and *slightly worse*. The owner's "rally and drown" question does have a
real answer — and it is that **volume and recent buying predict the drop, not the rally**.
Nothing here clears the deflated hurdle, and along the way I found and removed a lookahead
bug that had been inflating this whole family of rules by about 1.3 Sharpe.

---

## 0. What was measured, and what was not touched

| | |
|---|---|
| Data | `~/earn-panels/panel_1d.parquet` — 747 USDT tickers that ever existed, 2017-08 → 2026-09, 841,491 daily rows, survivorship-free |
| After the split guard | 777 series (24 tickers reused for a different token were split into segments at 30 break days, e.g. LUNA → LUNA 2.0) |
| After static exclusions | 190 removed: leveraged tokens (UP/DOWN/BULL/BEAR), stablecoins and fiat, the tokenised-equity `^[A-Z]{2,6}B$` pattern minus the six-coin crypto allowlist, and the named exclusions (PAXG, XAUT, WBTC, WBETH, BETH, BTTC) |
| Live databases | **Never opened.** `~/earn-run/knowledge/earn.db`, `~/earn-run/journal/journal.db` and the `ft_userdata` sqlite files were not read, copied or backed up, in any mode |
| Writes | Only `~/earn-work/im1` (a mirror), `~/earn-work/im1-scratch`, and this report. Nothing under `~/earn-run`. No commit, no order, no bot or cron touched |
| Cost floor | 0.30% round trip (10 bps fee + 5 bps slippage per side), on in every number below |
| Sharpe convention | Repo arithmetic Sharpe, `mean/std × sqrt(365)`, never CAGR/vol |
| Load discipline | One heavy job at a time, `nice -n 10`, `/proc/loadavg` checked before each step (1-min never above 6.0 at launch) |

### Point-in-time universe definitions

Both training sets are recomputed every single day from that day's data only — no
forward-looking membership.

| Set | Rule | Names per date (yearly mean) |
|---|---|---|
| **WIDE** (`WATCH`) | `earn.yaml universe.rules` analogue: ADV90 ≥ $1M, age ≥ 180d, 120d annual vol ≥ 0.20, weekend volume ratio ≥ 0.35, not statically excluded | 2018: 5.4 · 2019: 17.4 · 2020: 39.4 · 2021: 164.8 · 2022: 226.0 · 2023: 186.2 · 2024: **294.2** · 2025: 274.6 · 2026: 142.0 |
| **NARROW** (`ELIG`) | WIDE **plus** `universe.satellite_eligibility`: vol60 ≤ 1.00, age ≥ 1095d, ADV90 ≥ $10M — the standard a name must pass to be *enterable* | 2020: 1.0 · 2021: 3.9 · 2022: 10.6 · 2023: 14.6 · 2024: **23.5** · 2025: 18.3 · 2026: 17.2 |

`ELIG` resolving to 17.2 names in 2026 against the shipped snapshot's 16 enterable pairs
(31 authorised assets minus the 15 rendered `exit_only`) is the check that the panel
reconstruction matches the live funnel. The **test set is always `ELIG`**, for both arms. A
secondary variant restricts the test set further to the literal 16 names in today's
snapshot (BTC, ETH, ZEC, NEAR, XRP, SOL, DOGE, TRX, LINK, AAVE, LTC, XLM, WLD, ADA, SUI,
AVAX) and is reported beside it.

### The rule and its four knobs

The shipped short-horizon rule is `strategies/SleeveFast.py`: fast EMA over slow EMA, a
close above an N-bar breakout high, inside an ATR% band. Four knobs, 144 cells:

| Knob | Values swept | Shipped 1h default |
|---|---|---|
| `ema_fast` | 5, 9, 13 | 9 |
| `ema_slow` | 21, 34, 55 | 21 |
| `breakout_lookback` | 4, 6, 10, 20 | 6 |
| ATR band (`min`, `max`) | `none` (0, ∞) · `mid` (0.02, 0.10) · `tight` (0.03, 0.08) · `lowvol` (0, 0.06) | `none` |

Held fixed, not swept: exit on close below the slow EMA, hard stop −10% (`trading.defaults.stoploss.fixed_pct`),
entry filled at the **next bar's open** after the signal bar's close.

Book construction, identical for both arms so the comparison is fair: equal weight across
every test-set name currently in position, gross exposure `min(1, n_positions / 8)`
(mirroring `risk.max_open_positions: 8`), cash otherwise, 15 bps charged on every unit of
turnover.

### Walk-forward

Expanding in-sample window → 20-day embargo → fixed 180-day out-of-sample span. First OOS
fold opens 2022-01-01 (the first date `ELIG` carries a double-digit name count); 9 folds to
2026-06-09. **Every number labelled OOS is out of sample.** In-sample fitting objective:
mean net return per trade on the training universe (deliberately universe-size-invariant,
so a wide arm is not rewarded merely for generating more trades).

---

## 1. The lookahead bug — read this before any other number

The first pass of this study produced a 144-cell surface with median Sharpe **1.616** and a
maximum of **2.308**, and both fitted arms landing near 1.5. Those numbers were wrong.

The book was crediting each position the return `C[t]/C[t-1] − 1` on the bar it entered,
while the position was actually established at `O[t]`. It therefore collected the
**previous-close-to-next-open gap that it never owned**. Breakout entries gap up almost by
construction — that is what a breakout is — so the bias was large and one-directional.

| | biased | corrected |
|---|---|---|
| 144-cell median Sharpe | 1.616 | **0.258** |
| 144-cell max Sharpe | 2.308 | **0.706** |
| Cells beating BTC-hold 0.83 | 144 of 144 | **0 of 144** |

The gap was carrying roughly **1.3 Sharpe** — more than the entire honest signal. Every
number from Section 2 onward is from the corrected engine, where a position entered at
`O[t]` earns `O[t] → C[t]` on its first bar and `C[t-1] → C[t]` thereafter.

This is worth flagging beyond this study: **any backtest in this repo that computes a
position's first-bar return from the previous close is overstated the same way**, and the
overstatement is largest for exactly the breakout-shaped rules that look most attractive.

---

## 2. H1 — do parameters fitted on the wide universe generalise better? **REFUTED**

> **Sealed statement.** EMA/breakout/ATR-band parameters fitted on the WIDE eligible
> universe produce a higher costed OOS Sharpe on the 16 enterable names than the same
> parameters fitted on those 16 alone.
>
> **Sealed falsifier.** If the mean paired OOS Sharpe difference (wide − narrow) is ≤ +0.05,
> or its paired t-stat across folds is < 2.0, more data does not beat more relevance.
>
> Pre-registration `2026-09-30-h1-wide-training-transfer`, seal `4565af1c…`, verified intact
> at close. Outcome: **refuted**, zero audit problems.

### Per-fold OOS Sharpe, costed, on the point-in-time enterable universe

| fold (OOS start) | narrow-fit | wide-fit | BTC hold | EW-hold of `ELIG` | days BTC > 200d MA | wide − narrow |
|---|---|---|---|---|---|---|
| 2022-01-01 | −0.832 | −1.186 | −2.018 | −2.180 | 0% | −0.353 |
| 2022-06-30 | −1.308 | −1.598 | −0.338 | +0.077 | 0% | −0.290 |
| 2022-12-27 | +0.667 | +0.378 | +2.671 | +0.632 | 91% | −0.288 |
| 2023-06-25 | +3.107 | +3.248 | +2.121 | +2.216 | 67% | **+0.141** |
| 2023-12-22 | +0.892 | +0.900 | +1.749 | −0.076 | 100% | **+0.008** |
| 2024-06-19 | +2.897 | +2.741 | +2.123 | +1.950 | 59% | −0.156 |
| 2024-12-16 | −1.035 | −1.293 | +0.303 | −0.893 | 81% | −0.258 |
| 2025-06-14 | +0.074 | −0.040 | −0.613 | −0.047 | 77% | −0.114 |
| 2025-12-11 | −0.060 | +0.118 | −1.336 | −1.781 | 0% | **+0.178** |
| **pooled mean** | **+0.489** | **+0.363** | **+0.518** | **−0.011** | | **−0.126** |

**Paired test:** mean difference **−0.1258**, sd 0.1952, n = 9 folds, **t = −1.934**. The
wide arm wins **3 of 9** folds. On the literal-16 test variant: mean **−0.1987**, t −1.648,
wide wins 3 of 9.

Both halves of the falsifier fired: the difference is not merely below +0.05, it is
**negative**, and the t-stat has the wrong sign entirely. H1 is refuted.

### Why, mechanically

The wide arm is not short of data. It is short of *relevance*:

| fold | wide-arm IS trades | narrow-arm IS trades | ratio |
|---|---|---|---|
| 2022-01-01 | 163 | 51 | 3.2× |
| 2023-06-25 | 382 | 66 | 5.8× |
| 2024-12-16 | 961 | 174 | 5.5× |
| 2025-12-11 | 1147 | 231 | 5.0× |

Roughly **five times the training trades produced a worse parameter choice.** The extra
names are a different population — they are the ones the eligibility filter exists to
exclude — and fitting on them optimises for a distribution the book will never trade. This
is the same lesson the universe audit already recorded from the other direction (median
CAGR +18.40% on the 31 authorised names against −3.26% on the 383 ignored ones); it now
holds for *learning from* those coins, not only for *trading* them.

### Both directions of the rule, on the same sample

The stitched 2022-01-01 → 2026-06-09 OOS book, wide-fit set (13/55/20/`lowvol`):

| regime | n days | wide-fit | narrow-fit | BTC hold |
|---|---|---|---|---|
| full window | 1620 | +0.640 | +0.698 | +0.394 |
| BTC above 200d MA | 854 | +1.386 | +1.440 | **+2.219** |
| BTC below 200d MA | 766 | −1.074 | −0.987 | −1.304 |
| high BTC vol | 289 | −0.492 | −0.468 | −0.199 |
| low BTC vol | 1331 | +0.823 | +0.885 | +0.611 |

What it avoided: sitting at 34% average gross exposure, it lost 1.07 Sharpe through the 766
days BTC spent below its 200d MA, against BTC hold's −1.30 there.
What it gave up: in the 854 days BTC was above its 200d MA it scored 1.39 against BTC
hold's **2.22**. It surrendered **0.83 Sharpe of upside to buy 0.23 of downside
protection.** Netted over the window that is 0.640 against BTC hold's 0.394 on this
window — but below the repo's standing 0.83 baseline, and nowhere near the hurdle.

---

## 3. H2 — is the wide-fitted rule more stable? **YES, on its own metric — and it does not help**

> **Sealed statement.** Parameters fitted on the WIDE universe are more stable across
> walk-forward folds than parameters fitted on the 16 alone.
>
> **Sealed falsifier.** If the median across-fold coefficient of variation of the selected
> knobs is not at least 25% lower for the wide fit on at least 3 of the 4 knobs, H2 is dead.
>
> Pre-registration `2026-09-30-h2-wide-training-stability`, seal `bbfad752…`, intact.
> Closed **inconclusive** — see the note below.

### Selected knob set per fold

| fold | narrow-fit | wide-fit |
|---|---|---|
| 2022-01-01 | 5 / 21 / 4 / `none` | 13 / 55 / 20 / `lowvol` |
| 2022-06-30 | 9 / 34 / 10 / `none` | 13 / 55 / 20 / `lowvol` |
| 2022-12-27 | 5 / 55 / 10 / `lowvol` | 13 / 55 / 20 / `lowvol` |
| 2023-06-25 | 9 / 55 / 20 / `lowvol` | 13 / 55 / 20 / `lowvol` |
| 2023-12-22 | 13 / 55 / 10 / `lowvol` | 13 / 55 / 20 / `lowvol` |
| 2024-06-19 | 9 / 55 / 20 / `lowvol` | 13 / 55 / 20 / `lowvol` |
| 2024-12-16 | 9 / 55 / 20 / `lowvol` | 13 / 55 / 20 / `lowvol` |
| 2025-06-14 | 9 / 55 / 20 / `lowvol` | 13 / 55 / 20 / `lowvol` |
| 2025-12-11 | 9 / 55 / 20 / `lowvol` | 13 / 55 / 20 / `lowvol` |

### Dispersion

| arm | `ema_fast` CV | `ema_slow` CV | `lookback` CV | distinct band values | distinct knob sets |
|---|---|---|---|---|---|
| wide | **0.000** | **0.000** | **0.000** | 1 | **1** |
| narrow | 0.281 | 0.257 | 0.425 | 2 | 5 |

The falsifier did not trigger: the wide fit's CV is **100% lower on 3 of 3 numeric knobs**,
against a 25% threshold. It picked the identical set in all nine folds and never revised
itself. The narrow fit churned through five distinct sets, and its early folds were choosing
on **50–52 trades**.

**Both directions.** What stability bought: no fold inherited a knob set fitted on fifty
trades, so there is no parameter-churn risk at all. What it cost: **the stable set is the
worse set.** 0.640 stitched OOS Sharpe against the churning narrow fit's 0.698, and worse in
*every* regime row in Section 2's table. Stability was the honest prize on offer here, and
it turns out to be a prize for a race that does not pay.

**Why `inconclusive` and not `supported`.** The close-out audit refuses to stamp
`supported` on any claim whose headline is not a Sharpe clearing the deflated hurdle. A
coefficient of variation is not a Sharpe, so it cannot clear a Sharpe hurdle, and the audit
correctly declines. The measurement stands exactly as reported; the label is the gate
working as designed rather than a weakness in the result.

---

## 4. H3 — rally versus drown: is it separable? **YES — and the volume answer is backwards**

> **Sealed statement.** At least one computable cross-sectional feature separates the bar
> before a forward 20-day advance of ≥ +30% from the bar before a forward 20-day decline of
> ≤ −30%, with a Newey-West t exceeding 3.0 out of sample.
>
> **Sealed falsifier.** If no single feature and no linear combination reaches OOS |t| ≥ 3.0
> **and** |mean rank IC| ≥ 0.02, rally-versus-drop is not separable and H3 is dead.
>
> Pre-registration `2026-09-30-h3-rally-vs-drop-separation`, seal `e509879d…`, intact.
> Closed **inconclusive** for the same non-Sharpe-headline reason as H2.

### The label

Across the WIDE universe, label each bar `1` if its forward 20-day return is ≥ +30%, `0` if
≤ −30%, undefined otherwise. That yields **63,123 labelled bars** on **1,714 usable
cross-sections** (dates carrying at least 8 labelled names). In-sample to 2023-01-01
(24,866 rows, 52.3% rallies); out-of-sample 2023-01-01 onward (38,257 rows, 56.9% rallies).
Forward windows overlap by construction, so every t-stat below is **Newey-West corrected at
lag 20**, and the effective sample is about **44 independent cross-sections** OOS, not 877
dates and certainly not 63,123 rows.

### Every feature tested, ranked by |OOS t|

A **negative** IC means *higher feature value → more likely the DROP*. 23 features; the
Bonferroni threshold at 5% is **|t| ≥ 3.07**.

| feature | IC all | t all | IC OOS | t OOS | IC BTC>MA | IC BTC<MA | IC hivol | IC lovol |
|---|---|---|---|---|---|---|---|---|
| `vol60` | −0.1771 | **−8.78** | −0.1869 | **−8.18** | −0.1843 | −0.1677 | −0.1089 | −0.1973 |
| `vol90` | −0.1648 | −8.24 | −0.1821 | −7.29 | −0.1831 | −0.1411 | −0.0923 | −0.1863 |
| `vol20` | −0.1609 | −7.53 | −0.1770 | −6.99 | −0.1503 | −0.1746 | −0.1043 | −0.1777 |
| `atr_pct` | −0.1320 | −5.60 | −0.1503 | −5.34 | −0.1383 | −0.1237 | −0.0450 | −0.1578 |
| `vspike` (20d volume spike) | −0.0687 | −5.20 | −0.0768 | −4.84 | −0.0740 | −0.0617 | −0.0954 | −0.0607 |
| `buyvol_frac` (20d up-day volume share) | −0.0594 | −4.15 | −0.0699 | −3.97 | −0.0413 | −0.0830 | −0.0776 | −0.0540 |
| `ntrend` (7d/90d trade-count ratio) | −0.1135 | −5.27 | −0.1043 | −3.78 | −0.0867 | −0.1482 | −0.1651 | −0.0981 |
| `R180` | −0.0976 | −4.54 | −0.0989 | −3.58 | −0.0622 | −0.1429 | −0.1224 | −0.0902 |
| `vtrend` (30d/180d quote volume) | −0.1128 | −5.25 | −0.0972 | −3.57 | −0.0847 | −0.1494 | −0.1383 | −0.1052 |
| `R20` | −0.0913 | −4.75 | −0.0879 | −3.57 | −0.0631 | −0.1280 | −0.1158 | −0.0840 |
| `R30` | −0.1000 | −5.34 | −0.0823 | −3.54 | −0.0740 | −0.1337 | −0.1517 | −0.0846 |
| `volratio` (vol20/vol90) | −0.0597 | −3.06 | −0.0737 | −3.29 | −0.0333 | −0.0941 | −0.0503 | −0.0625 |
| `dist200` | −0.0973 | −4.36 | −0.0810 | −2.79 | −0.0540 | −0.1532 | −0.1491 | −0.0818 |
| `R7` | −0.0547 | −3.49 | −0.0494 | −2.52 | −0.0417 | −0.0716 | −0.0729 | −0.0493 |
| `R90` | −0.0895 | −3.97 | −0.0695 | −2.36 | −0.0513 | −0.1388 | −0.1282 | −0.0779 |
| `rs90` (90d relative strength vs BTC) | −0.0895 | −3.97 | −0.0695 | −2.36 | −0.0513 | −0.1388 | −0.1282 | −0.0779 |
| `dd_ath` | −0.0648 | −3.07 | −0.0591 | −2.16 | −0.0356 | −0.1026 | −0.1206 | −0.0482 |
| `clv20` (close-in-range, buy pressure) | +0.0415 | +2.24 | +0.0460 | +2.10 | +0.0814 | −0.0103 | −0.0044 | +0.0552 |
| `avgtrade_z` (avg trade size vs 90d) | −0.0546 | −3.53 | −0.0317 | −1.80 | −0.0462 | −0.0655 | −0.1077 | −0.0388 |
| `age` | +0.0437 | +2.31 | +0.0362 | +1.50 | +0.0651 | +0.0159 | +0.0491 | +0.0421 |
| `R365` | −0.0673 | −3.03 | −0.0399 | −1.42 | −0.0304 | −0.1272 | −0.1144 | −0.0547 |
| `adv90` | +0.0049 | +0.24 | +0.0307 | +1.26 | +0.0046 | +0.0053 | −0.0144 | +0.0107 |
| `amihud` | +0.0154 | +0.80 | −0.0140 | −0.63 | −0.0048 | +0.0417 | +0.0769 | −0.0028 |

Nine features clear Bonferroni OOS. **No feature flips sign between IS and OOS.** Every
regime column carries the same sign as the headline for every significant feature — this is
not a regime bet.

### Four findings, in order of what they change

**(a) Separability is real, and the separator is low volatility.** `vol60` reaches OOS rank
IC **−0.187** at NW t **−8.18** with 44 effective cross-sections. Among coins that made a
±30% twenty-day move, *the calm ones went up and the violent ones went down.* The falsifier
did not trigger, by a wide margin.

**(b) It is not new.** That is the loser-avoidance filter `growth-audit.md` already measured
and the system already ships (`universe.satellite_eligibility`: vol60 ≤ 1.00, age ≥ 1095d,
ADV ≥ $10M). H3's contribution is an independent confirmation on the owner's exact
framing — rally versus drown, cross-sectionally, out of sample, Bonferroni-corrected.
Cost of that filter, stated as always: P(90-day double) falls 5.41% → 4.31%, and ZEC at
+845% would have been excluded by it.

**(c) The owner's volume question has a clear answer, and it is the opposite of the
intuition.** *All five* volume, buy-pressure and activity features point the same way:

| feature | plain meaning | OOS IC | OOS t | reading |
|---|---|---|---|---|
| `buyvol_frac` | share of the last 20 days' volume on up days | −0.070 | −3.97 | more recent buying → **more likely to drop** |
| `vspike` | today's volume vs its 20-day median | −0.077 | −4.84 | a volume spike → **more likely to drop** |
| `ntrend` | 7-day vs 90-day trade count | −0.104 | −3.78 | accelerating activity → **more likely to drop** |
| `vtrend` | 30-day vs 180-day quote volume | −0.097 | −3.57 | rising volume trend → **more likely to drop** |
| `avgtrade_z` | average trade size vs its 90-day mean | −0.032 | −1.80 | same sign, not significant OOS |

The one buy-pressure feature with the intuitive sign, `clv20` (where the close sits inside
the day's range), reaches only t +2.10 OOS — below Bonferroni 3.07. So: **"is it being
bought heavily?" is a sell-side warning in this dataset, not a buy-side confirmation.** The
attention arrives with the top. This is consistent with the already-measured fact that 77.5%
of +100% run-ups are given back within 60 days.

**(d) Momentum is on the drop side too.** `R7`, `R20`, `R30`, `R90`, `R180`, `R365`,
`dist200` and `rs90` are all negative, and all strengthen when BTC is below its 200d MA
(`R180`: −0.062 above, −0.143 below). Recent gains precede the fall. This re-confirms the
negative cross-sectional momentum IC on a completely different label construction.

### The fitted combination, and the one thread worth building

A 23-feature logistic fitted on IS ranks and evaluated OOS:

| model | OOS AUC | OOS rank IC | NW t |
|---|---|---|---|
| 23-feature logistic, rally vs drop | 0.6071 | +0.1463 | +5.58 |
| **`vol60` alone** | — | **0.1869** | **8.18** |

**A single feature beats the 23-feature model** (|IC| 0.187 against 0.146). That is exactly
the pattern `ml-forecast.md` recorded — one feature, `−vol_60` cross-sectionally, beating
the best of 78 CPU and 9 GPU configurations at 4 of 6 targets. It reproduces here on a new
label. Any proposal to add a model where a rank sort would do should be read against this.

**The drop-only flag — the one number that improves on something already in the repo.**
Re-labelling one-sided (does this name fall ≥ 30% over the next 20 days? OOS base rate
6.20%, 275,478 OOS rows):

| metric | this run | the unfinished ML thread (`ml-forecast.md`) | hand-set baseline |
|---|---|---|---|
| OOS AUC | **0.6341** | 0.617 | 0.543 |
| top-decile lift | **2.205** | 1.73 | — |
| bottom-decile lift | **0.494** | — | — |

OOS decile lift, bottom to top: 0.494, 0.562, 0.634, 0.723, 0.824, 0.961, 1.102, 1.211,
1.282, **2.205** — monotone across all ten deciles, which is what a real ordering looks
like. A simple logistic on ranked *daily* features beats the nine-model-family
cross-sectional risk ordering that was measured at AUC 0.617 and **never wired into
anything**. Caveat that matters: the fitted coefficients lean on `age` (+3.13), `adv90`
(−1.09) and `amihud` (−1.17), so the model is partly re-learning the eligibility filter it
already sits behind.

This is the single most build-ready item in this report — and note what it is: a **risk
ordering**, which tightens exposure. It is not a buy signal.

---

## 5. Does anything clear the bar?

| quantity | value |
|---|---|
| Cumulative selection trials before this run | ~8,144 |
| Trials **this run** adds | **313** — 144 knob cells × 2 training arms (288) + 23 features + 2 fitted combinations |
| Cumulative | **8,457** |
| Baseline (repo arithmetic Sharpe) | 0.83 |
| `expected_max_sharpe` at 8,457 trials | 2.150 |
| **Deflated hurdle** | **2.980** |
| Best of anything measured here (corrected) | **0.706** — cell 9/55/10/`lowvol` |
| Both fitted arms | 0.640 (wide) · 0.698 (narrow) |
| BTC hold over the same window | 0.394 |

**Nothing clears the hurdle.** Not one of the 144 cells. The gap is not marginal — 0.706
against 2.980.

### The whole surface, corrected (pooled 2022-01-01 → 2026-06-09)

| statistic | Sharpe |
|---|---|
| min | 0.026 |
| p25 | 0.137 |
| median | 0.258 |
| p75 | 0.421 |
| max | 0.706 |
| cells > 0.83 (baseline) | **0 of 144** |
| cells > 2.980 (hurdle) | 0 of 144 |

Marginals — the plateau, not the peak. The ATR band is the knob that matters and the EMA
pair barely does:

| knob | mean Sharpe by value |
|---|---|
| ATR band | `lowvol` **0.532** · `none` 0.246 · `mid` 0.202 · `tight` 0.176 |
| `ema_slow` | 55 → **0.412** · 34 → 0.264 · 21 → 0.191 |
| `ema_fast` | 13 → 0.310 · 9 → 0.301 · 5 → 0.256 |
| `breakout_lookback` | 10 → 0.309 · 4 → 0.294 · 6 → 0.283 · 20 → 0.269 |

The `lowvol` band (ATR% ≤ 6%) is worth 2–3× every other band, and `ema_slow = 55` beats 21
by 2×. Both say the same thing the whole report says: **calm and slow wins; fast and
excited loses.** And that is a restatement of the shipped eligibility filter, not a new
edge.

### Named comparison, corrected engine

| configuration | Sharpe | CAGR | max DD | avg gross | mean net per trade |
|---|---|---|---|---|---|
| best of 144 (9/55/10/`lowvol`) | 0.706 | +18.7% | −33.8% | 0.37 | +8.83% |
| narrow-fit pick (9/55/20/`lowvol`) | 0.698 | +18.0% | −32.8% | 0.34 | +10.64% |
| wide-fit pick (13/55/20/`lowvol`) | 0.640 | +15.8% | −34.1% | 0.34 | +11.13% |
| no fitting, mid-plateau (9/34/10/`lowvol`) | 0.492 | +11.0% | −45.4% | 0.36 | +5.65% |
| no fitting, shipped 1h defaults (9/21/6/`none`) | 0.128 | −1.3% | −47.9% | 0.40 | +19.59% |
| BTC buy & hold | 0.394 | +7.3% | −66.9% | 1.00 | — |

Two things to notice. First, the walk-forward fitting *did* land in the right neighbourhood
once the lookahead was gone — the narrow-fit pick is the 2nd-best of 144 cells. Fitting was
not useless; it simply could not lift the family above the bar. Second, the shipped 1h
default shape (9/21/6, no band) is the **worst-performing configuration** of the four at
daily cadence, at Sharpe 0.128. Its mean per trade is the highest (+19.6%) and it still
loses, because it trades far more: this is the horizon arithmetic again.

Holding period, for the record: the surviving configurations hold **25–26 days** on average
(222–237 independent trades over 1,620 book days). That sits above the measured ~10-day
break-even holding period, which is why they are positive at all — and it is the clearest
statement of why the shipped 1h rule at +0.022% per trade against 0.300% of cost cannot work.

---

## 6. Answers to the owner's other questions, from config rather than opinion

These cost nothing to measure and were read straight out of `config/earn.yaml`.

### How often are we checking, and how often analysing?

| job | cadence (Gulf time) | what it does |
|---|---|---|
| `scanner` | **every 5 minutes** | signal scan across the watchlist |
| `healthcheck` | every 5 minutes | liveness, mode mismatch, kill switch |
| `ingest` | every 15 minutes | candles + the whitelisted news archive |
| `nav_tick`, `reconcile` | every 15 minutes | valuation and position truth |
| `tca_job` | hourly (:05) | execution cost measurement |
| **`research_run`** | **twice a day — 08:30 and 16:00** | the LLM decision run that produces a proposal |
| `daily_review` | daily 21:30 | grades the previous day, writes lessons |
| `discovery_light` | nightly 02:20 | light research pass |
| `discovery_deep` | Saturday 04:00 | deep research pass |
| `backtest_data`, universe `refresh` | Sunday 18:00 | data top-up, universe re-resolution |
| `review_run` | Sunday 20:00 | weekly review |
| `maintenance` | Monday 02:00 | self-maintenance |

**Trading decisions are evaluated on 4-hour candles** (`trading.timeframe: "4h"`), so the
bot's own entry/exit logic reconsiders six times a day; the *scanner* looks every 5 minutes
but is a signal feed, not an order path. So the honest summary: **we look every 5 minutes,
we decide on a 4-hour bar, and Claude analyses twice a day.**

Is that fast enough? Sections 2–5 say the bottleneck is emphatically not cadence. Every
fixed-hold book across 1h/4h/1d already sits at or below buy-and-hold; break-even holding is
~10 days; the configurations that survive here hold 25–26 days. **Checking more often would
cost money and buy nothing.** If anything the evidence points the other way — the shipped 1h
rule shape is the worst of the four named configurations precisely because it trades too
much.

### Internet search and news

The system already reads nine whitelisted feeds (CoinDesk, Cointelegraph, The Block,
Blockworks, Decrypt, Bitcoin Magazine, and the Ethereum Foundation, Bitcoin Core and Federal
Reserve primaries) on a 12-hour window with a **two-source corroboration rule**, classifies
them with Haiku on a $5/month budget, and keys on eight event families (etf, hack, delist,
lawsuit, upgrade, outage, depeg, macro). There is no Google Trends or social-sentiment
feed. Section 4(c) is the relevant evidence before adding one: the measurable proxies for
*attention and crowd buying already in the data* — volume spikes, up-day volume share,
trade-count acceleration — all predict the **drop**. A social-sentiment feed is another
attention measure. The prior from this data is that it would be a warning light, not a
buy signal, and it should be pre-registered as such before anyone pays for it.

### What happened to ML

Direction remains unforecastable at a size that survives 15 bps — 78 CPU and 9 GPU
configurations, best costed book ties BTC hold, one feature beat the whole zoo. Volatility
*is* forecastable and the shipped HAR+DVOL blend already reaches OOS R² 0.60 on BTC and 0.54
on ETH against the zoo's best 0.24. This run adds two things: `vol60` alone beating a
23-feature model on a brand-new label (Section 4), and the **drop-only risk ordering
improving on the never-wired thread** — AUC 0.634 against 0.617, top-decile lift 2.205
against 1.73, monotone across all ten deciles.

---

## 7. What I would do next, in priority order

1. **Fix the first-bar return convention wherever it appears.** The 1.3-Sharpe lookahead in
   Section 1 is a measurement bug, not a strategy question, and it makes breakout rules look
   good specifically. Worth an afternoon and a test.
2. **Wire the drop-only risk ordering as a de-risk input, and nothing else.** AUC 0.634,
   top-decile lift 2.205, monotone deciles, agreeing in sign with a filter already shipped.
   It tightens exposure; it never authorises an entry. It is the only item here that
   improves on a measured number already in the repo. Note that the related trim is *built
   and blocked* on three specific defects (no latch, fee-budget-silenceable largest
   de-risk, would let SleeveA sell under KILL) — those are the gate to clear first, and they
   are a human's call.
3. **Stop treating volume and buying pressure as bullish.** Five features, one sign, nine
   Bonferroni survivors, no regime flip, no IS/OOS sign change. If anything in the prompts
   or the scan stage currently reads rising volume as confirmation, it is reading it
   backwards.
4. **Do not widen the training universe.** H1 is refuted with the wrong sign. Five times the
   trades produced a worse parameter choice.
5. **Do not raise the cadence.** Nothing in this study or the prior ones supports it, and the
   holding-period arithmetic argues against it.
6. **Before buying a sentiment or Trends feed, pre-register it as a drop predictor.** That is
   what every attention proxy in the data already is.

---

## 8. Pre-registration record

| id | seal | claimed | outcome | audit problems |
|---|---|---|---|---|
| `2026-09-30-h1-wide-training-transfer` | `4565af1c…` intact | refuted | **refuted** | 0 |
| `2026-09-30-h2-wide-training-stability` | `bbfad752…` intact | inconclusive | **inconclusive** | 0 |
| `2026-09-30-h3-rally-vs-drop-separation` | `e509879d…` intact | inconclusive | **inconclusive** | 0 |

All three were sealed **before** any number was computed, all three seals verified intact at
close, and all three closed with zero audit problems. H2 and H3 are recorded `inconclusive`
rather than `supported` only because their headlines are a coefficient of variation and a
rank IC, and the audit will not stamp `supported` on a headline that is not a Sharpe clearing
the deflated hurdle. The measurements are exactly as reported above.

Records: `~/earn-work/im1/knowledge/state/hypotheses/*.json` (mirror workspace, not the live
data root). Scripts and intermediate outputs: `~/earn-work/im1-scratch/` —
`f1_feat.py` (features), `f5_fixed.py` / `f6_wf_fixed.py` (corrected engine, H1/H2),
`f7_h3.py` (H3), `f8_regime.py`, `f9_effn.py`; results in `wf_fixed.csv`,
`plateau_fixed.csv`, `plateau.csv` (the biased version, kept for the comparison in
Section 1), `h3_ic.csv`, `regimes.json`.

## 9. Honest limits

- **No live journal data.** The 2026-09-30 incident rule was obeyed absolutely: the live
  `earn.db`, `journal.db` and `ft_userdata` sqlite files were never opened. So nothing here
  is reconciled against actual fills, and the live evidence quoted (mechanical profit-takes
  10/10 +85.27; the trend-loss exit 0/8 −60.30) is **cited from the prior audit, not
  re-derived**.
- **Nine folds is nine folds.** The H1 paired t of −1.934 rests on nine observations. The
  *sign* is stable and the direction is corroborated by the trade-count mechanism, but the
  magnitude is not precisely estimated.
- **Daily, not 1h.** The panel is daily and only the 107-name watchlist has 1h feathers, so
  the shipped rule's exact intraday cadence could not be swept on the wide universe. What was
  tested is the rule's *form* at daily cadence. A 1h version restricted to the 107 feather
  names would test the cadence but cannot test the wide-universe question.
- **Taker-buy volume is missing from the panel.** `build_panel.py` kept only the first nine
  kline fields, so Binance's `taker_buy_base_volume` was unavailable. `buyvol_frac` is a
  *proxy* (share of 20-day volume on up-close days), not true taker-buy flow. Re-fetching
  with that column is cheap and would sharpen Section 4(c) considerably.
- **`ELIG` is a reconstruction.** It applies `satellite_eligibility` to panel data; it
  resolves to 17.2 names in 2026 against the live 16, which is close but not identical. Tick
  size and min-notional filters were not reconstructed.
- **The exit side was held fixed.** Exit on close below the slow EMA plus a −10% stop. Given
  that the live evidence says the exit is where this system loses, a study that fixes the
  exit cannot speak to the biggest known hole.
- **The stop is evaluated on closes, not intrabar lows**, so stop fills are optimistic by
  roughly the average intraday overshoot.
- **The drop-only model's coefficients partly re-learn the eligibility filter** (`age` +3.13,
  `adv90` −1.09, `amihud` −1.17), so its lift is not fully independent of what already ships.
- **This report is written under `evals/`, a tier-2 path.** Nothing was committed and no code
  was changed; if the tier boundary matters for a document, a human should move it to
  `reports/`.
