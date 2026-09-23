# Wide universe — design

**Status:** design, not landed. Nothing here is switched on until the packages in §4 ship and
the backtests in §6 are re-run against the historical whitelist.
**Scope change that triggered it (owner, verbatim):** *"also it should be going for all coins
in binance not just btc and eth when we test on demo and go live."*

Everything below was measured on this host between 2026-09-23 18:00 and 21:00 Gulf, against
the live Binance public API and a **survivorship-free** daily panel of 841,090 candles
covering all 735 USDT spot pairs that have ever existed (2017-08-17 → 2026-09-23). Scripts
and raw outputs are in the run scratchpad; every number in this document is reproducible from
`panel_1d.parquet`, `universe_stats2.csv` and `book_costs.csv`.

---

## 0. The headline, before the design

Two findings drive every decision in this document. Both are uncomfortable.

**(a) The wide universe is a worse asset than BTC, not a diversifier.** Over 2019-2026, taking
the median day and looking forward one year, the median coin that *passed a serious quality
filter* (≥$2M ADV, listed ≥180d, ann. vol ≥20%) returned **−37.6%**. Only **12.6%** of them
beat BTC over that year; only **21.7%** were positive at all. Equal-weighting the whole
eligible universe returned **+2.6% CAGR with a −96.6% drawdown**, against BTC's **+49.3% CAGR
with −76.6%**.

**(b) Cross-sectional momentum — the standard way to trade a wide book — is significantly
negative on this data, before costs.** The rank information coefficient of 90-day trailing
return against 90-day forward return, measured across 345 weekly cross-sections, is
**−0.067 with t = −7.4**. That is not "no edge", it is a reliably *wrong* signal at this
horizon. Holding the top 10 by 90d momentum, rebalanced weekly, returned **−39.1% CAGR** with
costs and **−34.5% CAGR at literally zero cost**.

So this document does **not** propose trading 400 coins. It proposes:

- a **wide watchlist** (≈100 pairs, quality-filtered, refreshed weekly) that the scanner, the
  brief and the AI sleeve can all see and reason about — this is the part of "all coins" that
  the evidence supports and that costs almost nothing;
- a **narrow tradeable set** (≈20-30 pairs) that the gate will let an order through for;
- a **small, capped, evidence-gated satellite sleeve** (≤10% of NAV to start, ≤4 names) that is
  the only way a non-core coin can actually be held — sized so that the measured
  expectation, which is roughly zero to slightly negative, cannot do real damage while the
  system earns the right to widen it.

That is the honest reading of the owner's instruction: the system *looks at* all of Binance,
and is *able* to trade anything that passes the quality bar, but the amount of money that can
sit in non-core names is set by evidence rather than by enthusiasm. §6 states plainly what
would make me widen it.

---

## 1. Universe rules

### 1.1 What is actually out there

| Measurement | Value |
|---|---|
| Symbols on spot `exchangeInfo` | 3,710 (1,373 `TRADING`, 2,337 `BREAK`) |
| USDT symbols in `exchangeInfo` | 748 |
| **USDT spot pairs with status `TRADING`** | **496** |
| …after removing leveraged tokens and stable/fiat bases | 480 |
| Total 30d average daily quote volume across them | **$4.84 B/day** |
| Share carried by the top 1 / 10 / 50 / 100 pairs | 26.1% / 67.3% / 85.5% / 92.6% |

"Roughly 400" was close; the real number today is **496**. The distribution is brutally
concentrated: the bottom 300 pairs together are 7.4% of turnover.

### 1.2 The filters, and the measurement behind each

Applied in this order. The funnel column is today's count.

| # | Filter | Threshold | Funnel | Why this threshold |
|---|---|---|---|---|
| 1 | Quote asset | `USDT` only | 496 | Contract, not evidence. Spot only, long only, no margin, no leverage. |
| 2 | Status | `TRADING`, `isSpotTradingAllowed`, `SPOT` in permissions | 496 | 2,337 symbols are `BREAK`; 466 of the 480 candidates carry `MARGIN`+`SPOT`, 14 carry `SPOT` only. Margin permission is *not* used and not required. |
| 3 | Leveraged tokens | base not matching `(UP\|DOWN\|BULL\|BEAR)$` | 480 | Only 2 remain listed today (`JUPUP`… family), but **49 existed historically** and they dominated 2021 volume. A backtest that forgets them is fiction. |
| 4 | Pegged assets | 120d annualised vol **≥ 20%** | 477 | Removes `U` (ann. vol **0.4%**, and it is **rank 23 by volume at $26M ADV** — a hardcoded stablecoin list would have missed it), `KGST`, `SPYB`. A 30% floor would also kill **TRX (0.21)**, which is a real coin. 20% is the largest floor that keeps every genuine crypto. |
| 5 | Not 24/7 | weekend volume **≥ 35%** of weekday volume | 415 | Crypto trades on Saturdays; tokenized US equities do not. Median across all pairs is **0.86**; the 62 pairs below 0.35 are 59 tokenized equities plus `PAXG` (tokenized gold, 0.288), `CELO`, `SENT`. Measured over 120 days, name-independent. |
| 6 | Tokenized real-world assets | explicit name rule + human list | 389 | The behavioural test is not sufficient: **27 `<TICKER>B` equity tokens survive it** (`AAPLB`, `TSLAB`, `MSTRB`, `QQQB`, `SPYB`, `NFLXB`…). The name rule `^[A-Z]{2,6}B$` catches them but would also catch **`ARB`, `BNB`, `CKB`, `DGB`, `SHIB`, `TRB`** — so the name rule carries a human-maintained *crypto allowlist* for those. Gold (`XAUT`, `PAXG`) is on the named exclusion list: it passes both behavioural tests and is still not spot crypto. |
| 7 | Non-ASCII base | base must be ASCII | 387 | Two pairs today: `牛来USDT` (**$32M ADV, rank 18**, 26.9% mean daily range) and `币安人生USDT`. Nothing downstream — journal, filenames, prompts, Telegram — is tested for these. Refuse rather than discover. |
| 8 | Tick size | 1 tick **< 50 bps** of price | 385 | Below one tick there is no price. `BTTC`'s tick is **270 bps** — a 2.7% round trip before fees. Only 3 pairs fail at 50 bps; 55 fail at 20 bps and 129 at 10 bps, which would remove real markets (`PEPE`, tick 22.7 bps, $37M ADV). 50 bps removes the impossible without removing the merely coarse. |
| 9 | Listing age | **≥ 180 days** | 376 | 145 of 480 pairs are under a year old and 90 under six months. A 180d floor is what the momentum/vol features need (a 90d lookback plus a 60d vol window plus slack) — not a claim that young coins are worse. Age floors of 30/60/90/180/365d changed the momentum backtest by less than 6 pp of CAGR, so this is a data-sufficiency rule, not an alpha rule. |
| 10 | **Median** 90d daily quote volume | **≥ $1,000,000** | **106** | See below. |

