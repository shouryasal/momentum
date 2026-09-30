# Measure 4 — Are we buying and selling at the right times, and do we capture rallies?

Status: measured 2026-09-30. Workspace `pa4`. Measurement only — no production code was
written, no commit was made, no bot, cron, unit or console was touched, nothing was written
into `~/earn-run`, and no order was placed. Scripts:
`evals/research/profit-audit/{entry_quality,exit_quality,rally_capture,gate_risk,paper_entries,pa4_verify}.py`.
Raw outputs: `evals/research/profit-audit/pa4-out/*.json`.

Everything is net of the standing cost floor: **0.30% a round trip** (10 bps fee + 5 bps
slippage per side), always on. Risk-adjusted return is the repo's **arithmetic Sharpe**
(mean/std x sqrt(365)), never CAGR/vol.

---

## The three answers in three sentences

1. **Are we buying at the right times?** No, and it does not matter much: the entry rule's
   day-picking is worth **nothing measurable** — at 30 days it beats entering one day later
   by +1.18% with a confidence interval of −0.41% to +3.01%, which straddles zero, and it is
   **beaten** by a random same-day pick from the same 31 pairs (−0.48%). The value in the
   rule is "be invested while the trend is up", not "pick the day".
2. **Are we selling at the right times?** The **10% stop is the best-timed exit in the
   system** (after it fires, the asset falls another −6.5% over 30 days on average; 80% of
   stops avoided a further fall). The **200-day regime flip is the worst**: after it fires
   the asset *rises* +3.3% over 30 days on average. And there are only ~5.7 sells a year,
   so selling late cost **1,169 USDT on a 20,000 pot in the median bad fall** and 2,571 USDT
   in the worst one.
3. **Do we capture rallies?** We capture a **median 22.8%** of each big rally. Against the
   naive ceiling (40% of BTC, 30% of ETH) that is 59% of the maximum; against a book that
   actually holds those weights and rebalances, it is **86%**. Over 2019–2026 the shipped
   book turned 20,000 into **120,143**; the most a *legal* version of your own mandate could
   have made is **280,695**; buying and holding Bitcoin made **453,580**. The mandate — not
   the timing — is what costs you most of that.

---

## 0. What "the shipped book" means here, and one thing you should know first

Two different rules are running in this repo and they must not be mixed up.

| | what it is | where it is | what this document measures |
|---|---|---|---|
| **The shipped rule** | `SleeveA`, 4h, BTC/ETH plus rotated satellites. Entry: 200-day MA with 2% hysteresis, stake multiplied by the 15-member trend ensemble. Exits: the MA flip, the 10% stop, the monthly circuit breaker. | `strategies/SleeveA.py` + `strategies/earn_base.py` | §1, §2, §3 — simulated 2019-01-01 → 2026-09-24 on the survivorship-free daily panel |
| **What is paper-trading right now** | `SleeveFast`, 1h, `fast-test` profile — a **plumbing test, not an edge**, as its own docstring says | `strategies/SleeveFast.py` | §4 — the 22 real paper trades, 2026-09-23 → 2026-09-30 |

### The thing you should know first: the shipped book can only buy 10 of its 31 pairs

Read out of the live `config/riskgate.json`, cross-checked in code
(`pa4_verify.py: check_3_reachable_universe`):

| group | count | assets | can the shipped sleeve hold it? |
|---|---|---|---|
| core | 2 | BTC, ETH | **yes** — 40% / 30% of NAV |
| satellite, enterable | 8 | AAVE, ADA, AVAX, LINK, LTC, SUI, WLD, XLM | **yes, barely** — 2 seats sharing 5% of NAV, so 2.5% each |
| satellite, `exit_only` | 15 | BCH, DOT, ENA, FET, FIL, HBAR, INJ, ONDO, PENGU, PEPE, PUMP, TAO, TRUMP, UNI, XPL | **no** — the weekly resolver has retired them |
| **major** | **6** | **DOGE, NEAR, SOL, TRX, XRP, ZEC** | **no — and this one looks like a defect** |

The six "major" pairs carry a 0.15 cap in the shipped config, so the risk gate would allow
them. But `SleeveA._book_targets` builds its candidate list as
`[a for a in states if a not in core and cfg.universe.is_satellite(a)]`, and
`RiskGate.is_satellite()` is exactly `tiers[a] == "satellite"`. A *major* is neither core
(`base_weights` is BTC/ETH only) nor satellite, **so it is never offered a seat**. Six pairs
are whitelisted, capped, priced, monitored — and structurally unbuyable. This is not a
return finding and needs no statistical hurdle; it is a code fact, and it is handed to the
lead as such.

---

## 1. (a) Entry quality — is the timing adding anything?

### Method

