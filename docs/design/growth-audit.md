# Growth audit — how to identify a coin will grow, what to add, and what to ask before buying

**Status:** audit, decision-grade. Nothing here is switched on until the build list in §6 ships
through its stated tier. No running bot, config or `.env` was touched producing it.

**Scope change that triggered it (owner, verbatim):** *"even if btc or other coins were high on
same days it could be low today, how to identify it will grow, same for other coins, we do demo
trading for all coins. what other strategies can be added. in history for these strategies and
time frame find for each coin what made it grow, what happened in the world and numbers that made
a coin grow. check everything and make a learning strategy. identify the ones which gave max
growth. does the model before making an order ask questions to itself, why this coin, why would it
grow, when to sell, if it drops when to sell, how to make max growth. think of everything and do
an audit."*

Four parallel audits ran on this host on 2026-09-24, each rebuilding its own survivorship-free
panel from `data.binance.vision` and `/api/v3/klines`. Where they agree on three independently
built panels, the result is triangulated and I treat it as solid. Where they disagreed I settled
it with a fifth measurement of my own (§1.6). Every artefact is listed in the appendix.

**The owner named the trap and it governs every line below:** a coin being up on some historical
day tells you nothing about today. So each claim here is labelled **FORWARD** (a feature known at
time T, tested against return after T, out of sample) or **COINCIDENT** (a description of what
accompanied growth after the fact). Coincident claims are reported only to be marked worthless
for trading. Two of the most persuasive tables in this document are in that category and are
labelled so in their headings.

---

## 0. The answer, before the detail

**Read this first, because it changes what the system should try to do.**

Growth in this market is **almost entirely market beta plus noise.** There is no forward-looking
way, in 841,491 daily candles across 747 coins and nine years, to identify which coin will grow.
The arithmetic, measured three ways:

| Measurement | Value |
|---|---|
| 2023-24: BTC's total return | **+466%** |
| 2023-24: median eligible coin's BTC-beta component | **+218%** |
| 2023-24: median eligible coin's actual total return | **−14%** |
| 2023-24: share of coins with positive coin-specific return | **6.2%** |
| 2023-24: share of coins that beat BTC | **3.4%** |
| Median alt-beta to BTC, every regime 2019→2026 | **~1.00** |
| First principal component of daily cross-sectional variance | **57-69%**, highest now at 69.0% |
| Median coin's drift after a BTC factor *and* an alt-market factor | within **±5%/yr of zero** in every regime except 2021, where it is **−21%/yr** |

A coin is a 1.0-beta bet on one factor, with extra volatility drag (median residual vol 85-127%
annualised, which costs a log-return holder 35-80%/yr) and worse convexity (down-day beta **1.38**,
up-day beta **0.86** — on the worst 10% of BTC days the wide book loses 1.34× BTC; on the best 10%
it gains 1.00×).

And the base rate that frames everything: across 49,833 eligible coin-weeks the **median** coin
returns **−7.65% over 30 days** and **−18.81% over 90 days**, with hit rates of 37.5% and 32.3%.
The median eligible altcoin loses money at every horizon. The mean is near zero only because a
few enormous winners drag it up.

So the honest answer to *"how do we identify it will grow"* is in three parts:

