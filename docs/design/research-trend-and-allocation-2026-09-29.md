# Research: trend and allocation — can the BTC/ETH trend ensemble earn more without giving its drawdown back?

**Status:** research, decision-grade, mostly negative. Nothing here is switched on. No bot, cron,
unit, console, config, strategy or `.env` was touched; `~/earn-run` was read only (feathers, the
DVOL cache); the panel is `~/earn-panels/panel_1d.parquet`. Workspace `~/earn-wk/re1`. No commit,
no `ruff format`.

**Theme (orchestrator, verbatim):** *"How to get more return out of the book that already works —
the BTC/ETH trend ensemble — WITHOUT giving back its drawdown."* Owner's frame: *"we need most
profit"; "not fewer trades but better trades"; "its supposed to earn not loose".*

**Every number below was computed on this host on 2026-09-29** by
`evals/research/trend-and-allocation/study.py` on the same arithmetic as `dip-strategy.md` §0.3
(`runs/features/trend.py` members, `evals/trend_ensemble_backtest.py` costed book, the shipped
`runs/features/volatility.py` estimator). Costs on throughout: 15 bps/side on BTC/ETH, 30 bps/side
on satellites, cash 0% except where the cash yield *is* the hypothesis, every weight lagged one
full day, book gross never above 1. Sharpe = CAGR / annualised vol (the study's convention; the
mean/std Sharpe is beside it in the JSON). Five hypotheses were **sealed in the hypothesis ledger
before anything was computed**; the seals verified at close.

---

## 0. The answer, before the detail

**Nothing tested raises the ensemble's return without giving back drawdown, at a size that
survives 15 bps and the deflated hurdle. The literature says the same thing, and says why.**

| # | hypothesis | OOS 2019-2026 result vs plain ensemble (49.33% CAGR / −45.62% MaxDD / Sharpe 1.19) | drawdown effect | verdict |
|---|---|---|---|---|
| 1 | vol-target the book with the shipped `sigma_hat` | walk-forward **40.89% / −37.06% / 1.22** (−8.4pp CAGR) | **−8.6pp shallower** | refuted as a return device; it is a drawdown device |
| 2a | BNB as a third core leg | **61.51% / −45.52% / 1.44** (+12.2pp) — all of it 2019-22 (2021: +344% vs +137%); 2023-24 −1.1pp, 2025-26 −0.05pp | unchanged | refuted (regime clause); hindsight selection |
| 2b | 10% satellite basket, top-K by volume, own ensembles, gated on BTC trend | top-4 **48.57% / −45.07% / 1.20** (−0.8pp) | +0.6pp shallower | refuted — a wash against the core it displaces |
| 3 | weight the 15 members by trailing Sharpe | walk-forward **44.46% / −57.59% / 1.01** (−4.9pp) | **−12pp deeper** | refuted, decisively; no persistence to exploit |
| 4 | Simple Earn yield on idle USDT (verified ~1.5-2% Real-Time APR) | **50.83% / −45.04% / 1.23** at 2% (+1.5pp; +1.1pp at 1.5%) | +0.6pp shallower | inconclusive: real arithmetic, not an edge, and the sealed drawdown clause was symmetric |
| 5 | Kaminski-Lo re-entry after the weight hits zero | walk-forward **48.46% / −46.67% / 1.17** (−0.9pp) | −1.1pp deeper | refuted — fires on 30-177 of 2,824 days |

Deflated hurdle for every one of these: BTC hold OOS Sharpe 0.81 + `expected_max_sharpe(N = 7,910,
T = 7.73)` = **2.43**. Best candidate anywhere in this document: 1.44, and it is the hindsight one.
Even counting only this document's 28 trials the hurdle is 1.88. `audit_stats.py check` refused all
three claims worth submitting (§9).

**Why the return side keeps failing, in one paragraph.** A long-only spot book capped at gross 1
can express every signal only as a *reduction* from full exposure (`dip-strategy.md` §0.2 item 3).
Every allocation device tested here — vol scaling, member re-weighting, re-entry delays — is a
reduction schedule. A reduction schedule can move drawdown a lot and return a little, and the sign
of the return effect is a coin flip against costs. The literature agrees: volatility targeting
"reduces the likelihood of extreme returns" and lifts Sharpe only for assets whose vol clusters
without a matching move in expected return (Harvey et al. 2018; Moreira & Muir 2017), and in
real-time out-of-sample form "generally earn[s] lower certainty equivalent returns and Sharpe
ratios than ... the original, unmanaged portfolios" (Cederburg et al. 2020). The ensemble's
equal weight is the 1/N result in miniature: trailing-Sharpe weights have no persistence to work
with (Spearman −0.21 to +0.14, §6). The one thing that *does* add return, the cash yield, is an
interest rate, not a signal — and it is +1.1 to +1.5pp with operational strings attached (§7).

**What this leaves the owner with.** The honest prize is still the drawdown prize
(`dip-strategy.md` §8.6). Two things in this document are worth a human's attention, neither of
which is alpha: (i) vol targeting at a 60% target buys **9pp of drawdown for 1.4pp of CAGR**
(t60, cap 2: 47.96% / −36.72% / 1.31 against 49.33% / −45.62% / 1.19) — a *preference* the owner
could take if −45% is more than the account can stomach, priced at almost double the fee drag
(2.54%/yr vs 1.36%); (ii) idle USDT can earn the standard Simple Earn rate for about +1pp/yr if
the plumbing is willing to hold cash outside the spot wallet. Everything else here is a
published negative that stops the next run spending the same trials.