Every day 2019-01-01 → 2026-09-24 on which the shipped rule would have entered, measured
against four benchmarks on the **same dates**, all net of the same 0.30% round trip:

| benchmark | what it is |
|---|---|
| **random same-day** | the *expected* return of a uniform random pick from every whitelisted pair with data that day, regardless of its regime (the cross-sectional mean is the exact expectation of one draw); a sampled draw is reported too, for the shape |
| **next bar** | the same pair, entered one day later, held the same number of days |
| **week later** | the same pair, entered seven days later |
| **always in** | the same pairs on *every* day they had data — the "is the edge entirely be-invested" control |

Three entry populations, because the shipped rule makes three different kinds of decision:

- **cross** (n=186): the fresh 0→1 regime flip with ensemble > 0 — the `trend` tag, the only
  genuine timing decision the rule makes;
- **dca** (n=1,226): the 7-day calendar top-up while the gate is open;
- **gate open** (n=9,295 asset-days): every day the gate was open, the pool the other two are
  drawn from.

Confidence intervals are paired bootstraps (5,000 resamples) on the difference.

### The fresh regime cross — the only real timing call the rule makes

| horizon | rule | random same day | next bar | a week later | always in |
|---|---|---|---|---|---|
| +1d | +0.58% (hit 48.9%) | +0.28% | +0.72% | +0.52% | −0.12% (hit 46.1%) |
| +7d | +1.19% (hit 49.7%) | +1.81% | +1.25% | +1.57% | +0.99% (hit 47.7%) |
| **+30d** | **+8.11% (hit 49.7%)** | **+8.59%** | **+6.93%** | **+5.29%** | **+6.02% (hit 48.7%)** |
| +90d | +12.70% (hit 45.1%) | +15.73% | +12.48% | +11.91% | +21.93% (hit 48.0%) |

Distribution at +30d, because the mean hides everything: the rule's median is **−0.65%**,
its 10th percentile is **−24.98%** and its 90th is **+36.46%**. Half of the entries lose
money at a month. The mean is a thin right tail.

Paired differences, with intervals:

| comparison | +1d | +7d | +30d | +90d |
|---|---|---|---|---|
| rule − random same day | +0.30% [−0.45, +1.14] | −0.62% [−2.12, +0.96] | **−0.48% [−5.31, +5.56]** | −3.03% [−9.21, +3.34] |
| rule − next bar | −0.14% [−1.38, +1.08] | −0.06% [−1.43, +1.29] | **+1.18% [−0.41, +3.01]** | +0.22% [−1.23, +1.58] |
| rule − a week later | −0.15% [−1.45, +1.18] | −0.38% [−3.32, +2.37] | **+2.52% [−1.85, +7.46]** | +0.79% [−3.17, +4.42] |

**Every interval contains zero.** The pre-registered falsifiers:

| hypothesis | falsifier | verdict |
|---|---|---|
| H4a — the rule beats a random same-day pick at +30d | the 95% CI on the paired difference contains 0 | **REFUTED** (−0.48%, CI −5.31% to +5.56%) |
| H4b — the rule beats entering one bar later by more than 0.30% at +30d | the difference is smaller than the round trip | **REFUTED** (+1.18%, CI spans zero; on the 9,062-observation gate-open pool the difference is a statistically real **+0.23%**, still *below* the 0.30% it costs) |
| H4c — the rule beats entering a week later at +30d | the CI contains 0 | **REFUTED on the 186 crosses** (+2.52%, CI spans zero); **supported on the 9,023-observation pool** (+1.40%, CI +0.93 to +1.87) |

Read plainly: the one-day precision of the entry is worth less than the fee it pays. Waiting
a *week* is measurably worse, which is the only timing statement the data supports — and it
is a statement about being invested, not about the day.

### The 7-day top-up is where it actually gets worse

| population | +30d rule | vs random same day | +90d rule | vs random same day |
|---|---|---|---|---|
| fresh cross (n≈181) | +8.11% | −0.48% [spans 0] | +12.70% | −3.03% [spans 0] |
| **DCA top-up (n≈1,193)** | **+4.69%** | **−3.52% [−5.03, −2.00], p<0.001** | **+14.09%** | **−19.69% [−23.84, −15.64], p<0.001** |
| gate open (n≈9,069) | +4.84% | −3.53% [−4.09, −2.97], p<0.001 | +13.96% | −18.61% [−20.09, −17.12], p<0.001 |

The calendar top-up buys, on average, **3.5 points less over 30 days and 19.7 points less
over 90 days** than a coin drawn at random from the same whitelist on the same day. That
gap is not a timing failure — it is the *selection*: the rule only ever tops up BTC and ETH,
and the random benchmark is free to draw a high-beta altcoin on a day the whole market is
rising. It is the same fact `growth-audit.md` reports from the other direction (only 3.4% of
coins beat BTC in 2023–24, rank IC −0.016 to −0.069). It is worth stating because it is the
honest answer to "are we missing something": on the *upside* in an up-market, yes, by design.

