# Research: execution and costs — 2026-09-29

**Theme:** execution-and-costs. **Workspace:** `re3` (`~/earn-wk/re3`, a mirror; nothing under
`~/earn-run` was written, no bot, cron, unit or console was touched, no venue was called).
**Question the owner asked, verbatim:** *"we need most profit"*, *"not fewer trades but better
trades"*, *"its supposed to earn not loose"*. **What this theme can answer:** how much of the
cost line can be recovered without giving the drawdown back, and which of the churn controls
the repo has proposed actually stops churn.

Everything below is measured (scripts in `evals/research/execution-and-costs/`, results in its
`results/` folder), pre-registered before it was measured (`knowledge/hypotheses/2026-09-29-*.json`,
each carrying its seal and the close-out audit), or cited from a primary source that was read.
Costs were on in every number: 15 bps per side (10 fee + 5 slippage, `config/backtest.yaml`) and,
where the theme is the fee itself, the fee schedule verified for this account tier on the day.

---

## 0. The answer, before the detail

1. **The fee schedule, verified today (binance.com/en/fee/schedule, 2026-09-29):** Regular User /
   VIP 0 is **0.100% maker / 0.100% taker**; paying in BNB makes both **0.075%**; VIP 1 needs
   **≥ 1,000,000 USD** of 30-day volume **and ≥ 5 BNB**. The BNB FAQ adds: the discount must be
   toggled on, an insufficient BNB balance means *the original fee is charged* (silently), and the
   25% is "valid until further notice". Maker = taker is still true, so every maker-side idea is
   worth exactly what `crypto-research.md` §1.10 said: zero fee saved at real fill risk.

2. **Two of the five candidate hypotheses were dropped before measurement because they are on the
   standing rejection ledger** (`research-scout`, `verdict=rejected`, entry `execution-timing`,
   matched on *limit maker / passive fill* and *entry timing*). The ledger's reason is quoted in §2.
   They were not "re-tested properly"; that is the rule, and the arithmetic behind it has not
   changed (§1).

3. **BNB fee payment (H-A) is worth 0.215% of NAV per year on the book that earns** (the BTC/ETH
   trend ensemble, 8.62× one-way turnover, fee line 0.862%/yr) **and 1.71%/yr on the plumbing
   profile** (fee line 6.85%/yr). A one-month fee float in BNB is 0.07% of NAV on the ensemble; its
   5th-percentile 30-day loss is 0.02% of NAV, its worst is 0.05%; BNB's daily beta to BTC is 0.92.
   The saving is three times the float, so the float cannot lose enough to matter (break-even loss
   is 300%). The pre-registered falsifier did not trigger. The ledger still records the outcome as
   **`inconclusive`**, because the closing audit compares the headline Sharpe (1.09) to the deflated
   hurdle (2.07) and a fee-schedule fact does not clear a search hurdle — nor should it be asked to.
   Separating Sharpe 1.09 from 1.08 needs about 249,000 years. It is arithmetic, not an edge, and it
   is the only cost lever the ledger leaves open that is not yet switched on.

4. **Hysteresis on the ensemble weight (H-C) is refuted** by its own pre-registration. The best
   band (x = 2/15, act only on a two-member move) saves 0.36 pp/yr of fee drag and moves full-window
   CAGR by +0.6 pp and MaxDD by −1.4 pp — inside noise — but the purged, embargoed walk-forward
   out-of-sample book ends **below** the unbanded ensemble (CAGR 49.05% vs 49.33%, MaxDD −46.25% vs
   −45.62%), which was the second clause of the falsifier. Every wider band and every partial-trade
   fraction loses 2–16 pp of CAGR. The fifteen-member average is already the smoothed position;
   smoothing it again only lags it.

5. **The minimum-holding gate is not the churn control (H-B refuted), and neither is `min_edge`.**
   On a replay of the fast profile calibrated to the freqtrade cell in
   `risk-and-ladder-2026-09-29.md` §2.3 (2,215 trades vs 2,199; net −26.6% vs −26.5%; fees
   10.5%/yr vs 10.3%), the R3 gate as written (block the trend-loss exit before H_min, stops never
   blocked) cuts fees per year by **0.2%** (not the 30% the falsifier demanded), because the fee line
   on that profile is set by the **entry-side** counters — 4 trades a day, 300-minute spacing and the
   1%-of-NAV monthly budget, which refused 42,291 candidate-hours — and not by any exit. What the gate
   changes is where trades leave: 36,200 blocked trend-loss signals become ROI, trailing and **6%
   stop** exits (69 → 377), and net goes from −26.6% to −29.7%. Blocking *every* non-stop exit for
   ten days turns the cost per holding day from 130 bps to 5.8 bps and the calibration-window net to
   +48%, at the price of a **−62% drawdown**, −25% in 2025-26 and −70% MaxDD in 2019-22: a regime bet
   on 2023-24. `min_edge` refuses **0%** of entries under the shipped L3R3 plan and 100% under the old
   L0/R0 plan; it is a plan validator, exactly as `risk-and-ladder` §2.5 describes it.

6. **What this theme cannot do, said plainly.** Nothing here raises the return of the earning book
   without giving the drawdown back. The ensemble's whole cost line is 1.29%/yr; the BNB toggle
   recovers 0.22 of it; the rest is the price of the 45-point drawdown improvement over holding BTC.
   The plumbing profile's 10%/yr of fees is a cadence fact, and the only levers that move it are the
   entry counters (`analogue-timing.md` §5.1 R4: `max_fee_pct_per_month` 0.01 → 0.0025) or not running
   it for P&L — which its own header already says.