---

## 1. Research-scout: the ledger check, and what was dropped

`scout.py check` on each of the five one-sentence ideas: **all five `verdict=open`** — none is on
the standing rejection ledger. The nearest entries and why these are not them:

| idea | nearest ledger entry | why it is not that entry |
|---|---|---|
| vol-target the ensemble book | (none; `crypto-research.md` §3 measured vol targeting on BTC alone: Sharpe 0.83 → 0.81, "Sharpe-neutral by construction") | new object (the ensemble book), the shipped HAR+DVOL forecast rather than trailing vol, and the cap>1 variant that can *add* exposure inside gross 1 |
| third asset / satellite basket | `cross-sectional-momentum`, `survivorship-universe` | ranking is by **volume**, not momentum (wide-universe.md measured momentum ranking at t = −7.4); the panel is survivorship-free (747 tickers, dead coins in); each satellite carries its **own** time-series ensemble |
| Sharpe-weighting the members | (none) | `dip-strategy.md` §10.2 item 7 records the members as ~1.23 independent bets but never tested re-weighting |
| cash yield on idle USDT | (none) | not a signal at all |
| Kaminski-Lo re-entry | (none; `analogue-timing.md` names the minimum-holding gate) | an exit-side rule, the open set the ledger itself points at |

Dropped before measurement (this run's own dead ends, recorded here, not in the ledger, which is
tier 2): a **leveraged** vol target (`dip-strategy.md` §4.4 already priced perp funding at 12.25%/yr
per 1× and the mandate is spot-only); ranking satellites by momentum (ledger); a **book-level**
(covariance) vol target as a separate hypothesis (it would have been a third parameter on H1 for
the same mechanism); Garg et al.'s dynamic speed selection as a separate hypothesis (it is a
re-weighting of fast vs slow members and is covered, unfavourably, by H3).

Trial cost spent: **28 selection trials** (12 + 4 + 6 + 3 + 3), added to the *workspace* counter
(`results/trial_counter.workspace.json`); the live `knowledge/state/trial_counter.json` is human-only
and must be advanced by 28 (§11).

---

## 2. What the literature says — sources read, with the one result each contributes

Read in full text: [1], [2]. Read as abstract or publisher page: [3], [4], [7], [8], [9], [10],
[11]. Read through a secondary review because the primary PDF was not text-extractable from this
host: [5], [6]. Not cited as sources because only search snippets were seen: DeMiguel-Garlappi-Uppal
2009 (1/N), Zakamulin 2014 (real-life MA timing).

| # | source | year | the one result this study leans on |
|---|---|---|---|
| 1 | Moskowitz, Ooi, Pedersen, "Time series momentum", *JFE* 104(2) 228-250 | 2012 | 12-month TSMOM positive in all 58 futures; positions sized to **40% ex-ante vol** with a 60-day-centre EWMA — vol scaling is there to make instruments *comparable*, not as alpha; "performs best during extreme markets"; 1-12 month persistence, partial reversal after |
| 2 | Moreira & Muir, "Volatility-managed portfolios", NBER w22208 / *JF* 72 | 2016/17 | scale by c/σ²(t−1): market alpha 4.9%, Sharpe +25%; the mechanism is "changes in factor volatilities are not offset by proportional changes in expected returns"; they claim survival under leverage constraints and transaction costs |
| 3 | Cederburg, O'Doherty, Wang, Yan, "On the performance of volatility-managed portfolios", *JFE* 137 | 2020 | 103 strategies: vol-managed portfolios "do not systematically outperform" in direct comparison; real-time versions "generally earn lower certainty equivalent returns and Sharpe ratios"; cause is instability of the spanning regressions — i.e. the *selected* target does not survive out of sample |
| 4 | Harvey, Hoyle, Korgaonkar, Rattray, Sargaison, Van Hemert, "The impact of volatility targeting", *JPM* 45(1) 14-33 | 2018 | 60 assets 1926-2017, 10% target: Sharpe gain only for "risk assets" (equity, credit), negligible for bonds/FX/commodities; **reduces left-tail severity in every asset class** because left tails happen at elevated vol |
| 5 | Baltas & Kosowski, "Demystifying time-series momentum strategies" (via Return Stacked review) | 2013 | a better vol estimator (Yang-Zhang) cuts turnover ~17%, the TREND rule ~24%, together ~35% of trading costs with no significant performance loss; post-2008 weakness traced to pairwise correlations |
| 6 | Garg, Goulding, Harvey, Mazzoleni, "Momentum turning points" / "Breaking bad trends" (via CXO Advisory review of the 2020 version; *JFE* 149) | 2020/23 | turning point = slow (12m) and fast (1-2m) signals disagree; the more turning points, the worse trend following does; **intermediate speeds beat the average of slow and fast**; a dynamic speed rule 9.4% vs 7.5%/yr over 1971-2019 (gross, 10% vol-scaled) |
| 7 | Kaminski & Lo, "When do stop-loss rules stop losses?", *JFM* 18 234-254 | 2014 | under a random walk 0/1 stop-loss rules "always decrease a strategy's expected return"; under momentum they can add value; 50-100 bps/month "stopping premium" during stop-out periods (US equities → bonds, 1950-2004) |
| 8 | Hurst, Ooi, Pedersen, "A century of evidence on trend-following investing", *JPM* 44(1) | 2017 | TSMOM positive in every decade since 1880 across 67 markets; positive in 8 of the 10 largest 60/40 drawdowns |
| 9 | Rozario, Holt, West, Ng, "A decade of evidence of trend following investing in cryptocurrencies", arXiv 2009.12155 | 2020 | crypto behaves like 20th-century commodities for trend following; the abstract's "255% walkforward annualised returns" is quoted here only to be marked **unverified and not reproduced** |
| 10 | "Time-series momentum in cryptocurrency markets: a pre and post spot Bitcoin ETF analysis" (Zenodo 19671502) | 2026 | vol-scaled monthly TSMOM, six assets, 2018-2026: 18.0% → 28.6% annualised, Sharpe 0.82 → 1.22; **underperformed buy-and-hold by 7.7% pre-ETF**, outperformed post; the difference is not significant (p = 0.58) |
| 11 | Binance, "Enjoy Up to 6% APR with USDT Flexible Products" (announcement, read 2026-09-29) | 2026 | promotion 2026-06-04 → 07-03: ≤ 200 USDT earns a 4% bonus tier on top of "approximately 2% Real-Time APR"; **above 200 USDT only the Real-Time APR applies**; redemption "Instant"; "may not be available in your region". A 2026-08 listing showed ~1.5% Real-Time |

