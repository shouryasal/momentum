# Analogue timing — do trajectory analogues, lifecycle stages or peer lead-lag pick better coins at better moments?

**Status:** audit, decision-grade. Nothing here is switched on until the build list in §9 ships
through its stated tier. No running bot, config, `.env` or `var/state/**` was touched producing it.
No `ruff format`, no commit. The three studies behind it changed no file in the repository.

**Why it exists (owner, verbatim):** *"the 20000 put in is worth less, system is not doing its job,
its supposed to earn not loose. so we need to build and improve until we are robust and best."*

**And the direction that shaped it (owner, verbatim):** *"not fewer trades but better trades, we
should be identifying right coins and right timings for them, using our skills ml and everything
through passes. we should also be looking for similar growth coins, like bnb grew, solana was
similar to bnb and its growth would have been similar, so a time strategy for it should be there.
this is just an example dont use it, things like this should be detected and planned."*

The instruction not to use the named example was followed literally. No pair was hand-picked
anywhere below. Every hypothesis was stated as a pattern class, searched across the whole
survivorship-free universe, and given a matched control and a placebo to beat. Three studies ran on
this host on 2026-09-25, each rebuilding its own point-in-time panel from `data.binance.vision`:

| workspace | hypothesis | panel |
|---|---|---|
| `an1` | trajectory analogues — X now resembles Y then, so X does what Y did | 733 symbols, 829,547 candles, 2017-08-17 → 2026-08-31, 484 alive / 269 dead |
| `an2` | lifecycle alignment, and peer lead-lag by behavioural clustering | 747 symbols, 841,491 candles, 2017-08-17 → 2026-09-24 |
| `an3` | entry timing, and the minimum-edge gate | 615 symbols after the universe rules, 790,716 candles, 206 dead |

---

## 0. The answer, before the detail

**Nothing found today beats holding BTC on return. The owner's hypothesis — that a coin about to
run can be recognised because its trajectory resembles an earlier coin's — is false, and it is
false at its core rather than at its edges: matching on the shape of the price path alone carries a
rank IC of +0.0123 with a non-overlapping t of 0.66, and the four boring scalars carried alongside
it (volatility, drawdown from the high, listing age, amplitude) reproduce the entire signal to
three decimal places. Peer lead-lag is reproduced by random groups of coins. Entry timing is
worth less than nothing: inside the state a rule selects, entering on the fresh signal day is
2.19 percentage points WORSE per trade than entering on a randomly chosen day in the same state.**

So the proposal in §5 is not a better selector. It is a tighter membership rule, a new gate refusal
and a smaller fee budget — a calmer book, and a smaller one. It is worth **+6.7 to +7.2 percentage
points of net-of-cost edge per trade** over the configuration the system ships today, it halves the
drawdown, and it makes **18.7 trades a year** where the shipped tier makes 43.8 and the plumbing
profile makes over a thousand. It still loses to BTC buy-and-hold on return by 5 to 8 points of
CAGR on the window where it can be measured.

**And the first thing to say about the $20,000 is what `dip-strategy.md` §0.5 already established:
it is worth less because BTC is worth less.** BTC is −25.9% over the last twelve months and −32.6%
below its own 365-day high. No long-only spot book made money holding this market. The second
thing is that the 92-trade / −1.29% figure quoted in the brief is a **30-day costed backtest of
`config/profiles/fast-test.yaml`**, the ten-hour plumbing profile — not a realised loss on the
owner's capital. It paid 1.35% of NAV in fees to net −1.29%, so the fees were indeed the whole
loss, and that profile's own comment already says it "must not be left running". §4.4 prices
exactly how it happened, and build item 2 is the two-character change that makes it impossible.

### 0.1 The four questions, decided

| the owner's question, formalised | answer | the number that decides it |
|---|---|---|
| **Does analogue matching work?** | **No** | Price-shape-only k-NN: rank IC **+0.0123**, non-overlapping t **0.66** at W=90/h=90, **−0.12** at W=180; positive in 48.2% of weeks; top-decile median 90d return **−19.16%** against a −18.97% base rate, i.e. **minus 0.63×** the round trip |
| Is it momentum in disguise? | **No — worse. It is our own exclusion filter in disguise** | It **survives** momentum and BTC beta (residual IC +0.0413, t 4.07) and then dies against vol60, age, liquidity and drawdown-from-ATH: residual **+0.0120 (t 1.72)**, and **+0.0020 (t 0.15)** in 2023-24. Scalars alone give +0.0593 against the full signal's +0.0592 |
| Can the "BNB/SOL-like run" class be detected systematically? | **Detected, yes. Predictive, no** | P(analogue ≥ +100% over 90d): rank IC **−0.0034 (t −0.26)**. Top decile P(actually doubled) **5.69%** against a **5.43%** base rate, with a median return **worse** than base. Residual flips sign: +0.045 (t 2.97) in 2019-22, **−0.036 (t −2.32)** in 2023-24 |
| Does DTW beat plain k-NN? | **No** | −0.0029 / +0.0069 / −0.0113 across three window-horizon pairs. Two of three go the wrong way, all inside the noise. **Do not build it** |
| **Is there a lifecycle stage to time?** | **No stage exists** | The cohort curve is **monotone** across 22 three-month buckets, Spearman(age, median excess) **+0.939, p = 1e-10**, and the maximum is the **last, open-ended bucket**. No local maximum at 6 months, 2 years or 4 years |
| Is age worth anything? | **As an eligibility bar and a drawdown filter, yes. As a return source, no** | Median forward-30d excess at 5y+: **+2.73% [+1.76, +3.51]**, hit 60.8% — **8.1× the round trip**. P(90d drawdown < −40%) falls **51.7% → 29.6% → 19.0%** intersected with low vol. But the age-4y+ equal-weight book returns **−14.3% CAGR at Sharpe 0.01**, the worst in its table |
| **Does peer lead-lag work?** | **No, and a random group does it just as well** | 888 events, **108/year**. Against a matched control (same-day coins with the same flat 5-day return from outside the cluster) laggards add **+0.14% to +1.15%** and **no CI excludes zero at any horizon or any entry delay**. Random pseudo-clusters reproduce the whole effect; **seed 2 beat the real clusters** |
| **Does entry timing carry net edge?** | **No — it is negative** | 200-draw random-entry placebo: a random day in the same state earns **+13.74%** net per trade against the fresh-signal day's **+11.55%**. Timing is worth **−2.19pp**, and the real rule sits at the **22nd percentile** of the placebo distribution |
| **What minimum-edge multiple belongs in the gate?** | **k = 5, and as a minimum HOLDING PERIOD, not a refusal of a coin** | As literally specified, k=3 refuses **0 of 463** entries and k=5 refuses **1** — vacuous, because crypto volatility dwarfs 30 bps. In the dual form it refuses **100%** of a 0.33-day intended hold, **96.7%** of one-day holds and **30.8%** of five-day holds, at zero measured cost where the system actually operates |

### 0.2 What survived, and what each piece is worth

Five things, and only one of them is new.

| # | rule | measured effect | multiple of the 0.30% round trip |
|---|---|---|---|
| R1 | `vol60 ≤ 0.70` on both tradeable tiers (tighten the leg we already own from 1.00) | net/trade **+4.37% → +7.69%**, CAGR 15.18% → 22.51%, MaxDD −44.70% → **−26.69%**, Sharpe 0.64 → 1.10 | **25.6×** per trade |
| R2 | `min_listing_age_days: 1460` on both tradeable tiers (from 730 / none) | median fwd-30d excess **+2.73%**, P(90d dd<−40%) **19.0%** at 4y+ ∩ low-vol against 51.7% at <6m | **8.1×** as a filter |
| R3 | **NEW** — `risk.min_edge`: a minimum holding period on the plan, k=5 | **1 refusal in 463** at the operating horizon; **100%** refusal of a 0.33-day hold | refuses everything below **1.0×** |
| R4 | `risk.max_fee_pct_per_month` 1.0% → 0.25% | surviving config spends **0.050-0.074%/month**; the plumbing profile spent **1.35%** | 3.4-5× headroom |
| R5 | Immediate entry, one clip, full size, next open | beats the best alternative by **+1.83pp to +11.82pp** net per trade | — |
| R6 | Recent strength (a new 20-40 day high) as an **observe-only** state condition | +7.69% → **+11.06-11.55%**/trade, t_clu 3.12-3.24, CI [+4.15, +18.73] | **36.9-38.5×**, CI floor **13.8×** |

R1, R2 and R4 are one number each in tables that already exist. R5 confirms what the system
already does and forbids three families from being built. R6 is the only thing that looks like a
new signal and §4.1 proves it is not one — it is a state filter that measures the same thing as the
trailing 30-day return, and even the cross-sectional momentum rank that `growth-audit.md` §2.5
rejected at −13.5% CAGR produces a statistically indistinguishable book inside this membership.
**R3 is the only genuinely new mechanism in the document.**

### 0.3 Three BTC baselines, because three windows

This matters before any comparison below is read. Each study measured buy-and-hold BTC over the
window its own eligibility rule permitted, and the numbers are not interchangeable:

| study | window | BTC CAGR | BTC Sharpe | BTC MaxDD | why this window |
|---|---|---|---|---|---|
| `an1` | 2019-01 → 2026-08 | **+48.4%** | 0.95 | −76.6% | panel start plus a year of warm-up |
| `an2` | 2019-01 → 2026-09 | **+48.9%** | 0.96 | −76.6% | same, one month longer |
| `an3` | 2020-08 → 2026-08 | **+28.54%** | **0.72** | −76.63% | eligibility cannot start earlier: `age ≥ 3y` binds against a store that begins 2017-08 |

BTC by regime on the `an1` panel: **+44.4% / +137.6% / −10.0%** for 2019-22 / 2023-24 / 2025-26.
And the baseline that matters more than BTC, because it is what the repo already proposes to ship:
`growth-audit.md` §1.5's exclusion filter, equal-weight, **+18.0% CAGR / Sharpe 0.58 / −79.5%**, or
**+20.6% / 0.61 / −76.5%** taking the top ten by ADV.

---

## 1. Trajectory analogues — the hypothesis, and why it fails at its core

### 1.1 The engine, and three independent proofs it does not look ahead

At each (coin, T) and window W ∈ {30, 90, 180} days: the log-price path rebased to the window
start, mean-pooled to 20 points, centred and L2-normalised so cosine distance *is* correlation and
the representation is level- and amplitude-free; the log-volume path to 10 points likewise; and
five standardised scalars (20d annualised vol, drawdown from ATH, log age, window amplitude in its
own vol units, vol20/vol60). One matrix serves as both query set and candidate pool, so a query and
a candidate are identical objects by construction. 764,823 windows at W=90; a candidate pool of
107,585 after requiring $1M median volume, a computable forward outcome and a 5-day sampling grid.
Matching by k-NN on the combined distance (three matmuls) and by banded DTW (Sakoe-Chiba r=4)
re-ranking the k-NN top 300. Analogues de-duplicated so two windows of the same coin must be ≥ W
days apart and at most three windows per coin contribute — a daily grid cannot fake k independent
analogues. k = 50, with 20 and 100 as sensitivities.

The causality constraint is the whole study, so it was asserted three ways:

1. **Pairwise.** A candidate window ending at index *j* may serve a query at index *i* only when
   *j + h ≤ i*, so the candidate's own forward outcome is fully observed at *i*. Measured:
   **0 violations in 49,361 rows, slack minimum 0 days** — the constraint binds exactly rather than
   being comfortably loose.
2. **Truncation invariance.** The entire feature panel rebuilt from a history cut at 2023-06-25 and
   compared to the full-history version at that date: **max |difference| = 0.000e+00** for age,
   medvol, vol60, vol20, vol120 and dd_ath; **0 mismatching names** in the eligibility mask;
   0.000e+00 on the window price vectors. Nothing changes when the future is deleted.
