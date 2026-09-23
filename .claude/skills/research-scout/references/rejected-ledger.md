# The standing rejection ledger

Everything below was **measured and killed**. The machine-readable copy lives in
`scripts/scout.py: LEDGER` and that copy is the authority — this page explains it, and a
test asserts every id here appears there and vice versa.

The ledger is tier 2. A human adds to it, and an automated run cannot remove from it,
because a search process that can edit its own list of things-not-to-search is not
constrained by it. If you believe an entry is wrong, propose a change against this page
with new evidence and let a human decide.

## Why a ledger, rather than "just don't repeat yourself"

Every idea costs a trial, and a trial is not free: the expected best in-sample Sharpe from
N zero-skill trials on this repo's 9.1-year sample is **N=10 → 0.86 · N=50 → 1.05 ·
N=200 → 1.19 · N=1000 → 1.33**. The trial counter in `edge-audit` only grows. So re-testing
a dead idea does not merely waste an afternoon — it **raises the bar that every honest idea
after it must clear**. A search process that does not know what it has already searched is
a random walk with a report attached.

---

## Rejected: the data does not exist, or cannot be backtested

**`onchain-flows`** — exchange netflows, whale deposits, and the whole CDD / LTH / SOPR /
NUPL / MVRV family. Blockscout's balance history returns exactly **10 days**;
mempool.space gives cumulative sums that must be accumulated forward; real-time SOPR and
NUPL are withheld for **7 days** behind a subscription. None of it can be backtested, and a
7-day-old profitability reading cannot inform a 1d decision. The strongest published result
is that BTC net inflows *lack* return predictability except at 4h. Observe-only at best.

**`historical-liquidations`** (observe-only, not rejected) — `allForceOrders` 404,
`forceOrders` 401 (account-scoped), the bulk `liquidationSnapshot` path 404, CoinGlass now
paid. The public `!forceOrder@arr` websocket is free but **forward-only**. A liquidation
recorder is worth starting now precisely because every day not collected is permanently
lost, but it may not feed a decision for at least twelve months. Until then cascades are
**inferred** from open interest plus price.

**`unlock-calendar`** — `api.llama.fi/emissions` returns HTTP **402**, and it is irrelevant
here regardless: BTC has no unlock schedule and ETH's emission is not a cliff. This would be
one of the more reliable effects if the universe ever widened to tokens with vesting.

**`etf-flow-proxy`** — US spot ETF flows are not obtainable free from this host (farside
403, DefiLlama `/etfs` 404, CoinGlass and SoSoValue key-gated, Yahoo `quoteSummary`
"Invalid Crumb"). The session-hours-spread proxy *is* free and backtestable, and that is
exactly the trap: it is a proxy for a number nobody can see, with **no measured relationship
to the thing it proxies**. A validated source is not a validated signal.

**`survivorship-universe`** — any widening that picks today's liquid listings selects on
coins that *survived*; the delisted ones are not in the klines API at all, so they cannot
lose money in your backtest. A wide-universe study is admissible only if it assembles
delisted pairs too and reports what it could not obtain.

---

## Rejected: it was tested and it is not there

**`taker-imbalance`** — CVD and taker-buy ratio as alpha. Two independent tests, both null:
NW(18) of forward 72h return on standardised taker ratio over 6,523 4h bars gives
**t = +0.11**, and +0.07 after controls. The 8h quintile sort looks beautifully monotone
(+0.20% → +0.58%) and that is how people get fooled — contemporaneous correlation with the
same bar's return is **0.414**, so the sort is re-sorting on past returns. CVD is a chart of
where price has been.

**`book-imbalance-slow`** — order-book imbalance at 4h+: t = −1.45 / −1.47 / −0.04 at
4h/24h/72h, with the sign backwards from folklore. Order-flow imbalance's documented horizon
is **tens of seconds**. (Book *depth* as a **volatility** input is a different claim and it
survived: t = −7.50 on forward realised vol, a 1.8× vol difference between depth quintiles.)

**`cme-gap`** — the strongest folklore in crypto, and it collapses against the right
control. 475 weekends: gaps >0.5% filled within 7 days **68.4%** of the time — against
**61.9%** for random midweek 25-hour moves of the same size. The whole phenomenon is 6.5pp
of ordinary mean reversion in a costume.

**`funding-clock`** — settlement hours returned +0.87 bps against +0.51 bps otherwise over
61,658 hourly bars, **t = 0.44**. No clock effect.

**`weekend-effect`** — weekday +0.74 bps/h against weekend +0.50 bps/h over 79,658 bars. No
tradable asymmetry. Volume genuinely is lower (0.65× and decaying), but the claim that
weekend books are *thin* is **false**: the weekend/weekday resting depth ratio is **1.021**.
Quiet, not thin.

**`halving-cycle`** — n = 2 observable halvings and they disagree at every horizon: +365d
was +562% in 2020 and +31% in 2024; −90d was +19.4% against −36.0%. The days-since-halving
bucket table is an artefact: the bins are two contiguous runs, so the effective n is 2, not
180.