What the literature predicted, before the tests: vol targeting shrinks tails and roughly holds
Sharpe [1, 4], adds return only when vol moves without expected return moving [2], and loses its
gain once the target is chosen in real time [3]; combining speeds beats picking one [6], but that is
what equal weight already does; stop-and-re-enter earns a premium only under momentum [7], which
the ensemble already harvests. The results below are what the theory said they would be.

---

## 3. Method — shared by every hypothesis

* **Panel.** `~/earn-panels/panel_1d.parquet`: 841,491 daily candles, 747 USDT tickers, 2017-08-17 →
  2026-09-24, dead coins in. BTC/ETH closes from it; `sigma_hat` from the 4h feathers in
  `~/earn-run/data/binance` (read-only) and the cached DVOL series.
* **Baselines, always both.** BTC buy-and-hold and the plain BTC/ETH 15-member ensemble (0.5 × own
  weight per leg).
* **Window.** Headline OOS 2019-01-01 → 2026-09-24 (2,824 days, 7.73y). Full panel 2017-08 → 2026-09
  (3,326 days) shown where it adds something.
* **Regimes.** 2019-22 (n = 1,461), 2023-24 (n = 731), 2025-26 (n = 632).
* **Walk-forward** for every hypothesis with a free parameter (H1, H3, H5): expanding in-sample
  from 2018-01-01 to 30 days before each OOS calendar year (the embargo), one OOS year at a time,
  2019-2026. Selection rule is the pre-registered claim itself: highest in-sample CAGR among
  variants whose in-sample MaxDD is within 2pp of the plain ensemble's; **when none qualifies the
  plain ensemble is used** — the null is the fallback, not the peak. The in-sample surface is
  reported whole (plateau, not peak). H2 and H4 fit nothing; their K, cap and APR were preset, so
  the whole window is reported as out-of-sample and the honest label is "unfitted".
* **Effective sample** (edge-audit `labels`, BTC 1d triple-barrier, 10-bar limit): 3,273 rows,
  mean uniqueness 0.1766, **EFFECTIVE_N 577.9** (5.66 rows per independent observation); 5-fold
  purged split, **0 leaks**, 115.6 per fold. ETH: 582.3.
* **Deflated hurdle** (edge-audit `hurdle`): N = 7,443 (`dip-strategy.md` §6.1 cumulative) + 439
  (`ml-forecast.md`) + 28 (this document) = **7,910**; T = 7.73 → `expected_max_sharpe` = 1.617,
  hurdle = 0.81 + 1.617 = **2.43**. Full-panel equivalent (T = 9.11, baseline 0.58): 2.07.
* **Power** (edge-audit `power`, 80%, two-sided): 1.31 vs 1.19 → **1,944 years**; 1.44 vs 1.19 →
  470 years; 1.22 vs 0.81 → 143 years.

Baselines on the headline window:

| book | CAGR | vol | Sharpe | MaxDD | avg gross | turnover/yr | fee/yr | months < −10% |
|---|---|---|---|---|---|---|---|---|
| BTC buy-and-hold | 49.70% | 61.41% | 0.81 | −76.63% | 1.00 | 0 | 0 | 18 |
| **plain BTC/ETH ensemble** | **49.33%** | 41.50% | **1.19** | **−45.62%** | 0.501 | 9.04× | 1.36% | 7 |

Per regime, plain ensemble vs hold (CAGR / MaxDD / Sharpe): 2019-22 **67.33 / −45.62 / 1.37** vs
45.35 / −76.63 / 0.62; 2023-24 53.70 / −28.34 / 1.47 vs **137.56 / −26.15 / 2.82**; 2025-26 **11.01 /
−25.98 / 0.46** vs −6.06 / −52.97 / −0.14.

---

## 4. `2026-09-29-ta-vol-target` — volatility-target the book with the shipped `sigma_hat`

**Hypothesis (pre-registered 2026-09-29, seal `5b8b6c3c23a9a09a`).** Scaling each leg by
`clip(target / sigma_hat, 0, cap)` with the shipped HAR+DVOL 7-day forecast, book gross ≤ 1,
predicts a higher OOS CAGR than equal exposure at 1d with no deeper max drawdown. 2 parameters
(target ∈ {20, 30, 40, 50, 60, 80} vol points, cap ∈ {1, 2}).