### Does the gate at least pick safer moments? On BTC and ETH, slightly — yes

`gate_risk.py`, forward 30 days, on the 10 reachable assets:

| population | n | mean return | hit rate | median forward worst drawdown | P(drawdown worse than −20%) | return / drawdown |
|---|---|---|---|---|---|---|
| gate **open**, all 10 | 9,069 | +4.84% | 47.0% | −11.84% | 29.2% | 0.33 |
| gate **shut**, all 10 | 14,325 | +6.78% | 49.8% | −10.63% | 27.1% | 0.49 |
| gate **open**, BTC/ETH only | 3,100 | +6.18% | 53.3% | −7.43% | **14.1%** | — |
| gate **shut**, BTC/ETH only | 2,488 | +4.43% | 57.2% | −6.95% | **17.9%** | — |

On the whole reachable set the gate is *backwards*: the days it sits out have higher forward
returns and a slightly smaller drawdown risk. On BTC and ETH alone — which is what the book
actually holds — it earns 1.75 points more over 30 days and cuts the chance of a worse-than
−20% fall from 17.9% to 14.1%. So the gate's value is a **risk** filter on the core, not a
return filter, and it does not travel to the alts.

### BTC and ETH on their own (n is tiny — read as an illustration, not evidence)

| horizon | BTC cross (n=17–18) | BTC every day (n≈2,800) | ETH cross (n=14–15) | ETH every day |
|---|---|---|---|---|
| +7d | +2.77% (hit 66.7%) | +0.83% (hit 51.3%) | +3.67% (hit 66.7%) | +1.04% (hit 51.1%) |
| +30d | +5.09% (hit 55.6%) | +4.82% (hit 55.6%) | +9.65% (hit 73.3%) | +5.97% (hit 54.5%) |
| +90d | +21.78% (hit 64.7%) | +17.37% (hit 57.2%) | +21.42% (hit 64.3%) | +19.36% (hit 57.0%) |

The crosses do beat "every day" at every horizon on both legs — and are beaten by a random
same-day pick at every horizon on both legs. **With 14 to 18 observations per cell none of
this clears any bar.** In 7.7 years the shipped rule made about **33 genuine timing
decisions on the core**. That is the whole sample. No statistic computed on 33 events can
settle whether entry timing works, and this document does not claim one does.

---

## 2. (b) Exit quality — did the exit avoid a fall or miss a rise?

### What can actually sell, in the shipped configuration

Read out of `earn.yaml` and confirmed against the code. Most of the exit machinery is
switched off:

| exit reason | status |
|---|---|
| take-profit ladder rung | **cannot fire** — `take_profit.ladder = []` (`:264`) |
| ROI | **cannot fire** — `roi_table = {0: 10.0}` = off (`:263`) |
| trailing stop | **cannot fire** — `trailing.enabled = false` (`:258`) |
| ATR stop | **cannot fire** — `atr.enabled = false` (`:260`) |
| daily stop | **cannot sell** — `daily_loss_response: hold` (`:189`), and `crisis-policy.md` §0 says that is correct |
| trend-ensemble trim | **not deployed** — built, 77 tests, refused by the order-path reviewer |
| force exit | human only; never fired in simulation |
| **200-day regime flip** | fires — ~2.2 times a year |
| **10% per-trade stop** | fires — ~2.7 times a year |
| **monthly −10% circuit breaker** | fires — 7 times in 9.11 years |

Over the full 9.11-year panel the shipped book produced **52 sells — 5.71 a year** (20 regime
flips, 25 stops, 7 monthly breakers) against 54 entries. `exit-and-horizon-2026-09-29.md`
reported 40 sells in 9.11 years on its construction; this one, built independently, gets 52.
The two differ in the satellite sleeve and in entry bookkeeping; **the order of magnitude is
the finding and both constructions agree on it.**

### After each exit, what did the money do? (forward return on the asset sold)

"Avoided a fall" = the asset was lower at that horizon, so selling was right.

| exit reason | n | mean weight sold | P&L booked | fwd +1d | fwd +7d | **fwd +30d** | avoided a fall at 30d |
|---|---|---|---|---|---|---|---|
| **10% stop** | 25 | 0.077 of NAV | −12.5% (0 winners) | +0.57% | −2.23% | **−6.51%** | **80.0%** |
| **200d regime flip** | 20 | 0.156 of NAV | +13.6% (35% winners) | −0.91% | +0.86% | **+3.26%** | 65.0% |
| **monthly −10% breaker** | 7 | 0.316 of NAV | +129.2% (100% winners) | +4.91% | +8.31% | **+2.84%** | 28.6% |