---

## 1. What the literature contributes, one result per source

Each entry names the year and the one result it contributes here. Nothing is cited that was not
read; where only an abstract could be read, that is said.

| source | year | the one result used |
|---|---|---|
| Almgren & Chriss, *Optimal execution of portfolio transactions*, J. Risk 3, 5–39 (full text read) | 2000 | The optimal schedule has an intrinsic half-life θ = 1/κ set by volatility, temporary impact and risk aversion, independent of order size under linear impact. As temporary impact → 0 the half-life → 0: for an order whose impact is 0.001 bps against a 10 bps fee (§1.10 of crypto-research), the model's answer is *execute at once*. Slicing a 500-USDT order buys nothing. |
| Gârleanu & Pedersen, *Dynamic trading with predictable returns and transaction costs*, J. Finance 68(6) (2012 working-paper text read) | 2013 | "Aim in front of the target, trade partially toward the aim"; net Sharpe ≈ 20% above the best static rule on commodity futures; proportional-cost models (Constantinides 1986) give no-trade regions. This is the theory behind H-C — and H-C shows the ensemble is already the aim portfolio: partial trading toward it (θ < 1) loses 2–16 pp of CAGR. |
| Hansen, Kim & Kimbrough, *Periodicity in cryptocurrency volatility and liquidity*, arXiv 2109.12142 (full text read) | 2021 | Binance and Coinbase Pro, 2019-01 → 2021-05: volatility and volume spike at 00h and 16h UTC (funding), less at 08h; within the hour there is "a large burst in volatility during the first minute of the hour", medium bursts at :15/:30/:45; weekends lower. The bot's order lands at the bar boundary, i.e. in the most volatile minute of the hour. |
| Aleti & Mizrach; Dyhrberg et al., as summarised in Hansen et al. §1 | 2018–21 | Crypto liquidity peaks when US and London equities are open; the widest spreads coincide with the hours CME BTC futures are closed. Consistent with the ledger's `weekend-effect` finding: quiet, not thin. |
| *Turn-of-the-candle effect in bitcoin returns*, Heliyon (read via PMC) | 2023 | 1-minute candles, seven exchanges, through 2021: returns concentrate at the :00/:15/:30/:45 minutes, ≈ 0.6–1.0 bps per minute, t > 9, attributed to candle-driven algorithms. Magnitude ≤ 1 bp — a tenth of one fee — which is why it is not a hypothesis here (ledger: `execution-timing`, "under 1% of the cost line"). |
| Albers, Cucuringu, Howison & Shestopaloff, *The good, the bad, and latency: exploratory trading on Bybit and Binance*, Quant. Finance 25(6) (abstract only; full text 403) | 2025 | Millions of live orders: taker orders suffer adverse selection — profitable orders fill worse than the book snapshot implied, and *marketable limit orders carry a substantial probability of failing to fill immediately*, rising with volatility, latency and thin books. The fast profile's marketable limit at +3 bps is exactly that order type. |
| *Explainable patterns in cryptocurrency microstructure*, arXiv 2602.00776 (HTML read) | 2026 | Binance perp books 2022-01 → 2025-10: a fixed-depth maker strategy is worse than a taker strategy on small caps and was "repeatedly filled on its bid-side quotes" into the 2025-10-10 crash. Passive provision without a rebate is adverse selection with no compensation — at VIP 0 there is no rebate. |
| Binance fee schedule and BNB FAQ (read) | 2026 | The numbers in §0.1. |

What the literature did **not** offer: a limit-order fill-probability model calibrated to Binance
spot at our size that is free to reproduce (Albers et al. propose one; the paper is paywalled and the
data is theirs), and any evidence that hour-of-day placement is worth more than 1 bp after fees.

---

## 2. Scouting note — the five candidates against the ledger

`research-scout` was run first (`scout.py check`). Its verdicts, in the words a run would actually
use:

| candidate (task brief) | ledger verdict | disposition |
|---|---|---|
| (1) maker-only entries with a bounded wait vs taker: fill rate, adverse selection, net cost | **rejected** — `execution-timing`, matched *limit maker, passive fill* | dropped |
| (2) paying fees in BNB (25%): what it is worth, BNB inventory risk | open | → hypothesis-lab, **H-A** |
| (3) entering on the bar after the signal at the open vs immediately: the cost of slowness | **rejected** — `execution-timing`, matched *entry timing* | dropped |
| (4) minimum-holding gate (k = 5) vs the target-vs-cost gate: which stops churn | open | → hypothesis-lab, **H-B** |
| (5) hysteresis on the ensemble weight: fee drag saved vs return lost | open | → hypothesis-lab, **H-C** |