**Falsifier.** OOS CAGR gain < 2.0pp, or MaxDD > 2.0pp deeper. **Triggered: yes** — CAGR gain
**−8.44pp**; MaxDD **+8.56pp shallower**.

**Ledger check** `open`. **Trials** 12. **Sample** EFFECTIVE_N 577.9. **Costs** 15 bps/side.

`sigma_hat` replayed exactly as `runs/features/volatility.py: forecast` does it: HAR walk-forward
on 4h realised variance (expanding, refit every 30 days, min 365 training days), blended 50/50 with
the walk-forward `a + b·DVOL` map where DVOL exists (1,952 days from 2021-03-24), trailing-30d
fallback before the first fit (358 days). 3,310 forecast days per asset.

### Result, out-of-sample

| | walk-forward (CAGR-s.t.-MaxDD) | walk-forward (Sharpe-selected, diagnostic) | plain ensemble | BTC hold |
|---|---|---|---|---|
| Sharpe | 1.22 | 0.99 | 1.19 | 0.81 |
| CAGR | **40.89%** | 30.62% | **49.33%** | 49.70% |
| MaxDD | **−37.06%** | −36.74% | −45.62% | −76.63% |
| fee drag/yr | 2.14% | 1.96% | 1.36% | 0 |
| months < −10% | 4 | 2 | 7 | 18 |

Chosen per OOS year: 2019 t20/cap1 (one in-sample year, 2018, a bear — the lowest target had the
best CAGR), 2020-21 t60/cap2, 2022-23 t80/cap1, 2024-26 t60/cap2. Excluding 2019: 43.94% /
−37.06% / 1.23 (plain over the same 2020+ years: 174.8, 136.6, −20.6, 55.5, 52.0, 7.5, 16.0).
This is Cederburg et al. in miniature: the Sharpe-selected variant, the way the literature picks,
lands at **0.99**, below the unmanaged book.

### By regime (walk-forward book vs plain)

| regime | n | candidate CAGR / MaxDD / Sharpe | plain | delta CAGR |
|---|---|---|---|---|
| 2019-22 | 1,461 | 48.84 / −37.04 / 1.42 | 67.33 / −45.62 / 1.37 | **−18.5pp** |
| 2023-24 | 731 | 52.30 / −32.17 / 1.41 | 53.70 / −28.34 / 1.47 | −1.4pp |
| 2025-26 | 632 | 13.41 / −27.71 / 0.50 | 11.01 / −25.98 / 0.46 | +2.4pp |

### Parameter surface (OOS 2019-2026, CAGR / MaxDD / Sharpe / fee)

| target | cap 1 | cap 2 |
|---|---|---|
| 20 | 16.35 / −13.85 / 1.27 / 1.03 | identical (scalar never reaches 1) |
| 30 | 24.75 / −20.14 / 1.28 / 1.51 | 24.77 / −20.14 / 1.28 / 1.53 |
| 40 | 32.48 / −26.04 / 1.29 / 1.81 | 33.29 / −26.04 / 1.29 / 2.03 |
| 50 | 38.69 / −31.40 / 1.28 / 1.90 | 41.54 / −31.56 / 1.31 / 2.37 |
| **60** | 43.37 / −35.71 / 1.27 / 1.83 | **47.96 / −36.72 / 1.31 / 2.54** |
| 80 | 47.18 / −42.49 / 1.22 / 1.59 | 54.02 / −47.99 / 1.26 / 2.62 |

The plateau is the finding: **every** cell scores Sharpe 1.22-1.31 against the plain 1.19, i.e.
vol targeting adds about 0.1 of Sharpe uniformly — Moreira-Muir's mechanism, at one tenth of their
equity-market size — while CAGR rises monotonically with the target and drawdown deepens with it.
The only cell that beats the plain book on CAGR (t80/cap2, +4.7pp) does so by taking a **deeper**
drawdown (−47.99% vs −45.62%): more exposure in calm markets is more exposure, not more edge. The
cell a human might actually want is t60/cap2: −1.4pp of CAGR for 8.9pp of drawdown and one fewer
−10% month per year, at a fee drag that nearly doubles (1.36% → 2.54%/yr, one-way turnover 9.0× →
17.0×). Baltas-Kosowski's turnover lesson applies — a smoother scalar or a rebalance band would
claw back some of that — and is left untested because it is another trial for at most ~1%/yr.

### Both directions

* **Avoided:** 8.6pp of drawdown; 3 of the 7 months below −10% (all in 2019-22, the March-2020 and
  2022 tails, exactly where Harvey et al. say the left tail lives).
* **Gave up:** 8.4pp of CAGR walk-forward; 18.5pp in 2019-22, where the book's own realised vol ran
  49% against targets of 20-60 and the scalar sat well below 1 through the 2020-21 rally.

### Verdict

`refuted` as a return device — the seal held, the falsifier fired on the CAGR clause. **As a
drawdown device it does what the literature says it does**, and if the owner's constraint is the
−45% worst case rather than the return, t60/cap2 is the priced option: it is not an edge (Sharpe
1.31 against a 2.43 hurdle; 1,944 years to separate from the plain book), it is a preference.

### What would change this

A live realised-vol series longer than 7.7 years — nothing available free. A turnover-aware
scalar (Baltas-Kosowski) could recover ≤ 1%/yr of the fee drag and would cost a trial.

