# Research: carry, leverage and on-chain — can the wired derivative signals be turned into money?

**Status:** research, decision-grade negative. Nothing here is switched on; nothing here should be.
No bot, cron, unit, console, config, `.env` or `var/state/**` was touched. Workspace `re2`.
**Date:** 2026-09-29. **Theme:** `carry-leverage-onchain`.

**The owner's goal, verbatim:** *"we need most profit"*; *"not fewer trades but better trades"*;
*"its supposed to earn not loose"*.

**The question this document answers:** the repo has wired perp funding, open interest, the
perp–spot basis and positioning (`runs/features/derivatives.py`, the `leverage-state` skill) and
has never turned any of them into a return. Four ways were proposed to do so. Each was written
down with the result that would kill it **before** any number was computed (§3), then measured
once, costs on, on the same conventions as the trend-ensemble study (`dip-strategy.md` §0.3).

---

## 0. The answer, before the detail

**All four hypotheses were refuted by their own pre-registered falsifiers. None of the wired
leverage signals raises the return of the BTC/ETH trend-ensemble book, and none lowers its
drawdown by more than it costs in return.** The honest negative is the finding.

| # | Hypothesis | Pre-registered falsifier hit? | What the numbers said (costs on, 15 bps/side) |
|---|---|---|---|
| H1 | Funding ≥ 40% ann. as a risk-OFF multiplier (×0.5) on the ensemble | **Yes, on all four clauses** | MaxDD −40.3% → −38.8% (**+1.6pp**, needed 2.0) for CAGR 44.6% → 36.7% (**−7.9pp**); 0.20pp of drawdown per pp of return; Sharpe better in 1 of 3 regimes; walk-forward OOS Sharpe **0.77 vs 0.90** unfiltered |
| H2 | Perp funding carry as a cash yield on idle USDT | **Yes** | Net **+0.16 / −0.13 / +0.12 pp/yr** of book CAGR in 2020-22 / 2023-24 / 2025-26 (needed ≥ 1.0). Gross funding on BTC has compressed **17.3% → 9.9% → 4.2%/yr**; a fully-collateralised carry fund on *all* capital earns **2.1%/yr** today |
| H3 | Open-interest build-up (24h OI ≥ +8%) as a de-risk multiplier (×0.5) | **Yes** | MaxDD **unchanged** (−40.35% → −40.40%), CAGR −1.6pp; Sharpe better in 1 of 3 regimes; walk-forward OOS **0.53 vs 0.68** |
| H4 | Basis momentum (5d change in perp–spot basis) predicts 7d return — *screen* | **Yes** | NW(7) t = **−0.10** (2019-23) and **−0.96** (2023-26) on BTC; the long-only top-tercile overlay returns **−23.8%/yr, MaxDD −90.5%, 124 turns/yr** against hold's +42.0% |
| — | Exchange netflow as an input | **Dropped by the ledger before measurement** | `onchain-flows` is a standing rejection: not backtestable from free data; the published result is that BTC net inflows *lack* return predictability except at 4h |

The one thing that **is** confirmed, again, is the thing the repo already knew and already uses:
funding forecasts the **tail**, not the sign. On this window the ≥ 40% flag raises
P(7d drawdown < −8%) from 22.0% to **41.4%** on BTC (ratio 1.88) and from 32.9% to **55.5%** on
ETH (1.68) — and BTC returned **+119%/yr annualised on those same flagged days** against +44% on
the others (ETH: +256% vs +55%). A risk-off multiplier on that flag therefore dodges the crashes
by dodging the rallies that carry them, and the rallies are bigger. That is `funding-as-direction`
from the rejection ledger, re-encountered from the sizing side: **the drawdown edge is real and it
is not monetisable as exposure reduction on a long-only spot book.** It belongs where
`leverage-state` already puts it — stop width and validator context — not in the weight.

**For the owner's goal, plainly.** Nothing in the carry/leverage/on-chain family changes the
frame set by `growth-audit.md` §5 and `dip-strategy.md` §0.4: the achievable prize is a drawdown
prize, and the return edge over holding BTC sits inside its noise band. The carry trade that the
literature (BIS 2023) and our own data say *did* pay 17–31%/yr in 2020-21 is (a) not available to a
spot-only mandate, (b) down to ~4%/yr gross and ~2%/yr fully collateralised in 2025-26, and (c)
would need a futures account, margin management and a mandate change to earn roughly what USDT
lending pays. §7 quantifies it so the owner can decide; the recommendation is **do not change the
mandate for it**.

---

## 1. Scouting note (`research-scout`, before anything was measured)

Ledger checks run with `.claude/skills/research-scout/scripts/scout.py check`:

| Idea, in the skill's one-sentence form | Verdict | Nearest ledger entry and why this is not it |
|---|---|---|
| 3-day annualised funding ≥ extreme level predicts a 7d drawdown on the ensemble leg, so scaling down on the flag reduces MaxDD per unit of return given up | `open` | `funding-as-direction` rejects funding as a *sell* signal; this tests the **tightening-only multiplier** the `leverage-state` design calls for. `growth-audit.md` §2.5 killed the **percentile-veto-of-entries** form on the old sleeve (CAGR 13.8% → 3.6%, MaxDD not improved) and kept the absolute ≥ 40% flag — that flag is what is tested here |
| Spot–perp basis / funding carry as a cash yield on idle USDT | `open` | Nothing on the ledger. Not executable by a spot-only book; quantified for the mandate decision |
| OI build-up (24h ≥ +8%, z ≥ 2, price-down/OI-up) predicts a forward drawdown, so de-risking on it cuts MaxDD | `open` | `oi-deleveraging-buy` rejects the **buy** leg; the drawdown leg (`crypto-research.md` §1.3, ratio ≈ 1.13 both halves) has never been costed as an overlay |
| Exchange netflow predicts 7d return or drawdown | **`rejected`** | `onchain-flows`: *"Cannot be backtested from free data … Blockscout's coin-balance-history-by-day returns exactly 10 days … The strongest published result is that BTC net inflows LACK return predictability except at 4h. Observe-only at best; never a decision input."* **Dropped. No measurement.** |
| Basis momentum (5d change in basis) predicts 7d return | `open` | Directional, so run as a **screen** (cannot produce a change); the design doc's whole record says the leverage surface forecasts the second moment |

Surfaces used (all from `scout.py surfaces`, all `backtestable=true`): the local 1d candle
store; the wired funding cache (`cache/derivatives/funding-*.feather`, REST `fapi/v1/fundingRate`,
BTC 2019-09-10+, ETH 2019-11-27+); the bulk-archive metrics (`sum_open_interest`, BTCUSDT
2020-09-01+, ETHUSDT 2021-12-01+, read through `runs.features.binance_archive.load_metrics`,
which carries all four loader traps); the perp 1d kline cache for the basis. `scout.py gate`
was not needed: no new endpoint was introduced. **The `futures/data/*` family (30-day retention)
was not touched.** Netflow data was neither fetched nor proxied.

---

## 2. Literature — primary sources read, with the one result each contributes

Only what was actually read is cited. One paper on funding-rate arbitrage risk/return
(ScienceDirect, 2025) returned HTTP 403 and is **not** cited. Two PDFs (Liu–Tsyvinski–Wu; He et
al.) could not be rendered on this host, so their claims below are taken from the publisher /
arXiv abstracts only, and nothing beyond the abstract is attributed to them.

| Source | Year | The one result used here |
|---|---|---|
| Schmeling, Schrimpf, Todorov, *Crypto carry*, BIS WP 1087 (later *Management Science*) | 2023 | Crypto futures carry averages **above 10%/yr and reaches 60%/yr**, driven by retail demand for leveraged upside and scarce arbitrage capital; **high carry predicts future price crashes**. Consistent with our funding→drawdown table, and with our measured compression of BTC funding from 17–31%/yr (2020-21) to 4%/yr (2025-26) as arbitrage capital arrived |
| He, Manela, Ross, von Wachter, *Fundamentals of Perpetual Futures*, arXiv 2212.06888 (v7) | 2022, rev. 2026 | Deviations from no-arbitrage perp prices are larger than in FX, **comove across currencies and diminish over time**; the implied arbitrage yields high Sharpe. The "diminish over time" is what §7 measures on Binance |
| Ackerer, Hugonnier, Jermann, *Perpetual Futures Pricing*, arXiv 2310.11771 / NBER w32936 / *Math. Finance* | 2023–25 | The perp price is the risk-neutral expectation of spot sampled at a random time set by the anchoring intensity; funding specifications exist under which perp = spot. **Funding is an anchoring mechanism, not a forecast of the spot drift** — theory gives no reason to expect it to predict direction |
| Liu, Tsyvinski, Wu, *Common Risk Factors in Cryptocurrency*, *J. Finance* 77(2) / NBER w25882 | 2022 | Market, size and momentum span the cross-section; the tested characteristics are **price- and market-based only** — no funding, OI, basis or flow variable is in the priced set |
| Liu, Tsyvinski, *Risks and Returns of Cryptocurrency*, *RFS* 34(6) | 2021 | Returns load on **network** (adoption) factors and attention/momentum; **production factors (mining cost) are not priced** — the same conclusion the ledger reached on `hash-ribbons` |
| Garcia Seuma, *Where does the criticality live? Early-warning signals are event-heterogeneous across seven crypto-perpetual liquidation cascades*, arXiv 2607.27070 | 2026 | Seven BTCUSDT cascades 2022-05 → 2025-10: price shows critical slowing in 5 of 7 and is silent in the two tariff shocks; only taker order-flow variance compression passes a placebo test (Fisher p ≈ 5×10⁻⁶) and even that is *"a population-level precursor, not a per-event alarm"*; funding *"settles every eight hours, far too coarsely to serve as an intraday early-warning variable"*. **No leverage variable is a per-event cascade alarm** — the OI de-risk idea (H3) starts from a weak prior |
| *Return and Volatility Forecasting Using On-Chain Flows in Cryptocurrency Markets*, arXiv 2411.06327 | 2024 | BTC net exchange inflows **lack return predictability except at 4h** and predict volatility negatively; USDT inflows predict BTC/ETH intraday returns. This is the result the ledger's `onchain-flows` entry rests on, and the data is vendor on-chain flow, not free |
| Boons, Porras Prado, *Basis-Momentum*, *J. Finance* 74(1) | 2019 | Basis-momentum from the **slope and curvature of the futures curve** predicts commodity spot and term premia. A single perpetual has no curve; the transplant (H4) has no structural reason to work and did not |
| Binance USDⓈ-M fee schedule (regular user: **0.02% maker / 0.05% taker**, 10% BNB discount) | 2026 | Used to cost the perp leg in §7. **Secondary sources** (finder.com, bitdegree.org): the Binance fee page requires a login and could not be read from this host. A human should confirm against the account's `tradeFee` before any mandate discussion |