In money, on a 20,000 pot, summed across all of an exit reason's firings:

| exit reason | saved (+) or cost (−) at +1d | at +7d | at +30d |
|---|---|---|---|
| 10% stop | −318 | +254 | **+1,704** |
| 200d regime flip | +354 | −844 | **+766** |
| monthly breaker | −2,374 | −3,869 | **−1,339** |

Pre-registered falsifiers:

| hypothesis | falsifier | verdict |
|---|---|---|
| H4d — mean forward 30d asset return after an exit is negative | it is ≥ 0 | **REFUTED for the regime flip (+3.26%) and the monthly breaker (+2.84%); SUPPORTED for the stop (−6.51%)** |
| H4e — the regime flip is better timed than the 10% stop | the stop's forward 30d is the more negative | **REFUTED — the stop is the better-timed exit, by 9.8 points** |

**This is the one clean, actionable exit finding in the study, and it is the opposite of what
you would expect.** The dumb mechanical 10% stop sells into a market that keeps falling four
times out of five. The clever-looking 200-day trend flip sells into a market that, on
average, is 3.3% *higher* a month later — it catches the slow bears (2022) and gets whipsawed
by the fast ones. And the monthly −10% circuit breaker is the worst-timed sell in the system:
it fires after a good run (it books +129% on average, so it is selling winners), and the
market is higher one day, one week and one month later every time, costing 1,339 USDT per
20,000 at a month. It is a circuit breaker, not a strategy, and it should not be judged as
one — but it should not be widened on the belief that it is protecting anything either.

### What did selling LATE cost, in money?

The exit-horizon study measured a median 24 extra days to halve exposure in the five worst
falls. This extends it to money: the same 0.40/0.30 targets with the ensemble trim wired to
the sell side (the counterfactual built, tested and *refused*) against the shipped book,
across the same five falls, on a 20,000 pot.

| peak → trough | days | BTC fall | shipped NAV | with-trim NAV | shipped worst drawdown | with-trim worst drawdown | **late-exit cost on 20,000** |
|---|---|---|---|---|---|---|---|
| 2021-11-08 → 2022-11-21 | 379 | −76.6% | −9.4% | −7.0% | −10.8% | −8.4% | **−479** |
| **2021-04-13 → 2021-07-20** | 99 | −53.1% | −13.7% | −0.8% | −34.0% | −8.4% | **−2,571** |
| 2025-10-06 → 2026-06-22 | 260 | −48.6% | −13.6% | −7.7% | −14.9% | −8.5% | **−1,169** |
| 2025-01-20 → 2025-04-08 | 79 | −25.4% | −10.0% | −4.8% | −12.0% | −6.0% | **−1,025** |
| **2024-03-13 → 2024-09-06** | 178 | −26.2% | −19.4% | −10.3% | −20.4% | −10.8% | **−1,823** |

Median **1,169 USDT per 20,000**, worst **2,571**. And it is not only money: the shipped
book's worst fall inside these five windows is **−34.0%** against the trimming book's
**−8.4%**. The pattern from the exit study repeats — the gap is widest in the *fast* falls
(April 2021, March 2024) and narrowest in the slow 2021–22 bear, where a 200-day average and
the ensemble happen to agree.

Whole-period, the two books:

| | CAGR | Sharpe (arith) | worst drawdown | average gross | fees/yr | sells/yr |
|---|---|---|---|---|---|---|
| shipped, 9.11y | 19.5% | 0.866 | −34.7% | 0.223 | 0.24% | 5.7 |
| with the ensemble trim | 14.7% | **1.121** | **−20.6%** | 0.146 | 0.29% | 15.0 |

Less money, much less risk, and a higher risk-adjusted score — the same conclusion the exit
study reached, reproduced by a second independent implementation. **It is still below the
deflated hurdle of ~2.32 and is not offered as an alpha claim.** It is a drawdown finding.

---

## 3. (c) Rally capture — do we make the most of a rally?

### The rally rule (stated, as asked)

A **25% zig-zag on daily closes**: mark a peak when price falls 25% from a running high,
mark a trough when it rises 25% from a running low; keep every trough→peak up-swing of
**at least +50%**. Reported at 20% / 25% / 30% reversal thresholds so the answer does not
depend on the knob. `pa4_verify.py` re-implements the zig-zag from scratch and finds the
**same 21 core rallies with identical troughs and peaks** — the segmentation is not an
artefact of one script.

| reversal threshold | rallies in BTC+ETH | median shipped capture |
|---|---|---|
| 20% | 25 | 18.7% |
| **25% (headline)** | **21** | **22.8%** |
| 30% | 15 | 13.2% |