---

## 5. `2026-09-29-ta-third-asset` — a third core leg, or a rules-selected satellite basket

**Hypothesis (pre-registered 2026-09-29, seal `c525bffc94263b4f`).** BNB with its own ensemble at
1/3 of the book, or a top-K-by-volume satellite basket (≥ 5M USDT 90-day median quote volume, ≥ 180
days listed, own ensemble, 30 bps/side) capped at 10% of NAV and held only while the BTC ensemble
weight exceeds 0.5, predicts a higher CAGR at 1d with no deeper MaxDD. 2 parameters (K ∈ {2, 4, 8},
cap 0.10, both preset from `wide-universe.md` §6). Nothing fitted.

**Falsifier.** CAGR gain < 2.0pp, or MaxDD > 2.0pp deeper, or gain positive in fewer than 2 of 3
regimes. **Triggered: yes, for both variants.** BNB: gain +12.18pp, MaxDD +0.10pp, **positive in 1 of
3 regimes**. Satellites (top-4): gain **−0.76pp**, MaxDD +0.55pp, positive in 1 of 3.

**Ledger check** `open` (nearest `cross-sectional-momentum` / `survivorship-universe`; ranking is
by volume, panel is survivorship-free). **Trials** 4. **Sample** EFFECTIVE_N 577.9. **Costs** 15 / 30 bps.

Satellite universe: 668 USDT tickers after removing stablecoins, fiat pairs, wrapped BTC/ETH and
leveraged tokens; on average **70 names eligible per day** from 2019. Each symbol's ensemble is
computed per contiguous listing segment and a calendar gap > 1 day resets it — the
last-valid-observation rule that splits the LUNA symbol reuse (`dip-strategy.md` §1.2).

### Result (whole window, unfitted)

| | BNB third leg | core 0.90 + top-4 sats | core 0.90 + top-2 | core 0.90 + top-8 | core 0.90 alone | plain ensemble | BTC hold |
|---|---|---|---|---|---|---|---|
| Sharpe | **1.44** | 1.20 | 1.15 | 1.17 | 1.19 | 1.19 | 0.81 |
| CAGR | **61.51%** | 48.57% | 46.59% | 47.37% | 44.59% | 49.33% | 49.70% |
| MaxDD | −45.52% | −45.07% | −44.72% | −44.79% | −41.75% | −45.62% | −76.63% |
| fee/yr | 1.47% | 1.40% | 1.42% | 1.39% | 1.22% | 1.36% | 0 |

The satellite leg on its own (top-4): 3.95% CAGR on an average gross of **0.035**, MaxDD −6.04%,
5,635 name-days held. The 10% of core it displaces was worth **4.74pp** (49.33 − 44.59). So the
basket returns what the core slice would have returned, with 30 bps costs and 70 names of
operational surface — `wide-universe.md` §6's "+0.4pp / −1.5pp" reproduced from a different angle.
Satellite count is not monotone (top-4 > top-8 > top-2), which is noise, not a curve.

### By regime

| regime | n | BNB leg | top-4 sats | plain |
|---|---|---|---|---|
| 2019-22 | 1,461 | **95.46 / −45.50 / 1.84** | 66.20 / −45.07 / 1.39 | 67.33 / −45.62 / 1.37 |
| 2023-24 | 731 | 52.59 / −26.42 / 1.55 | 53.80 / −28.60 / 1.50 | 53.70 / −28.34 / 1.47 |
| 2025-26 | 632 | 10.96 / −26.83 / 0.45 | 10.14 / −25.61 / 0.41 | 11.01 / −25.98 / 0.46 |

Calendar years, BNB leg vs plain: 2019 94.4 vs 51.7; 2020 123.0 vs 174.8; **2021 344.1 vs 136.6**;
2022 −24.2 vs −20.6; 2023 43.2 vs 55.5; 2024 62.6 vs 52.0; 2025 9.5 vs 7.5; 2026 13.1 vs 16.0.

### Both directions

* **Avoided:** nothing on the drawdown side (−45.5% either way); the satellites shave 0.6pp.
* **Gave up:** the BNB result **is one event** — the 2021 exchange-token rally — and BNB was chosen
  *because* it is the third-largest survivor, which is selection on the outcome, the same trap
  `dip-strategy.md` §6.2 labelled "hindsight basket" (1.32) and §2.4 priced at 1.5pp point-in-time.
  Outside 2021 the third leg is a coin flip: five of the other seven years are worse than the plain
  book. The satellites give up 0.8pp and 30 bps a side on every name.

### Verdict

`refuted`. The regime clause fired on both variants, and it fired for the right reason: a +12pp
headline carried entirely by one asset's one year is the shape the pre-registration was written to
catch. **A third core asset is not a research question; it is a universe decision (tier 2), and the
data says it is a bet on one coin's history.**

### What would change this

A point-in-time rule that would have selected BNB in 2019 without knowing 2021 — none was found;
`dip-strategy.md` §2.4 measured the closest candidate ("has survived a cycle") at 1.5pp.

---

## 6. `2026-09-29-ta-sharpe-weighting` — speed-weight the 15 members by trailing Sharpe

**Hypothesis (pre-registered 2026-09-29, seal `8b54eb009bde32da`).** Weights = positive part of
each member's trailing L-day Sharpe, normalised (equal weight when all ≤ 0), or equal weight on the
top 5 by trailing Sharpe; L ∈ {90, 180, 365}. 2 parameters.