1. **You mostly cannot.** Growth is a calendar event, not a coin-selection event: 220 sustained
   doublings since 2018 started in only 44 distinct months, and **10 months hold 62.3% of them**.
   Nothing measurable at time T forecasts which month — the two market-state variables that could
   (breadth, BTC's trend state) fail forward, one with a t-statistic of **+0.25** and the other
   with its sign inverted through an entire regime.
2. **You can reliably identify which coins will lose**, and that is a different and tractable
   problem. Low cross-sectional volatility, old listings and real liquidity each predict forward
   returns and forward drawdowns with the same sign in all three regimes, on three independently
   built panels. Combined they move the median forward 90-day return from **−20.2% to −7.2%** and
   cut the probability of a 40% drawdown from **41.2% to 18.3%**.
3. **And when you do catch growth, you will give it back unless you sell on a plan.** Of the 980
   qualifying +100% run-ups that actually happened, **77.5% were mostly or entirely given back
   within 60 days of the peak.** Today the system has **no take-profit level on any position** and
   the only channel through which a model could set one is a field nothing reads (§3.1).

**Therefore the objective changes.** Stop trying to find growth. Build a loser-avoidance filter,
take profit on a ladder because gains do not persist, and let the core carry the beta. That is not
a retreat from the owner's question — it is the measured answer to it, and it is the version of
the mandate the system was already written for: *smaller drawdowns and discipline, not fast gains.*

Two things follow immediately and both are in the build list:

- **7 of the 31 pairs the bots are trading right now would not pass** the measured quality bar
  (§1.5): `ZEC`, `PUMP`, `ONDO`, `ENA`, `PENGU`, `TRUMP`, `XPL`. Under the current
  `config/earn.yaml`, the `satellite` tier requires **only** $5M median volume and the 180-day
  watchlist age floor — no volatility ceiling and no meaningful age bar. That is the single
  highest-value fix in this document.
- **The decision path cannot currently ask "why this coin" at all**, and not because of the
  prompt: `knowledge/earn.db` holds **8 daily candles per pair** against the **201** that
  `compute_state.asset_state()` requires, so `knowledge/state/latest.json` has `assets: {}` and
  the model is handed no per-asset number for any of the 31 names. All three proposals ever
  produced abstained at 100% USDT. Fix the plumbing before the schema, or the only measurable
  change will be the shape of the abstain.

---

## 1. How do we identify a coin will grow?

### 1.1 The ceiling on what is knowable

§0 has the beta arithmetic. Two further measurements bound the problem.

**A satellite book is a coin-flip against BTC, in every regime but one.** Random *k* eligible
names, held 90 days, 15 bps/side, survivorship-free, 250 draws per monthly section:

| regime | k | median | % positive | BTC mean | **% of draws beating BTC** |
|---|---|---|---|---|---|
| 2021 bull | 4 | +19.5% | 63.0% | +14.3% | **55.0%** |
| 2019-20 | 4 | +10.2% | 58.7% | +43.4% | **38.7%** |
| 2022 bear | 4 | −25.1% | 19.5% | −14.9% | **26.6%** |
| 2023-24 | 4 | −2.0% | 48.0% | +23.9% | **22.5%** |
| 2025-26 chop | 4 | −22.2% | 20.1% | −3.3% | **17.2%** |

Going from k=4 to k=30 barely moves the median; it only narrows the distribution. This
independently reproduces the N_eff saturation already on file (1.70 at 5 names, 1.95 at 20).

**And alt outperformance cannot be timed.** Target: forward 90-day equal-weight-universe-minus-BTC.
Alts beat BTC on **23.7% of days** and in **6 of 31 quarters** since 2019, with 2021-Q1 dominating
nine years. Trailing 90d alt-minus-BTC predicts forward alt-minus-BTC with the **wrong sign**
(NW t = −1.59); its top quintile is positive only 18.6% of the time. **No feature tested puts
expected forward alt-minus-BTC above zero in any quintile.** "Alt season has started, pile in" is
precisely backwards.

### 1.2 FORWARD — the features that survived

A feature counts as surviving only if the sign holds in **all three regimes** (2019-22 bull-then-bear,
2023-24 recovery, 2025-26 chop). Statistics are weekly cross-sectional rank ICs with Newey-West
t-stats at lag = horizon/7, so the market-level return is differenced out and overlapping windows
cannot inflate significance.

| Feature at T | Horizon | Strength | Triangulated? | Verdict |
|---|---|---|---|---|
| **Cross-sectional 60d realised vol** (low good) | 7-90d | IC −0.140, t −6.4 at 30d; −0.160 at 90d | **Yes — 3 panels** | Strongest result in the audit |
| **Same, as a drawdown forecast** | 90d | P(dd<−40%) **3.5% → 50.4%** across vol bands, monotone in all 3 regimes | Yes | The version the gate should use |
| **Listing age** (older good) | 7-90d | IC +0.128 (7d) to +0.182 (90d), t +6 to +7.5 | **Yes — 2 panels** | Second strongest, independent of vol |
| Median 90d quote volume | 30-90d | IC +0.045 t +2.16 (30d) | **Yes — 3 panels** | Real, weak, never alone |
| Volume trend 30d/180d (**rising is bad**) | 90d | IC −0.069, t −3.28 | Single panel | Moderate; loses significance in 2025-26 |
| Distance from 90d high (near is good) | 30d | IC +0.054, t +4.33 | Single panel | Hypothesis only — see §2.4 |
| Extreme perp funding ≥40% ann → drawdown | **7d** | P(dd<−8%) ratio **1.41**, and **1.67 within the low-vol tercile** | Resolved this audit (§1.6) | Rare-event flag only |
| 365d momentum | 30-90d | IC +0.023 to +0.037, t +2.26 to +2.75 | **Yes — 3 panels** | Too weak to rank on; leave at 0.25 weight |

The headline is worth stating in levels, because the structure is sharper than the IC:

| 60d ann. vol at T | n | median fwd 30d | hit | **P(90d drawdown < −40%)** |
|---|---|---|---|---|
| **< 50%** | 1,150 | **+2.3%** | **56.5%** | **3.5%** |
| 50-75% | 5,726 | −2.7% | 44.7% | 16.8% |
| 75-100% | 12,745 | −7.9% | 36.4% | 35.0% |
| 100-125% | 13,029 | −10.8% | 34.0% | 48.9% |
| > 220% | 1,765 | −14.9% | 32.5% | **50.4%** |

The sub-50%-vol bucket is **the only bucket in the entire study with a positive median forward
30-day return and a hit rate above 50%.** It survived four controls: present in BTC-UP months
(t −4.09) as well as BTC-DOWN (t −6.87), so not a beta artefact; monotone within every liquidity
quintile, so not a size proxy; IC −0.095/−0.127/−0.154 after excluding the ten largest names every
week, so not "buy BTC/ETH in disguise"; and independent of age in a double sort (oldest × lowest-vol
median forward 30d **−1.5%** against newest × highest-vol **−15.7%**).

And listing age, which the repo currently treats as a data-sufficiency rule:

| age at T | n | median fwd 90d | **P(90d < −50%)** |
|---|---|---|---|
| 180-270d | 4,091 | −26.6% | **25.5%** |
| 1-1.5y | 6,679 | −23.1% | 18.2% |
| 2-3y | 10,012 | −17.9% | 13.5% |
| **> 4y** | 10,212 | **−14.7%** | **9.7%** |

**The break is at about 2 years, not 180 days.** `universe.rules.min_listing_age_days: 180` is
justified in config as "data sufficiency (90d lookback + 60d vol + slack)". Measured, it is one of
the two strongest forward-predictive features in the universe, and the tradeable tier is set far
too loose.

> **One implementation rule, and getting it wrong inverts the signal.** The volatility effect is
> about **which coin**, not **when**. Bucketing each coin's 60d vol against its **own trailing
> 365-day percentile** makes P(90d dd < −40%) **non-monotone** (40.6 / 34.1 / 33.8 / 40.9 / 47.2%),
> and in 2019-22 the *calmest* own-vol quintile had the **highest** tail risk at 53.5%. The feature
> must be ranked **across the cross-section at a point in time**. The natural way to write it — a
> rolling self-percentile, which is exactly how the funding feature is specified in
> `crypto-research.md` §1.2 — gets it backwards. This needs a regression test, not a comment.

### 1.3 COINCIDENT — what was observable before growth, and why it is worthless

**This table is not tradeable. It selects on the outcome.** It is here because it is the single
most persuasive artefact the audits produced, and every separation in it is destroyed or reversed
by the forward tests above.

Median feature value one day before a +100% run-up began:

| feature at t0−1 | sustained (n=220) | given back (n=760) | all eligible cells |
|---|---|---|---|
| prior 90d return | **+2.4%** | **−37.2%** | −8.9% |
| volume 30d/180d | **1.09** | **0.59** | 0.74 |
| distance from own 200dMA | **+2.1%** | **−30.8%** | −13.0% |
| above own 200dMA | **yes (100%)** | **no (0%)** | no |
| breadth (share above 200dMA) | 0.267 | 0.127 | 0.225 |

Conditioning on an episode having occurred, P(sustained | quartile) separates cleanly — distance
from 200dMA 0.06 → 0.39, volume trend 0.11 → 0.41 — against a 0.23 base. Now the forward tests on
the same features:

- **Above your own 200dMA** separated sustained from given-back episodes *perfectly* in hindsight
  and predicts **essentially nothing** forward (IC −0.003 at 30d, +0.006 at 90d, sign-flipping by
  regime). This is the sharpest example in the audit of a feature that *precedes* growth and does
  not *predict* it.
- **Volume trend runs the opposite way forward.** Sustained doublings had rising volume (1.09 vs
  0.59) — and rising volume forecasts **decline** (IC −0.069, t −3.28, and P(fwd 30d < −30%) rising
  14.7% → 18.2% across quintiles). Volume expansion *accompanies* growth and *predicts* its end.
- **Breadth** looks excellent pooled (the share of coins doubling over the next 90d rises 5.4% →
  14.4% across breadth quintiles) and its forward slope on median return has a t-statistic of
  **+0.25** with R² 0.002, with the regime splits inverting completely.

**This is the owner's trap, drawn from the data rather than asserted.** Anything that describes a
past winner will describe it beautifully and tell you nothing about tomorrow.

### 1.4 The base rates that make people fool themselves

Unconditional over 48,712 coin-weeks: **P(90d ≥ +100%) = 5.43%**, P(90d > 0) = 33.0%, median −18.8%.

**Does a coin that doubled double again?** Yes — and it is a lottery ticket, not an edge:

| condition | n | P(next 90d ≥ +100%) | median next 90d | P(next 90d < −50%) |
|---|---|---|---|---|
| base rate | 48,712 | 5.43% | −18.81% | 15.3% |
| trailing 30d ≥ +100% | 1,299 | **10.78%** | **−32.54%** | **32.0%** |
| trailing 30d ≥ +200% | 338 | **13.02%** | −34.80% | **35.8%** |

The probability of doubling roughly doubles, the median return halves, and the chance of losing
more than half your money doubles. **The distribution moves right in the tail and left in the
middle.** It also decayed to nothing: 15.5% against a 7.5% base in 2019-22, **4.7% against a 6.2%
base in 2023-24 — below base** — and 1.1% against 1.07% in 2025-26.

**Does a coin at a 1-year high keep rising?** At 90 days it has a **worse** median (−29.2% vs
−18.7%) and a **lower** hit rate (30.9% vs 33.0%) than a coin that is not. The forward *mean* is
positive (+7.3%) and that is the trap — it is one or two survivors in 651 observations.

**Does last month's strongest coin lead?** The single strongest coin of the prior 30 days lands in
the next month's **bottom decile 38.2%** of the time and its **top decile 18.5%** — twice as likely
to be worst as best — with a median next-30d return of **−12.6%**. The prior-month top 5% averages
−3.9%, worse than the universe and worse than the prior-month *bottom* 20%.

**And the one the owner's framing points straight at:** of 980 qualifying +100% run-ups, **77.5%
were mostly or entirely given back within 60 days of the peak.** The useful question is not "which
coin will grow" but "will I still have the gain in two months", and the answer is usually no.

### 1.5 The one construction that works — an exclusion filter, not a selector

Built only from features that held sign in all three regimes, all known at T:

```
vol60 ≤ 1.00 annualised  AND  listing age ≥ 1095d  AND  median 90d quote volume ≥ $10M
```

| | fails (n=45,398) | **passes (n=4,435)** |
|---|---|---|
| median fwd 30d | −8.41% | **−2.12%** |
| hit 30d | 36.7% | **44.9%** |
| median fwd 90d | −20.16% | **−7.17%** |
| mean fwd 90d | −1.78% | **+6.10%** |
| **P(90d drawdown < −40%)** | 41.2% | **18.3%** |
| P(90d return ≥ +100%) | 5.41% | 4.31% |

Better in **every regime on every leg**. In 2023-24 the passing names had a 49.9% 30-day hit rate
and a 50.8% 90-day hit rate. Two things make this different in kind from a backtested strategy:

- **It is a plateau, not a peak.** Across an 80-cell grid of thresholds (vol_max 0.8-2.0, age_min
  365-1460d, adv_min $2-25M), **100% of cells beat the all-eligible baseline on median forward 90d
  return AND 100% beat it on P(90d dd < −40%).** So pick the middle of the plateau, never the peak —
  the repo's own doctrine from the MA125 choice.
- **It survives the harsher survivorship convention.** Marking delisted coins to −100% instead of
  their last print **widens** the gap (−9.5% passing vs −21.8% failing).

**What it costs, stated plainly.** P(90d double) falls from 5.41% to 4.31% — you keep ~80% of the
lottery while cutting tail risk by more than half. Applied to the live book, that cost has a name:

**The 31 pairs the bots are trading today, measured 2026-09-24:**

| | Coins |
|---|---|
| **Pass tight** (vol≤1.00, age≥3y, adv≥$10M) — 14 of 31 | BTC, ETH, XRP, SOL, DOGE, TRX, LINK, AAVE, LTC, XLM, WLD, ADA, SUI, AVAX |
| **Pass loose** (vol≤1.25, age≥2y, adv≥$5M) — 24 of 31 | above plus DOT, HBAR, TAO, FIL, FET, INJ, BCH, NEAR, PEPE, UNI |
| **Fail both** — 7 of 31 | **ZEC** (vol 1.32), **PUMP** (vol 1.38, age 379d), **ONDO** (age 532d), **ENA** (vol 1.32, age 906d), **PENGU** (vol 1.05, age 647d), **TRUMP** (vol 1.30, age 614d), **XPL** (vol 1.01, age 365d) |

**ZEC is the honest cost of this filter and I am putting it in the text rather than a footnote.**
ZEC ran +845% from 2025-04-15 to 2025-10-11 and held +566% sixty days past the peak — the ninth
largest sustained episode in nine years, and the most recent one. The filter would have excluded it
on volatility, then and now. That is the trade: give up roughly one doubling in five to halve the
drawdown tail. The measured expectation says take it, and anyone who wants the other side of that
trade should say so explicitly rather than arriving at it by leaving the filter off.

### 1.6 NEW — resolving the funding conflict, and a general rule

Audit 1 and Audit 3 reached opposite conclusions on per-coin funding, which matters because a
shipped skill (`leverage-state`) is designed around it. I settled it by measuring both feature
forms against both horizons on the same survivorship-free panel (373 symbols with funding, 38,072
coin-weeks, 2019-09 → 2026-09).

**The rolling self-percentile form has no signal at any horizon.** Top-quintile / base ratio for
P(forward drawdown beyond the threshold):

| form | 7d dd<−8% | 30d dd<−20% | 90d dd<−30% | monotone? |
|---|---|---|---|---|
| **own-history 1y percentile** (Audit 3's form, and the form `crypto-research.md` §1.2 mandates) | **1.054** | **0.980** | **1.003** | no |
| same, survivors only (Audit 3's sample) | 1.052 | 0.981 | 1.015 | no |
| **absolute level ≥ 40% annualised** | **1.410** | 1.193 | 1.138 | n/a (flag) |

Survivorship is **not** the explanation — the survivors-only ratios are identical to three decimal
places' worth of meaning. The explanation is **feature form and horizon.** Audit 1's "46.5% →
61.0%" is an endpoints-only reading of a table whose middle is not monotone (53.5 → 58.7 → 55.0 →
61.0); recomputed, the dose-response does not exist. What exists is an **extreme-level flag**, and
it is strongest at **7 days**, not 90 — matching the horizon of the BTC result already on file.

**It is genuinely incremental to volatility**, which is the test that earns it a place:

| vol tercile at T | P(7d dd<−8%), funding < 40% | funding ≥ 40% | ratio | ex-biggest-cluster |
|---|---|---|---|---|
| **low vol** | 29.5% | **49.3%** | **1.673** | 1.283 |
| mid vol | 42.3% | 62.5% | 1.476 | 1.415 |
| high vol | 48.2% | 56.9% | 1.180 | 1.111 |

Median `vol60` inside each tercile is identical between hot and cold (0.753 vs 0.758 in the low
tercile), so this is not volatility in disguise. It spans 81 distinct weeks and 201 distinct
symbols. A threshold sweep gives a **plateau from ≥40 upward** (ratio 1.410 / 1.387 / 1.366 / 1.358
at 40 / 50 / 60 / 80; ex-cluster 1.310 / 1.308 / 1.314 / 1.337), so ≥40% annualised is the middle of
the plateau rather than its peak. By regime the sign holds throughout (ratio 1.18 in 2019-22, 1.98
in 2023-24).

**But it has nearly stopped firing**, and this is the part that decides how to build it: the flag
covers **12.25%** of eligible coin-weeks in 2019-22, **5.30%** in 2023-24 and **0.05%** — six
observations — in 2025-26. The basis trade has institutionalised exactly as `crypto-research.md`
§1.2 predicted. The tempting fix is to rescale to a percentile so it fires as often as it used to;
the table above shows that converts a real signal into noise.

> **The general rule, now measured twice on different features.** For volatility (§1.2) and for
> funding (here), the **absolute cross-sectional level** carries the information and the **coin's
> own history** does not. `crypto-research.md` §1.2's instruction — *"must be expressed as a
> rolling percentile, never a fixed threshold"* — was measured on **BTC alone**, where there is no
> cross-section and a percentile is the only available form. It must not be carried over to
> per-coin work, and the doc should say so rather than leaving the next run to inherit it.

### 1.7 What happened in the world — and why it cannot inform an entry

The owner asked what world events made coins grow. Measured on a 238-event dated calendar
(200 primary-source verified), with 20,000-draw permutation tests against random dates:

- **The entire ETF move is anticipation.** Pre-window [−20,−1] **+26.14%** against a +3.17%
  random-date null (**p = 0.0%**); event day +1.11% (p = 16.5%, not significant); [+1,+10]
  **−2.60%** and negative in 9 of 12 cases. By the time it is news, it is over.
- **Collapses break the price before the headline.** FTX, Terra, Celsius and the rest: pre[−5,−1]
  **−7.01%** (p = 0.1%), event day **−4.41%** (p = 0.0%), and **nothing significant afterwards**.
  Selling into a public collapse headline sells the low.
- **Scheduled macro is not tradable and does not even raise volatility.** FOMC (n=71) event-day
  excess +0.73%, t = +1.69; CPI (n=107) +0.13%, t = +0.31 — and the two have *opposite* signs
  (FOMC before-good/after-bad, CPI before-bad/after-good), which is the signature of noise. Both
  show realised vol **below** the unconditional average before and after. The best FOMC positioning
  rule earns 11.92% CAGR against 37.90% for simply holding. **Do not add a macro blackout**; the
  existing exclusion of scheduled macro from flags is correct and is now evidence-backed.
- **The 4-year halving clock has already failed once in the two times it can be observed.** 2020
  halving: +562% at one year. 2024 halving: **+31%**. BTC is 863 days past the 2024 halving at
  **+21%**, where the 2020 cycle was at **+141%** on the same day of the clock. That is the owner's
  question in its purest form, answered by the data: same day of the cycle, utterly different
  outcome.
- **Growth clusters into a few months and the clustering is not forecastable.** 62.3% of sustained
  doublings started in 10 calendar months. Post-hoc event narration about those months has **zero
  demonstrated forward value** and should be treated as entertainment, not input.

The one genuinely forward-looking event effect is **realised-volatility persistence**: rvol-z stays
at +1.39 (collapse), +2.14 (depeg), +2.19 (macro shock) through [+1,+5] against an unconditional
−0.05. That is direction-free risk information and is the only thing to act on around events.

**This also condemns the shipped event machinery.** `{{EVENT_STATS}}` in the decide prompt is
filled by `event_study.py`, which reports **raw forward means with no baseline, no pre-event window,
no scheduled/unscheduled split, no significance and no n**. Crypto drifted up, so every class reads
positive — including ETF, whose honest post-event number is *negative*. A model handed
`{"etf": {"mean_1d_pct": 2.4}}` reads an edge where the permutation test gives p = 16.5%. It also
anchors on the first 4h close at/after `published_at`, which for the 2025-10-10 14:57 UTC event is
hours after the move.

---

## 2. Which strategies to add

Eleven families were tested, costed, against BTC buy-and-hold **and** the shipped sleeve, split by
regime. **Ranked by what they add to the portfolio — measured as correlation with the shipped
return stream, not standalone Sharpe.** The mandate is smaller drawdowns, so that is the axis.

Baselines, 2021-01 → 2026-09, costs on. The last column re-scales to the shipped sleeve's own
18.1% vol, which is the only fair comparison across books of different gross:

| Book | CAGR | vol | Sharpe | MaxDD | CAGR @18% vol |
|---|---|---|---|---|---|
| BTC buy-and-hold | 21.0% | 57.2% | 0.62 | −76.6% | 11.2% |
| **Shipped sleeve (proxy)** | 13.6% | 18.1% | **0.80** | **−24.9%** | **14.4%** |

### 2.1 RANK 1 — Replace the single MA200 gate with an ensemble of the trend family

**This is the only change in the audit that improves both axes of the mandate.** Fifteen variants
(own-MA 50→250, Donchian 20/10 → 100/50), each costed, **signals averaged into one book traded
once** — not fifteen books.

| Book | CAGR | Sharpe | MaxDD | months < −5% | worst month | turn/yr |
|---|---|---|---|---|---|---|
| BTC buy-and-hold | 39.1% | 0.83 | −83.2% | 38 / 110 | −37.3% | 0 |
| single own-MA200 | 14.9% | 0.95 | −23.6% | 10 | −10.4% | 5.1 |
| single Donchian 55/20 | 17.8% | 1.28 | −18.0% | — | — | 5.3 |
| **ensemble avg(15)** | **18.1%** | **1.25** | **−17.8%** | **0** | **−4.9%** | 5.5 |

Blended against the shipped sleeve: 100% shipped → Sharpe 0.80 / MaxDD −24.9% / 14.4%
risk-normalised CAGR; 50/50 → 0.88 / −19.8% / 15.9%; **100% ensemble → 0.92 / −17.8% / 16.7%.**
Replace the gate; do not blend a quarter of it in.

Why I believe this rather than the usual backtest:

- **It selects nothing, so it owes no deflation.** Walk-forward "pick the best trailing-3y Sharpe
  each year" delivered Sharpe 1.22; equal-weighting all 15 with no selection delivered **1.31**.
  The 2022 walk-forward pick had an in-sample Sharpe of 2.22 and returned **−1.00** out of sample.
- **It holds in every window, which no single member does**: 1.78 (2017-20, never looked at) /
  0.59 (2021-22) / 1.44 (2023-24) / 0.54 (2025-26) — where Donchian 55/20 alone goes to **−0.14**
  in chop.
- **Not an exposure artefact**: average gross 0.177 vs 0.191, and own-MA200 scaled to the
  ensemble's exact vol earns 13.4% with −21.2% MaxDD against 18.1% and −17.8%.
- **Cost-robust**: ensemble Sharpe 1.31 / 1.27 / 1.25 / 1.19 / 1.12 at 0 / 10 / 15 / 30 / 50 bps
  per side, beating the single rule at every level.
- **It clears the multiple-testing hurdle, by a hair.** Sharpe 1.25 against an expected best of
  ~1.10 from 60 zero-skill trials, and ~1.19 from the ~177 trials run across this whole audit day.
  A margin of 0.06 on the pooled count is thin and I am reporting it as thin.

**What it costs, honestly.** It trades return for consistency monotonically: 1 variant 25.0% CAGR /
−25.9% DD / 4 bad months; 15 variants 18.1% / −17.8% / 0. Against the best single variant chosen in
hindsight (own-MA50, Sharpe 1.40) the ensemble **gives up 6.9pp of CAGR**. I recommend it because
1.40 sits inside the search-noise band and because the mandate is drawdowns — but a reader who
wants CAGR should know the price. And the 15 members are worth **1.23 independent bets** (mean
pairwise correlation 0.802): averaging removes *parameter-choice* risk, not market risk. If trend
following stops working, all 15 fail together.

### 2.2 RANK 2 — Add no new strategy family

Ten of eleven families are rejected below, and the eleventh is a reorganisation of the family
already running. The right answer to *"what else can we add"* is **a better-constructed version of
what we already own**, not a new sleeve.

### 2.3 RANK 3 — Add the exclusion filter as tier membership (this is §1.5, and it ranks here)

Not a strategy, and the highest-value change in the document. Measured effect in §1.5; the build
spec is item 2 in §6.

### 2.4 Two hypotheses for `strategy-lab` — observe only, never sized

- **Distance from the 90-day high** (IC +0.054, t +4.33) beating trailing 90d return (−0.016) over
  the *same* cross-sections. Interesting precisely because proximity to a high carries forward
  information that the return which got there does not. Needs an independent harness: this family
  is where a one-bar lookahead bug lived (see §5).
- **Short-horizon mean reversion** (5d < −15%, hold 10d): standalone Sharpe **0.38** — second worst
  in the audit — yet at **correlation 0.13** with the shipped sleeve it produces the **best blend
  drawdown of anything tested (−18.7%)**. That is the arithmetic this section is organised around:
  correlation beats standalone Sharpe. It is still not shippable, because it was the one survivor
  of five variants and the other four lost money (−7.0%, −4.6%, −0.3%, +4.2% CAGR).

### 2.5 Rejected, with the measurement

| Family | Measured result | Verdict |
|---|---|---|
| **New listings / listing effect** | Median new listing: **−17.9%** over 30d from day 1, **−29.3%** over 90d from day 7, **−52.6%** over a year from day 30 — while BTC over the identical days did +1.7% / +8.9% / +34.6%. Only 17-25% beat BTC. Costed book **−99.9%**. The 1-year *mean* is **+149.6%** against that −52.6% median: the lottery shape in its purest form. | **Reject outright.** Invisible on a survivor-only panel. The scanner must never present a new listing as an opportunity. |
| **Cross-sectional momentum, 30-90d** | Rank IC −0.016 to −0.069 across three panels, same sign everywhere. Costed: top-8 by 90d momentum, weekly, 15 bps → **−13.5% CAGR with a −98.9% drawdown**. Loses **before** costs. | **Reject.** Ranking satellites on trailing quarterly return is knowingly trading a wrong signal. |
| **Buy-the-dip / deepest drawdown** | Conditional table is inviting (5d<−15% → median fwd 10d +0.52% vs −1.10% baseline) and every costed book is a disaster: −25.2% to −51.9% CAGR, −89% to −99.6% total. The crowding conditioner runs **backwards** — crowded dips did *better*. On BTC, the quintile nearest the ATH has the **best** forward 30d. | **Reject.** The exact trap the owner named. |
| **Funding as an entry filter** | Vetoing entries above the 0.9 / 0.8 / 0.7 funding percentile cuts trend-sleeve CAGR **13.8% → 9.0% → 5.8% → 3.6%** and does **not** reduce drawdown (−15.9% → −19.5%). | **Reject as a veto.** Keep only the rare extreme-level drawdown flag from §1.6. |
| **Seasonality / day-of-week** | 7 tests, best \|t\| 2.49, expected best from noise ≈2.0; Friday flips +48.3 bps to +3.8 across halves. Trained on H1, traded in H2: **−89.3%**, because a day-level on/off rule runs 219 turns/yr = 33% of NAV in fees. | **Reject twice** — statistically and economically. |
| **BTC-dominance rotation** | 60d BTC-vs-alt relative strength has correlation **−0.005** with the next 60d of the same spread across 3,204 observations, quintiles non-monotone. The rotation book (7.9% CAGR) is worse than either leg held statically. | **Reject.** Spending fees on a zero-information signal. |
| **Volatility targeting as alpha** | Sharpe 0.66 / 0.69 / 0.63 across settings against static 0.40/0.30's 0.68. | **Reject as alpha, keep as sizing.** Selling it as a return improvement is the honest-looking lie already named in `crypto-research.md`. |
| **Per-asset weighting schemes** | Inverse-60d-vol vs fixed 40/30: Sharpe 1.03 both, MaxDD −39.2% vs −39.3%. Risk parity, inverse vol and ERC are the same book at correlation 0.78. | **Reject.** Sizing belongs at portfolio level. |
| **Narrative / event-driven entry** | §1.7. Breadth forward slope t = +0.25; BTC>200dMA inverts as a growth predictor through all of 2023-24; ETF post-event drift negative. | **Reject as entry input.** Keep news for hazards only. |
| **The satellite sleeve as a return source** | Core-only 33.3% CAGR / 1.03 Sharpe / −39.3% MaxDD; core + 4 satellites costs 2-3pp of CAGR and adds ~1pp of drawdown under **every** ranking tried (shipped score, low-vol, 365d momentum). The design doc measured +0.4pp on a shorter window without a delist haircut. | **Not a reject — but not an edge.** Both readings are noise around zero. Keep the 10% cap as an option premium and stop expecting it to pay. |

---

## 3. The self-interrogation before any buy

**The owner's question was: does the model ask itself why this coin, why would it grow, when to
sell, when to sell if it drops, how to make max growth? Measured answer: no — two of the five have
a required field, both unconstrained prose at portfolio level, and the exit plan it does emit is
read by nothing.**

### 3.1 What exists today

Sample size stated first: the entire production history of this decision path is **3 proposals,
5 signal validations, 8 watch events, 0 decision grades**, all from a 36-hour window. That cannot
establish a rate. It does establish that mechanisms exist or are absent, and that specific defects
fire in production.

| Owner's question | Field | Required? | Validated how | Per-coin? |
|---|---|---|---|---|
| why **this** coin rather than another | **none** | **ABSENT** | — | — |
| why would it grow | `rationale[]` | yes | 1-6 strings, ≤200 chars. **No content check at all** | **No** — portfolio-level |
| the numbers that support it | **none** | **ABSENT** — prompt says "each citing a number"; nothing verifies one is present | — | — |
| when to sell into strength | `plan.take_profit_pct` | **optional** (whole block nullable) | clamped 0.03-0.50 | No |
| when to sell if it drops | `plan.stop_pct` | **optional** | clamped 0.03-0.15 | No |
| what would prove me wrong | `invalidation` | yes | string, 10-300 chars. **That is the entire check** | No |
| rejected alternatives | **none in schema** | prose only in the prompt checklist | — | — |

Six defects, each verified in the code rather than inferred:

1. **`proposal.plan` has no effect whatsoever.** It is parsed, clamped, journalled, assigned to
   `self._plan` (`SleeveB.py:243`) and written to gate store key `sleeveb_plan` — and **read by
   nothing** except one test assertion. It looks exactly like a model-authored exit plan and
   changes no order, no stop and no take-profit. The most misleading artefact in this audit.
2. **There is no take-profit level on any live position.** `config/earn.yaml` ships
   `roi_table: {"0": 10.0}` (its own comment: *"10.0 = effectively off"*), `ladder: []` and
   `trailing.enabled: false`. Every watch event carries
   `take_profit_source: "off (roi_table effectively disabled, no ladder)"`. Against §1.4's
   finding that **77.5% of run-ups are given back within 60 days**, "how to make max growth" has
   no mechanism at all.
3. **The invalidation grammar parsed 1 of 3 real proposals, and the one that parsed was wrong in
   a dangerous direction.** The sentence was about the *benchmark* — "*if BTC ends the 7d horizon
   more than 5% HIGHER while the sleeves sat in USDT*". There is no concept word for "BTC", so the
   bare percentage fell through to the default fact `pnl_pct`, and "higher" is not in `_LOSS_WORDS`
   so the sign was never flipped. Result: **a predicate that fires when our own position is up 5%**
   — escalated as `invalidation_fired`, severity `escalate`, **bypassing the 90-minute cooldown**.
   A winning position would trip the loudest alarm in the system, labelled as a broken thesis.
   All 8 production watch events carry `{"parsed": false, "predicates": []}`.
4. **The per-coin "why" is a substring grep.** With no validator row, `watch/thesis.py` scans the
   last 5 proposals for one whose targets contain the base **as a key** — *not* as a non-zero
   weight — then picks "the line that names this asset" by `base in line`. Live consequence: a
   proposal with `BTC: 0.0` was matched to a real BTC holding. And over 31 tickers, `ETH` matches
   inside `ETHFI`.
5. **The richer per-pair record exists and died 5 times out of 5.** `signal_validations` is
   per-pair with a required thesis, reasons, counter-evidence, invalidation and horizon. Every one
   of the 5 production rows has `thesis = NULL`; 4 died because `validator.py:270-273` sets
   `parsed = None` — **discarding the entire verdict** — when any cited key is absent from
   `features.keys()`. The models cited `market_state.data_fresh` and `active_flags.data_stale`:
   real facts at exactly those dotted paths in the same evidence pack, just not in the `features`
   block. **They cited correctly and were punished for it.**
6. **Nothing grades whether a stated reason turned out right.** `decision_grades` is keyed one row
   per run and holds process-rubric booleans plus portfolio-level outcome; the post-mortem rubric
   grades **process deliberately** ("*a profitable decision with a broken process scores low*").
   `thesis_consistent` asks whether the rationale *followed from the inputs*, never whether it was
   *right*. The 2×2 that matters — thesis right/wrong × money made/lost — has no field, no table
   and no report. **A trade that made money for a reason the model did not give is today
   indistinguishable from one that made money for the reason it did.**

Two more, because they make the path look more disciplined than it is: the **decision panel** is
configured (`decide: panel.enabled: true`, three passes, quorum 2, confidence taken as the
*minimum*) and `run_panel` **has no caller outside tests**; and the **decide skill's 10-step
checklist** — which includes "name at least one rejected alternative" — is unreachable, because
`decide` has no `skills_from` and its `allowed_tools` excludes `Skill`.

And the ordering constraint that gates all of it: `knowledge/earn.db` holds **256 daily rows across
32 pairs — 8 per pair** — against the **201** `asset_state()` requires, while
`data/binance/*-1d.feather` holds BTC back to 2017-08-17. `knowledge/state/latest.json` has
`assets: {}`, measured. **The decide stage has literally no per-asset number for any of the 31
names, so every run correctly abstains.** Ship a per-coin schema before the backfill and the only
measurable change is the shape of the abstain.

### 3.2 What must be required — the candidate record

Schema v5: a **required `candidates` array**, one record per non-quote key of `targets`, bijection
enforced so an asset cannot be held without one. Every field validated, every field per-coin.

| Field | Type | What it forces | Graded afterwards by |
|---|---|---|---|
| `asset` | enum of the run's tradeable set | — | — |
| `weight` | must equal `targets[asset]` within `sum_tolerance` | deliberate redundancy — catches transcription | — |
| `thesis` | 40-400 chars | **the mechanism** by which this coin grows | `falsifier` replay |
| `why_this_one` | 30-300 chars | the comparison it won | reviewer, and `rejected[]` consistency |
| `rejected[]` | 1-3 × {asset, because 20-200} | the prompt's step-7 prose promoted to a field, **per coin** | realised return of the rejected name vs the chosen one |
| `evidence[]` | 2-6 × {key, value, as_of} | key validated against the run's own flattened feature namespace; **value must equal the host's number to 1e-6** | `evidence_stability` — how many cited values were still in the same decile at horizon |
| `horizon_hours` | 1-720, **per coin** | replaces one portfolio `horizon_days` that nothing enforces | resolution clock |
| `conviction` | 0-1 | calibrated P(this name beats **BTC** over **its** horizon) | calibration curve |
| `exit_strength` | {fact, op, threshold, action, fraction} | **when to sell into strength** | `exit_strength_hit` |
| `exit_weakness` | {fact, op, threshold, action, fraction} | **when to sell if it drops**; never looser than `risk.stoploss_per_trade` | `exit_weakness_hit` |
| `falsifier` | {fact, op, threshold} | **what would prove the thesis wrong** — deliberately *not* the stop, because a thesis can be wrong while the price has not moved | `falsifier_fired` |

**Structured predicates replace prose, and that is the highest-value part.** `fact` comes from a
closed enum matching the keys `watch/positions.Holding.facts` already computes.
`invalidation.Predicate` is then constructed directly: `parsed` is true by construction, the
"BTC ends the horizon 5% higher" → `pnl_pct >= 5` class of misparse becomes **impossible**, and the
measured 1-of-3 parse rate becomes 3-of-3. Keep `invalidation.parse()` for v4 replay only, and
record `parsed_source` on every watch event so the migration is measurable.

**Hand the model the four numbers rather than asking it to judge.** Each of the owner's questions
has a measured answer in this document, and the prompt should carry them as inputs the model must
cite:

| Question | The number it must reason against |
|---|---|
| why would it grow | "I cannot forecast that" is the honest default — 62.3% of sustained doublings started in 10 calendar months and nothing observable at T predicts which |
| when to sell into strength | **77.5%** of +100% run-ups were mostly given back within 60 days of the peak — a ladder is not optional |
| if it drops, when to sell | **P(90d dd < −40%) for this coin**, read off its vol band: 3.5% / 16.8% / 35.0% / 48.9% / 50.4% |
| how to make max growth | P(90d double) is **5.43%** unconditionally and **4.31%** after the filter — the upside is a tail you hold for, not a thing you time |

### 3.3 How each answer is checked and graded

New table `candidate_grades(run_id, asset, …)`, one row per candidate, resolved by **Python** at
that candidate's own `horizon_hours`, reusing `signals/outcomes.hit_for` and its code-fixed
`MIN_MOVE_PCT = 1.0` so scores stay comparable across prompt versions.

- `ret_pct`, `ret_vs_btc_pct` — because §1.1 says the only benchmark that means anything is BTC
  over the same window, not zero.
- `exit_strength_hit`, `exit_weakness_hit`, `falsifier_fired` — replayed against the actual candle
  path rather than read from the live watcher, **so a candidate is graded even when the position was
  never opened.** The counterfactual is most of the sample.
- `thesis_outcome` ∈ {`right_and_paid`, `right_not_paid`, `wrong_but_paid`, `wrong_and_lost`} — the
  2×2 of falsifier against return. **`wrong_but_paid` must be reported loudly in the weekly
  review; it is the most dangerous cell and today it is invisible.**
- `evidence_stability` — a cheap guard against a cited number being coincidence rather than cause.

Precondition, stated honestly: **roughly 100 candidate records before `conviction` calibration means
anything**, which at the current cadence is months. And the watcher must split
`invalidation_fired` into three kinds — `target_reached`, `stop_condition`, `falsifier_fired` —
because **a position hitting its profit target must not fire the same cooldown-bypassing alarm as a
broken thesis.** `falsifier_fired` is the kind that earns the local watcher its keep: it is the only
channel that can catch a position winning for the wrong reason.

---

## 4. The learning rule — how the system decides which strategy to use when

The owner asked for a learning strategy: the system should decide from its own record. Here is the
rule, and the reason each guard exists is a measurement in this document rather than a principle.

### 4.1 Select on regime, and only where the regime split was measured

Every family here was tested in three regimes, and **regime conditioning is the only selection this
data supports** — because the same feature genuinely changes value across regimes:

| Family | 2019-22 | 2023-24 | 2025-26 | Usable regime rule? |
|---|---|---|---|---|
| Trend ensemble | 0.59 | 1.44 | 0.54 | No — **use always**, it is positive everywhere |
| Exclusion filter | better | better | better | No — **use always**, all three legs hold |
| Donchian 55/20 alone | 0.40 | 1.61 | **−0.14** | This is why the ensemble exists |
| 365d momentum | −0.010 | +0.012 | **+0.121** | **No** — sign flips; do not condition on it |
| Gated combo vs BTC | +47.8 vs +44.5 | **+19.0 vs +137.4** | +9.1 vs −6.7 | Helps in chop and bear, left behind in a clean BTC bull |

**The rule that follows is deliberately narrow: condition on regime only where the sign was stable
and the magnitude varied. Never switch a family on because its sign flipped positive in the current
regime** — that is what 365d momentum would invite, and its sign flipping *is* the instability.

### 4.2 Weight by measured hit rate, with the hit rate defined against BTC

`candidate_grades` gives per-family, per-regime realised hit rate and `ret_vs_btc_pct`. Weighting
rule: a family's allocation moves only on **its own out-of-sample record**, graded against BTC over
the same window, and the metric on the console is **"fraction of positions that beat BTC"**. §1.1
says a random 4-name basket beat BTC in 17.2-38.7% of draws in four of five regimes; **if the
satellite sleeve sits below 50% after a few hundred fills, it has no case** and the widening
criterion in `wide-universe.md` §6 should refuse to fire.

### 4.3 What stops it chasing last week's winner

Four guards, each answering a specific measured failure:

1. **Walk-forward selection is banned, because it measured worse than no selection.** Best-of-15 by
   trailing 3-year Sharpe delivered 1.22; equal-weighting all 15 delivered 1.31. The 2022 pick had
   an in-sample Sharpe of 2.22 and returned **−1.00** out of sample. **Average parameters; do not
   choose them.**
2. **The trial counter is cumulative and persisted, not per-run.** This audit ran ~177 costed trials
   in one day. `edge-audit`'s `expected_max_SR(N, T)` already exists
   (`.claude/skills/edge-audit/scripts/audit_stats.py`) and gives 0.86 at N=10, 1.05 at N=50, 1.19
   at N=200 on 9.1 years. A system that auto-proposes changes **is** a search process; N must
   accumulate across runs in the journal, or every week resets the counter and the hurdle becomes
   decorative.
3. **`review.change_gates.walk_forward_min_out_sample_delta: 0.0` is a coin-flip hurdle** and must
   be replaced by `baseline + expected_max_SR(N_cumulative, T)`. On that rule, the trend ensemble
   clears by **0.06** and **nothing else in this audit clears at all** — which is exactly the
   discipline the owner is asking for, applied against the audit's own favourite results.
4. **Evidence class, not just score.** Two different kinds of claim must be graded differently, and
   conflating them is how a search result gets promoted:

| Claim type | Example | How it is judged |
|---|---|---|
| **Cross-sectional structure** | the exclusion filter: 49,833 observations, monotone in 3 regimes, **100% of an 80-cell grid**, robust to the survivorship convention | On that evidence. A Sharpe is not the right test and should not be quoted. |
| **Backtested rule** | the strategy table, the 2021-2026 "+383% vs BTC's +186%" | Against the **deflated** hurdle, with N recorded. This one does **not** clear and must be filed as search output. |

### 4.4 The decay re-test

`edge-audit`'s quarterly re-test is the right hook, and this audit supplies the four numbers that
would **overturn** its conclusions — which is what makes it falsifiable rather than a document.
Log weekly:

| Metric | Value now | What a change would mean |
|---|---|---|
| Median coin R² on BTC + alt factor | 0.54-0.62 | A sustained fall = coins becoming distinguishable |
| PC1 share of cross-sectional variance | **69.0% and rising** | A fall = coin selection starting to be worth paying for |
| Rolling 90d alt-minus-BTC | −31 log points | — |
| Down-beta minus up-beta | 1.38 vs 0.86 | Convergence = the negative convexity easing |

**A sustained fall in PC1 share together with a rise in median two-factor alpha is the only thing
that would make coin selection worth paying for.** Until then, the system should not try.

---

## 5. What this cannot do

Stated plainly, because the credibility of everything above depends on it.

**It cannot establish that any strategy here makes money forward.** Distinguishing Sharpe 1.25 from
0.80 at 80% power needs roughly a century of daily data. `modes.live.min_test_days: 90` proves the
plumbing and **nothing** about edge. The ensemble is one market's one nine-year history containing
maybe three genuine regimes.

**No rule beat BTC buy-and-hold on return over 2019-2026** — the best construction managed +45.3%
CAGR against BTC's +49.3%, with a worse Sharpe. Everything recommended here wins on drawdown and
Sharpe and loses on CAGR. If the owner's objective is maximum growth rather than smaller drawdowns,
**the measured answer is to hold BTC**, and this document should be read as an argument for a
smaller, calmer book rather than a better one.

**The filter's thresholds were chosen by me after seeing the bucket tables.** The 80-cell grid is
the strongest available defence and it is not a substitute for a second sample. The genuine
out-of-sample test is the next two years.

**Per-coin open interest was never tested.** The brief named it. ~900,000 daily bulk files with
four documented loader traps was out of budget, so the OI legs of the episode characterisation and
the forward test are simply absent. The existing BTC-only OI→drawdown result stands. **This is the
largest single gap.**

**Implied volatility could not be used cross-sectionally.** DVOL exists only for BTC and (from
2023-12) ETH, so the strongest volatility forecaster the repo owns cannot reach the other 496
names. Every vol number here is trailing realised, which the repo's own §1.1 shows is the weaker
estimator — so the cross-sectional vol result may **understate** what is achievable for BTC/ETH and
cannot be improved for anything else without paid data.

**Funding covers only coins with a perpetual, from 2019-09** — 373 of 498 ever-eligible symbols. The
§1.6 flag rests on 2019-22 and 2023-24; in 2025-26 it fires on six observations, so its live value
today is close to zero.

**The "what happened in the world" leg has no forward evidence by measurement, not by omission.**
Growth clusters into a few months; the two market-state variables that could anticipate such a
month both fail forward. No point-in-time news or macro panel was built, because ETF flows are
paywalled, historical liquidations do not exist free, and on-chain flow history is 10 days deep.
Event narration is **unfalsifiable at the horizons Earn trades.**

**Weekly sampling reduces but does not eliminate label overlap.** A 90-day window sampled weekly
overlaps 12 times over; Newey-West at lag 13 is the minimum honest correction, not a complete one.
**The 90-day t-statistics should be read as smaller than they print.**

**Regimes were split on calendar dates given in advance, not discovered** — the right choice for
avoiding fitting, but three regimes is just three.

**Nothing here was tested through Freqtrade.** All backtests are vectorised daily panels with
drifting intra-period weights. They ignore minimum notionals, step sizes, the 26 gate checks, limit
fill risk and the monthly fee budget. **Any number in the strategy table would move once real
execution and the gate are applied, and the direction of that move is down.**

**Daily bars only, close to close.** No stops, no ladder, no trailing stop, no rebalance band, no
intrabar path. These are tests of signal families, not of the shipped system. The shipped-sleeve
benchmark is a **proxy** returning +108% over 2021-2026 against the real thing's stated ~+75%, and
every comparison inherits that gap.

**The §3 audit is a code audit on n=3 proposals.** It proves mechanisms exist or are absent and that
specific defects fire; **"1 of 3 parsed" and "0 of 5 survived" are counts of every run that has ever
happened, not rates.** All 3 proposals abstained at 100% USDT and named no coin, so the per-coin
path has never been *exercised*.

**And the fix in §3 is unmeasured.** Structured predicates, a per-candidate record and a per-asset
grade remove failure modes I demonstrated — a weaker claim than improving returns. A forced
justification field can produce fluent justification for a bad trade; requiring a mechanism, a
named alternative and a falsifier raises the cost of doing that without preventing it. Anyone who
reads §3 as evidence that v5 will trade better has read it wrong.

**Two loader traps worth carrying forward.** `LUNAUSDT`'s symbol was reused for LUNA 2.0 on
2022-05-31, producing a **177,399× one-day "return"**; untreated it fabricates a +724% growth
episode and produced a **112,518× fake equity curve** in an early run. 24 symbols in the full USDT
history carry a >8× single-day discontinuity (23 are leveraged tokens rebasing). And a **one-bar
lookahead** in one audit's own code turned a cross-sectional breakout rule from Sharpe 0.54 / +191%
into **Sharpe 1.77 / +29,011%**. Both belong in the test suite, not in a document.

---

## 6. Build list, in priority order

Ranked by measured value per unit of risk. Tier per `CLAUDE.md`.

**1. Backfill 1d candles into `knowledge/earn.db`. Tier 2 (ops/data). Blocks items 3-6.**
Measured gap: **8 daily rows per pair** (256 across 32 pairs; **zero** pairs at the 201
`compute_state.asset_state()` needs) against **143,151 daily rows already in
`data/binance/*-1d.feather`**, BTC back to 2017-08-17. Until `knowledge/state/latest.json` has a
non-empty `assets` map, the decide stage has no per-coin number, every run correctly abstains, and
a per-coin schema only changes the shape of the abstain. **Nothing else in §3 is worth building
first.**

**2. Add a volatility ceiling and a real age floor to the tradeable tiers. Tier 2 (`config/earn.yaml`
+ resolver).** Today `universe.tiers.satellite` requires **only** `min_median_quote_volume_usdt:
5000000` — no volatility ceiling, no age bar beyond the 180-day watchlist floor. Add
`max_ann_vol_60d: 1.25` and `min_listing_age_days: 730` to `satellite`, and `max_ann_vol_60d: 1.00`
/ `min_listing_age_days: 1095` to `major`. No new gate check is needed: the resolver stops assigning
a tradeable tier, the cap resolves to zero, and the existing `tier` check refuses the order.
Measured: median forward 90d **−7.2% vs −20.2%**, P(90d dd<−40%) **18.3% vs 41.2%**, 1.1pp of
doubling probability given up, **100% of an 80-cell grid** beats baseline on both legs. Effect on
the live book: **7 of 31 pairs leave the tradeable set** — ZEC, PUMP, ONDO, ENA, PENGU, TRUMP, XPL.
Pick the middle of the plateau, never the peak.

**3. Give `self._plan` a reader, and ship a profit ladder. Tier 2 (`strategies/`) + tier 1 (params).**
Route each candidate's `exit_strength` into the existing `mechanics.roi_table` / `ladder` machinery
per pair, clamped to `plan_bounds` and never looser than config. Today `roi_table: {"0": 10.0}`,
`ladder: []`, `trailing.enabled: false`, `take_profit` is null on every live holding, and the
model's only profit-taking channel is a field nothing reads. Measured justification: **77.5% of
+100% run-ups were mostly given back within 60 days of the peak.** This is "how to make max growth"
having a mechanism for the first time.

**4. Proposal schema v5 — the `candidates` array with structured exits. Tier 2 (`schemas/`).**
Spec in §3.2. Two structural gate checks only — `has_candidate` and `exit_plan_present`, both pure
shape, both computable by the stdlib in-container loader. **The gate must not start judging theses**
or "Claude never sits in the order path" erodes. Keep v4 parseable for replay; stamp the lowest
version needed.

**5. Repair the per-pair thesis path before building a second one. Tier 2 (`runs/signals/`).**
`validator.py:270-273` discards the entire verdict when any cited key is missing from
`features.keys()`. Measured cost: **4 of 5 production validations destroyed** for citing real facts
at real dotted paths in the same pack. Two fixes: flatten the **whole** pack for the whitelist, and
change the failure mode from void-the-verdict to **drop-the-item-and-record-it** with a floor of 2
surviving citations. Keep the existing reject-whole-never-clamp doctrine for target *weights*, where
it belongs.

**6. Wire the panel. Tier 2 (`runs/research_run.py`) — the cheapest answer to the owner's actual
question.** `decide: panel.enabled: true` is configured with three passes, quorum 2, confidence
taken as the **minimum**, and an adjudicator that is explicitly not a tie-breaker — and `run_panel`
has **no caller outside tests**. Route `stage_decide` through it. Smaller than the schema change,
delivers genuine self-interrogation immediately, and starts accumulating the `disagreement_rates`
that later tell you whether a cheaper pass can be trusted alone. While there: either bind the decide
skill (`decide` has no `skills_from` and `allowed_tools` excludes `Skill`, so its 10-step checklist
is unreachable) or delete the checklist and keep one copy. **Two copies where one is dead is worse
than one.**

**7. Add the `CANDIDATE FEATURES` block to the decide prompt. Tier 1 (`prompts/research.v5.md`).**
Reuse the `pair_features` slice of the evidence pack that `signals/features.py` already computes
twice a cycle. Today the model gets `{asset, tier, max_weight}` per coin, dossiers for core-and-held
only, and an empty `assets` map — then is asked to choose among 31 names. **It cannot answer "why
this coin" because it has not been told anything about the coins.** Add `vol_ann_60d` and its
**cross-sectional rank at T** to that feature set — and a regression test that fails if the
implementation is a per-coin self-percentile rather than cross-sectional (§1.2).

**8. `candidate_grades` and the `thesis_outcome` 2×2. Tier 2 (`ops/sql/`, `runs/`).** §3.3. This is
the loop that makes §4 real; without it the learning rule has no input. Report `wrong_but_paid`
loudly in the weekly review.

**9. Replace the change gate's hurdle and persist the trial counter. Tier 2 (`config/earn.yaml` +
`runs/review_run.py`).** `walk_forward_min_out_sample_delta: 0.0` → `baseline +
expected_max_SR(N_cumulative, T)`, with N accumulating **across runs** in the journal. The helper
already exists in `edge-audit`. On this rule the trend ensemble clears by 0.06 and nothing else in
this audit clears at all.

**10. Take the trend ensemble through `strategy-lab`. Tier 2 edit to `strategies/SleeveA.py` plus a
tier-1 params change.** §2.1: Sharpe 0.80 → 0.92, MaxDD −24.9% → −17.8%, risk-normalised CAGR 14.4%
→ 16.7%, zero months below −5% in 110, holding in all four regime windows and at every cost level
to 50 bps/side. It ranks below the plumbing deliberately: it is the biggest *strategy* win and the
items above are what make any strategy gradeable. Record the trial count with it.

**11. Rewrite `event_study.py`. Tier 1 (skill body).** Six defects in §1.7: add symmetric pre-event
windows [−20,−1] / [−10,−1] / [−5,−1]; report **excess over an unconditional same-length baseline**,
never a raw mean; add a permutation p-value; split scheduled from unscheduled; label every class
with n and an explicit "not significant" above p=5%; and carry the 238-event dated calendar instead
of `news_items`, which holds 160 classified rows from 2023-10-26 in an archive whose directory
contains only `.gitkeep`.

**12. Scope the funding result and correct the percentile instruction. Tier 0 (docs) + tier 1
(`leverage-state`).** §1.6: implement per-coin funding as a **rare extreme-level flag** (≥40%
annualised, 3-day mean — the middle of a plateau, ex-cluster ratio 1.31), never a percentile, never
a veto, never a direction, and accept that it fires on ~0-5% of coin-weeks now. Add the scope note
to `crypto-research.md` §1.2 that its "rolling percentile, never a fixed threshold" instruction was
measured on **BTC alone** and inverts in the cross-section.

**13. Make the panels durable and wire them into `strategy-lab`. Tier 2 (`data/`).** The artefacts
are in WSL `/tmp` and `/home/shourya` and will not survive a reboot — which is how the previous
panel was lost. `~/earn-run/data/binance` holds **108 current survivors, 19 truncated at exactly
2021-01-01 by the downloader rather than by listing date**; the panels hold 733-777 symbols
including 230-282 dead ones. **Any backtest against the feather store is survivor-only and
start-date-biased.** Check the panel in as the fixture `wide-universe.md` §5.5 already recommends,
and make a survivor-only backtest fail a check.

**14. Two loader/harness tests. Tier 2 (`evals/`, `tests/`).** (a) The `LUNAUSDT` symbol-reuse trap
as a mandatory loader test — split every series at any >8× or <1/8× single-day move and keep both
halves. (b) A **shift-invariance assertion** on every backtest path: re-run with the signal lagged
one extra bar and require graceful degradation. A result that only works at zero lag is a bug
report, and one lived in this audit's own code.

**Two things to record rather than build:** the `90d`-momentum, breadth and narrative growth
selectors, with their measured refutations, into `crypto-research.md` §2 so no future run
rediscovers them; and `CLAUDE.md`'s "**17 checks**", which is now **26** in
`riskgate.py: CHECK_ORDER`.

### On the demo book, since the owner asked

Both bots are **already** placing dummy orders across the 31 pairs, so "if not already started" is
satisfied — this audit changed nothing about them, by instruction. Two findings bear directly on
what that demo will teach:

- **Grade every position against BTC over the same window, not against zero**, and put "fraction of
  positions that beat BTC" on the console as a first-class metric. A random 4-name basket beat BTC
  in 17.2-38.7% of draws in four of five regimes; against zero the demo will look like skill in any
  rising market.
- **Items 2 and 3 should land before any longer unattended run.** Without item 2 the book drifts
  into exactly the names the data says to avoid (7 of the 31 are already there); without item 3
  there is no take-profit level on any position, and §1.4 says gains do not persist. A profit-making
  demo that books no profit is not measuring what the owner wants measured.

---

## Appendix — provenance

All measurements taken 2026-09-24 on this host. Binance public endpoints only; no keys, no paid
data. Costs 15 bps/side throughout (30 bps for satellite-grade names where stated).

The four panels behind §1 were built in WSL `/tmp`, where the *previous* wide-universe panel was
lost. They have been copied to **`/home/shourya/earn-panels/`** (46 MB, with `build_panel.py`,
`feat.py`, `fwd.py`, `episodes.py`, `funding.py`, `ic.py`, `bt2.py` to rebuild them). That is
durable but still not version-controlled — build item 13.

| Artefact | What it is |
|---|---|
| `earn-panels/panel_1d.parquet` | 841,491 daily candles, 747 USDT symbols ever listed (777 series after splitting price discontinuities), 282 dead, 2017-08-17 → 2026-09-24 |
| `earn-panels/wk.parquet` | the forward-test panel: 49,833 coin-weeks, 498 symbols, 403 weeks, features at T and forward 7/30/90d returns plus forward 90d drawdown |
| `earn-panels/episodes.parquet` | 2,423 non-overlapping +100% run-ups; 980 eligible at t0; 220 sustained |
| `earn-panels/funding.parquet` | 2,567,781 funding prints, 653 USDT perpetuals, 2019-09-10 → 2026-09-24 |
| `/home/shourya/s5/raw/1d`, `/home/shourya/a2/close.parquet` | 718-733 symbol panels incl. 230-249 dead names, used for the beta decomposition and the event study |
| `/home/shourya/a2/events.csv` | 238 dated events, 200 primary-source verified, 33 secondary, 5 date-uncertain and labelled |
| `/home/shourya/a3/` | 15 trend variants, the ensemble, 11 strategy families, correlation-to-shipped blends |
| §1.6, §1.5 live-book table | measured for this document from `panel_1d.parquet` + `funding.parquet`; the funding form × horizon × survivorship design and the 31-pair filter application are original here |

**Survivorship convention:** a coin that stops trading inside the horizon is marked out at its last
traded print (1.43% of 30d observations, 3.72% of 90d). Every conclusion was re-run under the
harsher "delisting = −100%" convention; the filter's advantage **widens** under it.

**Trial count for the deflated hurdle: ~177 costed trials across the four audits** (~85 + ~60 + ~32
permutation tests). At N=200 on 9.1 years the expected best zero-skill Sharpe is **1.19**. Record
this N; do not reset it.