The ledger's reason for (1) and (3), verbatim: *"Measured twice on the live 1000-level book:
slippage versus mid is 0.001 bps on BTCUSDT and 0.019 bps on ETHUSDT at every size Earn will ever
trade, against a 10 bps fee. Maker equals taker at Binance spot VIP0 and Earn will never leave VIP0
(VIP1 needs $1M/30d; the cap implies about $15k/month, 67x short). Optimising when to execute
optimises under 1% of the cost line. The only real levers are the BNB discount and the number of
orders, both already bounded and already measured by the tca skill."* Today's fee-schedule check
(§0.1) confirms the premise: maker = taker = 0.100%. The task's own framing of (3) — "entering on
the bar after the signal at the open" — is what the profile already does (signal on the closed
candle, fill at the next open); `analogue-timing.md` §4.1 and §4.7 measured entry delay and entry
hour at daily and hourly resolution and found nothing (fixed 5-day delay: +4.14% vs +4.37% per
trade; best hour +13.5 bps with t 0.64, sign flipping between halves).

**Dead ends hit in this run** (recorded here, not in the ledger):

- The two run databases (`test-a-000`, `test-b-000`, read-only) hold 7 trades each. Every entry is
  a dry-run fill at the limit price 170–200 ms after submission, fee 0.1% in USDT, and no book
  snapshot: they can measure neither fill probability nor adverse selection, so even if (1) were not
  on the ledger it could not be tested from them. Their one usable number is the fee share (§3.3).
- Albers et al. is paywalled (tandfonline 403, SSRN 403); cited from the abstract only.

Trial cost of this run: **26 selection trials** added (1 for H-A, 18 for H-C's 6 × 3 surface, 7 for
H-B's k × exit-variant sweep). §6 gives the hurdle.

---

## 3. H-A — 2026-09-29-bnb-fee-discount

**Hypothesis (pre-registered 2026-09-29, seal `62f3908aef47ce16…`)** Paying Binance spot fees in
BNB (verified 0.075% against 0.100% at VIP 0, maker and taker alike) predicts a 25% reduction of
the fee line at a 1-year horizon, net of the P&L of a one-month fee float held in BNB, on the
BTC/ETH trend ensemble and on the fast-profile book.

**Falsifier** dead if the annual saving on the ensemble's measured one-way turnover is below 0.10%
of NAV per year, or if the 5th-percentile 30-day loss on a one-month fee float held in BNB exceeds
the annual fee saving in any of the three regimes.
**Triggered:** no — saving **0.215%/yr** against the 0.10% threshold; worst-regime float q05 loss
**0.022%** of NAV against the 0.215% saving.

**Ledger check** `verdict=open` (nearest entry: `execution-timing`, which names the BNB discount as
one of the two real levers). **Trial count** +1. **Sample** BTC/USDT 1d: 3,273 labelled rows →
**effective_n 523.9**; purged 5-fold, 0 leaks, 33-bar embargo. **Costs** 15 bps/side baseline,
12.5 bps/side (7.5 fee + 5 slippage) for the BNB book.

### 3.1 The ensemble at 15 vs 12.5 bps, both baselines beside it

Panel: `~/earn-panels/panel_1d.parquet` (survivorship-free), the study's conventions
(`evals/trend_ensemble_backtest.py`: one-day lag, cost on every weight change, Sharpe = CAGR/vol).

| window | book | CAGR % | vol % | Sharpe | MaxDD % | one-way turnover/yr | fee drag %/yr |
|---|---|---|---|---|---|---|---|
| 2017-08 → 2026-09 (3,326 d) | btc_buy_and_hold | 38.61 | 66.88 | 0.58 | −83.19 | — | 0.00 |
| | ensemble @ 15 bps (strategy_baseline) | 43.07 | 39.94 | 1.08 | −45.62 | 8.62 | 1.29 |
| | **ensemble @ 12.5 bps (fees in BNB)** | **43.37** | 39.94 | **1.09** | **−45.50** | 8.62 | **1.08** |
| 2019-22 (n = 1,461 d) | btc_buy_and_hold | 45.35 | 72.60 | 0.62 | −76.63 | — | — |
| | ensemble @ 15 | 67.33 | 49.06 | 1.37 | −45.62 | 8.41 | 1.26 |
| | ensemble @ 12.5 | 67.69 | 49.06 | 1.38 | −45.50 | 8.41 | 1.05 |
| 2023-24 (n = 731 d) | btc_buy_and_hold | 137.56 | 48.72 | 2.82 | −26.15 | — | — |
| | ensemble @ 15 | 53.70 | 36.52 | 1.47 | −28.34 | 10.35 | 1.55 |
| | ensemble @ 12.5 | 54.10 | 36.52 | 1.48 | −28.19 | 10.35 | 1.29 |
| 2025-26 (n = 632 d, out-of-sample slice) | btc_buy_and_hold | −6.06 | 43.71 | −0.14 | −52.97 | — | — |
| | ensemble @ 15 | 11.01 | 24.18 | 0.46 | −25.98 | 8.97 | 1.35 |
| | ensemble @ 12.5 | 11.26 | 24.19 | 0.47 | −25.83 | 8.97 | 1.12 |

The drawdown effect is +0.12 pp (less deep) on the full window and +0.15 pp per regime — the sign
is right and the size is nothing. The return effect is +0.25 to +0.40 pp of CAGR per regime, which
is the fee saved and no more.

### 3.2 What the saving is, per book

| book | fee line (10 bps) %/NAV/yr | saving at 25% %/NAV/yr | one-month float % NAV | float q05 30-d loss % NAV (worst regime) | float worst 30-d loss % NAV | break-even 30-d loss on the float |
|---|---|---|---|---|---|---|
| BTC/ETH ensemble (8.62× one-way) | 0.862 | **0.215** | 0.072 | 0.022 (2019-22) | 0.050 | 300% |
| fast profile (68.5× both sides, `risk-and-ladder` §2.3 L3R3: 10.28%/yr at 15 bps) | 6.853 | **1.713** | 0.571 | 0.179 (2019-22) | 0.401 | 300% |

BNB itself (BNB/USDT 1d, 2017-11 → 2026-09): 30-day return mean +12.7% (the token has appreciated;
that is not a forecast), q05 **−29.2%**, q01 −47.0%, worst **−70.2%**, max drawdown **−80.0%**,
annualised vol 96%, daily beta to BTC **0.92** (0.67 in 2023-24, 0.81 in 2025-26). A float sized at
one month of fees is small enough that even the worst 30 days in the record cost 0.05% of NAV on the
ensemble. **The second falsifier clause was structurally unreachable at a one-month float** — the
saving is 3 months of fees, the float is 1 month, and BNB cannot lose more than 100% — and that is
recorded here as a weakness of the pre-registration rather than a strength of the result. The float
would have to exceed **three months of fees** before its tail could touch the saving.

### 3.3 The paper book this week

From the two run databases (read-only), closed trades only:

| sleeve | closed trades | fees USDT | net USDT | gross USDT | fees / gross | saving at 25% |
|---|---|---|---|---|---|---|
| A | 6 | 4.59 | 29.34 | 33.94 | **13.5%** | 1.15 |
| B | 6 | 4.95 | 28.38 | 33.33 | **14.8%** | 1.24 |

The review's *"51% of gross went to fees"* was true at its writing (10 trades, +19.69 gross, +9.68
net); today's AVAX exit (+24.0 on each sleeve) moved the ratio to 14–15%. Both numbers are the same
fact from different days: on a profile with a 5-hour median hold, the fee share of gross swings
with the last trade. All fees were charged at 0.100% in USDT — the BNB discount is not on.