**Watchlist today: 106 pairs, carrying 83.7% of all USDT spot volume.**

### 1.3 Why *median* volume, and where the liquidity cliff really is

Using the 30-day **mean** is a trap: for **89 of 480 pairs the mean is more than twice the
median**, and at the extreme `G` shows $6.8M mean against a **$269k median** (ratio 25×). A
mean-based filter admits coins whose "liquidity" was one pump. The median is pump-resistant by
construction, so the floor is set on the median.

The cliff itself was measured by walking 38 live order books across the whole rank range.
Round-trip cost = half-spread walk in + half-spread walk out + 20 bps of fees:

| 30d ADV bucket | n | median spread | round trip, **$500** clip | round trip, **$2,000** clip | worst in bucket | median 5%-deep ask book |
|---|---|---|---|---|---|---|
| > $100M | 6 | 0.1 bps | 20.4 bps | **20.4 bps** | 21.4 | $3.62M |
| $20-100M | 7 | 4.2 bps | 24.2 bps | **24.6 bps** | 42.8 | $1.01M |
| $5-20M | 8 | 7.3 bps | 30.6 bps | **36.0 bps** | 54.6 | $136k |
| $1-5M | 8 | 12.2 bps | 39.7 bps | **47.8 bps** | 58.4 | $54k |
| < $1M | 9 | 10.7 bps | 39.1 bps | **54.9 bps** | **286.7** | $53k |

**The cliff is between $20M and $5M ADV**: cost roughly doubles (24.6 → 47.8 bps) and the
5%-deep book falls by an order of magnitude ($1.01M → $54k). At our clip sizes ($200-$2,000) a
$2,000 order is 0.07% of the deep book at rank 1-10, 0.84% at rank 11-50, 1.04% at rank 51-100
and **4.28% at rank 101-200** — that last one is where we stop being a price-taker and start
being the price.

So: **$1M median is the watchlist floor** (a name we are willing to *look at* and carry
features for) and **$5M median is the tradeable floor** (a name the gate will pass an order
for). The $5M tier is 32 pairs today; the $10M tier is 22.

The cost figure that matters for the strategy work is **~30 bps per side all-in for a
satellite-grade name**, which is the number the backtests in §6 use. It is not an assumption:
it is the $5-20M bucket's measured $2,000 round trip (36.0 bps) split across two sides plus
headroom.

### 1.4 Refresh cadence

Measured churn of the eligible set, weekly, 2021→2026:

- eligible set (ADV ≥ $2M): **median 11.0% of names change per week**, p90 18.8%
- top 50 by ADV: **median 6.0% per week**, p90 10.0%

**Refresh the watchlist weekly**, Sunday 18:00 Gulf, alongside `backtest_data`. Weekly matches
the observed churn: daily refresh would re-churn 1-2% of names for no informational gain and
would make the point-in-time record harder to keep; monthly would leave the list ~40% stale at
the tail.

Two exceptions bypass the weekly cadence and act immediately:

- **a delisting notice** (see §2.6);
- **a filter-4 or filter-5 flip** — an asset that starts behaving like a peg or stops trading
  on weekends is removed at the next scanner cycle, not at the next Sunday.

Every refresh writes a **point-in-time snapshot** (`knowledge/universe/YYYY-MM-DD.json`) with
the resolved list, every threshold, and the measured value of each metric per pair. This file
is the whitelist the bots run and the whitelist the backtests replay. It is the single artefact
that makes the live system and the backtest the same system (§5.3 explains why freqtrade
cannot do this for us).

### 1.5 A coin leaving the universe while we hold it

**Never a forced market dump into an illiquid book.** Leaving the universe is a *signal to
exit in an orderly way*, not an exit. Four cases, in descending urgency:

| Trigger | Action |
|---|---|
| **Binance delisting notice** (freqtrade's `Binance._get_spot_delist_schedule`, `has_delisting = True` — verified on the installed 2026.8) | Position moves to `exit_only`. Target weight → 0 over the remaining sessions, in slices capped at **1% of that pair's median daily volume**, using limit orders at the passive side. Hard deadline: fully flat **24h before** the announced delisting time. If the book cannot absorb it, the remainder goes at market at T−24h and the loss is journalled as a universe-management cost, not a strategy loss. |
| **Depeg / halt / hack flag** raised by `reg-watch` | Entries blocked immediately; exit follows the same sliced schedule but with a 48h deadline. This is the existing blackout mechanism, extended to a per-asset flag. |
| **Fails the quality filters** at a weekly refresh (volume, age, tick, 24/7) | Position moves to `exit_only` with **no deadline**. It is trimmed only when the normal rebalance band would trim it anyway, or when the satellite rotation replaces it. A coin dropping from $5.2M to $4.8M median volume is not an emergency. |
| **Fails filter 4 (peg) or filter 6 (real-world asset)** | Should be impossible for a held name, since these are checked before entry. If it happens, treat as the depeg case and alert. |

The exit ladder is deterministic and lives in the gate, not in a model. A position in
`exit_only` can never be increased, by any sleeve, for any reason.

---

## 2. Risk model for a wide book

### 2.1 The finding that sets every number here

I measured the average pairwise correlation of the point-in-time top-30 alts over rolling 60-day
windows since 2019, and split by regime:

| Regime | windows | avg pairwise corr | avg corr to BTC | avg beta to BTC |
|---|---|---|---|---|
| calm | 20 | **0.486** | 0.582 | **1.48** |
| drawdown (BTC >20% off its high) | 99 | **0.412** | 0.539 | 1.03 |
| rally | 20 | 0.373 | 0.462 | 1.03 |

Converting that to independent bets — `N_eff = N / (1 + (N−1)ρ)`:

| Regime | ρ | N=5 | N=8 | N=10 | N=15 | N=20 |
|---|---|---|---|---|---|---|
| calm | 0.486 | 1.70 | 1.82 | 1.86 | 1.92 | **1.95** |
| drawdown | 0.412 | 1.89 | 2.06 | 2.12 | 2.21 | **2.26** |
| rally | 0.373 | 2.01 | 2.22 | 2.30 | 2.41 | **2.47** |

**A twenty-alt book is worth about two independent bets.** Going from 5 names to 20 buys
0.25 of a bet and costs 4× the execution, 4× the monitoring and 4× the tokens.

On correlation "going to 1 in drawdowns" — stated plainly: **on 60-day windows it does not**
(drawdown ρ = 0.412, which is *lower* than calm). **On crash days it effectively does.** Across
the 20 worst BTC days since 2019, between **86.7% and 100%** of the top-30 alts fell with it,
and the *median* alt usually fell harder:

| Day | BTC | median top-30 alt | worst | share down |
|---|---|---|---|---|
| 2020-03-12 | −39.5% | **−43.9%** | −51.9% | 93.3% |
| 2021-05-19 | −14.4% | **−34.4%** | −48.5% | 100% |
| 2022-11-09 | −14.1% | **−17.9%** | −58.7% | 96.7% |
| 2022-05-09 | −11.6% | **−18.3%** | −52.9% | 100% |
| 2026-02-05 | −14.0% | −14.4% | −25.8% | 100% |

So the risk model must assume: **diversification across alts buys ~2 bets in normal times and
approximately nothing on the days that produce the drawdown, with downside beta above 1.**

### 2.2 Position caps

Replaces `risk.max_weight: {BTC: 0.40, default: 0.30}`, which silently gives every new asset a
30% cap the moment it is added to `universe.assets`. That default is the single most dangerous
line in the current config under a wide universe.

| Tier | Membership | Per-asset cap (fraction of sleeve NAV) |
|---|---|---|
| **Core** | `BTC` | 0.40 |
| **Core** | `ETH` | 0.30 |
| **Major** | tradeable tier, median 90d volume ≥ $25M, listed ≥ 2 years | 0.15 |
| **Satellite** | tradeable tier, median 90d volume ≥ $5M | **0.05** |
| **Watchlist only** | median 90d volume ≥ $1M | **0.00 — not tradeable** |

Tier membership is computed by the universe resolver and written into the point-in-time
snapshot. **There is no `default` cap.** An asset with no tier has a cap of zero and the gate
rejects any order for it. Adding a coin to a config list can no longer grant it a 30% cap by
accident.

### 2.3 Concurrency, exposure, and the correlation cap

| Control | Value | Justification |
|---|---|---|
| `max_gross_exposure` | 0.80 (unchanged) | Existing spec value. |
| `usdt_floor` | 0.20 (unchanged) | Existing spec value. |
| **`max_open_positions`** | **8** (core + majors + satellites, per sleeve) | Today this is `len(universe.pairs)` = 2, which becomes 106 by accident. N_eff saturates at ~1.95 by N=20; 8 positions already buy 1.82 of 1.95 available bets. |
| **`max_satellite_positions`** | **4** | §6 shows satellite count is monotonically harmful past ~4 in every window measured. |
| **`max_satellite_gross`** | **0.10 of NAV to start** | The whole satellite sleeve. At 10% the measured 2019-2026 result is 44.3% CAGR / −52.4% DD against core-only 45.5% / −50.9% — i.e. **it costs 1.2 pp of return and 1.5 pp of drawdown**, which is the price of the option. At 20% it costs 1.6 pp and 3.2 pp. 10% is what I am willing to pay to find out. |
| **`max_beta_to_btc`** | **1.30** portfolio-level, on 60d realised beta | Measured alt beta is 1.03-1.48. Without a cap, an 8-name alt book in a calm regime is a 1.5× levered BTC position wearing eight names — which is leverage by another route, and we are spot-only for a reason. |
| **`max_avg_pairwise_corr`** | **0.70** on the held book, 60d | Measured p90 of the calm regime is 0.591. A book above 0.70 is one position; the gate refuses the *incremental* entry that pushes it over. |
| `max_trades_per_day` | 4 per sleeve (unchanged) | |
| `max_orders_per_day` | **16** (from 12) | 8 positions × weekly rotation needs headroom; still a hard runaway stop. |
| `max_turnover_pct_per_day` | 0.50 (unchanged) | §6 measures the design at 7.0×/yr turnover, far inside this. |

The beta and correlation caps are computed by the **gate**, from candles it already has, at
entry time. They are not model inputs and no proposal can waive them.

### 2.4 Minimum notional, against the real Binance filters

Measured: `minNotional` is **$5 for 450 pairs and $1 for 30**. Binance is not the binding
constraint — **Earn's own `risk.min_notional_usdt = 25` is**, and above that, the 5%-of-NAV
rebalance dead-band is.

| Sleeve NAV | rebalance band (5% of NAV) | vs $25 floor | smallest trimmable position | max positions |
|---|---|---|---|---|
| $1,000 (live seed cap) | $50 | OK | $50 = 5.0% of NAV | ~20 |
| $2,000 | $100 | OK | $100 | ~20 |
| $10,000 (test seed) | $500 | OK | $500 | ~20 |

So 8 positions is comfortably feasible at the $1,000 live cap. But two rules follow:

- **`min_position_pct_nav = 0.02`.** A 5% satellite cap at $1,000 NAV is a $50 position; a
  half-trim is $25, exactly at the floor. Below 2% of NAV a position cannot be managed, only
  opened and closed, so the gate refuses to open it.
- **Step-size check at order time.** The coarsest step among the top 150 is `ZEC` at $1.56 of
  notional per step — irrelevant at $50 positions, but the check is cheap and the tail of the
  watchlist is not surveyed. `PrecisionFilter` handles this in freqtrade; the gate re-checks it
  because the gate, not freqtrade, is the authority.

### 2.5 What the gate enforces, restated

Every limit in §2.2-2.4 is a **deterministic pre-trade check in `strategies/riskgate.py`**,
evaluated against the point-in-time universe snapshot and the current book. A proposal is an
*input* to the gate, never a waiver. The gate's rejection reason strings gain
`tier:<asset>`, `beta_cap`, `corr_cap`, `min_position`, `exit_only:<asset>` so the Gate page
and the journal can say exactly which control fired.

### 2.6 Delisting notice

1. `reg-watch` and freqtrade's Binance delisting schedule both feed a per-asset
   `delisting_at` field in the universe snapshot.
2. On notice: asset tier → `exit_only`, cap → 0, entries refused, `DelistFilter` drops it from
   the freqtrade whitelist at the next refresh.
3. Exit ladder as in §1.5 — sliced, passive, ≤1% of median daily volume per slice, flat by
   T−24h.
4. Alert to Telegram immediately; the weekly risk report carries the realised cost of the exit
   separately from strategy P&L, so a delisting never contaminates the strategy's grade.

---

## 3. Selection

### 3.1 What the evidence says about ranking

I tested the obvious rule — rank by trailing return, hold the top N, rebalance, pay costs —
honestly, on the survivorship-free panel, over 2019-2026 including all of 2022.

**Rank information coefficient** (trailing return rank vs forward return rank, weekly
cross-sections, eligible universe only):

| signal | horizon | mean rank IC | t-stat | share of weeks > 0 |
|---|---|---|---|---|
| 30d momentum | 30d fwd | **−0.058** | **−6.22** | 37% |
| 90d momentum | 30d fwd | **−0.069** | **−6.68** | 39% |
| **90d momentum** | **90d fwd** | **−0.067** | **−7.39** | 34% |
| 180d momentum | 90d fwd | −0.017 | −2.00 | 46% |
| 365d momentum | 90d fwd | **+0.023** | **+2.26** | 50% |

Short and medium horizon momentum is **reliably negative** — this is reversal, and it is the
strongest single result in the study. Only the 365-day lookback is positive, and it is weak
(+0.023, t=+2.26, positive in exactly half of weeks).

**Portfolio results**, 2019-2026, weekly rebalance, 30 bps/side, survivorship-free:

| Strategy | CAGR | max DD | Sharpe | turn/yr |
|---|---|---|---|---|
| **BTC buy & hold (benchmark)** | **+49.3%** | −76.6% | 0.96 | 0 |
| BTC/ETH 50-50, weekly | +51.4% | −76.4% | 0.96 | 1.3 |
| **BTC60/ETH40 + BTC>200dMA gate (today's SleeveA shape)** | **+45.5%** | **−50.9%** | **1.01** | 4.7 |
| Equal-weight the whole eligible universe | +2.6% | −96.6% | 0.47 | 9.6 |
| Equal-weight top 50 by volume | −5.6% | −97.5% | 0.37 | 10.3 |
| Equal-weight top 10 by volume | +12.7% | −88.2% | 0.56 | 9.8 |
| Equal-weight top 10 by volume + BTC gate | +20.7% | −67.2% | 0.62 | 9.3 |
| **Top 10 by 90d momentum** | **−39.1%** | −99.7% | −0.01 | 33.1 |
| Top 10 by 90d momentum, **zero cost** | −34.5% | −99.6% | 0.02 | 32.7 |
| Bottom 10 by 90d momentum (reversal control) | −36.4% | −99.9% | 0.04 | 37.5 |
| Random 10 eligible coins (8 seeds) | median −23.2% | | | |
| Top 10 by 365d momentum | −3.7% | −98.0% | 0.42 | 20.5 |
| Top 10 by 365d momentum + BTC gate | +23.1% | −77.2% | 0.65 | 14.7 |

Three things to read off this table. Momentum loses **before costs**, so this is not a cost
problem. The **reversal control also loses** — both tails bleed, because the cross-section
itself bleeds; the negative IC only says the top tail bleeds faster. And **nothing built on the
wide universe came close to BTC buy-and-hold**, let alone beat the gated core on risk-adjusted
terms.

The core+satellite structure, which is what I actually propose:

| Structure (weekly, 30 bps, BTC>200dMA gated) | CAGR | max DD | Sharpe |
|---|---|---|---|
| core 100% (BTC60/ETH40) | +43.9% | −50.7% | 1.00 |
| core 90% + 4 satellites by 365d momentum | **+44.3%** | −52.4% | **1.00** |
| core 80% + 4 satellites | +43.9% | −54.1% | 0.97 |
| core 80% + 8 satellites | +41.5% | −53.9% | 0.95 |
| core 70% + 4 satellites | +43.0% | −55.7% | 0.94 |
| core 50% + 4 satellites | +39.5% | −59.0% | 0.85 |

And out of sample in the two sub-periods:

| | 2022-2023 (bear) | 2024-2026 |
|---|---|---|
| core 100% gated | +28.4% / −19.1% | +11.1% / −48.3% |
| core 90% + 4 sat | +28.7% / −22.6% | +10.7% / −45.9% |
| core 80% + 4 sat | +28.8% / −26.2% | +9.9% / −45.1% |
| core 80% + 8 sat | +26.8% / −25.1% | **+6.5% / −53.1%** |

The satellite sleeve is worth roughly **+0.4 pp of CAGR and −1.5 pp of drawdown at a 10%
allocation**, and gets worse from there. It is not an edge. It is an option on finding one,
and the 10% cap is the premium.

### 3.2 The selection procedure

**Ranking is deliberately not momentum.** Given a −7.4 t-stat against it, ranking satellites by
90-day return would be knowingly trading a wrong signal. The candidate score is a rank-average
of three components, each of which is either measured-positive or measured-neutral:

1. **Liquidity rank** — median 90d volume. Equal-weight top-10-by-volume beat equal-weight
   top-50 by 18.3 pp of CAGR; liquidity is the one cross-sectional sort that measured positive.
2. **Long-horizon trend** — 365d return, the only positive-IC lookback (+0.023, t=+2.26).
   Weighted lowest of the three.
3. **Trend quality** — price above its own 200d MA, and 60d realised vol inside
   [0.40, 1.50] annualised. This is a filter expressed as a score component, not a forecast.

Ties break toward the longer-listed, more liquid name. The score is computed by the resolver,
is written into the snapshot, and is fully reproducible — no model is in this loop.

**Rotation.** Satellites are reconsidered **weekly**, with hysteresis: an incumbent is replaced
only if the challenger outranks it by **≥ 3 positions**. Measured rebalance cadence sensitivity
for the core+4-satellite design: weekly +43.9%, fortnightly +47.2%, monthly +30.8%, two-monthly
+16.1%. Weekly-to-fortnightly is the plateau; monthly is materially worse because the 200d gate
stops being timely. Weekly with hysteresis lands inside the plateau while keeping the gate
responsive. Measured universe churn is 6% of the top-50 per week, so rotation is mostly about
rank changes, not listings.

**BTC and ETH as core.** They are never satellites, never rotated out, and never ranked. They
carry 60/40 of the core allocation. The whole book is gated by BTC's 200d MA: below it, the
system goes to cash (that is what produces −50.9% instead of −76.6%).

**When the system sits in cash.** Three independent triggers, any of which is sufficient:
BTC below its 200d MA with the existing 2% hysteresis; fewer than 2 satellites passing the
score floor while the core gate is also off; or any risk lock (daily stop, monthly stop,
staleness, kill file). Cash is a legitimate and frequent state — in 2022 the gated design was
in cash essentially all year, which is exactly why it returned +28% while BTC returned −65.5%.

### 3.3 How the AI sleeve expresses a view over N assets

The current schema hardcodes `BTC`, `ETH`, `USDT` in `schemas/proposal.json` and requires all
three. `schemas/proposal.py` is already better than the JSON suggests — `build_models(assets)`
generates the target model from `universe.assets`, so the *validator* generalises today. Three
things still break at N=50.

**(a) Dense targets do not scale.** Requiring every asset as a key means a 50-asset proposal is
50 numbers the model must emit and 50 keys of schema it must read. Change `targets` to a
**sparse map** validated against the point-in-time tradeable set:

```jsonc
"targets": {
  "type": "object",
  "propertyNames": { "pattern": "^[A-Z0-9]{2,12}$" },
  "additionalProperties": { "type": "number", "minimum": 0, "maximum": 1 },
  "minProperties": 1,
  "maxProperties": 9          // max_open_positions + USDT
}
```

Rules the validator enforces, which JSON Schema cannot: `USDT` is always required; every other
key must be in the **tradeable tier of the snapshot the proposal cites**; omitted assets mean
**zero**, explicitly, and that is stated in the prompt; the sum still equals 1 ± 0.001.

**(b) The proposal must name the universe it saw.** Add a required
`universe_snapshot` field (the snapshot's date + sha256). A proposal is validated against *that*
snapshot, not against whatever the universe happens to be when the file is read. This is what
makes a proposal replayable after the universe has moved, and it closes the hole where a model
proposes a weight for a coin that was delisted between decision and execution.

**(c) Version it as `schema_version: 4`.** `parse_any` already falls back to the keys a payload
carries, so v2 (BTC/ETH, no version) and v3 snapshots keep replaying. v4 adds sparse targets +
`universe_snapshot`; v3 remains readable.

The model's mandate over N assets is deliberately narrow: it may **choose which satellites to
hold and at what weight inside the caps**, it may **scale exposure down**, and it may go to
cash. It may **not** exceed a tier cap, hold a non-tradeable asset, exceed the satellite gross,
or breach the beta/correlation caps — those are gate refusals, and a proposal that violates one
is rejected whole rather than clamped, so the journal records a model that tried.

---

## 4. Code changes, by file, in three packages

Disjoint ownership. Each package edits only its own files; anything needed across a boundary is
a contract change in `docs/contracts.md` first.

### Package U1 — universe + data

Owns the resolver, the snapshot artefact, and everything that feeds candles.

| File | Change |
|---|---|
| `config/earn.yaml` | Replace `universe.assets`/`universe.pairs` with a `universe.rules` block (every threshold in §1.2), `universe.tiers` (§2.2), `universe.refresh` (cadence + snapshot path), `universe.core: [BTC, ETH]`, and `universe.excluded_bases` (the human-maintained real-world-asset list + the crypto allowlist for `<TICKER>B` names). Keep `data_only_symbols`. |
| `ops/config.py` | `Universe` model: `assets`/`pairs` become **computed properties** reading the current snapshot, not stored config. New `UniverseRules`, `UniverseTiers` models. Rewrite the `_validate` rules at lines ~1870-1885 that assert `pairs == [f"{a}/{quote}"]` and that `max_weight` covers every asset — those are exactly the two-asset assumptions. `news.asset_keywords` coverage check becomes a check over **core assets only**. |
| `ops/universe.py` **(new)** | The resolver. Pulls `exchangeInfo` + 24h ticker + daily klines, computes every metric in §1.2, assigns tiers, writes `knowledge/universe/<date>.json` atomically with the ruleset and a sha256. Pure, offline-testable against a fixture. |
| `ops/universe_refresh.py` **(new)** | The scheduled job (Sunday 18:00 Gulf): resolve → diff against current → write snapshot → emit the add/remove/tier-change list → trigger a data top-up for new names. Refuses to shrink the tradeable tier by more than 50% in one refresh without a human (a Binance API hiccup must not flatten the book). |
| `ops/bootstrap_data.sh`, `ops/refresh_backtest_data.sh` | Read pairs from the snapshot instead of the hardcoded `--pairs BTC/USDT ETH/USDT BNB/USDT`. Keep the `--config` flag (the bug noted in the script's own header). Batch downloads; new listings get history on first appearance. |
| `ops/check_gaps.py` | Iterate the snapshot; report gaps per pair with a tolerance for pairs younger than the requested range. |
| `knowledge/universe/` **(new)** | Point-in-time snapshots. Committed — they are the backtest's whitelist and must be reviewable. |
| `tests/test_ops/test_universe.py` **(new)** | Every threshold in §1.2 against a frozen `exchangeInfo` fixture; the funnel counts; the tier assignment; snapshot determinism. |

### Package U2 — risk + strategy

Owns the gate and both sleeves. **Does not touch the resolver or the proposal schema.**

| File | Change |
|---|---|
| `config/earn.yaml` (`risk:` block only) | Delete `max_weight.default`. Add `max_open_positions: 8`, `max_satellite_positions: 4`, `max_satellite_gross: 0.10`, `max_beta_to_btc: 1.30`, `max_avg_pairwise_corr: 0.70`, `min_position_pct_nav: 0.02`; `max_orders_per_day: 12 → 16`. |
| `strategies/riskgate.py` | `GateConfig` reads per-tier caps from the snapshot, not a flat `max_weight` dict (lines ~172-187). New checks: tier cap, unknown-asset→zero, satellite count, satellite gross, portfolio beta, average pairwise correlation, minimum position, `exit_only`. New reason strings. **This is the package's centre of gravity — the gate is the only thing standing between a wide universe and a bad afternoon.** |
| `strategies/SleeveA.py` | Core/satellite target construction: `base_weights` becomes `core_weights` (BTC 0.60 / ETH 0.40 of core) plus the §3.2 satellite score. Vol targeting applies to the whole book, not per asset. |
| `strategies/SleeveB.py` | Sparse target handling: absent asset = 0; positions in assets absent from the new proposal are exited through the normal band, not dumped. |
| `strategies/sleeve_common.py`, `strategies/mechanics.py` | Indicator helpers over N pairs; the satellite score as a pure function so backtest and live share one implementation. |
| `ops/gen_freqtrade_config.py` | `pair_whitelist` from the snapshot; `max_open_trades` from `risk.max_open_positions` (**not** `len(pairs)` — line 172); pairlist config per §5.3. |
| `tests/strategies/` | A gate test per new control, each asserting the refusal reason. Per §5.3, at least one test must assert that the backtest whitelist equals the snapshot whitelist. |

### Package U3 — decision layer

Owns the schema, the prompts, the scanner and the skills. **Does not touch the gate.**

| File | Change |
|---|---|
| `schemas/proposal.py` | `build_models` grows sparse targets, `universe_snapshot`, `schema_version: 4`; `validate_proposal` takes the snapshot and validates keys against the tradeable tier; `parse_any` keeps v2/v3 replay working. |
| `schemas/proposal.json` | Regenerated from `proposal_json_schema()` — it is already a rendered artefact, which is why this is a small change. |
| `strategies/proposal_loader.py` | The in-container stdlib loader mirrors the sparse rules and the snapshot check (defence in depth; it stays stdlib-only). |
| `runs/signals/features.py` | Two-stage features (§5.2): a **cheap** tier over the whole watchlist (5 keys), a **rich** tier (today's 24 keys) only for core + held + top candidates. The current `{p: _pair_features(...) for p in cfg.universe.pairs}` at line 264 is the exact line that turns 106 pairs into 22k tokens. |
| `runs/signals/detectors.py` | The seven per-pair loops iterate the watchlist; per-detector candidate caps so one volatile night cannot fill the queue. |
| `runs/signals/screener.py` | `{{FEATURES}}` gets the cheap tier plus the rich tier for the shortlist only. |
| `runs/build_prompt.py` | Dossier summaries for **core + currently held** only (line ~186 fans out over `universe.assets` today); a compact table for the rest. |
| `prompts/stages/scan.v1.md` → `scan.v2.md`, `prompts/research.v3.md` → `v4.md` | Explain the two-tier features, sparse targets, "absent means zero", and the tier caps the model is working inside. |
| `.claude/skills/asset-dossier`, `market-state`, `decide`, `reg-watch` | Dossiers on demand for the tradeable tier rather than a fixed two; market-state gains breadth across the watchlist; `decide` learns the sparse form; `reg-watch` emits per-asset delisting/depeg flags for §2.6. |
| `console/routers/market.py`, `portfolio.py`, `signals.py` | Universe page (tiers, snapshot diff, why each name is in or out); portfolio views over N positions. |

**Contract changes needed before any package starts** (`docs/contracts.md`): the snapshot file
format and path; `riskgate.json` gaining `tiers` and the new limits; proposal `schema_version 4`;
`universe.assets`/`pairs` becoming computed. U1 lands first; U2 and U3 run in parallel behind it.

---

## 5. Scaling problems and their answers

### 5.1 Candle downloads and storage — a non-problem

Measured from the existing feather store: **0.219 MB per pair-year at 1h**, 0.060 at 4h, 0.012
at 1d.

| Pairs | 1h+4h+1d, 3 years | 5 years |
|---|---|---|
| 12 (today) | 0.01 GB | 0.02 GB |
| 100 | 0.09 GB | **0.15 GB** |
| 200 | 0.17 GB | 0.29 GB |
| 496 (everything) | 0.43 GB | **0.72 GB** |

Download, measured against the live API: 0.45 s per 1,000-candle 1h request. A 106-pair
watchlist at 5 years across three timeframes is ≈5,658 requests ≈ **6 minutes at 6 threads**,
inside the existing `backtest_data` deadline of 1,800 s. Incremental weekly top-ups are
seconds. Storage and bandwidth are simply not the constraint; do not design around them.

The one real data risk is **rate limits during a first bootstrap of a wide universe**. Binance
allows 6,000 weight/min; klines are weight 2. Cap the resolver and the bootstrap at 6 threads
with a shared token bucket, and let a 429 back off rather than retry hard.

### 5.2 Token cost — the binding constraint

Measured size of the `{{FEATURES}}` block with today's 24 keys per pair:

| Pairs | chars | ≈ tokens (pretty) | ≈ tokens (compact) |
|---|---|---|---|
| 2 (today) | 1,815 | 453 | 346 |
| 20 | 17,998 | 4,499 | 3,442 |
| **50** | 45,027 | **11,256** | 8,617 |
| **100** | 90,034 | **22,508** | 17,231 |
| 496 | 446,778 | 111,694 | 85,528 |

`budgets.context_tokens.research` is **20,000**. A 100-pair watchlist rendered the way features
are rendered today **exceeds the entire research budget with the features block alone**, before
news, state, positions or lessons. This — not storage, not I/O — is what breaks at N=50.

**The answer is two-tier features.** Cheap tier over the whole watchlist: 5 keys
(`ret_24h_pct`, `vol_ann_20d`, `ma200_dist_pct`, `dip_from_high_pct`, `news_count_24h`) ≈
**2,300 tokens at 106 pairs, compact-encoded**. Rich tier (today's 24 keys) only for core +
currently held + the top ~10 detector candidates — ≈20 pairs ≈ 3,400 tokens. Total ≈5,700
tokens, which is *less than a third* of the budget and roughly 12× today's usage for 53× the
coins. Emit compact JSON, not `indent=2`: that alone is a 24% saving.

Two supporting rules: the scanner's `max_candidates_per_cycle: 5` stays (it is what stops a
volatile night from fanning out), and the detectors get per-detector caps so `move` firing on
40 alts at once cannot crowd out `near_stop` on a held position.

Per-cycle I/O, measured, is fine: 6.6 ms per pair to read 1h+1d feather, so 200 pairs is
**1.33 s** against a 240 s scanner deadline. The SQL volume is worth noting though — 288 cycles
a day × 106 pairs × 8 queries ≈ **244,000 sqlite queries/day** against `knowledge/earn.db`.
That wants one batched query per metric instead of one per pair, or the 5-minute cadence starts
competing with the ingest job for the write lock.

### 5.3 Freqtrade pairlist configuration — the finding that shapes the architecture

Verified against the **installed freqtrade 2026.8** (21 pairlist modules). Backtest support:

| `SupportsBacktesting.YES` | `StaticPairList`, `ShuffleFilter`, `OffsetFilter` |
|---|---|
| **`NO`** | **`VolumePairList`, `AgeFilter`, `SpreadFilter`, `VolatilityFilter`, `RangeStabilityFilter`, `DelistFilter`, `PercentChangePairList`, `ProducerPairList`** |
| `BIASED` | `PriceFilter`, `PrecisionFilter`, `MarketCapPairList`, `CrossMarketPairList`, `RemotePairList`, `PairInformationFilter` |

**Every dynamic pairlist we would want is unbacktestable.** If the live bots select their
universe with `VolumePairList` + filters, the backtest cannot reproduce the universe the live
bot traded, and the whole change-control protocol — costed backtest, walk-forward, decision
replay — becomes untestable exactly where the new risk lives.

So: **the universe resolver is ours, and freqtrade always runs `StaticPairList`.** The resolver
writes the snapshot; `ops/gen_freqtrade_config.py` renders it into `pair_whitelist`; the
backtest replays the historical snapshots. Live and backtest then read the same artefact by
construction.

Useful parameters confirmed present (they inform *our* thresholds even though we implement
them ourselves): `VolumePairList` supports `lookback_days`/`lookback_timeframe` with
`min_value`/`max_value`, so its rolling-window volume is genuinely candle-based;
`AgeFilter(min_days_listed, max_days_listed)`; `SpreadFilter(max_spread_ratio, default 0.005)`
— note our measured median spread at rank 101-200 is 14.7 bps, so a 50 bps default would let
through everything we care about excluding; `PriceFilter(low_price_ratio)` is exactly the
tick-as-fraction-of-price test in §1.2 filter 8; `VolatilityFilter(min_volatility,
max_volatility, lookback_days)`.

`DelistFilter` **does** work on Binance spot here: `Binance._ft_has["has_delisting"] = True`
and the class carries `_get_spot_delist_schedule` / `check_delisting_time` with a 300 s TTL
cache. Run it live as a belt-and-braces second line behind §2.6 — but the orderly exit ladder
is ours, because `DelistFilter` removes a pair from the whitelist, which is not the same as
exiting a position gracefully.

**`max_open_trades`** is currently `len(cfg.universe.pairs)` (line 172). Under a wide universe
that silently becomes 106. It must be `risk.max_open_positions` = **8**.

### 5.4 Backtest runtime

Today's 2-pair backtests are seconds. The scaling is roughly linear in pairs for freqtrade's
per-pair indicator population, and the dominant new cost is the **historical whitelist replay**:
the backtest must switch whitelist at each weekly snapshot boundary. Two consequences.

Run the strategy-lab sweeps on the **daily panel in-process** (the vectorised harness used for
this document runs a full 7.7-year, 686-symbol, weekly-rebalance simulation in **0.24 s**,
which is what made ~60 configurations affordable here), and reserve freqtrade
backtests for the handful of configurations that survive — where execution mechanics, order
types and the gate actually matter. The two must agree on the core+satellite baseline before
any result is trusted; that agreement is itself a test.

Also budget for the 30-position-snapshot decision replay (`review.replay`) growing with N:
each snapshot now carries a wider feature payload, so the replay budget of $8 should be
re-measured, not assumed.

### 5.5 Survivorship, and what it does to a naive backtest

Measured from `data.binance.vision` (735 USDT symbols ever, with per-month coverage):

| Month | USDT pairs listed then | still `TRADING` today | survival |
|---|---|---|---|
| 2019-06 | 48 | 34 | 70.8% |
| 2020-06 | 119 | 63 | 52.9% |
| 2021-01 | 234 | 114 | 48.7% |
| **2021-06** | **278** | **134** | **48.2%** |
| 2021-12 | 347 | 172 | 49.6% |
| 2022-06 | 345 | 188 | 54.5% |
| 2023-06 | 358 | 213 | 59.5% |
| 2025-06 | 408 | 342 | 83.8% |

**Fewer than half the pairs listed in mid-2021 still trade.** 252 USDT pairs are dead, median
lifetime 36 months, and delistings run 38 (2022), 24 (2023), 46 (2024), 51 (2025), 50 (2026
YTD) — this is a steady ~50/year process, not a one-off purge.

What that does to a naive backtest over "the top 50", measured by running the identical test
twice — once on today's survivors, once on the full point-in-time universe:

| Test | survivors-only (biased) | full universe (honest) | bias |
|---|---|---|---|
| Top 10 by 90d momentum, weekly, 30 bps | −34.0% CAGR | **−40.7% CAGR** | **+6.7 pp/yr** |
| Core 20% + 8 satellites, BTC gated | +9.4% CAGR | +8.0% CAGR | +1.4 pp/yr |

So survivorship bias is worth **1.4 to 6.7 percentage points of annual CAGR**, and it is worst
exactly where the strategy reaches furthest down the liquidity curve. Any backtest of this
design that uses only currently-listed symbols is overstating itself by roughly that much.

Usefully, `/api/v3/klines` **still serves history for delisted symbols** — verified on
`BREAK`-status pairs (`1INCHUPUSDT`, `A2ZUSDT`) and on `NBTUSDT`, which is absent from
`exchangeInfo` entirely. So there is no excuse for a survivorship-biased backtest here; the
data is free. The panel built for this document should be checked in as the strategy-lab
fixture.

---

## 6. What I would not do, and why

**I would not trade 400 coins, or 100.** §0 and §3.1 are the reason: the median quality-filtered
coin loses 37.6% over a year, only 12.6% beat BTC, and every wide-book construction I tested
underperformed a gated BTC/ETH core on return *and* drawdown *and* Sharpe. The owner asked for
all of Binance and the honest answer is that all of Binance is available to *look at* and the
evidence supports putting ~10% of NAV to work in it. I would rather say that now than discover
it on the demo account over three months.

**I would not rank satellites by 3-month momentum**, which is what "cross-sectional momentum,
rebalance the top N" normally means. The rank IC is −0.067 with t = −7.4 across 345 weekly
cross-sections. That is one of the few genuinely significant numbers in this study and it points
the wrong way.

**I would not use freqtrade's dynamic pairlists in production.** They are marked
`SupportsBacktesting.NO` in the installed version, and a universe that cannot be backtested
cannot be change-controlled. The resolver costs one new module and buys live/backtest identity.

**I would not let `max_weight.default` survive.** A `default: 0.30` that silently applies to
every newly added asset is how a wide universe turns into three 30% alt positions. Unknown
asset must mean cap zero.

**I would not exclude tokenized equities by name alone, or by behaviour alone.** The
`<TICKER>B` name test catches `ARB`, `BNB`, `CKB`, `DGB`, `SHIB`, `TRB` — real crypto. The
weekend-volume test misses 27 equity tokens. Both, plus a human list for gold, or the system
ends up trading Nvidia at 2am Gulf time with a crypto risk model.

**I would not raise the satellite allocation on backtest evidence alone.** The widening
criterion is out-of-sample: after ≥90 days of demo with the satellite sleeve live, the sleeve's
own realised Sharpe must beat the core's, its realised round-trip cost must be within 1.5× of
the 30 bps assumed here (the existing TCA freeze ratio), and zero gate breaches. Then 10% → 15%.
Never more than 5 pp at a time.

**And the honest case for keeping the traded set narrow even with a wide watchlist.** It is
strong, and it is this: a wide watchlist costs ~5,700 tokens and 0.15 GB and buys real
option value — the scanner sees a hack on a coin we do not hold, the brief has breadth, the
dossiers accumulate, and in twelve months there is a dataset to test satellite rules on that
did not exist today. A wide *traded* book costs 4× the execution, adds ~0.25 of an independent
bet (N_eff 1.70 → 1.95), raises drawdown in every window I measured, and on crash days behaves
like one levered BTC position with eight names on it. The asymmetry is the whole argument:
**look wide, trade narrow, and let the evidence buy the width.**

---

## Appendix — provenance

| Artefact | What it is |
|---|---|
| `panel_1d.parquet` | 841,090 daily candles, 735 USDT symbols incl. delisted, 2017-08-17 → 2026-09-23, 22.5 MB |
| `universe_stats2.csv` | 496 live pairs: filters, 30d/90d volume (mean and median), listing age, ann. vol, weekend ratio, tick bps |
| `book_costs.csv` | 38 live order books walked for $250-$10,000 clips, spread, 5%-deep depth |
| `vision_history.csv` | first/last month of data for all 735 USDT symbols, from `data.binance.vision` |
| `correlation_windows.csv` | 139 rolling 60d windows (99 drawdown, 20 calm, 20 rally): avg pairwise corr, corr to BTC, beta, regime |

All measurements taken 2026-09-23. Binance public endpoints only; no keys, no paid data.