**Falsifier.** OOS CAGR gain < 2.0pp, or MaxDD > 2.0pp deeper, or Spearman(trailing Sharpe, next
L-day Sharpe) < 0.10. **Triggered: yes, on all three clauses.** Gain **−4.87pp**; MaxDD **−11.97pp
deeper**; persistence BTC 0.142 / −0.008 / −0.112 and ETH −0.007 / −0.083 / −0.206 at L = 90/180/365.

**Ledger check** `open`. **Trials** 6. **Sample** EFFECTIVE_N 577.9. **Costs** 15 bps/side.

### The decay test first, because it decides the rest

Spearman rank correlation between the 15 members' trailing-L Sharpe and their *next* L days'
Sharpe, non-overlapping blocks from 2019: BTC L90 **+0.14** (20 blocks, 60% positive), L180 −0.01,
L365 **−0.11** (7 blocks, 29% positive); ETH −0.01 / −0.08 / **−0.21** (14% positive). There is
nothing to exploit, and at the horizons the ensemble actually trades the sign is *wrong*: a member
that just did well is, if anything, about to do worse — Garg et al.'s turning points seen from the
inside (the fast members win into the top and lose through the turn). Mean pairwise member
correlation from 2019 is 0.605 (the full-sample 0.802 in `dip-strategy.md` §10.2 is pulled up by
the shared 2017-18 warm-up).

### Result, out-of-sample

| | walk-forward | plain ensemble | BTC hold |
|---|---|---|---|
| Sharpe | 1.01 | 1.19 | 0.81 |
| CAGR | 44.46% | 49.33% | 49.70% |
| MaxDD | **−57.59%** | −45.62% | −76.63% |
| fee/yr | 2.03% | 1.36% | 0 |
| months < −10% | 11 | 7 | 18 |

Chosen per OOS year: L180/top5, L365/linear, L90/linear, L90/linear, L365/linear, L365/top5 ×3.
2020+: 41.45% / −57.59% / 0.97.

### Parameter surface (OOS, CAGR / MaxDD / Sharpe)

| L | linear | top-5 |
|---|---|---|
| 90 | 45.01 / **−57.59** / 0.99 | 47.47 / −49.50 / 1.04 |
| 180 | 49.41 / −50.81 / 1.13 | 48.61 / −49.63 / 1.11 |
| 365 | 48.35 / −45.14 / 1.15 | 48.19 / −46.62 / 1.13 |

**Every cell has a deeper drawdown than equal weight and none has a higher Sharpe.** The longest
lookback is the least bad because it is the closest to equal weight. By regime (walk-forward vs
plain): 2019-22 60.23 / −57.57 / 1.16 vs 67.33 / −45.62 / 1.37; 2023-24 51.61 / −34.13 / 1.32 vs
53.70 / −28.34 / 1.47; 2025-26 7.52 / −27.69 / 0.29 vs 11.01 / −25.98 / 0.46 — worse in all three.

### Both directions

* **Avoided:** nothing.
* **Gave up:** 4.9pp of CAGR, 12pp of drawdown, 0.7%/yr more fees (turnover 13.6× vs 9.0×), and
  the ensemble's one structural virtue — that no integer is chosen (`dip-strategy.md` §9 item 1).

### Verdict

`refuted`, decisively. This is the 1/N result: with ~578 effective daily observations and 15
members worth about one-and-a-quarter independent bets, any estimated weight is estimation error
wearing a Sharpe ratio. The equal-weight ensemble is not a placeholder for a better weighting; it
*is* the better weighting.

---

## 7. `2026-09-29-ta-cash-yield` — earn the Simple Earn rate on idle USDT

**Hypothesis (pre-registered 2026-09-29, seal `7bc8b9970f18b8b3`).** Idle USDT in Binance Simple
Earn USDT Flexible at the standard Real-Time APR predicts a higher CAGR at unchanged MaxDD. 1
parameter (the APR, a quoted rate: 1.5% / 2% / 4%).

**Falsifier.** CAGR gain at 2% APR < 1.0pp, or MaxDD changes by more than 0.5pp. **Triggered: yes,
on the drawdown clause only** — CAGR gain **+1.50pp** (+1.12pp at 1.5%, +3.01pp at 4%); MaxDD
**+0.58pp shallower**. The clause was written symmetric ("changes by") and fired on an improvement.
The seal forbids rewriting it, so the letter decides.

**Ledger check** `open`. **Trials** 3 (screens in substance — the effect is arithmetic).
**Sample** EFFECTIVE_N 577.9. **Costs** 15 bps/side on the book; the yield is applied to
`(1 − lagged gross) × APR / 365` daily.

### What is actually available on Binance spot, verified

From the announcement read on 2026-09-29 [11]: USDT Flexible pays a **Real-Time APR of about 2%**
(a 2026-08 listing showed ~1.5%), variable "every minute", plus a **bonus tier that covers only the
first 200 USDT** (4% during the June-July 2026 promotion). On a 20,000 USDT book with ~50% idle the
bonus tier is worth about **4-8 USDT a year** and is irrelevant; the standard rate is the whole
effect. Redemption is "Instant" for Flexible, inside Binance's daily quick-redeem quota. "Products
or features referred to above may not be available in your region" — UAE availability must be
confirmed on the account, not assumed. There is no USDT "staking"; this is the exchange lending the
balance out, so the counterparty is the same Binance the spot balance already sits with, under
terms Binance can change.