### 3.4 Both directions

- **Avoided:** 0.215% of NAV per year on the ensemble; 1.71%/yr on the fast profile; 1.15–1.24 USDT
  of this week's 9.54.
- **Gave up:** a standing long-BNB position of 0.07% of NAV (0.57% on the fast profile) with beta
  0.92 to BTC — a small addition to an already long-crypto book; an operational dependency (the
  toggle, and the float, which if it runs dry makes the discount lapse to 0.100% without a fill
  showing anything different); BNB is `data_only` in this universe, so the float is bought by the
  operator outside the bots and its P&L needs a line in reconciliation; and the 25% is "until further
  notice".

### 3.5 Verdict

**`inconclusive`** as recorded by the ledger (claimed `supported`; audit: *"headline 1.086 does not
clear the deflated hurdle 2.0692; beating the baseline is not the bar"*). The falsifier did not
trigger and the arithmetic is not in doubt; the hurdle is a search-correction for alpha claims and
this is not one. Read it as: **a certain 0.2%/yr on the earning book and 1.7%/yr on the plumbing
book, at a tail cost of 0.02–0.18% of NAV, needing an exchange-side toggle and a float, not a config
change.** What would change the verdict: nothing statistical — only the schedule changing.

---

## 4. H-C — 2026-09-29-ensemble-weight-hysteresis

**Hypothesis (pre-registered 2026-09-29, seal `a4f3f27b99a3cdbc…`)** A dead band x on the BTC/ETH
trend-ensemble weight (rebalance only when the target moves at least x from the held weight,
x ∈ 1/15 … 5/15) predicts fee drag reduced by ≥ 0.30 pp/yr with CAGR within 1.0 pp and MaxDD within
2.0 pp of the unbanded ensemble at the 9-year horizon. Two parameters: the band x and the fraction θ
of the gap traded once outside it (Gârleanu–Pedersen's partial trade; θ = 1 is the plain band).

**Falsifier** dead if no band x ∈ {1/15 … 5/15} reduces fee drag by ≥ 0.30 pp/yr while keeping
full-window CAGR within 1.0 pp and MaxDD within 2.0 pp of the unbanded ensemble, **or** if the
purged walk-forward out-of-sample banded book's CAGR is below the unbanded book's on the same days.
**Triggered:** **yes**, on the second clause — walk-forward OOS CAGR **49.05%** against the unbanded
**49.33%** (MaxDD −46.25% vs −45.62%). The first clause was *not* triggered: x = 2/15, θ = 1 saves
0.36 pp/yr with CAGR +0.59 pp and MaxDD −1.40 pp.

**Ledger check** `verdict=open` (nearest: `execution-timing` — not this, because the band changes
the *number* of orders, the one lever that entry leaves open). **Trial count** +18. **Sample**
effective_n 523.9 (1d). **Costs** 15 bps/side (also run at 12.5: same shape, every number +0.3 pp).

### 4.1 The full surface, 2017-08-17 → 2026-09-24, 15 bps/side

| book | CAGR % | vol % | Sharpe | MaxDD % | gross | turnover/yr | fee %/yr |
|---|---|---|---|---|---|---|---|
| btc_buy_and_hold | 38.61 | 66.88 | 0.58 | −83.19 | 1.00 | — | 0.00 |
| **unbanded ensemble (x = 0, θ = 1)** | **43.07** | 39.94 | **1.08** | **−45.62** | 0.45 | 8.62 | **1.29** |
| x = 1/15, θ = 1 | 43.07 | 39.94 | 1.08 | −45.62 | 0.45 | 8.62 | 1.29 |
| **x = 2/15, θ = 1** | **43.66** | 40.10 | 1.09 | **−47.02** | 0.45 | 6.17 | **0.93** |
| x = 3/15, θ = 1 | 41.16 | 38.98 | 1.06 | −47.93 | 0.45 | 4.75 | 0.71 |
| x = 4/15, θ = 1 | 37.41 | 39.44 | 0.95 | −45.34 | 0.47 | 3.65 | 0.55 |
| x = 5/15, θ = 1 | 33.23 | 36.60 | 0.91 | −46.72 | 0.43 | 2.74 | 0.41 |
| x = 0, θ = 0.5 | 42.40 | 40.15 | 1.06 | −46.83 | 0.45 | 6.32 | 0.95 |
| x = 2/15, θ = 0.5 | 38.20 | 38.69 | 0.99 | −48.22 | 0.46 | 3.39 | 0.51 |
| x = 0, θ = 0.25 | 40.09 | 40.54 | 0.99 | −50.51 | 0.45 | 4.96 | 0.74 |
| x = 2/15, θ = 0.25 | 36.20 | 38.62 | 0.94 | −49.59 | 0.46 | 2.78 | 0.42 |
| x = 5/15, θ = 0.25 | 27.52 | 35.27 | 0.78 | −47.52 | 0.47 | 1.01 | 0.15 |

(x = 1/15 is identical to unbanded because the ensemble moves on a 1/15 grid. The full 18-cell
table and every regime table are in `results/weight_hysteresis.json`.) **There is no plateau.** One
cell (x = 2/15, θ = 1) is level with the baseline; every neighbour is worse, monotonically in both
x and θ. Fees fall as designed — 1.29 → 0.15%/yr across the surface — and CAGR falls faster.

### 4.2 By regime (x = 2/15, θ = 1 against the unbanded ensemble and BTC hold)

| regime | n (days) | candidate CAGR / MaxDD / fee | unbanded CAGR / MaxDD / fee | BTC hold CAGR / MaxDD |
|---|---|---|---|---|
| 2019-22 | 1,461 | 67.46 / −47.02 / 0.91 | 67.33 / −45.62 / 1.26 | 45.35 / −76.63 |
| 2023-24 | 731 | 53.90 / −26.77 / 1.21 | 53.70 / −28.34 / 1.55 | 137.56 / −26.15 |
| 2025-26 | 632 | 12.57 / −25.08 / 0.91 | 11.01 / −25.98 / 1.35 | −6.06 / −52.97 |

Level in every regime; the fee saved shows up as return, nothing more.

### 4.3 Walk-forward, out of sample (expanding in-sample from 2017-08-17, 60-day embargo, yearly OOS 2019 → 2026, (x, θ) chosen by in-sample Sharpe)

| OOS year | in-sample end | pick x / θ | OOS CAGR banded | OOS CAGR unbanded | OOS CAGR BTC hold |
|---|---|---|---|---|---|
| 2019 | 2018-11-02 | 2/15 / 1.0 | 48.71 | 51.69 | 94.31 |
| 2020 | 2019-11-02 | 0 / 1.0 | 174.82 | 174.82 | 300.46 |
| 2021 | 2020-11-02 | 0 / 0.5 | 143.80 | 136.55 | 59.79 |
| 2022 | 2021-11-02 | 0 / 0.5 | −21.65 | −20.60 | −64.21 |
| 2023 | 2022-11-02 | 0 / 0.5 | 50.03 | 55.47 | 155.61 |
| 2024 | 2023-11-02 | 0 / 1.0 | 51.97 | 51.97 | 120.83 |
| 2025 | 2024-11-02 | 2/15 / 1.0 | 8.79 | 7.51 | −6.33 |
| 2026 | 2025-11-02 | 2/15 / 1.0 | 17.96 | 15.98 | −5.68 |

| pooled OOS 2019 → 2026 | CAGR % | vol % | Sharpe | MaxDD % | turnover/yr | fee %/yr |
|---|---|---|---|---|---|---|
| walk-forward candidate | 49.05 | 41.66 | 1.18 | −46.25 | 7.16 | 1.07 |
| **unbanded ensemble (strategy_baseline)** | **49.33** | 41.50 | **1.19** | **−45.62** | 9.03 | 1.36 |
| btc_buy_and_hold | 49.70 | 61.41 | 0.81 | −76.63 | — | — |
| fixed x = 2/15, θ = 1 (not chosen, for contrast) | 49.90 | 41.69 | 1.20 | −47.02 | 6.57 | 0.99 |

Fixed beats adaptive again (`growth-audit.md` §4.3, `analogue-timing.md` §5.2): the in-sample
Sharpe criterion picked θ = 0.5 for 2021-23, which lost. Even the fixed cell is +0.57 pp of CAGR for
−1.4 pp of MaxDD, and 1.20 against 1.19 of Sharpe needs 269,000 years to separate.

### 4.4 Both directions

- **Avoided:** 2.45 fewer one-way turns of NAV a year (8.62 → 6.17), 0.36 pp/yr of fee drag.
- **Gave up:** every rebalance the band skips is a one-member move of the ensemble, and those moves
  carry information: the drawdown is 1.4 pp deeper on the full window, the OOS book is 0.28 pp of
  CAGR and 0.63 pp of MaxDD worse, and every wider band or slower trade loses 2–16 pp of CAGR.

### 4.5 Verdict

**`refuted`** (ledger outcome `refuted`, seal intact). The fee saving is real and small; the return
effect is inside noise and the pre-registered out-of-sample clause went the wrong way. The
Gârleanu–Pedersen prescription assumes the target is noisier than the trade; here the target is a
fifteen-member average that is already the smoothed aim, and delaying it only lags it. A candidate
ledger line: *ensemble-weight-hysteresis — the one level cell saves 0.36 pp/yr of fees for 1.4 pp of
drawdown and loses out of sample; every neighbour loses 2–16 pp of CAGR.*

---

## 5. H-B — 2026-09-29-min-hold-vs-min-edge

**Hypothesis (pre-registered 2026-09-29, seal `de61520f6a762459…`)** On the fast-profile 1h signals
(EMA 6/18 breakout, 31 pairs), the k = 5 minimum-holding gate H_min = max(10, ⌈(k·c/(λ·sd20))²⌉)
days, λ = 0.20 fixed, c = 0.30%, applied to the discretionary trend-loss exit only, predicts a lower
cost per holding day and lower fees per year of NAV than the plan-shape `min_edge` gate at a
2.7-year horizon; and `min_edge` refuses 0 entries under the shipped L3R3 plan.

**Falsifier** the min-hold gate is not the churn control if it cuts fees per year of NAV by less
than 30% against the ungated replay, or if its net return is worse than the ungated replay by more
than the fee it saves in ≥ 2 of the 3 regimes; the `min_edge` claim is dead if the gate refuses more
than 10% of entries under the shipped L3R3 plan.
**Triggered:** **yes** — fees per year of NAV fall by **0.16%** (R3 as written) and *rise* by 38%
(all non-stop exits blocked) against the 30% cut demanded. The `min_edge` clause did not trigger:
it refuses **0%** of entries under L3R3 (smallest booked target 0.90% = floor 0.90%) and 100% under
L0/R0 (0.50% < 0.90%).

**Ledger check** `verdict=open` (nearest: `execution-timing` — not this, because the gate changes
holding period and order count, not placement). **Trial count** +7. **Sample** BTC/USDT 1h: 79,610
labelled rows → **effective_n 11,587.8**; purged 5-fold, 0 leaks, 796-bar embargo — and the
replay's 2,215 trades are far fewer independent events than that, because entries cluster into
market-wide trend turns (`analogue-timing.md` §4). **Costs** 15 bps/side on every fill.

### 5.1 Method, and the calibration that licenses it

`min_hold_gate.py` replays the profile's exact rule (EMA 6/18, 3-bar prior-high breakout, ATR band
0.15–7%, signal on the closed candle, fill at the next open; 6% stop; trailing 1.5%/0.8%; ROI
2.0/1.5/0.9% at 0/90/300 min; 2h re-entry cooldown) with the portfolio counters that set its
cadence (4 new trades per Gulf day, 300-minute spacing, 1%-of-NAV monthly fee budget), on the 31
whitelisted pairs' 1h feathers, 5% of NAV per trade, 10,000 USDT. Not modelled: the ladder (48
rungs in 2,199 freqtrade trades; the no-ladder cell was within 0.2 pp), the StoplossGuard lock,
tier caps above 5%. Why not `evals/backtest_api.py`: it runs freqtrade in docker (~50 CPU-minutes per
31-pair cell under load, on a host that hibernated on critical battery this morning), and its patch
namespace has no minimum-holding key — that is `strategies/**`, tier 2, being edited by other agents.