What the literature does **not** contain: any paper showing perp funding or open interest
forecasting the *sign* of the next week's return at a size that survives a taker fee. The
carry-crash link (BIS) and the OI-quadrant drawdown link (`crypto-research.md` §1.3) are
second-moment results, exactly as the repo's design doc says.

---

## 3. Pre-registration (`hypothesis-lab`, sealed before measurement)

Written and sealed with `.claude/skills/hypothesis-lab/scripts/hypothesis.py open`, ledger root
`evals/research/carry-leverage-onchain/ledger/` (copies in
`knowledge/hypotheses/carry-leverage-onchain/`). The seal is a SHA-256 over the statement,
falsifier, horizon, parameter count, regimes, costs and surface; `close` recomputed it and every
seal verified (`seal_ok=true`). **Opened 2026-09-29T14:14:48Z → 14:14:50Z; the study ran at
~14:19Z; closed 14:22:38Z → 14:22:40Z.**

| id | statement (short) | falsifier (short) | params | regimes |
|---|---|---|---|---|
| `2026-09-29-clo-funding-riskoff` | 3d funding ≥ 40% ann. on a leg predicts a 7d drawdown; ×0.5 on that leg cuts MaxDD by more than CAGR | MaxDD gain < 2.0pp, OR CAGR given up > MaxDD gained, OR Sharpe better in < 2 of 3 regimes, OR purged walk-forward OOS Sharpe < unfiltered | 2 (level/form, multiplier) | 2019-22 / 2023-24 / 2025-26 |
| `2026-09-29-clo-carry-cash-yield` | Fully-collateralised cash-and-carry on the book's idle USDT adds ≥ 1.0pp/yr CAGR net, in each regime | net < 1.0pp/yr in 2023-24 or 2025-26, OR the leg's worst cumulative net-funding peak-to-trough > 5% | 1 | 2020-22 / 2023-24 / 2025-26 |
| `2026-09-29-clo-oi-buildup-derisk` | 24h OI ≥ +8% (or z ≥ 2, or price-down/OI-up) predicts a 3–7d drawdown; ×0.5 cuts MaxDD by more than CAGR | same four clauses as H1 | 2 (trigger form, multiplier) | 2020-22 / 2023-24 / 2025-26 |
| `2026-09-29-clo-basis-momentum-screen` | SCREEN: 5d change in perp–spot basis predicts 7d spot return | NW(7) \|t\| < 2 in either half, OR sign flips, OR costed top-tercile overlay ≤ hold on Sharpe | 1 | as above |

The close-out audit (`hypothesis.py close`) found **zero procedural problems** on all four:
costs on and never lowered, both baselines present, an out-of-sample block, regime split with
n ≥ 30 in every cell, no parameters added, `effective_n` and a deflated hurdle from `edge-audit`,
both directions reported, and the falsifier value recorded. All four closed `refuted`.

---

## 4. Method

Everything is the trend-ensemble study's arithmetic, unchanged, so the numbers sit beside
`dip-strategy.md` §0.3 and `trend-ensemble.md` §1 without conversion:

- one long-only spot book, BTC/ETH, `0.5 × ensemble_weight` per leg (`runs.features.trend`),
  gross never above 1, cash earns 0%;
- **every weight lagged one full day** — the flag on day *t* (funding at the 16:00 UTC print;
  OI at the 23:55 archive row) scales the position held over day *t+1*;
- **15 bps per side on every weight change**, including the extra turnover the overlays create;
- `CAGR = Π(1+r)^(365/n) − 1`, `vol = σ√365`, `Sharpe = CAGR/vol`, MaxDD from the compounded
  curve — `evals/trend_ensemble_backtest.py: book_returns / stats`, imported, not re-implemented;