### Result (whole window; nothing fitted)

| | 1.5% APR | **2% APR** | 4% APR | plain ensemble | BTC hold |
|---|---|---|---|---|---|
| Sharpe | 1.22 | **1.23** | 1.26 | 1.19 | 0.81 |
| CAGR | 50.45% | **50.83%** | 52.34% | 49.33% | 49.70% |
| MaxDD | −45.18% | **−45.04%** | −44.45% | −45.62% | −76.63% |

By regime at 2%: 2019-22 69.07 vs 67.33; 2023-24 54.81 vs 53.70; 2025-26 **12.39 vs 11.01** — the
gain is largest, in relative terms, in the chop, because that is when the book is most in cash
(gross 0.38). Calendar years at 2%: 53.4 / 176.5 / 138.0 / −19.2 / 56.5 / 53.2 / 8.6 / 17.7 against
51.7 / 174.8 / 136.6 / −20.6 / 55.5 / 52.0 / 7.5 / 16.0.

### Both directions

* **Avoided:** 0.58pp of drawdown (cash accrues while the book is down).
* **Gave up:** nothing on the return side. Operationally: cash sits outside the spot wallet, so an
  entry needs a redemption first (an extra API step, an extra failure mode, and the risk gate's
  cash accounting has to know where the cash is); the rate is not contractual; regional
  availability is unverified from this host.

### Verdict

`inconclusive` by the letter — and it could not have closed `supported` in any case, because a
Sharpe of 1.23 does not clear the 2.43 hurdle and the close script downgrades on that alone. The
substance is not in doubt: **idle cash earning the standard rate adds about +0.5pp of CAGR per 1%
of APR on this book** (average idle 50%), compounding to +1.1 to +1.5pp at today's 1.5-2%. It is an
interest rate, not an edge; it is the only item in this document whose sign is certain; and it is
worth about 200-300 USDT a year on the owner's 20,000. Whether that pays for the plumbing is an
operator's call (`docs/design/paper-trading-review-2026-09-29.md` lists what the plumbing already
gets wrong with one wallet).

### What would change this

A one-sided re-registration ("drawdown no deeper than 0.5pp") would let the same numbers close
cleanly; the hurdle would still refuse `supported`. The real question is operational and belongs
to `exchange-ops`: whether Simple Earn balances can be read and redeemed through the API the bots
use, and whether the product is offered on the UAE entity.

---

## 8. `2026-09-29-ta-reentry` — a Kaminski-Lo re-entry rule after the weight hits zero

**Hypothesis (pre-registered 2026-09-29, seal `f9579df0c11937de`).** After an asset's ensemble
weight falls to zero, withhold re-entry until the trailing J-day return is positive (J ∈ {10, 20,
40}). 1 parameter.

**Falsifier.** OOS CAGR gain < 2.0pp, or MaxDD > 2.0pp deeper. **Triggered: yes** — gain **−0.87pp**
walk-forward; MaxDD −1.05pp deeper.

**Ledger check** `open`. **Trials** 3. **Sample** EFFECTIVE_N 577.9. **Costs** 15 bps/side.

### Result, out-of-sample

| | J10 | J20 | J40 | walk-forward | plain ensemble | BTC hold |
|---|---|---|---|---|---|---|
| Sharpe | 1.19 | 1.19 | 1.19 | 1.17 | 1.19 | 0.81 |
| CAGR | 49.48% | 49.53% | 49.38% | 48.46% | 49.33% | 49.70% |
| MaxDD | −45.44% | −45.33% | −46.25% | −46.67% | −45.62% | −76.63% |
| days withheld, 2019+ | 30 | 54 | 177 | — | 0 | — |

The rule has almost nothing to act on: the book's weight reaches exactly zero and then turns back
on so rarely that J10 changes 30 of 2,824 days. Average gross moves from 0.501 to 0.494 at most.
By regime the differences are inside ±1pp everywhere (2019-22 66.20 vs 67.33; 2023-24 52.98 vs
53.70; 2025-26 10.48 vs 11.01). 2020+: 48.86 / −39.58 / 1.20 (the 2020+ drawdown is shallower for
every book alike because the −45.6% trough is a 2019-20 event).

### Both directions

* **Avoided:** nothing measurable.
* **Gave up:** 0.9pp of CAGR walk-forward, from re-entering a few days later into rallies the
  Donchian members had already caught.

### Verdict

`refuted`. Kaminski-Lo's stopping premium exists *because* of momentum; a fifteen-member trend
ensemble is already a graduated stop-and-re-entry schedule, and a second one layered on top has no
information the first did not. This confirms `analogue-timing.md`'s reading in the other direction:
the minimum-holding gate stops sub-day churn, but at the daily horizon the ensemble's own hysteresis
is all the re-entry logic there is to have.

---

## 9. Edge-audit — what looked like it might survive, and the refusals

Three claims were worth putting to `audit_stats.py check` (effective_n 577.9, purged, embargo 30,
N = 7,910, T = 7.73, baseline 0.81):

| claim | Sharpe | verdict |
|---|---|---|
| H1 t60/cap2 vol-targeted ensemble, OOS 2019-2026 | 1.31 | **REFUSED**: does not clear the deflated hurdle 2.427 |
| H2 BNB third leg, OOS 2019-2026 | 1.44 | **REFUSED**: does not clear 2.427 (and is hindsight-selected) |
| H4 2% cash yield, OOS 2019-2026 | 1.23 | **REFUSED**: does not clear 2.427 |