**Calibration against the freqtrade cell** (`risk-and-ladder-2026-09-29.md` §2.3, L3R3,
2024-01-01 → 2026-09-23, 31 pairs):

| | trades | net | MaxDD | fees %/yr | mean hold | roi / trailing / stop / exit_signal |
|---|---|---|---|---|---|---|
| freqtrade (document) | 2,199 | −26.45% | 26.75% | 10.28% | 7.2 h | 905 / 397 / 60 / 837 |
| this replay, ungated | 2,215 | −26.56% | 26.93% | 10.52% | 5.6 h | 1,026 / 437 / 69 / 683 |

Net within 0.1 pp, drawdown within 0.2 pp, fees within 0.25 pp/yr, trade count within 16. The exit
mix leans more to ROI and less to trend loss (intrabar ROI is checked against the candle high here),
which is the direction that flatters the trend-loss gate, not the direction that hurts it.

### 5.2 The result — calibration window 2024-01-01 → 2026-09-23 (out of sample for k, λ, c, which are fixed from the daily study)

| variant | trades | net | CAGR | MaxDD | mean hold | fees % start NAV/yr | bps per holding day | win | exit_signal / roi / trailing / stop |
|---|---|---|---|---|---|---|---|---|---|
| **ungated (strategy_baseline)** | 2,215 | **−26.56%** | −10.69% | **−26.93%** | 5.6 h | **10.52%** | **130** | 62% | 683 / 1,026 / 437 / 69 |
| R3 as written, k = 3 | 2,232 | −29.92% | −12.22% | −30.86% | 14.0 h | 10.51% | 53 | 79% | 8 / 1,392 / 459 / 373 |
| **R3 as written, k = 5** | 2,232 | **−29.65%** | −12.09% | −30.58% | 14.0 h | **10.51%** | **53** | 79% | 1 / 1,394 / 460 / 377 |
| R3 as written, k = 10 | 2,232 | −29.73% | −12.13% | −30.66% | 14.0 h | 10.51% | 53 | 79% | 0 / 1,394 / 460 / 378 |
| all non-stop exits blocked, k = 3 | 2,197 | +39.12% | +12.86% | −62.39% | 126 h | 14.40% | 5.9 | 28% | 396 / 16 / 337 / 1,448 |
| **all non-stop exits blocked, k = 5** | 2,173 | **+47.96%** | +15.44% | **−62.28%** | 130 h | **14.54%** | **5.8** | 27% | 369 / 13 / 311 / 1,480 |
| all non-stop exits blocked, k = 10 | 1,879 | +11.70% | +4.14% | −64.85% | 168 h | 11.73% | 4.6 | 22% | 247 / 3 / 220 / 1,409 |
| btc_buy_and_hold, same window | — | — | +27.79% | −52.97% | — | 0 | 0 | — | — |