Capture ratio = the whole book's NAV return over the rally window ÷ the rallying asset's gain.

### The 21 big rallies in BTC and ETH, 2019–2026

| asset | trough → peak | days | asset gain | **shipped capture** | avg weight held | cap | % of rally days flat | static ceiling | constant-weight ceiling | shipped ÷ static | shipped ÷ constant-w |
|---|---|---|---|---|---|---|---|---|---|---|---|
| BTC | 2019-01-01 → 2019-06-26 | 176 | +245% | 23.7% | 0.131 | 0.40 | 52% | 40.0% | 27.7% | 0.59 | 0.86 |
| BTC | 2019-12-17 → 2020-02-14 | 59 | +56% | 16.7% | 0.061 | 0.40 | 71% | 39.8% | 35.8% | 0.42 | 0.47 |
| BTC | 2020-03-12 → 2021-01-08 | 302 | +745% | 26.5% | 0.239 | 0.40 | 16% | 40.0% | 19.5% | 0.66 | 1.36 |
| BTC | 2021-01-27 → 2021-04-13 | 76 | +109% | **73.0%** | 0.477 | 0.40 | 0% | 39.9% | 33.4% | 1.83 | 2.18 |
| BTC | 2021-07-20 → 2021-11-08 | 111 | +127% | **8.7%** | 0.099 | 0.40 | 32% | 39.9% | 32.4% | 0.22 | 0.27 |
| BTC | 2022-11-21 → 2024-03-13 | 478 | +363% | 25.2% | 0.252 | 0.40 | 24% | 40.0% | 24.9% | 0.63 | 1.01 |
| BTC | 2024-09-06 → 2025-01-21 | 137 | +97% | 15.9% | 0.280 | 0.40 | 24% | 39.9% | 33.4% | 0.40 | 0.48 |
| BTC | 2025-04-08 → 2025-10-06 | 181 | +63% | 35.7% | 0.307 | 0.40 | 8% | 39.8% | 35.4% | 0.90 | 1.01 |
| ETH | 2019-01-29 → 2019-06-26 | 148 | +225% | 25.8% | 0.102 | 0.30 | 43% | 30.0% | 20.6% | 0.86 | 1.25 |
| ETH | 2019-12-17 → 2020-02-14 | 59 | +134% | 7.0% | 0.034 | 0.30 | 75% | 29.9% | 22.6% | 0.23 | 0.31 |
| ETH | 2020-03-12 → 2020-09-01 | 173 | +341% | 9.1% | 0.123 | 0.30 | 22% | 30.0% | 18.3% | 0.30 | 0.50 |
| ETH | 2020-09-23 → 2021-02-19 | 149 | +510% | **52.8%** | 0.315 | 0.30 | 0% | 30.0% | 15.6% | 1.76 | 3.38 |
| ETH | 2021-02-28 → 2021-05-11 | 72 | +194% | 43.2% | 0.426 | 0.30 | 0% | 30.0% | 21.0% | 1.44 | 2.06 |
| ETH | 2021-06-25 → 2021-09-05 | 72 | +118% | **5.7%** | 0.057 | 0.30 | 18% | 29.9% | 23.8% | 0.19 | 0.24 |
| ETH | 2021-09-21 → 2021-11-08 | 48 | +74% | 15.2% | 0.115 | 0.30 | 0% | 29.9% | 25.6% | 0.51 | 0.59 |
| ETH | 2022-06-18 → 2022-08-13 | 56 | +99% | **0.0%** | 0.000 | 0.30 | **100%** | 29.9% | 25.1% | 0.00 | 0.00 |
| ETH | 2022-11-09 → 2023-04-16 | 158 | +92% | 24.9% | 0.131 | 0.30 | 41% | 29.9% | 25.5% | 0.83 | 0.98 |
| ETH | 2023-10-12 → 2024-03-11 | 151 | +164% | 44.0% | 0.198 | 0.30 | 13% | 29.9% | 21.5% | 1.47 | 2.04 |
| ETH | 2024-09-06 → 2024-12-08 | 93 | +80% | 22.8% | 0.040 | 0.30 | 69% | 29.9% | 25.5% | 0.76 | 0.90 |
| ETH | 2025-04-08 → 2025-08-22 | 136 | +228% | 9.3% | 0.091 | 0.30 | 61% | 30.0% | 20.3% | 0.31 | 0.46 |
| ETH | 2026-06-25 → 2026-09-21 | 88 | +77% | 10.3% | 0.054 | 0.30 | 62% | 29.9% | 25.3% | 0.34 | 0.40 |

Medians: shipped capture **22.8%**; weight held **0.123**, which is **38.4% of the cap**;
**24.3% of rally days entirely flat**; shipped ÷ static ceiling **0.593**; shipped ÷
constant-weight ceiling **0.857**.