- both baselines on every table: **BTC buy-and-hold** and the **unfiltered BTC/ETH ensemble**
  (the shipped strategy since `trend-ensemble.md`);
- regimes 2019-22 / 2023-24 / 2025-26 (OI: 2020-22, its archive starts 2020-09-01);
- **walk-forward**: expanding in-sample, one calendar year out of sample from 2021 (OI: 2022),
  a **7-day purge** at each boundary (the forward-drawdown horizon), pick = best in-sample Sharpe
  over the pre-registered grid, OOS years concatenated and compared with the unfiltered ensemble
  **on the same days**;
- **effective N** from `edge-audit` (`audit_stats.py labels/cv`, BTC 1d): **3,273 rows carry
  523.9 independent observations** (uniqueness 0.160, 6.25 rows per effective sample); 5 purged
  folds, 0 leaks, ~105 effective observations per fold. Flag-day counts are also reported as
  `days / 7`;
- **deflated hurdle** (`runs/features/sampling.py: deflated_hurdle`), baseline = BTC hold Sharpe
  on the window, N stated two ways: the **open selection family** (3 before this study + 24
  selection trials here = 27) and the **all-time research count** the design docs use
  (~7,900 + 27). The stricter one is the one that applies.

**Why not `evals/backtest_api.py`.** Its patch surface is `params.* / trading.* / execution.*`
of the container strategies; a funding or OI overlay on the ensemble weight is not expressible
in any of the three namespaces, so a docker run could not have tested these hypotheses. The
evaluator used here is the one `trend-ensemble.md` §1 verified reproduces the study's headline
to the decimal on both data sources, with the same cost model. This is a limit (§11), not a
shortcut: no stops, no intrabar path, no slippage beyond the flat 15 bps.

**Trials.** 27 variants were measured (H1: 5 forms × 3 multipliers; H3: 3 triggers × 3
multipliers; H2: 1; H4: 2). 24 are **selection** trials (H1, H3 could become a tier-2 build);
3 are screens. They were added to a trial counter in the research ledger root
(`evals/research/carry-leverage-onchain/ledger/knowledge/state/trial_counter.json`); the live
`knowledge/state/trial_counter.json` is tier 2 and **a human must add them** (24 selection, 3
screen, all `carry-leverage-onchain re2`).

Machine load: one study, 1.3 s wall, `nice -n 10`, load-average check before the run (1-minute
figure 2.1, rule is < 8).

---

## 5. H1 — funding extremes as a risk-OFF multiplier: refuted

Window 2019-09-15 → 2026-09-22, 2,565 days (7.03 y). ETH's funding starts 2019-11-27; before
that its leg is unscaled.

### 5.1 Headline, both baselines

| book | CAGR | vol | Sharpe | MaxDD | avg gross | turns/yr | fee drag/yr |
|---|---|---|---|---|---|---|---|
| BTC buy-and-hold | 35.2% | 60.4% | 0.58 | **−76.6%** | 1.00 | 0 | 0 |
| **BTC/ETH ensemble (unfiltered)** | **44.6%** | 39.9% | **1.12** | **−40.3%** | 0.49 | 9.2 | 1.38% |
| ensemble × funding flag (≥ 40% ann., ×0.5) — *pre-registered primary* | 36.7% | 35.0% | 1.05 | −38.8% | 0.46 | 12.1 | 1.82% |

Falsifier readout: **MaxDD +1.56pp (needed ≥ 2.0) · CAGR −7.94pp · 0.20pp of drawdown per pp
of return (needed ≥ 1.0) · Sharpe better in 1 of 3 regimes (needed ≥ 2) · walk-forward OOS
0.77 vs 0.90 (needed ≥).** Four clauses, four hits.

### 5.2 Per regime (Sharpe / MaxDD / CAGR), n beside each

| regime | n | BTC hold | ensemble | primary (≥40, ×0.5) |
|---|---|---|---|---|
| 2019-22 | 1,204 | 0.21 / −76.6% / 15.3% | **1.23 / −40.3% / 58.7%** | 1.03 / −38.8% / 40.4% |
| 2023-24 | 731 | **2.82 / −26.2% / 137.6%** | 1.47 / −28.3% / 53.7% | 1.54 / −23.3% / 54.3% |
| 2025-26 | 630 | −0.11 / −53.0% / −4.6% | 0.53 / −26.0% / 12.8% | 0.53 / −26.0% / 12.8% |