Two things are visible at once. **k does not matter** (the 10-day floor binds: at sd20 ≈ 3%/day
the formula gives 6 days at k = 5, so H_min = 10 for nearly every trade), and **the gate does not
touch the fee line**: 2,215 → 2,232 trades, 10.52% → 10.51%/yr. The counters that refused entries
on the ungated run — `day_cap` 14,539 candidate-hours, `spacing` 56,231, `fee_budget` **42,291** —
are what set the number of fills, and a fill costs the same whether it is held five hours or ten
days. The R3 gate blocked 36,200 trend-loss signals; those trades then left by ROI (1,026 → 1,394)
or by the **6% stop (69 → 377)**. A trend-loss exit on this profile is a small loss taken early;
blocking it converts some of them into full stops, and net goes from −26.6% to −29.7%.

Blocking every non-stop exit is a different strategy — buy the breakout, hold ten days or 6%,
whichever first — and its numbers are a regime bet: **+234% in 2023-24 (BTC hold +137%)**, −25% in
2025-26 (BTC −5%), +98% in 2019-22 with a **−70% drawdown**, and −62% drawdown on the calibration
window. Cost per holding day does fall from 130 to 5.8 bps, which is `analogue-timing.md` §4.4's
horizon-floor arithmetic reproduced with the real rule; the fee line does not fall, because the
budget (1% of NAV a month) still fills up.