Power, in case anyone proposes to "let it run and see": t60/cap2 vs plain, **1,944 years**; BNB
leg vs plain, 470 years; the plain ensemble vs hold, 143 years on this window (86 on the full panel,
`dip-strategy.md` §6.3). Thirty days of paper trading can say nothing about any row in this document.

The decay panel (`decay_panel.py`) was not re-run: no shipped feature changed and the quarterly
re-test belongs to the weekly review.

---

## 10. Honest limits

1. **Nothing here clears the hurdle, including the drawdown option.** Sharpe 1.31 against 2.43. The
   hurdle uses N = 7,910 because that is the family the repo has spent; §3a of `method.md` says a
   family closes only behind a merged change, and none has merged.
2. **Costs are the flat 15 bps.** No intrabar path, no impact, no failed fills. Vol targeting nearly
   doubles turnover, so it is the variant most exposed to the cost model being optimistic
   (`dip-strategy.md` §10.2 item 5: every number moves down once real execution applies).
3. **The walk-forward's first OOS year rests on one in-sample year** (2018). Excluded, H1 goes
   40.89 → 43.94% CAGR; the verdict does not change. The transition between years is not charged a
   rebalance; that flatters the walk-forward books by a fraction of a bp.
4. **`sigma_hat` before 2021-03 is HAR-only** (no DVOL), and before 2018-08 it is the trailing-30d
   fallback. That is exactly how the shipped code degrades, so the replay is faithful to the code,
   not to an idealised forecast.
5. **The regime split is three windows, not three independent samples.** 2023-24 and 2025-26 are
   731 and 632 days; a 2pp CAGR difference over 632 days is well inside noise (sd of annual Sharpe
   ≈ 1/√1.73y ≈ 0.76).
6. **BNB is the exchange's own token.** Beyond hindsight selection, holding it carries issuer and
   venue-concentration risk that a BTC/ETH book does not; that alone should keep it a tier-2 human
   decision, whatever a backtest said.
7. **The satellite universe uses the panel's quote volume** as the ADV proxy; `wide-universe.md`
   used a live `exchangeInfo`-based filter. The 668-symbol eligible set and the ~70 names/day are
   consistent with its ~100-name watchlist but were not reconciled symbol by symbol.
8. **The cash yield is not backtestable as a rate.** The APR series does not exist; the study applies
   today's quoted 1.5-2% flat across 2019-2026, when the true rate ranged from near zero to well above
   (Simple Earn paid double digits at moments in 2021). The number is an illustration of the
   mechanism at today's rate, not a history.
9. **The sealed falsifier for H4 was badly worded** (symmetric drawdown clause). It is recorded as
   written and the outcome is recorded as the letter demands. Lesson for the next pre-registration:
   write drawdown clauses one-sided.
10. **Sources.** Two papers were read in full text, two through secondary reviews (Baltas-Kosowski,
    Garg et al.) because their PDFs were not extractable here; the rest as abstracts. The crypto
    trend paper's "255% walkforward" is quoted only as unverified. DeMiguel 2009 and Zakamulin 2014
    were not read beyond search snippets and are not cited as evidence.
11. **Load rule kept.** One backtest at a time, `nice -n 10`, 1-minute load peaked at 3.4 on a
    12-core host; the whole study runs in about 30 seconds per pass.

---

## 11. Artefacts, and what a human must do

| artefact | path |
|---|---|
| study code | `evals/research/trend-and-allocation/study.py`, `close_out.py` |
| results | `evals/research/trend-and-allocation/results/{baselines,h1,h2,h3,h4,h5}.json`, `results/close/*.json` |
| sealed and closed pre-registrations | `knowledge/hypotheses/trend-and-allocation/2026-09-29-ta-{vol-target,third-asset,sharpe-weighting,cash-yield,reentry}.json` (seal verified at close on all five) |
| edge-audit state | `results/edge_audit.json` (labels, cv, hurdle, power), `results/trial_counter.workspace.json` (28 entries) |
| papers (text where extractable) | `~/earn-wk/re1-papers/` |

Human-only follow-ups (tier 2, none done here):

1. Advance the live `knowledge/state/trial_counter.json` by **28 selection trials** (the workspace
   copy has the 28 named entries to paste).
2. If the owner prefers a −37% worst case to a −46% one at −1.4pp of CAGR and +1.2%/yr of fees:
   the ensemble-book vol target at 60 points, cap 2, is the priced option (§4). It is a preference,
   not a finding, and `sleeve_a.vol.target_annual` already exists as the knob.
3. If ~+1pp/yr for 20k of plumbing is worth it: ask `exchange-ops` whether Simple Earn Flexible
   is offered on the UAE entity and reachable through the bots' API permissions before anything is
   designed (§7).
4. Three candidates for the standing rejection ledger, on a human's say-so: **member Sharpe
   weighting** (§6, dead), **a second re-entry rule on the daily ensemble** (§8, dead), **a third
   core asset chosen by size or survivorship** (§5, hindsight). Vol targeting should *not* go on the
   ledger — it is a working drawdown device that merely is not a return device.

**Not done:** no commit; no `.env` read; no write under `~/earn-run`, `var/**` or the live
`knowledge/state/**`; no bot, cron, unit or console touched; no venue called; `strategies/**`,
`config/**`, `runs/**`, `ops/**`, `console/**` untouched.