The only regime the flag helps is 2023-24, and there it fires into a bull market the ensemble
was already under-capturing (53.7% against hold's 137.6%). In 2025-26 the flag **never fires**
(funding has not reached 40% annualised since 2024 — the `growth-audit.md` §1.6 finding that the
flag covered 0.05% of coin-weeks in 2025-26, seen again on BTC/ETH), so the primary rule and the
ensemble are the same book. In 2019-22 it gives up 18pp of CAGR for 1.5pp of drawdown.

### 5.3 The plateau — every cell, so the peak is not mistaken for the finding

| form | ×0.25 | ×0.5 | ×0.75 |
|---|---|---|---|
| abs 20% ann. (BTC 334 d, ETH 399 d flagged) | 30.9% / 1.09 / **−35.1%** | 36.0% / **1.16** / −36.6% | 40.7% / **1.16** / −38.1% |
| abs 30% (232 / 277 d) | 31.2% / 0.98 / −38.5% | 36.0% / 1.07 / −38.8% | 40.5% / 1.11 / −39.2% |
| **abs 40% (169 / 211 d) — primary** | 32.3% / 0.96 / −38.4% | **36.7% / 1.05 / −38.8%** | 40.8% / 1.10 / −39.2% |
| abs 60% (85 / 124 d) | 37.5% / 1.05 / −39.5% | 40.1% / 1.09 / −39.5% | 42.5% / 1.11 / −39.5% |
| rolling q80 (365d) — the `leverage-state` cut (440 / 431 d) | 26.3% / 0.84 / −39.4% | 32.6% / 0.98 / −39.4% | 38.8% / 1.07 / −39.5% |

(cells are CAGR / Sharpe / MaxDD; unfiltered ensemble 44.6% / 1.12 / −40.3%.)

Read across: **no cell beats the unfiltered ensemble on CAGR**, two cells beat it on Sharpe by
0.04 (inside search noise — `power`: telling 1.16 from 1.12 apart needs **16,188 years**), and
the deepest drawdown improvement anywhere is 5.2pp (abs20 ×0.25) bought with 13.7pp of CAGR.
The **rolling-percentile form is the worst row**: in a compressed-funding regime a percentile
fires on non-extremes (440 days), and in 2025-26 it cuts Sharpe from 0.53 to 0.27–0.45. That is
the `growth-audit.md` §1.6 rule — *the absolute level carries the information, the coin's own
history does not* — reproduced on the two core assets.

### 5.4 Both directions of the rule (primary, ≥ 40% ×0.5)

| leg | days flagged | n_eff (÷7) | share | asset return on flagged days (ann.) | on other days | P(7d dd < −8%) flagged | unflagged | ratio |
|---|---|---|---|---|---|---|---|---|
| BTC | 169 | 24 | 6.6% | **+119%/yr** | +44%/yr | **41.4%** | 22.0% | **1.88** |
| ETH | 211 | 30 | 8.2% | **+256%/yr** | +55%/yr | **55.5%** | 32.9% | **1.68** |

What it avoided: on flagged days where the asset fell, the halved leg saved **+90pp** (BTC) /
**+120pp** (ETH) of simple-summed book return. What it gave up: on flagged days where the asset
rose, it forfeited **−142pp** / **−172pp**. Net over the window **−52.7pp** of simple-summed book
return. The drawdown forecast is real — the tail ratio here (1.88) is *stronger* than the design
study's 1.30–1.46 — and the days it flags are the best days the book has.

### 5.5 Walk-forward (purged, expanding, annual OOS)

| OOS year | pick (form, ×) | IS Sharpe | OOS Sharpe | unfiltered OOS Sharpe |
|---|---|---|---|---|
| 2021 | abs20, 0.25 | 2.21 | 1.27 | **2.03** |
| 2022 | abs20, 0.50 | 2.23 | −1.71 | −1.71 |
| 2023 | abs20, 0.75 | 1.28 | **1.70** | 1.62 |
| 2024 | abs20, 0.50 | 1.40 | 1.20 | **1.34** |
| 2025 | abs20, 0.50 | 1.38 | 0.27 | 0.27 |
| 2026 (to 09-22) | abs20, 0.75 | 1.18 | 1.17 | 1.17 |
| **concatenated OOS, 2,091 d** | | | **21.4% / 0.77 / −35.1%** | **34.5% / 0.90 / −40.3%** |

The walk-forward always picks the *loosest* level (20%) — the cell that fires most — and loses
13pp of CAGR out of sample for 5pp of drawdown. In three of six years the pick never fires.

### 5.6 Hurdle

Baseline (BTC hold) Sharpe 0.58 on 7.03 years. Deflated hurdle **1.70** at N = 27 (open family)
and **2.28** at N = 7,927 (all-time). The best in-sample cell is 1.16; the pre-registered primary
is 1.05; the unfiltered ensemble itself is 1.12. Nothing in this section is within 0.5 of either
bar, and `edge-audit`'s own `hurdle` against the ensemble as baseline gives **2.82**.

---

## 6. H3 — open-interest build-up as a de-risk trigger: refuted

Window 2020-09-03 → 2026-09-22, 2,211 days (6.06 y). ETH's archive starts 2021-12-01; before
that its leg is unscaled. `sum_open_interest` (contracts) from the bulk archive through the
trap-handled loader (out-of-order rows, duplicate timestamps, near-zero OI all handled there).

| book | CAGR | Sharpe | MaxDD | turns/yr | fee drag/yr |
|---|---|---|---|---|---|
| BTC buy-and-hold | 39.7% | 0.69 | −76.6% | 0 | 0 |
| **ensemble (unfiltered)** | **47.4%** | **1.20** | **−40.35%** | 9.1 | 1.36% |
| OI 24h ≥ +8%, ×0.5 — *primary* | 45.8% | 1.17 | −40.40% | 13.0 | 1.95% |

Falsifier readout: **MaxDD −0.05pp (worse) · CAGR −1.57pp · Sharpe better in 1 of 3 regimes ·
walk-forward OOS 0.53 vs 0.68.** Refuted.

The grid (CAGR / Sharpe / MaxDD; unfiltered 47.4 / 1.20 / −40.35):

| trigger | days flagged BTC / ETH | ×0.25 | ×0.5 | ×0.75 |
|---|---|---|---|---|
| OI 24h ≥ +8% | 77 / 56 | 45.0 / 1.15 / −40.5 | 45.8 / 1.17 / −40.4 | 46.7 / 1.18 / −40.3 |
| OI 24h-change z(180d) ≥ 2 | 50 / 51 | 45.1 / 1.15 / −40.0 | 45.9 / 1.17 / −40.1 | 46.7 / 1.18 / −40.2 |
| price down & OI up (quadrant) | 524 / 405 | 41.0 / 1.12 / −40.6 (**44 turns/yr, 6.6% fees**) | 43.3 / 1.16 / −40.5 | 45.5 / 1.18 / −40.4 |

Nothing moves MaxDD by more than 0.4pp in either direction. The quadrant trigger fires on a
quarter of all days and turns the book into a fee machine (44 one-way turns/yr, 6.6%/yr in fees
at ×0.25). Both directions on the primary: BTC flagged 77 days (n_eff 11), asset return on those
days **+125%/yr** vs +47%; tail ratio 1.52 (33.8% vs 22.2%). ETH: 56 days, **+191%/yr**, tail
ratio **0.92** — on ETH the OI trigger does not even predict the drawdown. This is
`crypto-research.md` §1.3's ratio ≈ 1.13 on a much thinner sample: too weak to size on, and
firing on rallies.

Walk-forward picks bounce between `oi_chg_ge_8pct` and `price_down_oi_up`; concatenated OOS
(1,726 days, 2022-26) **14.5% / 0.53 / −28.2%** against the unfiltered ensemble's **19.4% / 0.68 /
−28.3%** on the same days, with 2.6× the turnover. Hurdle: 1.90 (family) / 2.52 (all-time)
against a best in-sample cell of 1.18.

The cascade literature (§2, Garcia Seuma 2026) said this before we measured it: leverage
variables do not carry a per-event warning, and the two largest 2025 cascades were exogenous
shocks that no build-up preceded.

---

## 7. H2 — carry as a cash yield on idle capital: refuted, and quantified for the mandate decision

**Stated first: a spot-only book cannot do this.** A cash-and-carry is long spot and **short a
perpetual**; Earn's mandate (`CLAUDE.md`, `config/earn.yaml: universe`) is spot-only, and the
risk gate has no path for a derivatives leg. The numbers below say what the owner would be
buying with a mandate change, so the decision can be made on a number rather than a story.

### 7.1 What the carry leg itself earned (short perp receives funding; % of hedged notional)

| | BTC gross funding /yr | share of prints negative | worst cum. funding peak-to-trough | ETH gross funding /yr |
|---|---|---|---|---|
| 2020-22 | **17.3%** | 14.6% | −1.52% | 21.9% |
| 2023-24 | **9.9%** | 9.3% | −0.09% | 10.6% |
| 2025-26 | **4.2%** | 18.5% | −0.41% | 3.6% |
| by year: 2020 / 21 / 22 / 23 / 24 / 25 / 26 | 17.2 / **30.6** / 4.2 / 7.9 / 11.9 / 5.1 / **2.9** | 2026: 26% | | 27.4 / **37.5** / 0.8 / 8.3 / 13.0 / 4.9 / **1.8** |

Basis P&L of the hedge (spot return minus perp return, daily closes) is −0.3%/yr in 2020-22 and
≈ 0 since. The leg is as safe as the literature says (worst cumulative giveback 1.5%, and only in
2020) — and it is **compressing exactly as BIS 2023 and He et al. predict**: from 31%/yr at the
2021 peak to under 3%/yr in 2026.

A **pure carry fund** — all capital, fully collateralised (half spot, half USDT margin at 1×, so
liquidation needs roughly a doubling) — would have earned **8.7% / 5.0% / 2.1%/yr** gross in the
three regimes. In 2025-26 that is below what USDT lending or a T-bill fund pays, before futures
fees and before the operational risk of running margin.

### 7.2 What it would add to *this* book (carry on the idle share only)

Idle capital = 1 − gross of the unfiltered ensemble, day by day. Costs: every unit of idle
capital moved into or out of carry pays the spot leg 15 bps and the perp leg 5 bps (VIP0 taker,
§2), i.e. **10 bps per unit one-way**, and the idle share moves with the ensemble's own 9×/yr
turnover.

| regime | mean idle share | gross carry pp/yr of NAV | resize cost pp/yr | **net pp/yr** | (50/50 BTC-ETH) |
|---|---|---|---|---|---|
| 2020-22 | 0.50 | 1.04 | 0.88 | **+0.16** | +0.19 |
| 2023-24 | 0.36 | 0.90 | 1.03 | **−0.13** | −0.07 |
| 2025-26 | 0.62 | 1.02 | 0.90 | **+0.12** | −0.02 |

Falsifier: net < 1.0pp/yr in 2023-24 (**−0.13**) and in 2025-26 (**+0.12**). Refuted. Worst
cumulative net drawdown of the carry sleeve: −1.3% of NAV.

Arithmetic sensitivity, not a new trial: if the implementation toggled **only the perp hedge**
(keeping spot BTC on the idle capital and paying 5 bps/unit instead of 10), the cost column
halves and the net becomes roughly **+0.6 / +0.4 / +0.6 pp/yr** — still under the 1.0pp bar in
every regime, and it would require the book to hold un-hedged spot in the moments between
ensemble sells and hedge opens. The gross column is the ceiling: **about 1pp/yr of book CAGR is
all the funding market offers the idle half of a 2-asset ensemble today**, against 44.6% CAGR
and a 40% drawdown that the mandate question is really about.

**Recommendation on the mandate:** do not change it for carry. The prize is ~1pp/yr gross,
falling, with margin operations and a new venue surface (`fapi` order path, liquidation risk on
the short, USDT-M margin calls in a spot-only risk gate) attached. If the owner wants the carry
regardless, the honest number is 2–5%/yr on the capital it is run on, and it should be a
separate sleeve with its own gate, not an overlay on this one.

---

## 8. H4 — basis momentum (screen): null

Perp–spot basis from daily closes: mean **−0.16 bps** (sd 8.2) in 2019-09 → 2023-03 and
**−2.99 bps** (sd 3.7) since — the perp now sits consistently ~3 bps *below* spot, confirming the
`leverage-state` note that "basis > 0 is bullish" has the wrong sign on this venue.

| asset | half | n (n_eff) | β (fwd 7d ret per bp of 5d basis change) | **NW(7) t** | R² | t on basis level |
|---|---|---|---|---|---|---|
| BTC | 2019-09 → 2023-03 | 1,265 (180) | −2.7e−5 | **−0.10** | 0.00001 | −0.98 |
| BTC | 2023-03 → 2026-09 | 1,295 (185) | −8.7e−4 | **−0.96** | 0.001 | +0.90 |
| ETH | 2019-11 → 2023-03 | 1,185 (169) | −5.3e−4 | −0.90 | 0.001 | +0.90 |
| ETH | 2023-03 → 2026-09 | 1,295 (185) | −1.5e−3 | −1.28 | 0.002 | +0.37 |

Costed long-only overlay (hold BTC when the 5d basis change is in its trailing-365d top tercile,
2020-09-08 → 2026-09-22): **−23.8% CAGR, Sharpe −0.76, MaxDD −90.5%, 124 one-way turns/yr,
18.6%/yr in fees**, against hold's +42.0% / 0.73 / −76.6%. Negative in all three regimes. Boons
and Porras Prado's predictor lives in the slope and curvature of a *term structure*; a single
perpetual has none, and the screen says so.

---

## 9. Edge audit (`edge-audit`) — applied to the best-looking cell, which still fails

Nothing survived its falsifier, so there is no survivor to audit. The audit was run anyway on the
one thing a reader might be tempted by — the abs20 ×0.5 / ×0.75 cells at in-sample Sharpe 1.16
against the ensemble's 1.12:

- **Sample**: 3,273 daily rows → **523.9 effective observations** (`labels`, uniqueness 0.160);
  5 purged, embargoed folds, 0 leaks (`cv`). The flag itself is live on 334–399 days per leg,
  i.e. **48–57 effective observations**.
- **Hurdle**: 0.58 + expected max Sharpe(27, 7.03 y) = **1.70**; at N = 7,927, **2.28**; against
  the ensemble as baseline, **2.82**. In-sample 1.16 clears none of them.
- **Power**: distinguishing 1.16 from 1.12 at 80% power needs **16,188 years** of live data;
  distinguishing the ensemble's 1.12 from hold's 0.58 needs **75 years**.
- **Walk-forward**: the picked cells lose to the unfiltered ensemble out of sample (0.77 vs 0.90).

Verdict: not reportable as an improvement. It is search noise on top of the ensemble.

---

## 10. What this means for "most profit"

1. **The leverage surface is a risk instrument, not a return instrument, and it now has four
   more costed negatives behind that statement.** Funding's drawdown forecast is the strongest
   thing in this study (tail ratio 1.88 on BTC) and *every* way of monetising it as exposure
   reduction loses more return than it saves — because the flagged days are the best days. Its
   place is where `leverage-state` already puts it: how wide a stop needs to be, what a validator
   should say about a proposal's size. **Do not build a funding or OI multiplier into the
   ensemble weight.**
2. **Carry is the one genuine yield in the family and the book cannot hold it.** It would add
   ~1pp/yr gross to this book, is worth 2–5%/yr on dedicated capital, and is falling. Not worth
   a mandate change; recorded so the question does not need re-asking.
3. **Netflow is not a research question here**; the ledger has it and the literature agrees.
4. **The return question remains an exit-and-sizing question**, as `rejected-ledger.md`'s
   "what is not in this ledger" says: take-profit, stop width given the forecast drawdown, and
   the shipped strategy's missing exit rule. This theme adds nothing to the entry side, and it
   was not expected to.
5. **For the ledger** (a human decision, tier 2): four `refuted` closes that could become
   entries — `funding-riskoff-multiplier` ("dodges the crashes by dodging the rallies that carry
   them; 0.20pp of drawdown per pp of return"), `oi-buildup-derisk` ("MaxDD unchanged, −1.6pp
   CAGR, ETH tail ratio 0.92"), `carry-cash-yield` ("~1pp/yr gross on the idle half, 2.1%/yr
   fully collateralised in 2025-26, not spot-executable"), `basis-momentum-single-perp` ("no
   curve, t ≈ −0.1 / −1.0, overlay −90% drawdown").

---

## 11. Honest limits

- **Evaluator.** Daily closes, flat 15 bps, no stops, no intrabar path, no slippage or impact
  beyond the fee, cash at 0%. Every number moves *down* once real execution applies
  (`dip-strategy.md` §7). `evals/backtest_api.py` could not express these overlays (§4).
- **One market, one history.** 7 years of funding, 6 of open interest, maybe three regimes;
  524 effective daily observations; the ≥ 40% flag has fired on 24–30 effective occasions on
  BTC/ETH and **not at all since 2024**. A rule that cannot fire cannot be tested in the regime
  that matters most for the next decision.
- **ETH's derivative history is shorter** (funding 2019-11, OI 2021-12); its leg is unscaled
  before those dates in every overlay, which favours the overlays slightly.
- **Carry costing is a model.** 10 bps per unit resized, futures fee from secondary sources (the
  Binance page needs a login), no funding-interval changes, no margin-call or liquidation path,
  no exchange-risk. The gross column does not depend on any of that and is the ceiling.
- **Two papers read from abstracts only** (LTW 2022; He et al. 2022) because the PDFs could not
  be rendered here; one paper (funding arbitrage risk/return, 2025) not read at all (403) and not
  cited.
- **Trial counter.** The live tier-2 counter was not touched; the 27 trials are recorded in the
  research ledger root and must be added by a human.
- **This says nothing about intraday liquidation-cascade signals**: the free liquidation stream is
  forward-only (`historical-liquidations`, observe-only) and the one paper that studied cascades
  at that resolution found no per-event alarm.

---

## 12. Artefacts

| what | where |
|---|---|
| Study (four hypotheses, one run) | `evals/research/carry-leverage-onchain/scripts/study.py` |
| Close-out builder (result JSONs → ledger `close`) | `evals/research/carry-leverage-onchain/scripts/close.py` |
| Every number in this document | `evals/research/carry-leverage-onchain/results/results.json` |
| Per-hypothesis result files audited at close | `evals/research/carry-leverage-onchain/results/<id>.result.json` |
| Sealed pre-registrations + close-outs (ledger root) | `evals/research/carry-leverage-onchain/ledger/knowledge/state/hypotheses/*.json` |
| Copies for the hypothesis queue | `knowledge/hypotheses/carry-leverage-onchain/*.json` |
| Edge-audit outputs and this study's trial counter | `evals/research/carry-leverage-onchain/ledger/knowledge/state/{edge_audit,trial_counter}.json` |
| Data read (read-only) | `~/earn-run/data/binance/*-1d.feather`, `~/earn-run/cache/derivatives/{funding,perp}-*.feather`; metrics zips copied to `~/re2-data/binance_archive/` so the loader's rollup never wrote into the runtime |

Reproduce:

```bash
cd <repo>
export EARN_STATE_ROOT="$PWD/evals/research/carry-leverage-onchain/ledger"
nice -n 10 ~/earn-dev/.venv/bin/python evals/research/carry-leverage-onchain/scripts/study.py \
    --run-root ~/earn-run --archive-root ~/re2-data/binance_archive
~/earn-dev/.venv/bin/python evals/research/carry-leverage-onchain/scripts/close.py
```