3. **A deliberate leak, as a positive control.** With the constraint removed, 48,888 of 49,361 rows
   use future analogues and the IC goes from **+0.0237 to +0.0838**, non-overlapping t from **0.10
   to 5.21**. The test can see leakage; it simply does not see any in the honest pipeline.

Point three is the one that earns the negative its credibility, and build item 5 makes it a
standing requirement.

### 1.2 The decomposition that answers the question

W=90, h=90 — the configuration where the signal looks strongest. Weekly cross-sections, rank IC of
the analogue-implied median forward return against the realised forward return:

| variant | mean IC | NW t | **non-overlap t** | % wk pos | top-decile median 90d | base rate | edge | × round trip |
|---|---|---|---|---|---|---|---|---|
| FULL (price shape + volume shape + 5 scalars) | +0.0592 | 4.08 | 3.81 | 63.5 | −17.47% | −18.97% | +1.50pp | 5.00× |
| **price SHAPE ONLY — the idea being tested** | **+0.0123** | **0.77** | **0.66** | **48.2** | **−19.16%** | −18.97% | **−0.19pp** | **−0.63×** |
| price shape + scalars, no volume | +0.0640 | 5.40 | 2.28 | 69.4 | −17.41% | −18.97% | +1.56pp | 5.20× |
| **SCALARS ONLY, no shape at all** | **+0.0593** | **4.17** | 1.67 | 64.6 | −17.70% | −18.97% | +1.27pp | 4.24× |

**Scalars alone reproduce the full signal to three decimals.** The trajectory comparison — the
entire hypothesis — contributes nothing, and on its own is slightly worse than the base rate. At
W=180 shape-only gives +0.0129 with a non-overlapping t of **−0.12**, and its sign flips by regime
(+0.0255 / +0.0095 / **−0.0156** at W=90; +0.0248 / **−0.0145** / +0.0231 at W=180).

Then the control regression the brief asked for — per cross-section, regress the ranked signal on
ranked controls and IC the residual:

| controls | regime | raw IC | raw t | resid IC | resid t | R² of signal on controls |
|---|---|---|---|---|---|---|
| R30, R90, beta60 | ALL | +0.0516 | 4.30 | +0.0413 | 4.07 | 0.203 |
| + vol60, log-age, log-medvol, dd-from-ATH | ALL | +0.0516 | 4.30 | **+0.0120** | **1.72** | 0.347 |
| + same | 2019-22 | +0.0780 | 3.89 | +0.0191 | 2.02 | 0.454 |
| + same | 2023-24 | +0.0189 | 1.61 | **+0.0020** | **0.15** | 0.251 |
| + same | 2025-26 | +0.0429 | 2.70 | +0.0113 | 0.80 | 0.256 |

**The diagnostic the brief predicted came back sharper than predicted.** The analogue signal is
*not* momentum in disguise — it survives momentum and beta cleanly. It is the repo's own exclusion
filter in disguise, re-encoded and degraded. For scale: the shipped single features measure IC
**−0.140 / −0.160** (vol60) and **+0.128 / +0.182** (age). A 764,000-window k-NN engine recovers
about one third of what one already-shipped feature gives for free.

### 1.3 The test that settles it — inside the universe a live book would use

| W | h | universe | weeks | coin-weeks | mean IC | NW t | % wk pos |
|---|---|---|---|---|---|---|---|
| 30 | 7 | exclusion-filter names only | 254 | 4,241 | **−0.0254** | −1.37 | 42.1 |
| 90 | 30 | exclusion-filter names only | 250 | 4,168 | **−0.0108** | −0.41 | 48.0 |
| 90 | 90 | exclusion-filter names only | 242 | 4,049 | **−0.0485** | −1.42 | 44.2 |

Negative at every window and every horizon, in the only place the signal could ever be used.

### 1.4 The owner's example, formalised — and why the engine was right and the trade was wrong

P(analogue ≥ +100% over 90d) as a "detect the growth class" signal, W=90: rank IC **−0.0034**
(t −0.26). Realised outcomes by decile of that probability:

| bucket | n | P(actually doubled) | median fwd | mean fwd | P(fwd < −50%) |
|---|---|---|---|---|---|
| top decile | 5,012 | **5.69%** | −19.11% | −0.47% | 16.26% |
| 5th-9th decile | 19,358 | 5.32% | −18.88% | −1.34% | 15.49% |
| ALL = base rate | 48,392 | **5.43%** | −18.97% | −1.19% | 15.40% |
| bottom decile | 4,239 | 5.14% | −22.11% | −2.58% | 16.96% |

A 0.26pp lift on a 5.43% base rate, with a median return **worse** than base.

**And the failure is instructive rather than a bug, which is why it is in the body and not a
footnote.** The highest-P(2×) rows in 2023-24 were ARB, INJ, ARKM, LDO, FLOKI and XAI, matched to
January-2021 windows of ONE, EOS, NEO, BAND and ZRX. The engine found the alt-season analogues
correctly. Realised: ARB −28.4%, INJ −37.7%, ARB −29.3%, ARKM −2.3%, LDO +18.5%, FLOKI +2.6%,
FLOKI −62.3%, XAI −54.5%. **The resemblance was real. The outcome depended on a market regime that
the shape does not encode** — which is the same finding `growth-audit.md` §1.7 reached from the
event calendar: growth is a calendar event, 62.3% of sustained doublings started in ten calendar
months, and nothing observable at T says which month.

### 1.5 The strongest fragment found anywhere, pressed until it broke

The risk moment, not the return moment, carries the information — consistent with the repo's
standing result that crypto's public information predicts the second moment. P(analogue drawdown
> 20%) against the **realised** forward 90-day drawdown, W=90:

| stage | ALL | 2019-22 | 2023-24 | 2025-26 |
|---|---|---|---|---|
| raw IC (NW t) | +0.1031 (7.35) | +0.1121 (4.64) | +0.0921 (4.99) | +0.0966 (5.42) |
| non-overlapping t | 4.65 | 3.20 | 2.33 | **1.11** |
| resid after R30/R90/beta | +0.0819 (9.97) | +0.0796 (6.05) | +0.0775 (6.28) | +0.0930 (6.41) |
| resid after + vol60/age/medvol/dd | **+0.0277 (4.64)** | +0.0362 (4.15) | +0.0210 (2.30) | +0.0196 (1.65) |

Quintiles are monotone — P(dd>40%) 36.71 → 39.16 → 40.01 → 41.55 → **43.64%**. And then it dies on
three counts: inside the exclusion-filter universe its tertiles are **non-monotone with no
separation** (P(dd>40%) 19.83 / 18.02 / 20.64 against 19.44% for the universe as a whole); at
W=180 the residual after full controls is +0.0121 (t 1.46) with 2023-24 at −0.0070; and its 7pp
economic spread is **one seventh** of what vol60 alone delivers (3.5% → 50.4%). It is filed as a
failure with the reasons stated, and §7.3 puts it where it belongs — as a forecasting *target*, not
a trading signal.

### 1.6 Every costed analogue portfolio

Weekly Sunday rebalance, 15 bps/side, 20% haircut on any position in a coin that delists, gross 1.0:

| book | CAGR | Sharpe | MaxDD | turn/yr | 19-22 | 23-24 | 25-26 |
|---|---|---|---|---|---|---|---|
| BTC buy & hold | **+48.4%** | **0.95** | −76.6% | 0 | +44.4 | +137.6 | −10.0 |
| EXCLUSION FILTER top-10 by ADV | +20.6% | 0.61 | −76.5% | 7.6 | +25.7 | +82.8 | −33.9 |
| EXCLUSION FILTER EW | +18.0% | 0.58 | −79.5% | 9.6 | +20.7 | +75.8 | −30.7 |
| **best analogue book: top-10 (W90,h90) inside the filter** | **+15.4%** | **0.55** | **−82.3%** | **20.0** | +23.0 | +49.3 | −27.4 |
| analogue top-10 (W90,h90) inside $5M tradeable | −4.2% | 0.40 | −96.9% | 51.2 | +10.0 | +42.9 | −57.4 |
| analogue top-5 (W30,h7) inside $5M tradeable | −21.8% | 0.21 | −99.3% | 86.4 | −1.0 | +15.8 | −72.3 |
| the same top-10 (W90,h30) at **literally zero cost** | **+3.6%** | 0.50 | −96.2% | — | — | — | — |
| sign check: BOTTOM-10 (W90,h30), $5M | −36.3% | −0.04 | −99.8% | 54.0 | −12.9 | −18.8 | −77.5 |

The best of 24 constructions is worse than the exclusion filter alone on CAGR, Sharpe **and**
drawdown at double the turnover. The zero-cost row is the diagnostic: turnover 53.6/yr × 30 bps =
8.0%/yr of fees accounts exactly for the gap between +3.6% and −4.4%, **and the signal still loses
to both benchmarks with costs switched off.** This is a bad signal that costs then bury, not a good
signal killed by costs. The bottom-10 book at −36.3% confirms the ordering is real; the ordering is
just inside a universe where the top is losing.

### 1.7 The sample size, because the row count is not reportable

| W | h | coin-weeks | weekly sections | NON-OVERLAPPING | independent bets/section (ρ=0.45) | EFFECTIVE_N | overstatement |
|---|---|---|---|---|---|---|---|
| 30 | 7 | 49,335 | 399 | 399 | 2.20 | ~878 | 56× |
| 90 | 30 | 49,018 | 395 | 79 | 2.20 | ~174 | 282× |
| 90 | 90 | 48,392 | 387 | **30** | 2.20 | **~66** | **733×** |

The headline +0.059 rests on roughly **66 effective observations**. Quoting 48,392 would overstate
the sample by 733×. This is enough to say "no economically usable edge" and **not** enough to
distinguish IC +0.012 from zero.

---

## 2. Lifecycle — monotone, not staged

Every coin aligned by days since its first Binance USDT candle instead of by calendar date. Forward
30-day return in excess of the same-date cross-sectional median of the same universe — the one
factor that `growth-audit.md` §0 measures at 57-69% of variance. Confidence intervals by
month-block bootstrap (2,000 draws, resampling calendar months), which handles both the
within-date correlation and the window overlap that a naive t on 55,151 rows does not.

| age bucket | n obs | n coins | median excess | 95% CI | hit>0 | net of 0.30% |
|---|---|---|---|---|---|---|
| 0-3m | 1,160 | 394 | −4.06% | [−5.52, −2.76] | 41.0% | −4.36% |
| 3-6m | 4,575 | 419 | −2.45% | [−3.45, −1.42] | 44.0% | −2.75% |
| 6-9m | 4,184 | 385 | −1.71% | [−2.43, −0.88] | 44.7% | −2.01% |
| 9-12m | 3,886 | 352 | −1.87% | [−2.72, −0.98] | 43.0% | −2.17% |
| 1-1.5y | 6,836 | 347 | −0.87% | [−1.63, −0.23] | 46.3% | −1.17% |
| 1.5-2y | 5,969 | 312 | −0.05% | [−0.58, +0.45] | 49.5% | −0.35% |
| 2-3y | 10,117 | 302 | +0.20% | [−0.04, +0.58] | 50.8% | −0.10% |
| 3-4y | 7,929 | 249 | +0.24% | [−0.06, +0.68] | 51.2% | −0.06% |
| 4-5y | 5,255 | 173 | +1.36% | [+0.73, +1.99] | 55.9% | +1.06% |
| **5y+** | 5,283 | 101 | **+2.73%** | **[+1.76, +3.51]** | **60.8%** | **+2.43%** |