### Where do the misses come from?

Four candidate causes were asked for. Measured, in order of size:

| cause | measured | share of the shortfall |
|---|---|---|
| **1. position too small** | median weight held during a rally is **0.123** against a cap of 0.40 / 0.30 — 38% of what is permitted. Removing the **30% annual vol target** raises average gross exposure from 0.307 to 0.395 and median rally capture from 22.8% to **31.4%**. | **the largest, by far** |
| **2. not invested at all** | flat on a median **24.3%** of rally days; in the ETH +99% run of mid-2022, flat on **100%** of them (the 200-day MA had not turned back up) | **second** |
| **3. exited early** | **7 exits inside 6 of the 21 rallies** — 5 regime flips, 2 stops. 15 of 21 rallies had no exit at all. | **small** |
| **4. the pair is not in the universe** | see below — **the single biggest number in this document** | **dominant, if you count rallies rather than money** |

And the surprise: the **ensemble entry gate is not the binding constraint**. Removing it
raises average gross from 0.307 to 0.356 (+0.049) and median capture from 22.8% to 23.6%.
Removing the vol target raises gross to 0.395 (+0.088) and capture to 31.4%.

| hypothesis | falsifier | verdict |
|---|---|---|
| H4f — shipped capture ≥ 0.50 × the caps-ceiling capture in core rallies | median ratio < 0.50 | **SUPPORTED** — median 0.593 |
| H4g — the ensemble entry gate binds harder than the 30% vol target | removing the vol target raises rally exposure more | **REFUTED** — the vol target binds nearly twice as hard (+0.088 vs +0.049 of gross) |

### Cause 4: 401 of 422 rallies happened in coins the book cannot meaningfully own

Every ≥+50% rally (25% zig-zag) in all 31 whitelisted pairs, 2019–2026:

| group | rallies ≥ +50% | median gain | most the shipped book could ever have added to NAV |
|---|---|---|---|
| **core (BTC, ETH)** | **21** | +127% | **+43.7%** |
| satellite, enterable — 2 seats sharing 5% of NAV | 128 | +97% | **+2.4%** |
| satellite, `exit_only` | 167 | +95% | **0.0%** |
| **major — blocked by the `is_satellite` filter** | **106** | **+107%** | **0.0%** |
| **total** | **422** | | |

**273 of 422 big rallies were in coins the shipped sleeve literally cannot buy**, and 106 of
those are blocked by what looks like a code defect rather than a decision. Per asset the
worst cases are ZEC (27 rallies, unbuyable), FET (26, unbuyable), LINK (22, 2.5% cap), NEAR
(20, unbuyable), DOGE (19, unbuyable), INJ (19, unbuyable).

Before this reads as "we are leaving a fortune on the table": `growth-audit.md` and
`dip-strategy.md` measured what happens if you *do* chase them. A costed top-8 momentum
rotation over the same universe returns **−13.5% CAGR at −98.9% drawdown** and loses before
costs; **0 of 6,720** dip/satellite configurations beat holding BTC; spreading the same
signal across an altcoin basket turns +42.9% CAGR into **−8.1%**. The 273 unreachable
rallies are visible in hindsight and were not selectable in advance. What *is* a defect is
that six of the 31 pairs are unbuyable for a reason nobody chose.

### The ceiling: what is the most a long-only spot book at these caps could do?

The identity is simple arithmetic. A long-only spot book holding weight *w* of NAV in the
rallying asset, the rest in USDT earning nothing, ends a rally of *R* at **1 + w·R**. Its
capture ratio **is w**. No signal, no model and no amount of cleverness can raise it, because
the book cannot borrow, cannot short and cannot hold more than the cap. This is
`dip-strategy.md` s0.2's finding restated: *a long-only spot book at full exposure cannot
express upward conviction.* Quantified:

| ceiling | value | why |
|---|---|---|
| single-asset rally, BTC | **0.40** | the cap is the capture |
| single-asset rally, ETH | **0.30** | the cap is the capture |
| single satellite | 0.025 | 5% gross ÷ 2 seats |
| both legs rallying together | **0.70** | BTC 0.40 + ETH 0.30; median measured both-legs capture 63.3% |
| the configured `max_gross_exposure` | 0.80 — **unreachable** | the only caps above the satellite 0.05 are BTC's 0.40 and ETH's 0.30, and 0.40 + 0.30 = 0.70. The 0.20 USDT floor therefore never binds either. |
| a book that *rebalances* to those weights | **median 0.251, as low as 0.156** | holding a constant weight sells the winner all the way up; in BTC's +745% run the ceiling is only 19.5% |