### 5.3 By regime (net over the regime; k = 5; BTC hold beside)

| regime | trades | ungated net / MaxDD / fees %/yr | R3 as written net / MaxDD / fees | all-exits-blocked net / MaxDD / fees | BTC hold CAGR / MaxDD |
|---|---|---|---|---|---|
| 2019-22 (8 pairs with data) | 3,204 | −46.57 / −46.66 / 9.32 | −45.56 / −45.83 / 9.54 | +98.24 / **−70.50** / 26.72* | 44.47 / −76.63 |
| 2023-24 | 1,611 | −21.75 / −21.66 / 10.51 | −19.42 / −21.00 / 10.69 | +234.03 / −41.55 / 18.86* | 137.31 / −26.15 |
| 2025-26 | 1,409 | −19.15 / −20.23 / 10.97 | −24.32 / −26.05 / 10.64 | **−25.06** / −59.08 / 7.75 | −5.24 / −52.97 |

\* fees are stated against the window's starting NAV; where NAV doubled, the 1%-of-*running*-NAV
budget is still what bound — the fee rate on running NAV stayed near 12%/yr. R3 as written is
worse than ungated in one regime of three (2025-26), better in two; that clause was not what killed
it — the fee clause was.

### 5.4 Both directions

- **Avoided (R3 as written):** cost per holding day 130 → 53 bps; 36,200 trend-loss exits that
  were each a small realised loss.
- **Gave up (R3 as written):** nothing on the fee line (−0.16%), and 3.1 pp of net over 2.7 years,
  because 308 of the blocked exits became 6% stops. **(All exits blocked):** the profile's only
  virtue — a 27% drawdown — for a 62% one; and 2025-26.

### 5.5 Verdict, and what actually stops churn on this profile

**`refuted`** (ledger outcome `refuted`, seal intact). Neither gate is the churn control:

- **`min_edge`** is a plan validator. It refuses 0% or 100% of a plan's entries and cannot tell one
  trade from another. That is what `risk-and-ladder` §2.5 built and what it should stay.
- **The minimum-holding gate** changes the horizon, not the fill count, so on a profile whose fills
  are capped by the entry counters it cannot reduce fees; and applied to the trend-loss exit alone
  it makes the book worse by turning early small losses into stops.
- **The fee line on the fast profile is set by three entry-side numbers** — `max_trades_per_day: 4`,
  `min_entry_spacing_min: 300`, `max_fee_pct_per_month: 0.01` — and the last one binds every month.
  `analogue-timing.md` §5.1 R4 (0.01 → 0.0025) is the lever that cuts it, by construction, to a
  quarter; it is tier-2 config and a human's decision. The honest sequencing has not changed: the
  profile is a plumbing test and should not be run for P&L.

Candidate ledger line (human decision): *min-hold-as-churn-control — on a fill-capped profile the
holding gate leaves the fee line unchanged (10.52 → 10.51%/yr); as written it converts 308
trend-loss exits into 6% stops and costs 3 pp of net; blocking every non-stop exit is a different
strategy with a −62% drawdown.*

---

## 6. The statistical bar