**There is no stage.** Spearman(age bucket, median excess) = **+0.939, p = 1e-10** across 22
three-month buckets, and the maximum sits in the last, open-ended bucket (63-66 months). Identical
under the harsher delisting = −100% convention (+2.72% [+1.73, +3.48]) and at 90 days (+6.54%
[+4.82, +8.09], hit 66.2%).

**This corrects a shipped document.** `growth-audit.md` §1.2 says *"the break is at about 2 years,
not 180 days"*. At three-month resolution there is no break at all — the coarse buckets made one.
The consequence for the build list is concrete: the age bar should be set **as old as the universe
allows**, not at an apparent kink.

Most of the effect is volatility wearing age's clothes — Spearman(age, median 60d vol) = **−0.981**
— but not all of it. The double sort separates inside every volatility tercile:

| age | lowest-vol tercile | mid | highest-vol tercile |
|---|---|---|---|
| <6m | +0.30% (n=911) | −1.38% (1,431) | −4.02% (3,380) |
| 1-2y | +1.22% (3,125) | −0.40% (5,000) | −2.13% (4,680) |
| 2-4y | +1.53% (6,515) | +0.07% (6,498) | −1.45% (5,033) |
| **4y+** | **+3.17% (6,018)** | +0.76% (2,816) | −0.37% (1,704) |

It is not the mega-caps either: excluding the ten largest by adv90 each date gives 4y+ = +1.53%
[+0.88, +2.20]; also excluding the whole top liquidity quintile gives +1.11% [+0.33, +1.84]. Same
sign in all three regimes (+4.08% / +1.44% / +2.25%).

**Where age pays hardest is drawdown**, forward 90 days:

| age | n | P(90d dd < −40%) | P(< −50%) | median 90d minimum |
|---|---|---|---|---|
| <6m | 5,690 | 51.7% | 36.8% | −41.1% |
| 1-2y | 12,715 | 43.3% | 27.8% | −35.7% |
| 2-4y | 17,902 | 37.0% | 21.7% | −32.1% |
| **4y+** | 10,263 | **29.6%** | **15.6%** | −27.7% |
| **4y+ ∩ lowest-vol tercile** | 5,863 | **19.0%** | **8.1%** | — |

### 2.1 The trap, and it is the whole story

**The age edge lives entirely in the median and the hit rate. It does not exist in the mean.**

| age | mean excess fwd-30d | 95% CI |
|---|---|---|
| 0-3m | +6.50% | [+2.32, +11.02] |
| 1-1.5y | +5.24% | [+2.82, +8.01] |
| 2-3y | +5.47% | [+3.87, +7.23] |
| 5y+ | +6.25% | [+4.18, +8.88] |

Flat. Young coins have a worse median and the same mean, because they carry the fat right tail. **An
equal-weight long-only book is a mean object**, so the cross-sectional median edge does not
transfer, and the costed books prove it:

| book, 2019-01 → 2026-09 | CAGR net | Sharpe | maxDD | turnover/yr |
|---|---|---|---|---|
| **BTC buy-and-hold** | **+48.9%** | **0.96** | −76.6% | 0.13× |
| eligible universe EW | +3.7% | 0.48 | −95.9% | 4.30× |
| age <1y | −20.3% | 0.24 | −98.7% | 6.95× |
| **age 4y+** | **−14.3%** | **0.01** | −86.8% | 2.67× |
| lowest-vol tercile only | +19.5% | 0.63 | −90.5% | 9.01× |
| lowvol + age ≥ 2y | +10.9% | 0.52 | −88.3% | 7.69× |

**The cohort with the best median excess return has the worst Sharpe in the table.** That
full-period result is partly an artefact of a thin early 4y+ cross-section and the study said so:
restricted to 2023-01-01 onward, where 50+ names qualify, the age filter clearly *helps* on top of
low vol — lowvol+age≥4y returns **+13.4% CAGR (Sharpe 0.51)** at 30-day rebalance and **+25.4%
(0.68)** at 60-day, against the eligible universe at −19.6% / −18.6%. It beats the alt base rate by
33-44 points and loses to BTC (+54.2%, Sharpe 1.16) by 29-41. **Note the direction of that
correction: the number that would have been reported as a win is the one that was contaminated.**

### 2.2 Purged, embargoed walk-forward on the age cut

Threshold chosen on the training block from seven candidates, scored only on the next six months,
30-day purge and 15-day embargo:

| result over 12 blocks | value |
|---|---|
| beat the eligible universe EW | **7 / 12**, median margin +5.7pp |
| beat BTC | **4 / 12**, median margin **−5.4pp** |
| median block CAGR | book +62.6%, universe +39.5%, **BTC +85.6%** |
| drop the single 2021-H1 block | mean advantage over the universe collapses +10.8pp → **+2.6pp** |

33% of blocks beating BTC sits **inside** the 17.2-38.7% band `growth-audit.md` §1.1 already
measured for a **random** four-name basket. Out of sample the selector is not distinguishable from
random draws on the only axis that matters.

---

## 3. Peer lead-lag — dead, and the placebo is what kills it

Clusters formed every 21 days from the trailing 120 days of daily returns, strictly before the
formation date, on two inputs: raw returns and **market-residual** returns (each coin regressed on
the equal-weight universe factor over the same window — the "behaviour, not beta" version the
hypothesis needs). Hierarchical clustering, average linkage on 1−corr, 3-25 members. Event: a
member's trailing 5-day return ≥ 20% and it is the cluster maximum; laggards are members at ≤ 5%.
Entry at the close **after** the signal day, never the same bar.

**Frequency is not the problem.** 888 events, 666 distinct signal days, **108 events per year**,
mean cluster 4.3 names, 1.87 laggards per event, median leader move +28.5%.

| h (days) | laggard RAW mean | 95% CI | net of 0.30% | hit>0 | median raw |
|---|---|---|---|---|---|
| 1 | +0.03% | [−0.49, +0.61] | −0.27% | 47.4% | −0.24% |
| 3 | +0.49% | [−0.87, +1.91] | +0.19% | 46.2% | −0.76% |
| 5 | +1.12% | [−0.89, +3.08] | +0.82% | 47.4% | −0.60% |
| 10 | +2.16% | [−1.31, +6.18] | +1.86% | 46.2% | −1.42% |
| 20 | −1.00% | [−5.75, +4.20] | −1.30% | 37.0% | −6.17% |

Every CI straddles zero, the hit rate is below a coin flip at every horizon, and the median is
negative at every horizon — the best-looking cell (+1.86% at h=10) is a tail artefact on which
54.8% of trades lose.

### 3.1 The matched control, which is the decisive test

Against same-day coins with the **same** flat trailing-5-day return, drawn from the whole eligible
universe but **outside** the leader's cluster:

| h | laggard raw | matched control | **laggard − matched control** | laggard − universe median |
|---|---|---|---|---|
| 1 | +0.03% | −0.12% | **+0.14% [−0.20, +0.54]** | +0.87%* [+0.54, +1.25] |
| 3 | +0.49% | −0.10% | **+0.59% [−0.20, +1.50]** | +1.93%* [+1.20, +2.78] |
| 5 | +1.12% | +0.13% | **+0.99% [−0.05, +2.07]** | +2.79%* [+1.69, +3.88] |
| 10 | +2.16% | +1.01% | **+1.15% [−0.70, +3.50]** | +4.12%* [+2.15, +6.57] |
| 20 | −1.00% | −0.63% | **−0.38% [−2.49, +1.57]** | +4.26%* [+2.04, +6.58] |

(* = CI excludes zero.) **Cluster membership contributes nothing.** The impressive "+0.87% to
+4.26% against the universe median" is entirely reproduced by *any* same-day coin that has not
moved in five days. It is not lead-lag; it is the negative median of the altcoin cross-section
being an easy thing to beat while still losing money. Cluster-adjusted — the common move removed as
the brief demanded — gives +0.00%, +0.14%, +0.31%, +0.36%, +1.03%, −0.09% across h=1..20, with one
of 36 grid cells excluding zero, which is what 5% of 36 looks like.

### 3.2 The placebo, which closes it

| variant | market-adjusted range across h=1..20 | best raw net cell |
|---|---|---|
| REAL residual clusters | +0.86% … +4.37% | +1.86% (h=10) |
| PLACEBO seed 0 | +0.55% … +3.96% | −0.09% |
| PLACEBO seed 1 | +0.73% … +3.90% | −0.63% |
| PLACEBO seed 2 | +0.79% … **+5.79%** | **+1.36%** |

Random pseudo-clusters with the same size distribution reproduce the entire effect, and one of
three seeds **beats** the real thing on both metrics.

### 3.3 Everything else in the family, and the reason it cannot be rescued

- **Pairwise** — the single highest residual-correlation peer of the breakout coin, median trailing
  residual correlation 0.519, a genuinely tight behavioural pair. This is the literal form of the
  owner's example: **+0.18% to +0.84%** over matched control, no CI excluding zero.
- **Entry delay** d+1 / d+2 / d+3 / d+5: best diff-vs-control +0.99%; no CI excludes zero at any
  delay.
- **The current regime**: 2025-26 laggard returns are significantly **negative** at every horizon
  with every CI excluding zero — −0.74%, −1.27%, −1.96%, −2.55%, −5.55%, −9.06%. Trading this today
  is a reliable way to lose money, not a neutral null.
- **The squeeze, so nobody retries with a tighter cut**: min corr 0.6 → 8.9 events/yr (73 total,
  untestable); 0.4 → 108/yr, null; 0.2 → 628/yr and raw net −0.06% at h=1. Tighten the peer
  definition and the sample vanishes; loosen it and the edge vanishes. **There is no setting in
  between.**
- **And the reason, measured on a fourth independent panel**: first-eigenvalue share of the
  point-in-time eligible correlation matrix **mean 0.586** (range 0.342-0.860), participation-ratio
  effective breadth **3.17 names** out of ~136 eligible. A behavioural cluster in this market is
  the market wearing a hat, which is exactly why the placebo works.

---

## 4. Timing — and the one gate that is worth building