Against the generous static ceiling the shipped book captures **59.3%** of the maximum.
Against the constant-weight ceiling — closer to what the shipped rebalance band actually
does — it captures **85.7%**. The honest reading: **most of the "missed rally" is the mandate,
not the timing.**

### The whole 2019–2026 period, in money, on a 20,000 USDT pot

All books on the identical span 2019-01-01 → 2026-09-24, same costs.

| book | total multiple | 20,000 becomes | CAGR | Sharpe (arith) | worst drawdown | avg gross | legal? |
|---|---|---|---|---|---|---|---|
| **shipped** | **6.01×** | **120,143** | 26.1% | **0.916** | −45.8% | 0.307 | yes |
| shipped without the ensemble gate | 8.32× | 166,408 | 31.5% | 0.982 | −47.0% | 0.356 | yes |
| shipped without the 30% vol target | 7.86× | 157,142 | 30.5% | 0.911 | −50.2% | 0.395 | yes |
| **0.40 BTC / 0.30 ETH, rebalanced in the 2% band — the mandate-legal ceiling** | **14.03×** | **280,695** | 40.7% | **0.969** | −61.3% | 0.700 (max 0.736) | **yes** |
| buy and hold at the caps, never rebalanced | 14.92× | 298,314 | 41.8% | 0.880 | −75.5% | 0.930 | **no** — breaches the 0.40/0.30 and 0.80 caps |
| buy and hold BTC | 22.68× | 453,580 | 49.7% | 0.964 | −76.6% | 1.000 | (the benchmark) |
| daily-foresight oracle (sells the top every day) | 2.7e10× | — | 2,133% | 10.90 | −0.3% | 0.358 | not a thing that exists |

Baseline check: on this 2019–2026 span BTC hold is Sharpe **0.964**, not the 0.83 quoted
elsewhere. `pa4_verify.py` recomputes BTC hold on the **full** panel (2017-08-17 →
2026-09-24) and gets **0.827** — the repo's canonical 0.83 reproduces exactly. Both figures
are right for their span; every comparison in this section uses the same 2019–2026 span for
every book.

**The headline the owner asked for.** The shipped book made **120,143 of the 280,695** its own
mandate legally allowed — **42.8%** — while taking **75%** of the mandate-legal drawdown
(−45.8% against −61.3%). Its risk-adjusted score, 0.916, is *below* both the mandate-legal
ceiling's 0.969 and BTC hold's 0.964. So the missing 160,552 USDT did not buy better
risk-adjusted returns. The only thing it bought is a shallower worst fall, and
`exit-and-horizon-2026-09-29.md` shows even that is achieved by accident: the book's exposure
falls because the asset falls, not because it sold.

Per-rally money, for scale (each figure on a flat 20,000 pot inside that one rally — **these
are not additive into a portfolio total**): across the 21 core rallies the shipped book gave
up **64,854 USDT** against the static caps ceiling, and 1,951,998 against daily foresight. The
second number exists only to show what "perfect timing" would be worth, and it is not
available to anyone.

---

## 4. The 22 real paper trades — what actually happened on this machine

Read-only copies of `ft_userdata/{a,b}/tradesv3.sqlite` and
`ft_userdata/{a,b}/runs/test-{a,b}-000.sqlite`, all 22 now closed. Forward prices from
Binance's **public** `/api/v3/klines` (market data, no key, no trading endpoint), because the
local candle store stops at 2026-09-23 16:00Z, before paper trading began.

**These are `SleeveFast` on the 1h `fast-test` profile — not the rule measured above** — over
about 29 waking hours spread across seven days, with the host asleep for most of the window.
Horizons are **hours**, not days. Nothing here is evidence about anything; it is a check that
the live path does what the code says.

Realised: **22 trades closed, 10 winners (45.5%), −44.81 USDT total** on a 20,000 simulated
pot (−0.22%), mean +0.04% and median −0.37% per trade.

Entry quality, net of the 0.30% round trip:

| horizon after entry | n | mean | median | hit rate |
|---|---|---|---|---|
| +1h | 22 | −0.04% | −0.98% | 18.2% |
| +4h | 22 | −0.82% | −1.42% | 9.1% |
| +24h | 22 | +0.64% | −0.48% | 45.5% |
| +72h | 22 | +0.52% | −1.14% | 36.4% |

At its own median holding period the fast rule's entries are under water 9 times out of 10.
That is the same verdict `exit-and-horizon-2026-09-29.md` reached from 9 years of simulation
(+0.022% of gross edge per trade against the 0.300% it costs — a 13.7× shortfall) and from the
live loss (−1.29%/30d with 1.35% in fees — a 22× shortfall). Three methods, one answer.

Exit quality — forward move on the asset after the sell, and what selling cost or saved on the
actual stake:

| exit reason | n | booked P&L | fwd +24h | move to now | cost (+) / saving (−) of having sold, USDT |
|---|---|---|---|---|---|
| `exit_signal` (EMA trend loss) | 8 | −60.31 | +0.64% | +0.86% | **+34.53 cost** |
| `trailing_stop_loss` | 7 | +33.68 | +2.02% | +0.89% | **+13.03 cost** |
| `roi` | 3 | +51.58 | −4.22% | −4.38% | **−31.85 saved** |
| `force_exit` | 2 | −42.21 | +0.12% | +0.15% | +1.39 cost |
| `target_zero` (name left the universe) | 2 | −27.56 | −1.59% | −1.19% | **−91.59 saved** |

At +4h, 72.7% of the exits avoided a fall; at +24h, 59.1%. Netted across all 22, selling
rather than holding to now **saved about 74 USDT** — against a realised loss of 44.81. So the
exits were, if anything, the better half of the fast profile; the entries were the problem. On
22 trades over six days that is noise, and it is recorded as noise.

One thing worth the lead's attention: the two `target_zero` exits — the sleeve noticing a name
had left the tradeable universe — were the single most valuable sells in the sample (91.59 USDT
saved). The universe machinery is doing real work.

---

## 5. Statistical honesty

- **Trials added.** Counting a distinct book or parameterisation evaluated as one selection
  trial: 6 books in the rally study (shipped, no-ensemble, no-vol-target, oracle, caps-pinned,
  BTC hold) × 3 zig-zag thresholds = 18, plus 2 exit counterfactual books, plus 3 entry-offset
  variants (0/1/7 bars), plus 1 mandate-legal ceiling book = **24**. Cumulative:
  ~8,134 → **~8,158**. The deflated hurdle stays at roughly **2.32** in arithmetic Sharpe.
- **Nothing here clears that hurdle and nothing here is offered as an edge.** The highest
  Sharpe measured anywhere in this document is **1.121** (the trim counterfactual), well below
  2.32. Every finding is either a *diagnostic of the deployed book* (capture ratios, exit
  timing, drawdown, limit compliance) or a *code fact* (six unbuyable majors). Diagnostics and
  defects need no hurdle because they are not claims about beating the market. **If anyone
  later quotes §3's 280,695 as an alpha discovery they are misquoting it** — it is what
  removing a discretionary constraint would have done in this one sample, with a −61.3%
  drawdown attached.
- **Effective sample size is the binding limit on (a) and (b), not the day count.** The shipped
  rule made ~33 genuine entry decisions on the core and 52 sells in 9.11 years. Forward windows
  overlap heavily, so the 9,069-observation gate-open pool contains far fewer independent
  observations than its n suggests. Conclusions are stated as "not measurable" rather than
  "zero" wherever that is the honest answer.
- **Both baselines are reported beside every result**: BTC buy-and-hold and the deployed rules.
- **Costs are on everywhere.** 0.30% a round trip, subtracted exactly once per round trip.
- **Cross-checks that passed.** BTC hold reproduces the repo's canonical Sharpe 0.83 on the
  full panel (0.827). An independently written zig-zag finds the same 21 core rallies with
  identical dates. The unreachable-universe count was read out of the shipped
  `config/riskgate.json` and then confirmed against `SleeveA.py` and `riskgate.py` line by
  line. The sell count (52 in 9.11y) is the same order of magnitude as the exit study's 40 from
  a different implementation.

## 6. What I cannot stand behind

- **Any per-cell conclusion about BTC or ETH entry timing.** n = 14–18. The direction is
  suggestive; the sample is not.
- **The exact 22.8% median capture.** It moves to 18.7% at a 20% zig-zag and 13.2% at 30%. The
  *shape* — a capture in the twenties against a ceiling in the thirties-to-forties — is robust;
  the decimal is not.
- **The 280,695 mandate-legal ceiling as a recommendation.** It is a measurement of what the
  caps permit, on one realised path, with a −61.3% drawdown. It says nothing about what the
  next seven years will do, and holding 0.70 of NAV through a −76% BTC fall is a decision about
  temperament, not statistics.
- **The 22 paper trades as evidence of anything.** Six days, 29 waking hours, a profile whose
  own docstring calls it a plumbing test.
- **The forward returns after the two still-recent exits.** They are hours old; the +30d column
  cannot exist yet and is reported absent rather than guessed.
- **The trim counterfactual as a thing to ship.** It was refused by the order-path reviewer for
  four named reasons (no latch, the largest de-risk is fee-budget-silenceable, a resting buy can
  fake a breach, and it would let SleeveA sell under KILL). This document measures what it would
  have been worth; it does not re-litigate the refusal.
- **Whether the six unbuyable majors are a defect or an unwritten decision.** The code fact is
  certain; the intent is not mine to declare.