**`cross-sectional-momentum`** — Sharpe 1.04 / 0.85 / 0.55 for 20 / 60 / 120-day lookbacks,
and every variant has a worse max drawdown (−77% to −89%) than simply holding BTC.
Parameter instability of that size across a routine choice is the definition of an artefact.

**`coinbase-premium`** — mean cross-venue spread −5.3 bps, sd 6.0, p95|·| 15.5 bps, i.e.
**inside a single taker fee** — and most of what remains is the Tether basis rather than a
dislocation. Any cross-venue number must be divided by a live USDT/USD rate first or it just
measures Tether.

**`hash-ribbons`** — graded folklore by the researcher who proposed it. A handful of
non-independent events each rationalised after the fact, and post-2024 miners hedge
production rather than force-sell it.

**`eth-burn`** — `eth_feeHistory` reaches back about 1,024 blocks, and the "ultrasound
money" framing decayed hard after EIP-4844 moved L2 data to blobs. A demand gauge, not a
supply story, with no measured link to our decisions.

**`onchain-valuation`** — MVRV / NUPL reconstruction. Genuinely clever (realized cap *is*
available undelayed, which routes around the paywall) but it is a weeks-to-months tilt for a
system that decides at 4h/1d, every published threshold is known-overfit across cycles, and
no forward-return evidence at our horizon was ever produced. It does not change a decision.

**`exploit-count`** — the free hacks dataset has recent entries of $4.9M, $4.4M, $35k and
**$16k**. Counting events treats a $16k rug as a bridge failure. Only size relative to the
contagion surface could matter, and that threshold must be calibrated before anything is
wired.

---

## Rejected: it is backwards, or it worked and then died

**`funding-as-direction`** — "high funding means overheated, therefore sell". Backwards. The
≥40% annualised bucket has the **second-highest** median 7-day return (+1.41%). What funding
predicts is the tail: P(7d dd < −8%) rises monotonically **17.0% → 41.4%**, and the ratio to
baseline *strengthened* across halves (1.30 → 1.46). Funding sizes you down; it never points
you anywhere.

**`oi-deleveraging-buy`** — the most instructive entry in this ledger, because it **worked
and then died**, which is worse than never working. `OI 24h < −5%`: H1 2023-09→2025-03 gave
+1.83% at a 67% hit rate against a 55% base; H2 2025-03→2026-09 gave −0.18% at 46.5% against
a 51.4% base. Linear t went from −3.66 to −0.04. A full-sample backtest shows +1.16% and
ships a corpse. Only the **drawdown** leg survived both halves — and that is exactly what
`leverage-state` uses.

---

## Rejected: the effort is aimed at the wrong thing

**`execution-timing`** — entry timing, cost-truth, slicing, passive fills, repricing.
Measured twice on the live 1000-level book: slippage versus mid is **0.001 bps** (BTCUSDT)
and **0.019 bps** (ETHUSDT) at *every* size Earn will ever trade, against a **10 bps** fee.
Maker equals taker at Binance spot VIP0, and VIP1 needs $1M/30d against an implied
~$15k/month — **67× short**, so Earn will never leave VIP0. This is not a false signal, it
is a false priority: the only cost levers that exist are the BNB discount and the number of
orders, both already bounded and already measured by `tca`.

**`meta-labelling`** — run honestly it made things *worse*: OOS accuracy 0.496 against a
0.503 base rate over 851 days, and as a filter it cut Sharpe 1.16 → 0.80 and CAGR
53.5% → 23.1%. The binding constraint is **effective sample size**, not algorithm choice, so
no new model fixes it.

**`hmm-regime`** — a 2-state Gaussian HMM fitted on the **full sample** (deliberate
look-ahead, the most flattering possible test) scored Sharpe 0.78, below the no-look-ahead
MA200×volTarget at 0.87 and far below MA100×volTarget at 1.14. The trend/vol quadrant does
the same job with two comparisons and no fitting.

**`deep-learning-ohlcv`** — about **3,020 effective independent observations** and maybe
three genuine regimes in nine years. Parameter count over information. This is a sample-size
fact, not a preference about architectures.

**`live-testing-proves-edge`** — distinguishing Sharpe 1.14 from 0.83 at 80% power needs
about **245 years**; even 2.00 against 0.83 needs about 15. A 90-day live test proves the
*plumbing* works and nothing whatever about edge. Say the number, not the sentiment.

---

## What is *not* in this ledger

Ideas nobody has tested are `verdict=open`. That is not encouragement — it is permission to
state a falsifiable hypothesis and hand it to `hypothesis-lab`. The open set includes the
questions this system's own record keeps pointing at: **when to take profit**, **how wide a
stop should be given the forecast drawdown**, **how many satellites are worth holding**
(diversification saturates: N_eff 1.70 at 5 names, 1.95 at 20), and **what the shipped
strategy's missing exit rule costs** (+75.4% against buy-and-hold BTC's +194% over
2021-2026). Those are exit and sizing questions, not another entry signal.