Selection held fixed (`growth-audit.md` §1.5's filter plus the 15-member trend ensemble), exit held
fixed, only entry varied. Conditions evaluated on the close of day *t*; every fill at the **open**
of a later day; no same-bar execution anywhere.

| entry rule | n | NET/trade | median | hit | t_clu | boot 95% CI | trades/yr | CAGR | MaxDD | Sharpe | fill% |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **immediate (next open)** | 463 | **+4.37%** | −5.58% | 19.7% | 1.45 | [−1.10, +10.34] | 55.6 | 15.18% | −44.70% | 0.64 | 100% |
| fixed 5-day delay | 443 | +4.14% | −4.73% | 30.0% | 1.53 | [−0.82, +9.73] | 53.2 | 7.17% | −42.21% | 0.40 | 100% |
| pullback to MA50 | 247 | +2.54% | −2.70% | 30.4% | 0.96 | [−1.71, +8.29] | 29.7 | 2.19% | −36.61% | 0.22 | 46% |
| pullback to MA20 | 291 | −0.64% | −3.43% | 23.4% | −0.52 | [−2.83, +2.14] | 34.9 | −4.35% | −49.16% | −0.16 | 57% |
| pullback to prior 10d low | 227 | −1.28% | −3.44% | 30.0% | −1.10 | [−3.38, +1.17] | 27.3 | −5.18% | −51.30% | −0.31 | 45% |
| pullback −1 ATR20 | 252 | +0.18% | −2.69% | 31.4% | 0.09 | [−2.97, +4.71] | 30.3 | −1.73% | −39.65% | 0.00 | 51% |

Every per-trade mean carries a one-way cluster-robust t on the **entry month**, because 450 trades
drawn from 83 coins whose entries cluster into market-wide trend turns is not 450 observations. The
naive t on the shipped configuration roughly halves under that clustering (2.90 → 1.45).

### 4.1 Entry timing does not exist — and the placebo is how we know

200 random-entry draws: same symbols, same filters, same exit, entry day drawn uniformly from that
symbol's eligible days instead of the signal day.

| placebo pool | placebo mean | p5 | p95 | REAL rule | TIMING edge | real's percentile |
|---|---|---|---|---|---|---|
| shipped: eligible + trend-on days | +4.81% | +1.85% | +7.71% | +5.16% | +0.35pp | 56.5 |
| gated: eligible + trend-on, stop-room s=0.75 | +7.09% | +3.88% | +10.19% | +8.46% | +1.37pp | 76.0 |
| survivor: eligible + trend-on days | +9.64% | +6.07% | +13.27% | +11.55% | +1.91pp | 78 |
| **survivor: eligible + trend-on AND at a 30d high** | **+13.74%** | **+9.80%** | **+18.13%** | **+11.55%** | **−2.19pp** | **22** |

**Once you condition on the state the rule requires, entering on the first such day is worse than
entering on a random such day.** Selection is worth +9.6% to +13.7% net per trade. Timing is worth
between −2.2pp and +1.9pp, which is nothing. Everything in this test that looked like a timing edge
is a selection effect wearing a timing costume — and that is the direct answer to the "right
timings" half of the owner's direction.

### 4.2 The pullback paradox — the largest effect measured anywhere, and it is unusable

Paired on the same signal, both rules filled, exit not involved:

| rule | n paired | cheaper than immediate | t_clu | 95% CI (bps) | cheaper on | × round trip |
|---|---|---|---|---|---|---|
| MA20 pullback | 234 | **+6.16%** | **10.16** | [+503, +738] | 84% | 20.5× |
| MA50 pullback | 208 | **+6.18%** | **7.47** | [+467, +789] | 81% | 20.6× |
| prior 10d low | 179 | **+9.49%** | **14.19** | [+819, +1086] | 95% | 31.6× |
| −1 ATR20 | 203 | **+8.60%** | **16.32** | [+764, +963] | 100% | 28.7× |

The pullback price is genuinely 6-9% cheaper when it arrives, with the largest t-statistics in the
entire study. **And that is exactly why the rule fails.** MA50 pullback, 10-day window, all 442
signals:

| | n | share | the deferred half, vs the t+1 open |
|---|---|---|---|
| a pullback comes | 222 | 50% | **−5.85%** |
| no pullback, deadline fill at t+11 | 220 | 50% | **+7.55%** |
| probability-weighted | 442 | 100% | **+0.82%** worse |

This is the single most seductive table in the document and the reason a conditional average must
never be read as an expectation.

### 4.3 Scaling in, priced honestly

The usual fee argument for scaling in is simply wrong: bps are proportional to notional, so N clips
of 1/N pay **the same 15 bps** in total. What it actually pays is drift — 1 clip +8.46%, 2 clips
+8.57%, 3 clips +7.85%, 5 clips +7.30%. Adding a 2 bps penalty per extra fill changes the third
decimal. Conditional two-clip variants: 8 cells, all worse (net/trade 8.59% → 4.82-6.02%, CAGR
20.57% → 10.9-13.8%).

### 4.4 The cost floor is a HORIZON floor, not a cost-level floor

| max hold H | n | mean GROSS | × round trip | mean NET | t_clu | boot 95% CI | CAGR |
|---|---|---|---|---|---|---|---|
| **1d** | 544 | **−0.92%** | −3.1× | **−1.22%** | **−4.56** | **[−1.77, −0.67]** | −9.72% |
| 2d | 534 | −0.32% | −1.1× | −0.62% | −1.50 | [−1.43, +0.23] | −4.93% |
| 3d | 525 | −0.03% | −0.1× | −0.33% | −0.66 | [−1.37, +0.67] | −2.04% |
| 5d | 511 | +0.18% | 0.6× | −0.12% | −0.18 | [−1.44, +1.22] | 0.00% |
| **10d** | 493 | +0.39% | 1.3× | **+0.09%** | 0.09 | [−1.92, +2.16] | 0.03% |
| 20d | 474 | +1.52% | 5.1× | +1.22% | 0.64 | [−2.21, +5.18] | 4.01% |
| 40d | 463 | +5.46% | 15.6× | **+4.37%** | 1.45 | [−1.10, +10.34] | 15.18% |
| 60d | 453 | +3.35% | 11.2× | +3.05% | 1.08 | [−1.98, +8.37] | 9.54% |

**H=1 is the only significantly negative result in the entire study.** Break-even is about ten
days. Cost *level* barely matters — moving 0 → 40 bps per side moves the tightened book's CAGR
21.59% → 18.88%. The variable that matters is cost per holding day:

| configuration | trades/yr | mean hold | fee %/yr of NAV | fee %/month | **bps per holding day** | CAGR |
|---|---|---|---|---|---|---|
| eligible, vol60 ≤ 1.00 (shipped) | 43.8 | 13.1d | 1.64% | 0.137% | **2.29** | 15.18% |
| eligible, vol60 ≤ 0.70 | 23.6 | 16.3d | 0.89% | 0.074% | **1.84** | 22.51% |
| **the surviving cell (§5)** | **18.7** | **22.9d** | **0.60%** | **0.050%** | **1.31** | 22.11% |
| **the fast-test plumbing profile, measured** | **~1,119** | **0.33d** | **16.4%** | **1.35%** | **90.9** | **−14.6%** |

**The cost accounting reproduces the shipped profile exactly**, which is why the last row can be
trusted: 1.35%/month of fees implies a mean position of 4.89% of NAV against the profile's declared
`target_pct_nav: 0.05`; annualised, 16.4% of fee drag against a measured net of −14.6%. The fees
*were* the loss. And `config/profiles/fast-test.yaml` already records that `risk.max_fee_pct_per_month:
0.01` is exhausted in about 22 days at that cadence — so the limit existed, it just sat far enough
above the bleeding to let a month of it happen first.

### 4.5 The minimum-edge gate, measured in both forms

λ was measured, not assumed: λ = E[gross] / E[sd20·√H] = **0.228** full sample, and **+0.558 /
−0.113 / −0.124** across the three regimes.

**(a) As literally specified** — refuse unless λ·sd20·√H ≥ k·0.30%, at the 40-day horizon in use:

| k | σ_H threshold | refused | n | NET/trade | CAGR | Sharpe |
|---|---|---|---|---|---|---|
| off | — | 0 | 463 | +4.37% | 15.18% | 0.64 |
| **3** | 3.95% | **0** | 463 | +4.37% | 15.18% | 0.64 |
| **5** | 6.58% | **1** | 462 | +4.38% | 15.20% | 0.64 |
| 10 | 13.16% | 68 | 413 | +4.25% | 13.28% | 0.58 |
| 15 | 19.74% | 190 | 337 | +2.32% | 5.18% | 0.32 |
| 20 | 26.32% | 378 | 212 | +0.33% | −1.27% | 0.06 |

**Vacuous at k=3 and k=5, and costly above.** Crypto volatility is so large relative to 30 bps that
a lower bound on the available move never binds at the horizon this system trades, and above k=10 it
refuses the low-volatility names, which are the good ones.

**(b) The dual form, which is the specification that works** — H_min = (k·c / (λ·sd20))²:

| k | gross needed | H_min at sd20 p10 | p50 | p90 | refuses a 0.33-day hold for |
|---|---|---|---|---|---|
| 3 | 0.90% | 4.1d | 1.2d | 0.5d | 98.5% of eligible coin-days |
| **5** | **1.50%** | **11.5d** | **3.5d** | **1.5d** | **100%** |
| 10 | 3.00% | 45.9d | 13.8d | 5.9d | 100% |

Refusal rate by intended horizon at k=5: H=1 **96.7%**, H=2 76.1%, H=3 52.5%, H=5 30.8%, H=10
14.0%, H=20 4.5%, H=40 **0.2%**. That is the shape a gate should have — invisible where the system
operates, absolute against what lost the money.

**(c) And the inequality that actually moves P&L is the same variable bounded ABOVE**, so the fixed
stop is not inside the coin's own noise (s = stop / σ_H, 10% stop):

| s | sd20 cap | n | NET/trade | t_clu | 95% CI | P(stop) | CAGR | MaxDD | Sharpe |
|---|---|---|---|---|---|---|---|---|---|
| off | — | 463 | +4.37% | 1.45 | [−1.10, +10.34] | 38.9% | 15.18% | −44.70% | 0.64 |
| 0.4 | 3.95%/d | 257 | +6.78% | 1.86 | [+0.18, +13.66] | 27.2% | 19.13% | −26.76% | 0.97 |
| 0.5 | 3.16%/d | 156 | +8.52% | 2.06 | [+0.71, +16.87] | 18.0% | 17.44% | −23.10% | 1.06 |
| 0.6 | 2.64%/d | 98 | +9.87% | 1.93 | [+0.64, +20.98] | 13.3% | 13.05% | −13.10% | 1.05 |

### 4.6 And that is not a new gate — it is the vol leg again

| control | n | NET/trade | t_clu | 95% CI | P(stop) | hold | CAGR | MaxDD | Sharpe |
|---|---|---|---|---|---|---|---|---|---|
| vol60 ≤ 1.00 (shipped) | 463 | +4.37% | 1.45 | [−1.10, +10.34] | 38.9% | 13.1d | 15.18% | −44.70% | 0.64 |
| vol60 ≤ 0.85 | 360 | +5.19% | 1.77 | [−0.35, +11.31] | 37.2% | 13.8d | 18.96% | −40.93% | 0.76 |
| **vol60 ≤ 0.70** | 216 | **+7.69%** | **2.26** | **[+1.32, +14.67]** | 27.8% | 16.3d | **22.51%** | **−26.69%** | **1.10** |
| vol60 ≤ 0.60 | 144 | +8.37% | 2.09 | [+1.33, +16.68] | 18.8% | 17.2d | 16.52% | −20.32% | 1.12 |
| vol60 ≤ 0.50 | 84 | +7.40% | 1.52 | [−0.06, +18.40] | 15.5% | 19.3d | 8.23% | −11.77% | 0.84 |
| the new sd20 stop-room gate at s=0.5 | 156 | +8.52% | 2.06 | [+0.71, +16.87] | 18.0% | 15.8d | 17.44% | −23.10% | 1.06 |
| **BOTH at once** | 145 | **+5.51%** | 1.54 | [−1.04, +12.91] | — | 16.8d | 10.69% | −26.10% | 0.72 |

**Applying both is worse than either. They are the same effect measured twice.** So ship the leg,
not a second control — and that is why §5's R1 is a config number and not a new check. A plateau,
not a peak, over a 45-cell vol × age × ADV grid: of the 27 cells with vol_max ≤ 0.70, **100% beat
the shipped cell on net/trade, 100% on MaxDD and 100% on Sharpe**. Across seven alternative
selection signals it wins 6 of 7 on net/trade and 7 of 7 on MaxDD; the single exception is "no
trend signal at all", which says precisely what the vol cap is — a **stop-survivability condition
on a trend trade**, not an alpha source. Every regime improves: −2.09 / +10.48 / −2.26 becomes
**+0.92 / +12.11 / +1.54**.

### 4.7 Time of day, day of week, the funding clock — a clean no

| test | result |
|---|---|
| best entry hour, paired against the 00:00 open | 15:00 UTC, **+13.5 bps** = 0.45× the round trip, t_clu **0.64**, CI [−31, +54] bps |
| max \|t_clu\| over 23 paired hourly trials | **1.85** against an expected maximum from 23 zero-skill trials of **2.00** — fails outright |
| split-half stability | correlation **−0.21**; same sign in **6 of 23** hours; H1's best hour (23:00, +53.9 bps) is H2's worst (**−60.0 bps**) |
| Asia 00-07 UTC | **costs** 14.4 bps (t_clu −1.75) — the one directional reading, and it says immediate is right |
| funding clock, pre- vs post-settlement | +3.3 vs −3.5 bps, \|t_clu\| ≤ 0.29 |
| day of week, as an entry offset | max \|t_clu\| **1.60** over 7 trials against an expected maximum of **1.46**; Mon, Wed and Sun flip sign between halves |

Day of week is now refuted twice — `growth-audit.md` §2.5 rejected it as an on/off rule, and this
rejects it in the only remaining form, an entry offset.

### 4.8 Where the money actually is — the exit mix

450 trades, 40-day cap, 15% stop:

| exit reason | n | share | NET/trade | mean hold | best single |
|---|---|---|---|---|---|
| hard stop | 97 | 21.6% | −15.30% | 10.0d | −15.30% |
| trend-off (ensemble < 0.5) | 242 | 53.8% | −5.51% | **8.0d** | **+0.36%** |
| 40-day time stop | 111 | 24.7% | **+46.31%** | 37.5d | +361.76% |

**76% of trades lose. The entire P&L comes from the 24.7% that survive to the time stop**, and the
trend-off exits at eight days are a pure cost sink whose best single outcome is +0.36%. The sd20
gradient is the mechanism: across sd20 quintiles the stop-out rate falls 35.6% → 4.4% and net/trade
rises +1.37% → +10.70%, monotone. A minimum holding period on the *discretionary* exit only (stops
never blocked) is return-neutral — +7.69 / +7.67 / +7.79 / +8.03 / +8.21 / +7.94% at 0/3/6/10/15/20
days, all CIs overlapping — while cost per holding day falls 1.84 → 1.38 bps. **It buys cost-rate
control, not return**, and that is the honest claim for R3.

---

## 5. The combined proposal — the smallest set of rules

### 5.1 The rules

```
MEMBERSHIP (point-in-time, computed by the resolver, tier 2 config)
  R1  vol60 ann.  <= 0.70          on satellite AND major        (from 1.00 / absent)
  R2  listing age >= 1460 days     on satellite AND major        (from 730 / absent)
      adv90       >= $10M          unchanged from growth-audit §1.5
  R6  at a new 20-40 day high      OBSERVE ONLY for one quarter — logged, gates nothing

ENTRY (strategy, unchanged — this rule forbids changes rather than making one)
  R5  immediate, one clip, full size, at the next bar's open.
      No pullback wait. No scale-in. No time-of-day or day-of-week condition.

GATE (deterministic, tier 2, refusal only)
  R3  min_edge:  min_hold_days = max(10, ceil((k*c / (lambda_floor*sd20))^2))
                 k = 5, c = 0.0030, lambda_floor = 0.20 FIXED, never re-fitted
                 plus the lambda-free auditable twin:
                 cost_bps_per_holding_day <= 3.0   (= 30 bps / 10 days)
  R4  max_fee_pct_per_month: 0.01 -> 0.0025
```

That is the whole proposal: three config numbers, one new gate check, and one prohibition.

### 5.2 What it measures

Net-of-cost edge per trade, with a cluster-robust t on the entry month and a month-block bootstrap
CI, at the sleeve's real 10% stop and a 40-day cap:

| configuration | n | trades/yr | NET/trade | hit | t_clu | 95% CI | hold | CAGR | MaxDD | Sharpe |
|---|---|---|---|---|---|---|---|---|---|---|
| shipped tier (vol60 ≤ 1.00) | 463 | 55.6 | +4.37% | 19.7% | 1.45 | [−1.10, +10.34] | 13.1d | 15.18% | −44.70% | 0.64 |
| R1 only (vol60 ≤ 0.70) | 216 | 25.9 | +7.69% | 28.7% | 2.26 | [+1.32, +14.67] | 16.3d | 22.51% | −26.69% | 1.10 |
| R1 + R6 at 20d | 174 | 20.9 | **+11.06%** | 43.7% | **3.24** | **[+4.30, +17.92]** | 22.0d | 23.54% | −18.49% | 1.21 |
| R1 + R6 at 30d | 156 | **18.7** | **+11.55%** | 45.5% | **3.18** | **[+4.28, +18.48]** | 22.9d | 22.11% | −19.66% | 1.18 |
| R1 + R6 at 40d | 149 | 17.9 | +11.49% | 45.6% | 3.12 | [+4.15, +18.73] | 23.3d | 20.55% | −19.70% | 1.07 |

- **Net-of-cost edge:** **+11.06% to +11.55% per trade** = **36.9× to 38.5×** the 0.30% round trip.
  At the bootstrap CI's lower bound (+4.15%) it still clears by **13.8×**.
- **Event frequency:** **18.7 to 20.9 trades per year** per sleeve at 8 slots, mean hold 22-23 days,
  time in market 15.1% of the 8-slot capacity, fee drag **0.60%/yr = 0.050% of NAV per month** at
  **1.31 bps per holding day**. For contrast, the family the brief asked about would have generated
  108 peer-lead-lag events a year, all of them worthless.
- **Versus the exclusion-filter baseline:** +6.7 to +7.2pp of net edge per trade, +5 to +8pp of
  CAGR, and the drawdown falls from −44.70% to −18.49%. This is the comparison that matters, because
  the exclusion filter is what the repo already proposes to ship.
- **Versus BTC buy-and-hold on the same window** (+28.54% CAGR, Sharpe 0.72, −76.63%): it **loses 5
  to 8 points of CAGR**, and wins 57 points of drawdown and 0.35-0.49 of Sharpe. On the longer
  windows of `an1`/`an2`, where BTC is +48.4-48.9% at Sharpe 0.95-0.96, nothing here is close on
  return.
- **Plateau, not peak:** ten breakout lookbacks from 10d to 365d all positive, all three regimes
  positive at every setting but one, and all 12 cells of an ensemble-threshold × vol_max grid have
  CIs excluding zero (ci_lo +0.47 to +4.35, t_clu 2.05 to 3.31).
- **Skew-robust where the shipped cell is not:** winsorised 10%/10% the proposal is **+5.57%
  (t 3.82)** against the shipped cell's **−0.33% (t −0.44)**; capped at +25% it is **+3.28%
  (t 2.87)** against the shipped cell's **−1.59% (t −2.53)**; dropping 2023 **and** 2024 it is
  +3.89% against −3.19%; leave-one-symbol-out it is positive for all 29 folds. The top 1% of trades
  carry **16%** of net P&L against the shipped cell's **47%**.
- **Purged (40d) and embargoed (10d) walk-forward** on the lookback: pooled out-of-sample
  **+13.84%/trade, t_clu 2.69, CI [+3.90, +24.07]**. And **fixed beats adaptive** — choosing the
  threshold out of sample gives +6.61% with a CI including zero, against +8.42% for simply fixing
  the middle of the plateau, exactly as `growth-audit.md` §4.3 requires.

### 5.3 The current regime, stated before anything else is read

| configuration | 2025 on $20,000 | 2026 YTD |
|---|---|---|
| shipped tier (vol60 ≤ 1.00) | **−$4,700** | +$3,725 |
| R1 (vol60 ≤ 0.70) | +$1,653 | +$838 |
| **R1 + R6 (30d high)** | **+$4,123** | **+$1,113** |

And the number that must travel with that table: across **52 configurations** measured on
2025-01-01 → 2026-08-31, **0 have a bootstrap 95% CI excluding zero**, the median is **−0.26% net
per trade** and **29 of 52 are negative**. The surviving cell's own 2025-26 slice is n=46, +3.46%,
t_clu 1.46, CI **[−0.91, +7.64]**. The improvement over the shipped configuration is real and it is
not statistically distinguishable from zero in the regime we are actually in.

### 5.4 What it is, and what it is not

**It is a calmer book and a smaller one.** It does not beat BTC on return and this document does not
claim it does. The thing in this project that *does* beat holding BTC on return, drawdown and Sharpe
at the same time is `dip-strategy.md` §0.3's **BTC/ETH trend ensemble on the core** (+43.1% CAGR /
−45.6% / Sharpe 1.08 over 9.11 years against hold's +38.6% / −83.2% / 0.58). **This proposal is what
stops the satellite sleeve leaking; it is not what makes it earn.** The core carries the return, the
satellite sleeve carries a 10% cap that `growth-audit.md` §2.5 already prices as an option premium
rather than an edge, and the honest sequencing is: ship the core change first, then these.

**And on the owner's framing, as a matter of measured fact.** Every route to *more* trades reduced
net-of-cost edge per trade: the looser tier (55.6 → 151.5 trades/yr moved +4.37% → +0.82%), shorter
horizons (H=40 → H=5 moved +4.37% → −0.12%), extra entry conditions that fired more often, and
scaling in. The answer is **fewer AND better**, and the arithmetic reason is one line: **cost is
paid per fill, and edge accrues per day held.** 18.7 trades a year against the shipped 43.8 and the
plumbing profile's ~1,119.

---

## 6. The statistical bar

### 6.1 Trials, counted

| source | selection trials | screens |
|---|---|---|
| standing count carried by `dip-strategy.md` §6.1 | 7,443 | ~930 |
| `an1` — trajectory analogues | **0** (all 75 recorded as screens: the run produced no candidate, so no maximum was ever taken) | 75 |
| `an2` — lifecycle and peer lead-lag | **57** | 3 |
| `an3` — entry timing and the gate | **265** | — |
| **cumulative** | **7,765** | **≈1,008** |

`an1`'s 75 are screens under `edge-audit/references/method.md` §3a: the study was exploratory,
produced no change proposal, and deflating the standing hurdle by trials that could not have
produced a change corrects for a selection that never happened. They are recorded for ever and do
not raise the hurdle. `an2`'s and `an3`'s do, because both produced candidates.

### 6.2 The hurdle, from the repo's own estimator

`runs/features/sampling.py: expected_max_sharpe`, verified against its pinned self-test values
(0.862 at N=10, 1.050 at 50, 1.187 at 200 on T=9.1) and against each study's own arithmetic:

| N | what N is | E[max Sharpe] | against BTC 0.72 (an3 window) | against BTC 0.95 |
|---|---|---|---|---|
| 265 | `an3` alone, T=8.3y | 1.270 | **1.99** | — |
| 322 | this study's selection trials, T=8.3y | 1.308 | **2.03** | 2.26 |
| **7,765** | **cumulative, T=8.3y** | **1.559** | **2.28** | **2.51** |
| 7,765 | cumulative, T=9.11y (full panel) | 1.488 | 2.21 | 2.44 |

**The best book Sharpe produced by anything in the three studies is 1.29** (the 10-day-high cell;
`an3` quoted 1.23 for the cell it recommended). Against the cumulative hurdle of **2.28** the
shortfall is **0.99**. Nothing here clears, and neither does anything else in this repository's
history — `dip-strategy.md` §6.2 reached the same place with the trend ensemble at 1.08 against
2.066.

### 6.3 The two verdicts disagree, and the cumulative one binds

`growth-audit.md` §4.3 set the precedent that a **membership or threshold rule** is judged on the
per-trade net edge with a cluster-robust t, a plateau across a grid and consistency across regimes —
"a Sharpe is not the right test and should not be quoted". On that test:

| bar | value | best measured t_clu | clears? |
|---|---|---|---|
| expected best \|t\| from 265 zero-skill trials (`an3`'s own count) | 2.88 | 3.31 | **yes**, by 0.43 |
| expected best \|t\| from 322 (this study's selection trials) | 2.94 | 3.31 | **yes**, by 0.37 |
| **expected best \|t\| from the cumulative 7,765** | **3.81** | **3.31** | **no**, short by 0.50 |

**Report the failing one.** `edge-audit`'s hard stop is explicit — N only grows, the family resets
only when a change the loop authored reaches `merged` — so the number that governs a change proposal
today is 7,765 and the per-trade t bar is 3.81. On that bar **nothing in this document clears
either**, and §9's items are justified as *coverage* and *cost* measurements rather than as edge
claims wherever that is possible. One caveat in the other direction, recorded rather than used: the
cumulative count is dominated by `dip-strategy.md`'s 6,720-cell portfolio grid, which is a sweep
over one family and not 6,720 independent hypotheses. That is an argument for settling the counting
convention (§9 item 12), not for lowering the number now.

### 6.4 Power — what a live period can and cannot settle

`runs/features/sampling.py: years_to_detect`, 80% power, two-sided α = 0.05:

| comparison | years to detect |
|---|---|
| the surviving cell 1.21 vs BTC 0.72 | **98** |
| R1 alone 1.10 vs BTC 0.72 | 156 |
| lowvol+age≥4y 0.68 vs BTC 0.96 | 270 |
| **the best analogue book 0.55 vs the exclusion filter it re-encodes 0.61** | **5,096** |
| R6 at 30d (1.18) vs R6 at 20d (1.16) | 66,107 |

The fourth row is the one to keep: **you could run the analogue engine for five thousand years and
not prove it differs from the filter it is a degraded copy of.** `modes.live.min_test_days: 90`
proves the plumbing and nothing about edge.

---

## 7. How this enters the decision passes

The constraint first: **nothing here becomes a direct order signal, and the gate stays the only
thing that authorises a trade.** R3 is a *refusal* — it can only make a trade not happen. R1, R2 and
R6 are *membership*, so they act by the resolver declining to assign a tradeable tier, which
resolves the cap to zero and lets the existing `tier` check refuse the order. No model can widen any
of them: all four are tier 2.

### 7.1 Deterministic features computed by Python — the trustworthy layer

| feature | where it is computed | where it acts |
|---|---|---|
| `vol_ann_60d` **and its cross-sectional rank at T** | `runs/signals/features.py: _pair_features`, panel `ml/features.py: f_vol` + `cross_sectional_rank` | R1 membership, and evidence |
| `listing_age_days` | resolver, already computed | R2 membership, and evidence |
| `adv90` median quote volume | resolver, already computed | membership, unchanged |
| `dd_from_ath` | `ml/features.py: f_drawdown_from_ath` | evidence and ML only |
| `at_new_high_20d/30d/40d` | resolver; `ml/features.py: f_dist_from_high` is the panel form | R6, **observe-only for one quarter** |
| `sd20`, `min_hold_days`, `cost_bps_per_holding_day` | `strategies/riskgate.py`, from `self._returns(pair)` — which `beta_cap` and `corr_cap` at lines 892-894 already load, so no new plumbing | R3, the new `min_edge` check |

`min_edge` goes into `CHECK_ORDER` after `fee_budget` and before `min_notional` — it is a plan-shape
refusal, so it belongs with the budget checks and ahead of the order-feasibility ones. It must
**not** join `EXIT_CHECK_ORDER`: a minimum holding period that could block an exit would be a stop
that a cost rule can veto, and `mechanics.is_risk_exit` exists precisely so risk exits are never
blocked.

**One regression test is mandatory and `growth-audit.md` §1.2 already demands it:** the volatility
feature must be ranked **across the cross-section at a point in time**, never as the coin's own
rolling percentile. The natural implementation is the wrong one — bucketing each coin's vol against
its own trailing 365-day percentile makes P(90d dd < −40%) non-monotone and inverts the sign in
2019-22. This study adds the same warning for age: `age` is Binance-**listing** age, so R2 is a
vintage effect as much as a maturity effect, and any implementation that substitutes a "token age"
from an external source is measuring something this panel never tested.

### 7.2 Evidence handed to the model tiers

Added to the `CANDIDATE FEATURES` block of the decide prompt (`growth-audit.md` build item 7, tier 1),
as numbers the model must cite rather than judgements it must form:

1. **This candidate's `min_hold_days`.** The single most useful new input. Today a model can author a
   `plan` with any horizon and discover the refusal at the gate; handed the floor up front, the
   per-candidate `horizon_hours` of schema v5 becomes a field with a computed lower bound. A
   candidate whose `horizon_hours` implies a hold shorter than `min_hold_days` is refused, and the
   model is told so before it writes it.
2. **This candidate's vol band and its measured P(90d drawdown < −40%)** — 3.5 / 16.8 / 35.0 / 48.9 /
   50.4% — and **its age bucket's**: 51.7 → 29.6 → 19.0%. These answer "if it drops, when do I sell"
   with a number rather than a feeling.
3. **The refutation list**, so a model cannot re-derive what is already dead. A `scan` output of the
   form "this coin's chart looks like X in 2021" must be rejected at `validate` (tier ≥ 3) with a
   citation, not weighed at `decide`. The list is §9's item 11.

**What is deliberately not handed over:** the analogue distribution, the DTW distance, the peer
cluster, P(analogue ≥ +100%), the entry hour and the weekday. Each is refuted with a trial count.
Handing a model a refuted feature and instructing it not to use the feature is worse than not
computing it.

### 7.3 What becomes an ML feature

For the forecasting stack in `ml/` — which, per its own `__init__`, forecasts nothing yet and exists
to make a later forecast honest:

- **Keep as features:** the five scalars that reproduced the whole analogue signal — vol20/vol60,
  drawdown from ATH, log age, window amplitude in own vol units, vol120. Four already exist in
  `ml/features.py`; the amplitude-in-own-vol-units ratio is the only addition, and it is one line.
- **ADD AS A LABEL, NOT A FEATURE — the most valuable thing in this study.** Forward 90-day
  **drawdown**, beside the forward return label in `ml/labels.py`, scored in `ml/metrics.py`. The
  single strongest measurement anywhere in the three studies was an analogue moment predicting
  realised forward drawdown (raw IC +0.1031, NW t 7.35, same sign in all three regimes, residual
  +0.0819 after momentum and beta). It failed as a tradeable signal and it is the right forecasting
  *target*: it says, for the third time in this repo after DVOL and funding, that this market's
  public information predicts the **second moment**, not the first. A forecasting stack aimed at
  returns is aimed at the harder half.
- **Do not add:** any k-NN or DTW analogue feature, any peer-cluster or cluster-laggard feature, any
  time-of-day or day-of-week feature, any own-history percentile form of volatility or funding.
- **Keep the machinery, not the hypothesis.** The panel builder, the split guard, the
  truncation-invariance assertion, the deliberate-leak positive control, the matched control and the
  placebo are reusable and they are the only reason these negatives can be trusted. `ml/features.py:
  assert_no_lookahead` and `ml/splits.py: assert_no_leakage` already exist; §9 items 5 and 6 extend
  them to the panel build and to `strategy-lab`'s protocol.

### 7.4 Where the multi-pass panel weighs them

`config/models.yaml` already configures the `decide` panel: opus@max and opus@high as votes,
fable@max as adjudicator, quorum 2, `min_confidence: 0.6`, confidence taken as the **minimum**, and
the verdict enum `[hold, add, trim, rotate, exit, abstain]` — the *shape* of the action, never the
weights. `run_panel` still has no caller outside tests (`growth-audit.md` build item 6).

Nothing in this study changes that design, and that is the point: **every finding here is
membership or horizon, and neither is a weight.** They enter as inputs to the votes, not as a new
verdict and not as a number the panel averages. The right division of labour is:

| stage | tier | what this study puts there |
|---|---|---|
| `scan` | ≥ 2 | nothing new. It may surface a name; it may not argue an analogue |
| `validate` | ≥ 3 | the refutation list, as grounds for rejecting a thesis with a citation |
| `decide` | ≥ 4, never local | `min_hold_days`, the vol band's P(dd), the age bucket's P(dd), per candidate |
| `adjudicate` | ≥ 4 | unchanged — it sees whatever the votes saw |
| the gate | no model | R3's refusal, and the tier refusal that R1/R2 produce |

And the disagreement rates the panel journals are what would later license a cheaper pass — which is
the same evidentiary standard this document applies to itself.

### 7.5 One prohibition that must be written into config, not just intended

**λ_floor may never be re-fitted, per regime or otherwise.** Measured λ is +0.558 in 2023-24,
−0.113 in 2019-22 and −0.124 in 2025-26 — the wrong sign in two of three regimes. R3 therefore
enforces a **necessary** condition (cost must be small relative to the move the coin's own
volatility makes available over the intended hold) and **cannot certify a positive expectation**.
That is why λ_floor is pinned at a conservative 0.20 rather than the fitted 0.228, and why the
prohibition belongs in the config comment and the `x-effects` block where a future run will read it,
not in a design document it may never open.

---

## 8. Honest limits

**The negative is the strong part of this document, and it is strong.** The analogue idea fails at
its core, not at its margins: shape-only matching has a non-overlapping t of 0.66 at W=90 and −0.12
at W=180, and the scalars alone reproduce the full signal to three decimals. A better matcher cannot
rescue that — the information was never in the shape. Peer lead-lag is refuted by a placebo, which
is the strongest form of refutation available. Entry timing is refuted by a placebo in the other
direction, which is rarer and more useful: the signal day is measurably *worse* than a random day in
the same state.

**What this cannot establish.** The 90-day analogue IC rests on ~30 non-overlapping cross-sections
carrying ~66 effective observations — enough to say "no economically usable edge", not enough to
distinguish IC +0.012 from zero. Three regimes is three, and 2019-22 carries the whole 90-day result
(+0.0895 against +0.0189 in 2023-24) while 2025-26 has six non-overlapping windows at h=90, so its
t-statistics are decorative.

**Twelve walk-forward blocks is not a powered test.** 7/12 and 4/12 both carry a standard error near
14 percentage points. It can rule out a large effect; it cannot establish a small one.

**The one fragment nobody could kill outright** is the analogue drawdown moment, which holds +0.0277
(t 4.64) after the exclusion filter's own axes with the same sign in all three regimes. It is filed
as a failure on three stated grounds — non-monotone inside the tradeable universe, dead at W=180,
and an economic spread one seventh of vol60's — and a longer sample could legitimately revisit it.
§7.3 sends it to the ML label set rather than to a trading rule, which is the honest disposition.

**One market-timing cell in this study produced +16.0% CAGR** (the cross-sectional mean analogue
signal above its own expanding median, W=90/h=30, Sharpe 0.56, MaxDD −73.1%). Both its immediate
quantile neighbours lose money (−2.2% at q30, −1.9% at q70), its IC test is insignificant with the
wrong sign, and it was one cell of 29. It is reported because hiding it would be worse, not because
it means anything — a peak, not a plateau, which is this repo's own definition of unusable.

**A bug was found and fixed mid-study, and it changed conclusions.** A stop hit on the entry bar
left a position holding a slot for ever (27 of 463 trades). Pre-fix, `an3` was about to report "the
10% stop at the shipped vol limit gives CAGR 0.84% and MaxDD −73.8%"; the true figures are 15.18%
and −44.70%. Every book-level number here is post-fix; per-trade numbers were never affected. **The
20-40 day breakout finding only appeared after the fix** — that is, it came from continuing to
search, which is why its t is reported against a corrected expected maximum rather than against 2.0.

**The books are panel approximations, not Freqtrade backtests.** No risk gate, no tier caps, no beta
or correlation cap, no `min_position_pct_nav`, no rebalance band, no order feasibility against
Binance symbol filters, no monthly or daily loss stop. Any number would move once the gate applies,
and the direction is down. The 8-slot equal-weight book also runs only ~15% invested at the
surviving configuration, which understates what the same edge earns at the sleeve's real gross and
overstates its drawdown resilience relative to a concentrated book.

**Timeframe mismatch with the live sleeves.** Exits are evaluated on daily closes and filled at the
next daily open; the sleeves run on 4h. More sharply: §4.5(c) and §4.6 rest on a **fixed percentage**
stop. `trading.defaults.stoploss.atr` exists and is disabled, and enabling it changes the mechanism
those sections measure — the whole of §4.5(c) and §4.6 would need re-measuring first.

**Survivorship barely bites in `an3`, and that is a limitation not a strength.** Only 9 of the 83
ever-eligible names are dead, three of those are ticker migrations rather than failures, and **zero
trades ended in a delisting** — so the harsh −100% convention changes nothing at all there. The
panel is honest; this particular test does not stress it. `an1` and `an2` do: both re-ran every
conclusion under the harsh convention and `an1`'s advantage *widened* (+0.0246 against +0.0237).

**The eligible universe is narrow and starts late.** The filter admits 83 symbols ever out of 615,
and eligibility cannot begin before 2020-08-15 because `age ≥ 3y` binds against a store starting
2017-08. "2019-22" is really 2020-08 to 2022-12 and holds 17-22 trades at vol60 ≤ 0.70. 2023-24
carries 93 of the surviving cell's 156 trades and essentially all the statistical weight.

**Skew.** The surviving cell is much healthier than the shipped one but it is still a right-tail
business: drop its ten best trades and +11.55% becomes +3.34%.

**The 30d-versus-60d rebalance gap is a noise floor.** Halving the cadence added +12pp of CAGR to an
identical selection while saving about 0.7%/yr of cost — 17× smaller. Differences of ~10pp between
books in these tables are not distinguishable, so do not rank them by a few points.

**Age is a median-and-hit-rate edge with no mean edge, and no exit rule was measured.** The
mechanical way to convert one into the other is to cut the left tail — a stop or a profit ladder —
and `growth-audit.md` §1.4 already says 77.5% of +100% run-ups are given back within 60 days while
no position carries a take-profit level. Until an exit rule is measured, the honest status of the
whole lifecycle result is: **a better filter for what to allow into the book, with no demonstrated
way to turn it into return.**

**Three effects of R2 and R1 on the live book are not measured here.** `growth-audit.md` §1.5's
tighter tier (vol ≤ 1.00, age ≥ 3y, ADV ≥ $10M) already cuts the 31 traded pairs to 14. R1 and R2
cut further and the surviving count is a **required precondition** of build item 3, not something to
estimate — the resolver must be run and the list printed before the change is written.

**No funding, open interest or basis entered any representation.** 2.57M funding prints across 653
perps were used only for the funding-clock hour test. An analogue defined partly by leverage state
is a different, untested hypothesis.

**Daily bars only.** Nothing here says anything about entry timing inside a day, and a sub-daily
peer lead-lag (hours, not days) is not ruled out by any of it. It is also the regime where 0.30% is
hardest to clear, and where the plumbing profile already paid 1.35% in fees to net −1.29%. **Do not
fund that search on the strength of this null.**

**Panels end before today.** `an1` ends 2026-08-31 — 24 days short — and `an2` ends 2026-09-24.
`an1`'s candidate pool is on a 5-day grid, unverified at W=30 where 5 days is a sixth of the window.
`an2`'s peer lead-lag was not re-run under the harsh delisting convention (the horizons are 1-20
days, so the omission is probably immaterial, but it is an omission), and used one clustering
lookback, one linkage and one cadence.

**The market control is the cross-sectional median, not a fitted factor model.** It removes slightly
less than a two-factor model would, which flatters the age result. The excess-over-BTC variant is
**negative in every age bucket** (−5.25% at 5y+ over 30d) and is the more conservative reading.

**No study saw a green test suite end to end, and none of them changed a file.** `an3` ran
`earn-test an3 -q` over `tests/strategies`, `tests/test_foundation` and `tests/test_evals` — the risk
gate, config load and sync, the backtest API and metrics, everything this work touches — and got
**1,024 passed in 141.67s**. The full-suite runs in all three workspaces were still executing after
one to two hours with five to twelve competing pytest processes from other sessions saturating the
host. `an2` reported one failure, and it reproduces on inspection and is **pre-existing and
unrelated**: `tests/test_foundation/test_contracts.py::test_no_task_may_ask_for_a_single_turn`
asserts `pytest.raises(ValidationError)` at line 281, while `ops/models_config.py:606` now catches
pydantic's `ValidationError` and re-raises `ops.config.ConfigError`, which subclasses `Exception` and
not `ValidationError`. The config layer still refuses `max_turns: 1` correctly; only the expected
exception type is stale. It is a one-line test fix and it is build item 13. Since none of the three
studies modified the working copy, those runs are a **baseline**, not a check on this work, and no
passing suite is claimed for it.

---

## 9. Build list, in priority order

Tier per `CLAUDE.md`. Nothing here is switched on by this document. Each item carries the
measurement that justifies it, its share of the trial count, and whether the deflated hurdle applies.

**1. Advance `knowledge/state/trial_counter.json` before any figure here is quoted at a change
gate. Tier 2 (`knowledge/state/**`, human only).** +322 selection trials and +78 screens from this
study, on top of `dip-strategy.md`'s ≈7,443 and ≈930 → **7,765 selection / ≈1,008 screens**. The
file is **empty on this checkout**, which means every hurdle any proposal quotes today is
under-corrected. At N=7,765 over T=8.3y the expected best zero-skill Sharpe is **1.559**, the hurdle
against BTC's 0.72 is **2.28** against a best measured 1.29, and the expected best |t| is **3.81**
against a best measured 3.31. **This item is first because it is the item that makes every other
item's justification checkable**, and because it is the one that says no.

**2. `risk.max_fee_pct_per_month` 0.01 → 0.0025. Tier 2 (`config/earn.yaml`).** Measured: the
surviving configuration spends **0.050-0.074% of NAV per month**, so 0.25% leaves 3.4× to 5× of
headroom; the plumbing profile spent **1.35%** and would have been halted after **~5.6 days**
instead of the ~22 that `config/profiles/fast-test.yaml` records. Trials: **0 as an edge claim** —
this is a budget, and a fee rate is an accounting identity, so no hurdle applies. Cheapest change in
the study and it needs no model. **One consequence to decide with open eyes:** `risk` is not in
`ops.config.PROFILE_ALLOWED_PREFIXES`, so a profile cannot loosen this, and the fast-test profile
becomes un-runnable for thirty days. Its own comment already says it must not be left running; a
ten-hour run spends ~0.019% of NAV and is unaffected.

**3. Tighten the vol leg to 0.70 and set a real age floor of 1460 days on both tradeable tiers.
Tier 2 (`config/earn.yaml` + resolver).** `universe.tiers.satellite` today requires **only**
`min_median_quote_volume_usdt: 5000000` — no volatility ceiling, no age bar beyond the 180-day
watchlist floor. Add `max_ann_vol_60d: 0.70` and `min_listing_age_days: 1460` to **both**
`satellite` and `major`. No new gate check is needed: the resolver stops assigning a tradeable tier,
the cap resolves to zero, and the existing `tier` check refuses the order. Measured: net/trade
+4.37% → **+7.69%** (t_clu 1.45 → 2.26, CI [−1.10, +10.34] → [+1.32, +14.67]), CAGR 15.18% →
22.51%, MaxDD −44.70% → **−26.69%**, Sharpe 0.64 → 1.10, stop-out 38.9% → 27.8%; better in **all
three regimes**; **100% of 27 cells** with vol_max ≤ 0.70 in a 45-cell grid beat the shipped cell on
net/trade, MaxDD and Sharpe; wins in 6 of 7 alternative trend definitions and 7 of 7 on MaxDD. For
the age leg: median forward-30d excess **+2.73% [+1.76, +3.51]** at 5y+, monotone across 22 buckets
(Spearman +0.939, p=1e-10), P(90d dd < −40%) **51.7% → 19.0%** intersected with low vol. Trials:
**59 of the 265** (45-cell grid + 7 alternative signals × 2 tiers). Hurdle: on `growth-audit.md`
§4.3's cross-sectional-structure standard it passes; on the deflated Sharpe bar it fails, as does
everything in this repo's history. **Precondition: run the resolver and print the surviving pair
list first** — §8 says why that number must not be estimated. Pick the middle of the plateau, never
the peak.

**4. `risk.min_edge` — the minimum-holding-period gate. Tier 2 (`config/earn.yaml` +
`strategies/riskgate.py`).** A new `min_edge` entry in `CHECK_ORDER` between `fee_budget` and
`min_notional`, **never** in `EXIT_CHECK_ORDER`. `min_hold_days = max(10, ceil((k·c /
(λ_floor·sd20))²))` with **k = 5, c = 0.0030, λ_floor = 0.20 fixed and never re-fitted**, plus the
λ-free auditable twin `cost_bps_per_holding_day ≤ 3.0`. `sd20` comes from `self._returns(pair)`,
already loaded by `beta_cap` and `corr_cap`, so there is no new plumbing. Measured: **1 refusal in
463** at the operating horizon, i.e. zero cost where the system operates, against **100%** refusal
of a 0.33-day intended hold, 96.7% of H=1, 76.1% of H=2 and 30.8% of H=5. The 10-day floor is the
**measured** break-even: −1.22% at H=1 (t_clu −4.56, CI [−1.77, −0.67] — the only significantly
negative result in the study), −0.12% at H=5, +0.09% at H=10, +4.37% at H=40. k=5 because k=3 lets a
two-day trade through 74% of the time and k=10 costs 1.9pp of CAGR while refusing the low-vol names
that are the good ones. Trials: **46 of the 265** (6 k-values + 28 H×k cells + 12 band cells).
Hurdle: **not applicable** — this item claims no return. Its claim is coverage: it costs nothing
where the system operates and refuses what lost the money, and §7.5's prohibition on re-fitting
λ_floor must ship in the config comment with it. Check the interaction with
`trading.defaults.scheduled_dca` (weekly, `chunk_pct_nav: 0.05`) before merging: a scheduled
accumulation is not a trade with an intended exit horizon and must be exempt by design rather than
by accident.

**5. The panel as a checked-in fixture, and the three assertions that made these negatives
trustworthy. Tier 2 (`data/`, `tests/`, `evals/`).** The artefacts are in WSL `/home/shourya/{an1,
an2,an3}` and will not survive a reboot — which is how the previous panel was lost, and
`growth-audit.md` build item 13 already asked for this. Three assertions, all of which ran in this
study and none of which is in the suite: (a) the **split guard** measured against the previous
*valid* close with a 10-day survival requirement — LUNA went dark 2022-05-14..05-30, so a naive
gap rule misses it, and 17 tickers split of which 14 are leveraged DOWN-token rebases; (b)
**truncation invariance** — rebuild every feature from a truncated history and require bit-equality
(`ml/features.py: assert_no_lookahead` exists; extend it to the panel build, where `an1` measured
max |difference| = 0.000e+00 at 2023-06-25 and `an2` at five separate dates); (c) a **deliberate-leak
positive control** — a variant with the causality constraint removed must score detectably better,
or the test cannot see leakage at all (`an1`: honest non-overlapping t 0.10 against leaking 5.21).
Trials: 0. This is infrastructure.

**6. Make a matched control and a placebo mandatory in `strategy-lab`'s protocol. Tier 1 (skill
body) + tier 2 (`evals/`).** **The single most transferable finding in this document.** Peer
lead-lag looked like +0.87% to +4.26% against the universe median and was entirely reproduced by any
same-day coin that had not moved (+0.14% to +1.15%, no CI excluding zero) and by random
pseudo-clusters (one of three seeds beat the real thing). `an3`'s random-entry placebo converted an
apparent timing edge into −2.19pp. **Two of the three studies' decisive refutations came from a
control, not from a t-statistic.** A candidate without a matched control and a placebo is not
reviewable, and `edge-audit` should refuse it the way it already refuses a row count and an unpurged
score. Trials: 0.

**7. Recent strength as an observe-only membership condition. Tier 2 (resolver feature) + tier 1
(prompt).** Ship `at_new_high_20d/30d/40d` as a logged boolean; gate nothing for one quarter.
Measured: +7.69% → **+11.06-11.55%** net per trade, t_clu 3.12-3.24, CI [+4.15, +18.73], hit 28.7%
→ 43.7-45.6%, Sharpe 1.07-1.21, positive in all three regimes, plateau across ten lookbacks,
purged/embargoed OOS +13.84% (t_clu 2.69). Trials: **23 of the 265**; best t_clu 3.24 clears the
265-trial bar of 2.88 and **fails** the cumulative 7,765-trial bar of 3.81. **Two caveats must
travel with it in the config comment:** the placebo says the fresh-breakout *day* is worth −2.19pp
against a random day in the same state, so it is a state filter and not an entry trigger; and inside
this membership the trailing-30-day-return threshold (+8.98%) and the cross-sectional momentum rank
`growth-audit.md` §2.5 rejected at −13.5% CAGR (+9.44%, CAGR 25.75%) produce statistically
indistinguishable books, so it is a redundant expression of one coin-state and **must not be sold as
a new signal**. It ranks below items 2-4 deliberately: they are the ones that do not need it.

**8. Add a forward-drawdown label to the ML harness. Tier 2 (`ml/labels.py`, `ml/metrics.py`).**
Forward 90-day drawdown beside the forward return label, with its own scoring. Measured
justification: the strongest thing in three studies was a drawdown moment predicting realised
forward drawdown (raw IC +0.1031, NW t 7.35, same sign in all three regimes, residual +0.0819 after
momentum and beta) — it failed as a trade and it is the right target. This is the third independent
confirmation, after DVOL and funding, that this market's public information predicts the second
moment. Trials: 0 — a retarget, not a search.

**9. Add `min_hold_days` and the two drawdown tables to the decide prompt's candidate block. Tier 1
(`prompts/research.v5.md`, `prompts/stages/validate.v1.md`).** Rides on `growth-audit.md` build item
7 rather than duplicating it. Three additions: the candidate's `min_hold_days`, so `horizon_hours`
has a computed floor before the gate refuses it; the candidate's vol band and age bucket with their
measured P(90d dd < −40%); and the refutation list as grounds for a `validate` rejection. Trials: 0.

**10. The cross-sectional-rank regression test for the volatility feature. Tier 2 (`tests/`).**
`growth-audit.md` §1.2 asked for this and it is still absent: a test that **fails** if `vol_ann_60d`
is implemented as the coin's own rolling percentile rather than a cross-sectional rank at T.
Measured cost of getting it wrong: P(90d dd < −40%) goes non-monotone (40.6 / 34.1 / 33.8 / 40.9 /
47.2%) and inverts in 2019-22, where the *calmest* own-vol quintile carried the **highest** tail
risk at 53.5%. R1 is the reason this becomes urgent — it is now a membership rule, so the wrong form
would silently select the wrong coins. Trials: 0.

**11. Record the dead ends where a future run and a future model will hit them. Tier 0
(`crypto-research.md` §2) + tier 1 (prompts).** The list below. Fourteen families, each with its
measurement. Trials: 0.

**12. Settle two conventions a human owns. Tier 1 (`edge-audit/references/method.md`) + tier 0.**
(a) The hurdle convention clash `dip-strategy.md` §6.4 named — `baseline + expected_max_sharpe` (the
skill's own rule, used throughout this document) versus `expected_max_sharpe` alone
(`growth-audit.md` §2.1). (b) Whether an N-cell grid sweep over one family counts as N selection
trials: the cumulative 7,765 is dominated by one 6,720-cell grid, and §6.3 applies the conservative
reading while noting it is arguable. Settle both once, in the skill that owns the definition, rather
than per report.

**13. Fix the one stale test. Tier 2 (`tests/`).**
`tests/test_foundation/test_contracts.py::test_no_task_may_ask_for_a_single_turn` expects
`ValidationError` while `ops/models_config.py:606` now raises `ops.config.ConfigError`. The
behaviour under test is correct; the expected exception type is stale. One line. Reported because it
is currently the only red in the directories this work touches, and a red suite makes the next
run's evidence unreadable.

### Dead ends, recorded so nobody rebuilds them

* **Trajectory analogues in every form.** §1. Shape-only IC +0.0123, non-overlapping t 0.66; scalars
  alone reproduce the whole signal; negative inside the tradeable universe at every window and
  horizon; every one of 24 costed portfolios worse than the filter it re-encodes, and negative in
  the $5M universe with −97% to −99% drawdowns.
* **Dynamic time warping.** §0.1. Two of three configurations go the wrong way and all three are
  inside the noise. Do not pay for it.
* **The growth-analogue class — "find the next one".** §1.4. Top decile P(doubling) 5.69% against a
  5.43% base, with a *worse* median return, and the residual flips sign between regimes.
* **Lifecycle stage timing.** §2. The curve is monotone with no local maximum anywhere; there is
  nothing to time, only a filter to set.
* **Age as a return source.** §2.1. −14.3% CAGR at Sharpe 0.01, the worst in its table. A
  cross-sectional median edge is not a long-only mean edge and must never be reported as one.
* **Peer lead-lag, in every form: clusters, single tightest pair, every delay from d+1 to d+5, every
  horizon from 1 to 20 days.** §3. No CI excludes zero against a matched control, random clusters
  reproduce it, and 2025-26 is significantly loss-making at every horizon.
* **Waiting for a pullback.** §4.2. All four definitions lose on net/trade, CAGR, Sharpe and fill
  rate. The pullback price is genuinely 6-9% cheaper when it comes, and that is why the rule fails.
* **Scaling in.** §4.3. Monotone decline past two clips, and the fee argument for it is arithmetically
  wrong.
* **Volume confirmation on a breakout.** Non-monotone across four thresholds (+6.24 / +6.16 /
  +8.01% against +6.16% for the breakout alone); the 2.0× cell is the maximum of four and is search,
  not signal.
* **Volatility-contraction entries.** Worse than baseline at all three thresholds and worse than
  their own expansion control. The hypothesis has the wrong sign: the *level* of volatility matters,
  not its contraction against its own history.
* **Time of day, the funding clock, and day of week (now refuted twice).** §4.7. Max |t| below the
  expected maximum from noise, split-half correlation −0.21, and H1's best hour is H2's worst.
* **The minimum-edge gate as a lower bound used to refuse a coin.** §4.5(a). Vacuous at k≤5,
  destructive above k=10 because it refuses the low-vol names.
* **A second stop-room gate on top of the tightened vol leg.** §4.6. The same effect measured twice;
  applying both is worse than either.
* **Adaptive selection of any threshold by walk-forward.** Pooled OOS CI includes zero and yearly
  picks flip violently. Pick the middle of the plateau — `growth-audit.md` §4.3, third time.
* **Any take-profit that caps winners at +25%.** It turns the shipped configuration significantly
  negative (−1.59%, t −2.53). The top 1% of trades carry 47% of net P&L there. A ladder must take
  partial profit, never cap the position.
* **More trades as a route to more money.** §5.4. Every widening measured reduced net edge per
  trade.

---

## 10. What would tell us in a quarter that this was right, and what would tell us to stop

**The frame first: a quarter cannot tell you about return.** The surviving configuration makes 18.7
trades a year, so a quarter is about five trades; §6.4 puts statistical separation from BTC at 98
years. A quarter can only tell you whether the mechanism behaves.

| # | check | threshold | where it comes from |
|---|---|---|---|
| 1 | **Fee drag on budget.** Realised fees ≤ 0.10% of NAV per month | measured 0.050-0.074%; alarm at 0.10%; hard stop at the new 0.25% | §4.4. This is the check that would have caught the plumbing profile on day 6 instead of day 22 |
| 2 | **Mean hold ≥ 10 days** and cost per holding day ≤ 3.0 bps | the R3 floor, measured at 1.31 bps | §4.4. If realised holds are shorter than the plan, `min_edge` is being routed around |
| 3 | **`min_edge` refusal rate below 1% of entries** | 1 in 463 measured | §4.5(b). A higher rate means the sleeve is trying to trade a horizon it was not built for |
| 4 | **Trades per year in 12-28** | 18.7 measured | §5.2. Above it, something is whipsawing; below it, a membership rule is stuck |
| 5 | **Stop-out rate ≤ 30%** | 27.8% at vol60 ≤ 0.70 against 38.9% shipped | §4.6. The vol cap's entire mechanism is stop survivability |

**Stop — any one is sufficient:** realised fees above 0.25% of NAV in a Gulf month (the new budget,
i.e. the gate is doing the stopping); mean realised hold below 5 days for two consecutive weeks
(§4.4 puts net/trade at −0.12% there); the `min_edge` check firing on more than 10% of entries; or a
realised drawdown worse than −30% while BTC's own drawdown is shallower than −15%.

**Explicitly not a stop signal:** losing to BTC in a rising market. §5.2 says it will, by 5 to 8
points of CAGR, and §5.4 says the core is where return is supposed to come from. Stopping for that
reason is buying the insurance and cancelling it the month before the fire.

---

## Appendix — provenance

All measurements taken 2026-09-25 on this host. Binance public endpoints and
`data.binance.vision` only; no keys, no paid data. Costs **15 bps per side = 0.30% round trip**
throughout, from `config/backtest.yaml` (10 bps fee + 5 bps slippage).

| workspace | artefacts | not version-controlled |
|---|---|---|
| `/home/shourya/an1` | scripts, feature panel, 25 signal panels (`sig_*.parquet`), 733 symbols / 829,547 candles | yes — build item 5 |
| `/home/shourya/an2` | panel rebuilt from `~/earn-panels/panel_1d.parquet`, `assert_pit.py` (all pass), cluster and placebo output | yes |
| `/home/shourya/an3` | scripts and 29 result files in `out/*.txt`, panel from `~/s5/raw/1d` | yes |

**Survivorship convention:** a coin that stops trading inside the horizon is marked out at its last
traded print. Every `an1` and `an2` conclusion was re-run under the harsher "delisting = −100%"
convention; `an1`'s analogue IC moved +0.0237 → +0.0246 and `an2`'s age result moved by less than
0.03pp. In `an3` the convention changes nothing, because zero trades ended in a delisting (§8).

**Symbol-reuse handling**, three independent implementations agreeing: `an1` split at any >8× up-gap
measured against the previous **valid** close, confirmed only if the new level survived 10 days (17
tickers; LUNA @2022-05-31 at 177,400×, COCOS @2021-01-23, DREP @2021-04-02, plus 14 leveraged
DOWN-token rebases), keeping LUNA's genuine −94% and −99.97% collapse days unsplit. `an2` fired on
24 symbols / 30 break days. `an3` used one general rule for every symbol — any >30-day listing gap
**or** any overnight ratio outside [1/5, 5] — splitting 15 symbols including LUNA, FTT, BNX, COCOS,
DREP, QUICK, STRAX, SUN and VIDT.

**Regimes** were split on calendar dates given in advance — 2019-22 / 2023-24 / 2025-26 — not
discovered. Three regimes is three.

**Trial count for the deflated hurdle: 322 selection trials and 78 screens from this study**, taking
the cumulative counter to **7,765 selection / ≈1,008 screens**. At N=7,765 over T=8.3 years the
expected best zero-skill Sharpe is **1.559** and the expected best |t| is **3.81**. Record these; do
not reset them. `knowledge/state/trial_counter.json` was **not** written by this document — build
item 1, and that file belongs to `edge-audit` and `strategy-lab`.