| item | value |
|---|---|
| selection trials added by this run | **26** (H-A 1, H-C 18, H-B 7) |
| live open family before this run (`~/earn-run/knowledge/state/trial_counter.json`, read-only) | N = 3 (5 measurements all time) |
| family after this run, if a human advances the live counter | N = 29 → expected max Sharpe 0.99, **hurdle 1.57** (baseline 0.58) |
| cumulative count the design documents carry (`analogue-timing.md` §6.1: 7,765) + 26 | N = 7,791 → expected max Sharpe 1.49, **hurdle 2.07** (baseline 0.58); **2.57** against the ensemble's 1.08 |
| effective sample size | 1d: 3,273 rows → **523.9**; 1h: 79,610 rows → **11,587.8** (purged 5-fold, 0 leaks) |
| years to tell 1.09 from 1.08 (H-A), 1.20 from 1.19 (H-C fixed cell) at 80% power | **249,000** and **269,000** |
| headline vs hurdle | H-A 1.09, H-C 1.18, H-B −0.5 (the profile is negative) — none clears either hurdle |

The trial counter in the live root was **not** advanced (tier 2, human write); the workspace copy of
the counter with all 26 entries is saved at `results/trial_counter.workspace.json`. The ledger
records were written by `hypothesis.py` into the workspace's `knowledge/state/hypotheses/` and
copied to `knowledge/hypotheses/` in the repo; the seal covers content, not location, and all three
verify (`seal_ok=true`).

---

## 7. Honest limits

1. **The H-B replay is not freqtrade.** It is calibrated to the freqtrade cell within 0.1 pp of net
   and 16 trades, but intrabar ordering, the ladder and the StoplossGuard are approximations. The
   finding does not depend on them: fills are capped by entry counters in both engines.
2. **2019-22 on the fast profile is eight pairs**, not 31 (the alts' 1h history starts 2020-21);
   that regime's rows describe a BTC/ETH-plus-six book.
3. **H-A's second falsifier clause could not trigger** at a one-month float (§3.2). It is reported
   as such. The first clause was the binding one and it was cleared by 2×.
4. **H-C's walk-forward selection statistic** (in-sample Sharpe) was chosen when the script was
   written, before any number was seen, but it was not named in the sealed pre-registration; the
   fixed-cell result is shown beside it so the reader can see both.
5. **Fees on H-B are stated against the window's starting NAV**; on the windows where NAV doubled
   the running-NAV rate is what the budget capped.
6. **Albers et al. is cited from its abstract.** Hansen et al., Almgren–Chriss and Gârleanu–Pedersen
   were read in full (the last as its 2012 working paper).
7. **Nothing here was tested live**, and a live quarter would not settle any of it (§6, last row).
8. **The BNB discount is a policy of the venue**, "valid until further notice"; the 0.215%/yr is
   exactly as durable as that sentence.

---

## 8. What would change the picture, and what will not

- **Will change nothing:** maker-only entries (ledger; maker = taker verified today), entry-hour or
  next-bar timing (ledger; `analogue-timing` §4.1/§4.7), the turn-of-the-candle minute (≤ 1 bp),
  slicing (Almgren–Chriss with zero impact), a band or partial trading on the ensemble (H-C),
  a holding gate on a fill-capped profile (H-B).
- **Would change the fee line and is not a research question:** the BNB toggle with a one-month
  float (§3), and on the plumbing profile the entry counters (`analogue-timing` §5.1 R4).
- **Would change the return picture:** nothing in this theme. The earning book's cost line is
  1.29%/yr and the drawdown prize it buys is 38 points against holding BTC. The remaining question
  the documents keep pointing at is exits and sizing on the ensemble itself (`dip-strategy.md`
  §9, the ledger's "what is not in this ledger"), not execution.

---

## Appendix — provenance

| artefact | path |
|---|---|
| pre-registrations, sealed, with close-out audits | `knowledge/hypotheses/2026-09-29-bnb-fee-discount.json`, `…-ensemble-weight-hysteresis.json`, `…-min-hold-vs-min-edge.json` |
| scripts | `evals/research/execution-and-costs/{_common,bnb_discount,weight_hysteresis,min_hold_gate}.py` |
| results | `evals/research/execution-and-costs/results/{bnb_discount,weight_hysteresis,min_hold_gate}.json`, `result_*.json` (the closing inputs), `min_hold_gate.log`, `trial_counter.workspace.json` |
| data read | `~/earn-panels/panel_1d.parquet`; `~/earn-run/data/binance/{BTC,ETH,BNB,…31 pairs}_USDT-{1h,1d}.feather`; `~/earn-run/ft_userdata/{a,b}/runs/test-{a,b}-000.sqlite` (`mode=ro`) |
| edge-audit outputs | `audit_stats.py labels/cv` on BTC/USDT 1d and 1h; `hurdle --baseline 0.58` at N = 26, 29, 7,791 and `--baseline 1.08` at 7,791; `power 1.09/1.08` and `1.20/1.19` |
| load discipline | one replay at a time, `nice -n 10`; 1-minute load never above 3.2 |
| reproduce | `cd ~/earn-wk/re3 && EARN_DATA_DIR=~/earn-run/data EARN_STATE_ROOT=$PWD ~/earn-dev/.venv/bin/python evals/research/execution-and-costs/<script>.py --panel ~/earn-panels/panel_1d.parquet` |
